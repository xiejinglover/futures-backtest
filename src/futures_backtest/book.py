from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date

from .types import Order, TargetPosition


@dataclass
class WorkingOrder:
    """Mutable lifecycle state around an immutable routed order."""

    order: Order
    triggered: bool = False


@dataclass
class Working:
    """One underlying target whose contract orders are still working."""

    underlying: str
    signal_day: date
    target: TargetPosition
    expires_on: date | None
    states: list[WorkingOrder] = field(default_factory=list)

    @property
    def orders(self) -> list[Order]:
        return [state.order for state in self.states]


class RestingOrders:
    """Limit and stop orders that keep working until filled or expired.

    The target is keyed by underlying, while every child order remains bound to its
    concrete symbol. Different instruments therefore skip unrelated global time
    slots independently. A new target supersedes the old target for that underlying.

    The reason this exists at all is bar granularity. With one bar per day an
    order could be tested and cancelled inside a single bar, but a day of minute
    bars would turn the same code into a one-minute order, and the fill rate
    would then drift with the bar period rather than with the market.
    """

    def __init__(self) -> None:
        self._working: dict[str, Working] = {}

    def __len__(self) -> int:
        return sum(len(item.orders) for item in self._working.values())

    def place(
        self,
        signal_day: date,
        target: TargetPosition,
        orders: list[Order],
        expires_on: date | None,
    ) -> None:
        if orders:
            self._working[target.underlying] = Working(
                underlying=target.underlying,
                signal_day=signal_day,
                target=target,
                expires_on=expires_on,
                states=[WorkingOrder(order) for order in orders],
            )

    def holds(self, target: TargetPosition) -> bool:
        """True when this exact target is already working.

        A position-target strategy re-emits the same target on every bar. Treating
        that as a new instruction would cancel and re-place the order every bar,
        which is the one-minute-order behaviour this class exists to avoid.
        """
        current = self._working.get(target.underlying)
        return current is not None and current.target == target

    def working(self) -> list[tuple[Working, WorkingOrder]]:
        return [
            (item, state)
            for underlying in sorted(self._working)
            for item in (self._working[underlying],)
            for state in list(item.states)
        ]

    def update_triggered(self, state: WorkingOrder, triggered: bool) -> None:
        state.triggered = triggered

    def apply_fill(self, underlying: str, state: WorkingOrder, filled_lots: int) -> None:
        item = self._working.get(underlying)
        if item is None:
            return
        remaining = state.order.lots - filled_lots
        if remaining > 0:
            state.order = replace(state.order, lots=remaining)
            return
        item.states = [candidate for candidate in item.states if candidate is not state]
        if not item.states:
            del self._working[underlying]

    def cancel(self, underlying: str) -> list[Working]:
        item = self._working.pop(underlying, None)
        return [item] if item is not None else []

    def cancel_all(self) -> list[Working]:
        cancelled = [self._working[underlying] for underlying in sorted(self._working)]
        self._working.clear()
        return cancelled

    def cancel_expired(self, day: date) -> list[Working]:
        expired = [
            underlying
            for underlying, item in self._working.items()
            if item.expires_on is not None and item.expires_on <= day
        ]
        return [self._working.pop(underlying) for underlying in sorted(expired)]
