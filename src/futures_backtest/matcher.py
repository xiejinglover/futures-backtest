from __future__ import annotations

import math
from datetime import date

from .account import Account
from .config import ExecutionConfig
from .dataset import MarketDataset
from .types import Bar, Fill, Order

EPSILON = 1e-9


def _align(price: float, tick: float, side: str) -> float:
    """Snap a price onto the contract's tick grid, always against the trader."""
    steps = price / tick
    aligned = math.ceil(steps - EPSILON) if side == "buy" else math.floor(steps + EPSILON)
    return round(aligned * tick, 10)


def closing_direction(side: str) -> str:
    """A sell closes a long; a buy closes a short."""
    return "long" if side == "sell" else "short"


def bar_path(bar: Bar) -> tuple[float, float, float, float]:
    """The order in which a bar is assumed to have visited its four prices.

    OHLC alone cannot say whether the high or the low came first, so this reads
    the open's position: whichever extreme sits closer to the open is assumed to
    have been reached first. It is a guess, and a shorter bar period is the only
    real cure.
    """
    if bar.open - bar.low <= bar.high - bar.open:
        return (bar.open, bar.low, bar.high, bar.close)
    return (bar.open, bar.high, bar.low, bar.close)


def path_reach(bar: Bar, price: float) -> int:
    """Which leg of :func:`bar_path` first ran through ``price``.

    Used to put several fills inside one bar into a plausible order, which
    matters when they compete for the same free margin.
    """
    path = bar_path(bar)
    for index in range(1, len(path)):
        low, high = sorted((path[index - 1], path[index]))
        if low - EPSILON <= price <= high + EPSILON:
            return index
    return len(path)


class Matcher:
    """Bar-level fill simulation: tick grid, price limits, margin, and fees."""

    def __init__(self, dataset: MarketDataset, config: ExecutionConfig):
        self.dataset = dataset
        self.config = config

    def execute(self, order: Order, bar: Bar, account: Account) -> list[Fill]:
        info = self.dataset.contracts[order.symbol]
        reference = order.reference_price

        if order.lots <= 0:
            return [self._reject(order, reference, "non_positive_lots")]
        if bar.volume <= 0:
            return [self._reject(order, reference, "no_volume")]
        if self.config.enforce_price_limits:
            locked = self._limit_locked(order.side, reference, bar)
            if locked is not None:
                return [self._reject(order, reference, locked)]

        if order.limit_price is None:
            price, slippage_ticks = self._fill_price(order.side, reference, info.tick_size, bar)
        else:
            limit = self.aligned_limit(order)
            reached = self._limit_fill_price(order.side, limit, bar)
            if reached is None:
                return [self._reject(order, limit, "limit_not_reached")]
            # A limit order pays no slippage: it either got the price it asked for
            # or a better one on a gap. Reporting the gap gain as slippage would
            # feed a negative cost into the roll cost aggregation.
            price, slippage_ticks = self._clamp_to_limits(reached, bar), 0.0

        if order.offset == "open":
            direction = "long" if order.side == "buy" else "short"
            lots = self._openable_lots(order, account, direction, price)
            if lots <= 0:
                return [self._reject(order, price, "insufficient_margin")]
            commission = self._commission(order.symbol, order.trading_day, "open", lots, price)
            account.apply_open(order.symbol, direction, lots, price, commission)
            return [self._fill(order, lots, price, commission, slippage_ticks, 0.0, lots)]

        direction = closing_direction(order.side)
        held = account.position(order.symbol, direction).lots
        lots = min(order.lots, held)
        if lots <= 0:
            return [self._reject(order, price, "insufficient_position")]
        yesterday, today = account.close_lots_split(order.symbol, direction, lots)
        fills: list[Fill] = []
        for offset, portion in (("close", yesterday), ("close_today", today)):
            if portion <= 0:
                continue
            commission = self._commission(order.symbol, order.trading_day, offset, portion, price)
            pnl = account.apply_close(
                order.symbol,
                direction,
                portion,
                price,
                commission,
                from_today=offset == "close_today",
            )
            fills.append(
                self._fill(order, portion, price, commission, slippage_ticks, pnl, lots, offset)
            )
        return fills

    def limit_reached(self, order: Order, bar: Bar) -> float | None:
        """Fill price if a resting limit would trade on this bar, else ``None``.

        A read-only probe, so the scheduler can test an order against every bar
        of the day without minting a rejection record each time. Saying no here
        leaves the order working: an untraded bar or a locked board is a reason
        not to fill, not a reason to cancel.

        The reference price is taken from this bar rather than from the bar the
        order was placed on, matching what :meth:`execute` will be handed once
        the answer is yes.
        """
        if order.limit_price is None or order.lots <= 0 or bar.volume <= 0:
            return None
        if self.config.enforce_price_limits:
            if self._limit_locked(order.side, bar.open, bar) is not None:
                return None
        return self._limit_fill_price(order.side, self.aligned_limit(order), bar)

    def aligned_limit(self, order: Order) -> float:
        """The order's limit snapped onto the contract's tick grid."""
        tick = self.dataset.contracts[order.symbol].tick_size
        return self._align_limit(order.side, float(order.limit_price or 0.0), tick)

    def cancel(self, order: Order, reason: str) -> Fill:
        """A record for an order that stopped working without ever trading."""
        return self._reject(order, self.aligned_limit(order), reason)

    # -- pricing ----------------------------------------------------------

    def _limit_locked(self, side: str, reference: float, bar: Bar) -> str | None:
        if side == "buy" and bar.upper_limit is not None:
            if reference >= bar.upper_limit - EPSILON:
                return "limit_up"
        if side == "sell" and bar.lower_limit is not None:
            if reference <= bar.lower_limit + EPSILON:
                return "limit_down"
        return None

    def _fill_price(
        self, side: str, reference: float, tick: float, bar: Bar
    ) -> tuple[float, float]:
        sign = 1.0 if side == "buy" else -1.0
        price = self._clamp_to_limits(
            _align(reference + sign * self.config.slippage_ticks * tick, tick, side), bar
        )
        return price, abs(price - reference) / tick

    def _clamp_to_limits(self, price: float, bar: Bar) -> float:
        if bar.upper_limit is not None:
            price = min(price, bar.upper_limit)
        if bar.lower_limit is not None:
            price = max(price, bar.lower_limit)
        return price

    def _align_limit(self, side: str, limit: float, tick: float) -> float:
        """Snap a limit onto the tick grid without making the order more aggressive.

        This is the mirror image of :func:`_align`, which snaps a *fill* price and
        therefore rounds the expensive way. Here a buy limit rounds down and a sell
        limit rounds up, so an off-grid request can only ever fill less often.
        """
        steps = limit / tick
        aligned = math.floor(steps + EPSILON) if side == "buy" else math.ceil(steps - EPSILON)
        return round(aligned * tick, 10)

    def _limit_fill_price(self, side: str, limit: float, bar: Bar) -> float | None:
        """Where a day limit order would have filled, or ``None`` if it never did.

        A bar that opens through the limit fills at the open, because by then the
        resting order is marketable. Otherwise the day's extreme has to reach the
        limit, and ``limit_fill_rule`` decides whether merely touching it counts.
        """
        touch = self.config.limit_fill_rule == "touch"
        if side == "buy":
            if bar.open <= limit + EPSILON:
                return bar.open
            reached = bar.low <= limit + EPSILON if touch else bar.low < limit - EPSILON
            return limit if reached else None
        if bar.open >= limit - EPSILON:
            return bar.open
        reached = bar.high >= limit - EPSILON if touch else bar.high > limit + EPSILON
        return limit if reached else None

    # -- costs and limits -------------------------------------------------

    def _commission(self, symbol: str, day: date, offset: str, lots: int, price: float) -> float:
        info = self.dataset.contracts[symbol]
        charge = self.dataset.charge(symbol, day)
        if offset == "open":
            rate_key, lot_key = "open_fee_rate", "open_fee_per_lot"
        elif offset == "close_today" and (
            "close_today_fee_rate" in charge or "close_today_fee_per_lot" in charge
        ):
            rate_key, lot_key = "close_today_fee_rate", "close_today_fee_per_lot"
        else:
            rate_key, lot_key = "close_fee_rate", "close_fee_per_lot"
        notional = lots * info.multiplier * price
        return notional * charge.get(rate_key, 0.0) + lots * charge.get(lot_key, 0.0)

    def _openable_lots(self, order: Order, account: Account, direction: str, price: float) -> int:
        required = account.margin_for(order.symbol, direction, order.lots, price, order.trading_day)
        available = account.available(order.trading_day)
        if required <= available + EPSILON:
            return order.lots
        if self.config.on_margin_short == "reject":
            return 0
        per_lot = account.margin_for(order.symbol, direction, 1, price, order.trading_day)
        if per_lot <= 0:
            return 0
        return max(0, min(order.lots, int(math.floor((available + EPSILON) / per_lot))))

    # -- fill records -----------------------------------------------------

    def _fill(
        self,
        order: Order,
        lots: int,
        price: float,
        commission: float,
        slippage_ticks: float,
        realized_pnl: float,
        total_filled: int,
        offset: str | None = None,
    ) -> Fill:
        return Fill(
            trading_day=order.trading_day,
            timestamp=order.timestamp,
            underlying=order.underlying,
            symbol=order.symbol,
            side=order.side,
            offset=offset or order.offset,
            requested_lots=order.lots,
            filled_lots=lots,
            price=price,
            commission=commission,
            slippage_ticks=slippage_ticks,
            realized_pnl=realized_pnl,
            status="filled" if total_filled == order.lots else "partial",
            reason=order.reason,
        )

    def _reject(self, order: Order, price: float, reason: str) -> Fill:
        return Fill(
            trading_day=order.trading_day,
            timestamp=order.timestamp,
            underlying=order.underlying,
            symbol=order.symbol,
            side=order.side,
            offset=order.offset,
            requested_lots=order.lots,
            filled_lots=0,
            price=price,
            commission=0.0,
            slippage_ticks=0.0,
            realized_pnl=0.0,
            status="rejected",
            reason=order.reason,
            reject_reason=reason,
        )
