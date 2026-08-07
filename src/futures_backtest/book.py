from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .types import Order, TargetPosition


@dataclass
class Working:
    """One underlying's limit orders that are still waiting for a fill."""

    underlying: str
    signal_day: date
    target: TargetPosition
    orders: list[Order] = field(default_factory=list)


class RestingOrders:
    """Limit orders that keep working across bars until the close cancels them.

    Keyed by underlying, because a strategy expresses itself as one net target per
    underlying: a new, different target supersedes whatever is still working.

    The reason this exists at all is bar granularity. With one bar per day an
    order could be tested and cancelled inside a single bar, but a day of minute
    bars would turn the same code into a one-minute order, and the fill rate
    would then drift with the bar period rather than with the market.
    """

    def __init__(self) -> None:
        self._working: dict[str, Working] = {}

    def __len__(self) -> int:
        return sum(len(item.orders) for item in self._working.values())

    def place(self, signal_day: date, target: TargetPosition, orders: list[Order]) -> None:
        if orders:
            self._working[target.underlying] = Working(
                underlying=target.underlying,
                signal_day=signal_day,
                target=target,
                orders=list(orders),
            )

    def holds(self, target: TargetPosition) -> bool:
        """True when this exact target is already working.

        A position-target strategy re-emits the same target on every bar. Treating
        that as a new instruction would cancel and re-place the order every bar,
        which is the one-minute-order behaviour this class exists to avoid.
        """
        current = self._working.get(target.underlying)
        return current is not None and current.target == target

    def working(self) -> list[tuple[Working, Order]]:
        return [
            (item, order)
            for underlying in sorted(self._working)
            for item in (self._working[underlying],)
            for order in list(item.orders)
        ]

    def fill(self, underlying: str, order: Order) -> None:
        item = self._working.get(underlying)
        if item is None:
            return
        item.orders = [candidate for candidate in item.orders if candidate is not order]
        if not item.orders:
            del self._working[underlying]

    def cancel(self, underlying: str) -> list[Working]:
        item = self._working.pop(underlying, None)
        return [item] if item is not None else []

    def cancel_all(self) -> list[Working]:
        cancelled = [self._working[underlying] for underlying in sorted(self._working)]
        self._working.clear()
        return cancelled
