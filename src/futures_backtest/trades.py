from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, datetime

from .matcher import closing_direction
from .types import Fill


@dataclass(frozen=True)
class Trade:
    """One round trip: lots opened at one moment and closed at another.

    Beware of comparing ``gross_pnl`` against a fill's ``realized_pnl``. Settlement
    resets each position's average price to the settlement price every evening, so
    a fill reports only the last leg of the move, and the earlier legs live in
    ``settlement_variation`` on the nav. This is the whole move, entry to exit.
    """

    underlying: str
    symbol: str
    direction: str  # "long" | "short"
    lots: int
    entry_day: date
    entry_time: datetime
    entry_price: float
    exit_day: date
    exit_time: datetime
    exit_price: float
    holding_minutes: float
    gross_pnl: float
    commission: float
    net_pnl: float


@dataclass
class _Lot:
    day: date
    time: datetime
    price: float
    commission: float


class TradeLedger:
    """Pairs closing fills with the lots they closed, oldest lot first.

    The account keeps only a net average price per position, which cannot say
    when a lot was opened. This keeps that entry detail so holding time and
    per-round-trip profit are answerable, which is what turns a day of T+0
    turnover into something a person can read.
    """

    def __init__(self, multipliers: dict[str, float]):
        self._multipliers = multipliers
        self._open: dict[tuple[str, str], deque[_Lot]] = {}
        self.trades: list[Trade] = []

    def record(self, fill: Fill) -> None:
        if fill.filled_lots <= 0:
            return
        per_lot = fill.commission / fill.filled_lots
        if fill.offset == "open":
            direction = "long" if fill.side == "buy" else "short"
            queue = self._open.setdefault((fill.symbol, direction), deque())
            for _ in range(fill.filled_lots):
                queue.append(_Lot(fill.trading_day, fill.timestamp, fill.price, per_lot))
            return

        direction = closing_direction(fill.side)
        # Lots opened together are reported as one round trip; the split into
        # close and close_today is an accounting detail, not a second trade.
        queue = self._open.get((fill.symbol, direction), deque())
        closed: list[_Lot] = []
        while queue and len(closed) < fill.filled_lots:
            closed.append(queue.popleft())

        sign = 1.0 if direction == "long" else -1.0
        multiplier = self._multipliers.get(fill.symbol, 1.0)
        start = 0
        while start < len(closed):
            lot = closed[start]
            lots = 1
            while start + lots < len(closed) and (
                closed[start + lots].time == lot.time and closed[start + lots].price == lot.price
            ):
                lots += 1
            gross = sign * (fill.price - lot.price) * multiplier * lots
            commission = (lot.commission + per_lot) * lots
            self.trades.append(
                Trade(
                    underlying=fill.underlying,
                    symbol=fill.symbol,
                    direction=direction,
                    lots=lots,
                    entry_day=lot.day,
                    entry_time=lot.time,
                    entry_price=lot.price,
                    exit_day=fill.trading_day,
                    exit_time=fill.timestamp,
                    exit_price=fill.price,
                    holding_minutes=(fill.timestamp - lot.time).total_seconds() / 60.0,
                    gross_pnl=gross,
                    commission=commission,
                    net_pnl=gross - commission,
                )
            )
            start += lots
