from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import pandas as pd

from .types import AccountSnapshot, BacktestDataError, Bar, TargetPosition


@dataclass(frozen=True)
class StrategyContext:
    """Everything a strategy may read, and nothing it may mutate.

    ``history`` and ``bar`` stop at the current bar; asking for anything later
    raises instead of quietly returning future data.
    """

    trading_day: date
    timestamp: datetime
    bars_seen: int
    bars: dict[str, Bar]
    account: AccountSnapshot
    underlyings: tuple[str, ...]
    _history: Any
    _routing: dict[str, str]
    _tick_sizes: dict[str, float]

    def bar(self, underlying: str) -> Bar | None:
        """The current bar of the contract the framework is trading, if any."""
        return self.bars.get(underlying)

    def history(
        self, underlying: str, bars: int | None = None, symbol: str | None = None
    ) -> pd.DataFrame:
        return self._history(underlying, self.timestamp, bars, symbol)

    def trading_symbol(self, underlying: str) -> str:
        """Read-only: which month contract the router is using right now."""
        symbol = self._routing.get(underlying)
        if symbol is None:
            raise BacktestDataError(f"no routed contract for {underlying} on {self.trading_day}")
        return symbol

    def tick_size(self, underlying: str) -> float:
        """Minimum price increment of the routed contract, for pricing limits."""
        tick = self._tick_sizes.get(underlying)
        if tick is None:
            raise BacktestDataError(f"no routed contract for {underlying} on {self.trading_day}")
        return tick

    def net_lots(self, underlying: str) -> int:
        return self.account.net_lots.get(underlying, 0)


class Strategy(Protocol):
    """Underlying-level signal producer.

    Implementations receive ``parameters`` from the config and return a
    ``TargetPosition`` (or a list of them for multiple underlyings). Returning
    ``None`` means "no change today".
    """

    parameters: dict[str, Any]

    def on_bar(
        self, context: StrategyContext
    ) -> TargetPosition | list[TargetPosition] | None: ...


class BaseStrategy:
    """Convenience base class that stores ``parameters`` and no-ops ``on_bar``."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})

    def on_bar(self, context: StrategyContext) -> TargetPosition | list[TargetPosition] | None:
        return None


def load_strategy(path: str, parameters: dict[str, Any]) -> Strategy:
    module_name, _, attribute = path.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        raise BacktestDataError(
            f"cannot import strategy module {module_name!r}: {error}. The CLI looks "
            "for it next to the config file; install your strategy package or move "
            "the config beside it"
        ) from error
    try:
        factory = getattr(module, attribute)
    except AttributeError as error:
        raise BacktestDataError(f"{module_name!r} has no attribute {attribute!r}") from error
    instance = factory(parameters)
    if not hasattr(instance, "on_bar"):
        raise BacktestDataError(f"strategy {path!r} does not implement on_bar")
    if not hasattr(instance, "parameters"):
        instance.parameters = dict(parameters)
    return instance


def normalize_targets(
    result: TargetPosition | list[TargetPosition] | None,
    allowed: tuple[str, ...],
) -> list[TargetPosition]:
    if result is None:
        return []
    targets = [result] if isinstance(result, TargetPosition) else list(result)
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, TargetPosition):
            raise BacktestDataError(
                f"strategy returned {type(target).__name__}, expected TargetPosition"
            )
        if target.underlying not in allowed:
            raise BacktestDataError(
                f"strategy targeted {target.underlying!r}, which is not in data.underlyings"
            )
        if target.underlying in seen:
            raise BacktestDataError(
                f"strategy returned two targets for {target.underlying!r} on one bar"
            )
        seen.add(target.underlying)
    return targets
