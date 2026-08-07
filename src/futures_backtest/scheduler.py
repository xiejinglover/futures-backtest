from __future__ import annotations

import json
import os
import platform
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd

from .account import Account
from .book import RestingOrders, Working
from .config import BacktestConfig
from .dataset import MarketDataset, build_dataset
from .matcher import Matcher, path_reach
from .performance import compute_metrics, write_outputs
from .router import Router
from .strategy import Strategy, StrategyContext, load_strategy, normalize_targets
from .trades import TradeLedger
from .types import (
    AccountSnapshot,
    BacktestDataError,
    BacktestResult,
    Bar,
    DataAdapter,
    EventKind,
    Fill,
    Order,
    RollLog,
    TargetPosition,
)


class Scheduler:
    """Replays bars in order and emits ``BAR`` / ``ROLL`` / ``SETTLE`` events.

    Every bar runs as: cross the resting limit orders, execute the targets carried
    from the previous bar, ask the strategy, then place whatever it asked for. The
    first bar of a trading day additionally rolls today's lots into yesterday's and
    handles rolls and expiries; the last bar cancels what is still working and
    settles. A daily dataset has one bar per day, so that sequence collapses into
    the open-phase / close-phase / settle day the engine has always run.

    Signals are carried as *targets*, not as pre-resolved orders, so a target
    decided before a roll is executed against the contract that is actually routed
    when it fills.
    """

    def __init__(
        self,
        config: BacktestConfig,
        dataset: MarketDataset,
        strategy: Strategy,
    ):
        self.config = config
        self.dataset = dataset
        self.strategy = strategy
        self.router = Router(dataset, config.routing)
        self.matcher = Matcher(dataset, config.execution)
        self.account = Account(
            dataset, config.portfolio.initial_cash, config.portfolio.margins_default
        )
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.rolls: list[RollLog] = []
        self.events: list[dict[str, Any]] = []
        self.nav: list[dict[str, Any]] = []
        self.book = RestingOrders()
        self.ledger = TradeLedger(
            {symbol: info.multiplier for symbol, info in dataset.contracts.items()}
        )
        # Carried to the *next bar*, which on a daily dataset is the next day.
        self.pending_targets: list[tuple[date, TargetPosition]] = []
        self.skipped_targets: list[dict[str, Any]] = []
        self._bars_seen = 0

    # -- helpers ----------------------------------------------------------

    def _prices(self, bars: dict[str, Bar], field: str) -> dict[str, float]:
        return {symbol: float(getattr(bar, field)) for symbol, bar in bars.items()}

    def _day_bars(self, day: date) -> dict[str, Bar]:
        bars: dict[str, Bar] = {}
        for symbol in self.dataset.contracts:
            bar = self.dataset.last_bar_of_day(symbol, day)
            if bar is not None:
                bars[symbol] = bar
        return bars

    def _routing(self, day: date) -> dict[str, str]:
        return {
            underlying: self.router.trading_symbol(underlying, day)
            for underlying in self.config.data.underlyings
        }

    def _bar_for(self, order: Order, bars: dict[str, Bar]) -> Bar:
        bar = bars.get(order.symbol)
        if bar is None:
            raise BacktestDataError(
                f"no bar for {order.symbol} on {order.trading_day}; cannot execute order"
            )
        return bar

    def _execute(self, order: Order, bar: Bar) -> list[Fill]:
        fills = self.matcher.execute(order, bar, self.account)
        self.fills.extend(fills)
        for fill in fills:
            self.ledger.record(fill)
        return fills

    def _submit(self, orders: list[Order], bars: dict[str, Bar]) -> list[Fill]:
        produced: list[Fill] = []
        for order in orders:
            bar = self._bar_for(order, bars)
            self.orders.append(order)
            produced.extend(self._execute(order, bar))
        return produced

    def _day_slots(self, day: date) -> list[tuple[datetime, dict[str, Bar]]]:
        """The bars of ``day``, grouped into the instants the loop steps through.

        A daily dataset gets a single slot built the way it always was, from each
        contract's last bar of the day. Daily timestamps are labels rather than
        instants, and contracts whose label differs must still be seen together.
        """
        if not self.dataset.intraday:
            bars = self._day_bars(day)
            if not bars:
                return []
            return [(max(bar.timestamp for bar in bars.values()), bars)]
        slots = []
        for stamp in self.dataset.timestamps_of_day(day):
            bars = self.dataset.bars_at(stamp)
            if bars:
                slots.append((stamp.to_pydatetime(), bars))
        return slots

    def _record_event(self, kind: EventKind, day: date, **payload: Any) -> None:
        self.events.append({"trading_day": day, "kind": kind.value, **payload})

    # -- phases -----------------------------------------------------------

    def _roll(
        self,
        day: date,
        timestamp: datetime,
        bars: dict[str, Bar],
        prices: dict[str, float],
    ) -> None:
        for underlying in self.config.data.underlyings:
            if not self.router.is_roll_day(underlying, day):
                continue
            # Whatever is working belongs to the contract being left behind.
            self._cancel(self.book.cancel(underlying), day, "cancelled_on_roll")
            from_symbol = self.router.previous_trading_symbol(underlying, day)
            to_symbol = self.router.trading_symbol(underlying, day)
            net = self.account.symbol_net_lots(from_symbol) if from_symbol else 0
            self._record_event(
                EventKind.ROLL,
                day,
                underlying=underlying,
                from_symbol=from_symbol,
                to_symbol=to_symbol,
                net_lots=net,
            )
            orders = self.router.roll_orders(underlying, day, timestamp, self.account, prices)
            if not orders:
                continue
            fills = self._submit(orders, bars)
            executed = [fill for fill in fills if fill.filled_lots]
            if not executed:
                continue
            out = [fill for fill in executed if fill.reason == "roll_out"]
            into = [fill for fill in executed if fill.reason == "roll_in"]
            slippage_cost = sum(
                fill.slippage_ticks
                * self.dataset.contracts[fill.symbol].tick_size
                * fill.filled_lots
                * self.dataset.contracts[fill.symbol].multiplier
                for fill in executed
            )
            self.rolls.append(
                RollLog(
                    trading_day=day,
                    underlying=underlying,
                    from_symbol=from_symbol or "",
                    to_symbol=to_symbol,
                    net_lots=net,
                    close_price=out[0].price if out else 0.0,
                    open_price=into[0].price if into else 0.0,
                    commission=sum(fill.commission for fill in executed),
                    slippage_cost=slippage_cost,
                    realized_pnl=sum(fill.realized_pnl for fill in executed),
                )
            )

    def _apply_targets(
        self,
        targets: list[tuple[date, TargetPosition]],
        day: date,
        timestamp: datetime,
        bars: dict[str, Bar],
        prices: dict[str, float],
    ) -> None:
        for signal_day, target in targets:
            if self.router.is_roll_day(target.underlying, day) and not (
                self.config.routing.allow_signals_on_roll_day
            ):
                self.skipped_targets.append(
                    {
                        "signal_day": signal_day,
                        "execution_day": day,
                        "underlying": target.underlying,
                        "net_lots": target.net_lots,
                        "reason": "roll_day",
                    }
                )
                continue
            orders = self.router.signal_orders(target, day, timestamp, self.account, prices)
            resting: list[Order] = []
            for order in orders:
                if order.limit_price is None:
                    self._submit([order], bars)
                    continue
                bar = self._bar_for(order, bars)
                self.orders.append(order)
                if self.matcher.limit_reached(order, bar) is not None:
                    self._execute(order, bar)
                elif self.matcher.submittable(order, self.account):
                    resting.append(order)
                else:
                    self._drop(signal_day, target, order, day, "insufficient_margin")
            self.book.place(signal_day, target, resting)

    def _cross(self, bars: dict[str, Bar]) -> None:
        """Trade the orders that were already working when this bar opened.

        Several orders can come due on one bar, and they compete for the same free
        margin, so they are filled in the order the assumed intrabar path reaches
        their prices rather than in whatever order the book happens to hold them.
        """
        ready = []
        for working, order in self.book.working():
            bar = bars.get(order.symbol)
            if bar is None:
                continue
            price = self.matcher.limit_reached(order, bar)
            if price is not None:
                ready.append((path_reach(bar, price), order.underlying, working, order, bar))
        for _, _, working, order, bar in sorted(ready, key=lambda item: item[:2]):
            self.book.fill(working.underlying, order)
            # Re-stamp onto the bar that actually traded it: the order was written
            # against the bar it was placed on, and the matcher reads the reference
            # price and the trading day off the order.
            traded = replace(
                order,
                timestamp=bar.timestamp,
                trading_day=bar.trading_day,
                reference_price=bar.open,
            )
            self._execute(traded, bar)

    def _cancel(self, workings: list[Working], day: date, reason: str) -> None:
        """Stop orders from working and record the signals they never delivered.

        The share of signals a limit order misses is the main cost of trading
        passively, so a cancelled order leaves the same paper trail as a rejection.
        """
        for working in workings:
            for order in working.orders:
                self._drop(working.signal_day, working.target, order, day, reason)

    def _drop(
        self,
        signal_day: date,
        target: TargetPosition,
        order: Order,
        day: date,
        reason: str,
    ) -> None:
        fill = self.matcher.cancel(order, reason)
        self.fills.append(fill)
        self.skipped_targets.append(
            {
                "signal_day": signal_day,
                "execution_day": day,
                "underlying": target.underlying,
                "net_lots": target.net_lots,
                "reason": reason,
                "limit_price": fill.price,
            }
        )

    def _place(self, day: date, targets: list[TargetPosition], last: bool) -> None:
        """Route fresh targets to the next bar, leaving unchanged ones working.

        A position-target strategy re-emits the same target on every bar. Taking
        that as a new instruction would cancel and re-place the order every bar,
        which would make the fill rate a function of the bar period. On the last
        bar of the day nothing survives the close anyway, so every target is
        carried instead.
        """
        carried: list[tuple[date, TargetPosition]] = []
        for target in targets:
            if not last:
                if self.book.holds(target):
                    continue
                self._cancel(self.book.cancel(target.underlying), day, "superseded")
            carried.append((day, target))
        self.pending_targets = carried

    def _decide(self, day: date, timestamp: datetime, bars: dict[str, Bar]) -> list[TargetPosition]:
        routing = self._routing(day)
        visible = {
            underlying: bars[symbol]
            for underlying, symbol in routing.items()
            if symbol in bars
        }
        context = StrategyContext(
            trading_day=day,
            timestamp=timestamp,
            bars_seen=self._bars_seen,
            bars=visible,
            account=self.account.snapshot(day),
            underlyings=tuple(self.config.data.underlyings),
            _history=self.dataset.history,
            _routing=routing,
            _tick_sizes={
                underlying: self.dataset.contracts[symbol].tick_size
                for underlying, symbol in routing.items()
                if symbol in self.dataset.contracts
            },
        )
        result = self.strategy.on_bar(context)
        targets = normalize_targets(result, tuple(self.config.data.underlyings))
        if self.config.execution.market_fill == "same_close" and any(
            target.limit_price is not None for target in targets
        ):
            raise BacktestDataError(
                "a limit price cannot be combined with execution.market_fill="
                "'same_close': the decision is taken at the close of the very bar "
                "that would have to prove the fill, so any resting fill would be "
                "look-ahead. Use market_fill='next_open' for limit orders"
            )
        return targets

    def _settle(self, day: date) -> AccountSnapshot:
        for symbol in {position.symbol for position in self.account.open_positions()}:
            bar = self.dataset.last_bar_of_day(symbol, day)
            if bar is not None:
                self.account.mark(symbol, bar.close)
        outcome = self.account.settle(day)
        snapshot = self.account.snapshot(day)
        self._record_event(EventKind.SETTLE, day, **outcome, equity=snapshot.equity)
        self.nav.append(
            {
                "trading_day": day,
                "cash": snapshot.cash,
                "margin": snapshot.margin,
                "available": snapshot.available,
                "equity": snapshot.equity,
                "unrealized_pnl": snapshot.unrealized_pnl,
                "settlement_variation": outcome["settlement_variation"],
                "realized_pnl_cum": self.account.realized_pnl,
                "commission_cum": self.account.total_commission,
                "net_lots": json.dumps(snapshot.net_lots, sort_keys=True),
            }
        )
        if snapshot.available < -1e-6:
            raise BacktestDataError(
                f"account is short of margin after settling {day}: "
                f"equity={snapshot.equity:.2f} margin={snapshot.margin:.2f}"
            )
        return snapshot

    # -- main loop --------------------------------------------------------

    def run(self) -> None:
        self.dataset.dominant_coverage()
        for day in self.dataset.trading_days:
            slots = self._day_slots(day)
            for index, (timestamp, bars) in enumerate(slots):
                first = index == 0
                last = index == len(slots) - 1
                open_prices = self._prices(bars, "open")
                close_prices = self._prices(bars, "close")

                if first:
                    self.account.roll_today_into_yesterday()
                    if self.config.routing.roll_timing == "next_open":
                        self._roll(day, timestamp, bars, open_prices)
                    for underlying in self.config.data.underlyings:
                        self._submit(
                            self.router.expiry_orders(
                                underlying, day, timestamp, self.account, open_prices
                            ),
                            bars,
                        )
                self._bars_seen += 1

                self._cross(bars)
                carried, self.pending_targets = self.pending_targets, []
                self._apply_targets(carried, day, timestamp, bars, open_prices)

                self._record_event(EventKind.BAR, day, symbols=sorted(bars))
                targets = self._decide(day, timestamp, bars)
                if last and self.config.routing.roll_timing == "same_close":
                    self._roll(day, timestamp, bars, close_prices)
                if targets:
                    if self.config.execution.market_fill == "same_close":
                        self._apply_targets(
                            [(day, target) for target in targets],
                            day,
                            timestamp,
                            bars,
                            close_prices,
                        )
                    else:
                        self._place(day, targets, last)

                if last:
                    self._cancel(self.book.cancel_all(), day, "limit_not_reached")
                    self._settle(day)

    # -- frames -----------------------------------------------------------

    def frames(self) -> dict[str, pd.DataFrame]:
        return {
            "orders": pd.DataFrame([asdict(order) for order in self.orders]),
            "fills": pd.DataFrame([asdict(fill) for fill in self.fills]),
            "rolls": pd.DataFrame([asdict(item) for item in self.rolls]),
            "events": pd.DataFrame(self.events),
            "nav": pd.DataFrame(self.nav),
            "skipped_targets": pd.DataFrame(self.skipped_targets),
            "trades": pd.DataFrame([asdict(trade) for trade in self.ledger.trades]),
        }


def _run_id(config: BacktestConfig, data_version: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    underlyings = "-".join(config.data.underlyings)
    return f"{stamp}_{underlyings}_{data_version[:12]}"


def run_backtest(
    config: BacktestConfig,
    data_adapter: DataAdapter | None = None,
    run_id: str | None = None,
) -> BacktestResult:
    dataset = build_dataset(config.data, data_adapter)
    strategy = load_strategy(config.strategy.path, config.strategy.parameters)
    scheduler = Scheduler(config, dataset, strategy)
    scheduler.run()

    frames = scheduler.frames()
    metrics = compute_metrics(frames, config.portfolio.initial_cash)
    identifier = run_id or _run_id(config, dataset.metadata.data_version)
    run_path = config.output.root / identifier

    metadata = {
        "run_id": identifier,
        "created_at": datetime.now(UTC).isoformat(),
        "data": dataset.describe(),
        "settle_fallbacks": dataset.settle_fallbacks,
        "lookahead_dominant": config.routing.lookahead_dominant,
        "dominant_lag": config.routing.dominant_lag,
        "dominant_warmup_fallbacks": sorted(
            {f"{underlying}@{day}" for underlying, day in scheduler.router.warmup_fallbacks}
        ),
        "strategy": config.strategy.path,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
    }
    if config.routing.lookahead_dominant:
        metadata["warning"] = (
            "routing.lookahead_dominant=true: same-day dominant decisions explain "
            "same-day fills; these results contain look-ahead and are research-only"
        )
    write_outputs(run_path, config, frames, metrics, metadata)

    return BacktestResult(
        run_id=identifier,
        run_path=run_path,
        data_version=dataset.metadata.data_version,
        status="ok",
        metrics=metrics,
    )


def validate_config(
    config: BacktestConfig, data_adapter: DataAdapter | None = None
) -> dict[str, Any]:
    """Load and check data without trading, mirroring the ``validate`` command."""
    dataset = build_dataset(config.data, data_adapter)
    dataset.dominant_coverage()
    router = Router(dataset, config.routing)
    load_strategy(config.strategy.path, config.strategy.parameters)
    routing_preview = []
    for underlying in config.data.underlyings:
        for day in dataset.trading_days:
            symbol = router.trading_symbol(underlying, day)
            if router.is_roll_day(underlying, day):
                routing_preview.append(
                    {
                        "trading_day": day,
                        "underlying": underlying,
                        "roll_to": symbol,
                        "roll_from": router.previous_trading_symbol(underlying, day),
                    }
                )
    return {
        "status": "ok",
        "data": dataset.describe(),
        "strategy": config.strategy.path,
        "dominant_lag": config.routing.dominant_lag,
        "lookahead_dominant": config.routing.lookahead_dominant,
        "dominant_warmup_fallbacks": sorted(
            {f"{underlying}@{day}" for underlying, day in router.warmup_fallbacks}
        ),
        "rolls": routing_preview,
    }
