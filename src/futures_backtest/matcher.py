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

        price, slippage_ticks = self._fill_price(order.side, reference, info.tick_size, bar)

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
        price = _align(reference + sign * self.config.slippage_ticks * tick, tick, side)
        if bar.upper_limit is not None:
            price = min(price, bar.upper_limit)
        if bar.lower_limit is not None:
            price = max(price, bar.lower_limit)
        return price, abs(price - reference) / tick

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
