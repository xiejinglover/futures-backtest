from __future__ import annotations

from datetime import date, datetime, timedelta

from .account import Account
from .config import RoutingConfig
from .dataset import MarketDataset
from .types import BacktestDataError, Order, TargetPosition


class Router:
    """Turns underlying-level intent into month-contract orders.

    The whole look-ahead question lives here. A strategy deciding on trading day
    ``T`` cannot know which contract the exchange will call dominant after ``T``
    closes, so by default the router trades the contract published for ``T-1``
    (``routing.dominant_lag``). That is also how a live desk behaves: you route
    to the contract that was already confirmed.
    """

    def __init__(self, dataset: MarketDataset, config: RoutingConfig):
        self.dataset = dataset
        self.config = config
        self.warmup_fallbacks: set[tuple[str, date]] = set()

    # -- dominant resolution ----------------------------------------------

    def trading_symbol(self, underlying: str, day: date) -> str:
        """Contract the framework is allowed to trade for ``underlying`` on ``day``."""
        reference = day
        if self.config.dominant_lag:
            previous = self.dataset.previous_trading_day(day, self.config.dominant_lag)
            # The first days of the window have no earlier day *inside* it, but the
            # dominant map usually reaches further back, so ask the calendar day
            # before rather than reusing the window's own first day.
            reference = previous or (day - timedelta(days=1))
        symbol = self.dataset.dominant_symbol(underlying, reference)
        if symbol is None and reference < day:
            # No published history before the window at all: the only record we can
            # use is the one dated today, which is a one-off warm-up compromise.
            symbol = self.dataset.dominant_symbol(underlying, day)
            if symbol is not None:
                self.warmup_fallbacks.add((underlying, day))
        if symbol is None:
            raise BacktestDataError(
                f"dominant map does not cover {underlying} on {reference} "
                f"(needed to trade {day} with dominant_lag={self.config.dominant_lag})"
            )
        if symbol not in self.dataset.contracts:
            raise BacktestDataError(f"dominant symbol has no contract: {symbol}")
        return symbol

    def previous_trading_symbol(self, underlying: str, day: date) -> str | None:
        previous = self.dataset.previous_trading_day(day, 1)
        if previous is None:
            return None
        try:
            return self.trading_symbol(underlying, previous)
        except BacktestDataError:
            return None

    def is_roll_day(self, underlying: str, day: date) -> bool:
        previous = self.previous_trading_symbol(underlying, day)
        return previous is not None and previous != self.trading_symbol(underlying, day)

    # -- order construction -----------------------------------------------

    def roll_orders(
        self,
        underlying: str,
        day: date,
        timestamp: datetime,
        account: Account,
        price_of: dict[str, float],
    ) -> list[Order]:
        """Close whatever sits on the retired contract and reopen it on the new one."""
        old_symbol = self.previous_trading_symbol(underlying, day)
        new_symbol = self.trading_symbol(underlying, day)
        if old_symbol is None or old_symbol == new_symbol:
            return []
        net = account.symbol_net_lots(old_symbol)
        orders: list[Order] = []
        if net == 0:
            return orders
        if old_symbol not in price_of or new_symbol not in price_of:
            raise BacktestDataError(
                f"cannot roll {underlying} on {day}: missing a tradable bar for "
                f"{old_symbol} or {new_symbol}"
            )
        orders.append(
            Order(
                trading_day=day,
                timestamp=timestamp,
                underlying=underlying,
                symbol=old_symbol,
                side="sell" if net > 0 else "buy",
                offset="close",
                lots=abs(net),
                reference_price=price_of[old_symbol],
                reason="roll_out",
            )
        )
        orders.append(
            Order(
                trading_day=day,
                timestamp=timestamp,
                underlying=underlying,
                symbol=new_symbol,
                side="buy" if net > 0 else "sell",
                offset="open",
                lots=abs(net),
                reference_price=price_of[new_symbol],
                reason="roll_in",
            )
        )
        return orders

    def signal_orders(
        self,
        target: TargetPosition,
        day: date,
        timestamp: datetime,
        account: Account,
        price_of: dict[str, float],
    ) -> list[Order]:
        """Translate a target net lot count into open/close orders.

        The comparison is against the underlying's *total* net exposure, so a
        position still parked on a retired contract is netted rather than
        double-counted.

        A ``limit_price`` on the target only reaches the order routed to the
        current contract. Leftovers unwinding on a retired month trade at their
        own price level, where an absolute limit meant for another contract would
        be meaningless, so those stay market orders.
        """
        underlying = target.underlying
        symbol = self.trading_symbol(underlying, day)
        current = account.net_lots(underlying)
        difference = int(target.net_lots) - current
        if difference == 0:
            return []
        if symbol not in price_of:
            return []

        orders: list[Order] = []
        remaining = abs(difference)
        side = "buy" if difference > 0 else "sell"
        # Shut the opposing side before opening: an exchange has no netting for you.
        opposing = "short" if difference > 0 else "long"
        for held_symbol in self._symbols_with(account, underlying, opposing, symbol):
            if remaining <= 0:
                break
            position = account.position(held_symbol, opposing)
            lots = min(remaining, position.lots)
            if held_symbol not in price_of:
                continue
            orders.append(
                Order(
                    trading_day=day,
                    timestamp=timestamp,
                    underlying=underlying,
                    symbol=held_symbol,
                    side=side,
                    offset="close",
                    lots=lots,
                    reference_price=price_of[held_symbol],
                    reason="signal",
                    limit_price=target.limit_price if held_symbol == symbol else None,
                    stop_price=target.stop_price if held_symbol == symbol else None,
                    time_in_force=target.time_in_force,
                )
            )
            remaining -= lots
        if remaining > 0:
            orders.append(
                Order(
                    trading_day=day,
                    timestamp=timestamp,
                    underlying=underlying,
                    symbol=symbol,
                    side=side,
                    offset="open",
                    lots=remaining,
                    reference_price=price_of[symbol],
                    reason="signal",
                    limit_price=target.limit_price,
                    stop_price=target.stop_price,
                    time_in_force=target.time_in_force,
                )
            )
        return orders

    def expiry_orders(
        self,
        underlying: str,
        day: date,
        timestamp: datetime,
        account: Account,
        price_of: dict[str, float],
    ) -> list[Order]:
        """Flatten contracts that are about to expire, if a buffer is configured."""
        buffer = self.config.force_close_before_expiry_days
        if not buffer:
            return []
        orders: list[Order] = []
        for position in account.open_positions():
            if position.underlying != underlying:
                continue
            info = self.dataset.contracts[position.symbol]
            if info.expire_date is None:
                continue
            if (info.expire_date - day).days > buffer:
                continue
            if position.symbol not in price_of:
                continue
            orders.append(
                Order(
                    trading_day=day,
                    timestamp=timestamp,
                    underlying=underlying,
                    symbol=position.symbol,
                    side="sell" if position.direction == "long" else "buy",
                    offset="close",
                    lots=position.lots,
                    reference_price=price_of[position.symbol],
                    reason="expiry",
                )
            )
        return orders

    def _symbols_with(
        self, account: Account, underlying: str, direction: str, current: str
    ) -> list[str]:
        """Held symbols on one side, retired contracts first so leftovers unwind."""
        symbols = [
            position.symbol
            for position in account.open_positions()
            if position.underlying == underlying and position.direction == direction
        ]
        return sorted(symbols, key=lambda symbol: (symbol == current, symbol))
