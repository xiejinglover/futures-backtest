"""Example strategies, shipped so a fresh install has something runnable.

This module is **example-level code**. It is not part of the framework contract,
carries no stability promise, and may be removed in a future minor release. Real
strategies belong in your own installable package; see the README.

Both strategies only ever name an underlying, never a month contract: the router
resolves the tradable contract and moves the position when the dominant changes.
"""

from __future__ import annotations

from ..strategy import BaseStrategy, StrategyContext
from ..types import TargetPosition


class BuyAndHoldUnderlying(BaseStrategy):
    """Hold a fixed number of lots on one underlying.

    Parameters: ``underlying``, ``lots`` (default 1), ``warmup`` (default 1).
    """

    def on_bar(self, context: StrategyContext) -> TargetPosition | None:
        underlying = self.parameters.get("underlying", context.underlyings[0])
        lots = int(self.parameters.get("lots", 1))
        if context.bars_seen < int(self.parameters.get("warmup", 1)):
            return None
        return TargetPosition(underlying=underlying, net_lots=lots)


class MovingAverageCross(BaseStrategy):
    """Long above the moving average, short below, sized in lots.

    Reads history through ``context.history``, which never returns a bar past the
    one the strategy is standing on.

    Parameters: ``underlying``, ``window`` (default 5), ``lots`` (default 1),
    ``allow_short`` (default true; when false the strategy goes flat instead).
    """

    def on_bar(self, context: StrategyContext) -> TargetPosition | None:
        underlying = self.parameters.get("underlying", context.underlyings[0])
        window = int(self.parameters.get("window", 5))
        lots = int(self.parameters.get("lots", 1))
        allow_short = bool(self.parameters.get("allow_short", True))

        history = context.history(underlying, bars=None)
        closes = history.groupby("trading_day")["close"].last()
        if len(closes) < window:
            return None
        average = float(closes.tail(window).mean())
        latest = float(closes.iloc[-1])
        if latest > average:
            return TargetPosition(underlying=underlying, net_lots=lots)
        if allow_short:
            return TargetPosition(underlying=underlying, net_lots=-lots)
        return TargetPosition(underlying=underlying, net_lots=0)
