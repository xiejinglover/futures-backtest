"""Example strategies, shipped so a fresh install has something runnable.

This module is **example-level code**. It is not part of the framework contract,
carries no stability promise, and may be removed in a future minor release. Real
strategies belong in your own installable package; see the README.

Both strategies only ever name an underlying, never a month contract: the router
resolves the tradable contract and moves the position when the dominant changes.
"""

from __future__ import annotations

from datetime import time

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


class IntradayRangeBreakout(BaseStrategy):
    """Take the break of the session's opening range, and go home flat.

    Needs intraday bars: it decides several times a day and closes what it opened
    before the session ends, which is the thing a one-bar-per-day loop cannot
    express. Set ``data.bar_freq`` to the period your source actually delivers.

    Parameters: ``underlying``, ``lots`` (default 1), ``opening_bars`` (default 4,
    how many bars form the range), ``flat_after`` (default ``"14:00"``, the time
    after which it only reduces), and ``limit_offset_ticks`` (default 0).

    A non-zero offset waits for a pullback that many ticks back inside the range
    instead of chasing the break. That makes the entry a limit order, and because
    it is priced off the range rather than off the moving close, the order stays
    put and works across the rest of the session. Breaks that never pull back are
    simply missed, and land in ``skipped_targets.csv`` as ``limit_not_reached``.
    """

    def on_bar(self, context: StrategyContext) -> TargetPosition | None:
        underlying = self.parameters.get("underlying", context.underlyings[0])
        lots = int(self.parameters.get("lots", 1))
        opening_bars = int(self.parameters.get("opening_bars", 4))
        offset = float(self.parameters.get("limit_offset_ticks", 0))

        bar = context.bar(underlying)
        if bar is None:
            return None
        today = context.history(underlying, symbol=context.trading_symbol(underlying))
        today = today[today["trading_day"] == context.trading_day]
        if len(today) < opening_bars:
            return None

        if self._closing(context):
            return TargetPosition(underlying=underlying, net_lots=0)
        if context.net_lots(underlying) != 0:
            return None

        high = float(today["high"].iloc[:opening_bars].max())
        low = float(today["low"].iloc[:opening_bars].min())
        if bar.close > high:
            direction = 1
        elif bar.close < low:
            direction = -1
        else:
            return None
        limit = None
        if offset:
            # Anchored to the range edge, not to the close, so the price stays the
            # same on every later bar and the order keeps working instead of being
            # cancelled and re-placed.
            edge = high if direction > 0 else low
            limit = edge - direction * offset * context.tick_size(underlying)
        return TargetPosition(
            underlying=underlying, net_lots=direction * lots, limit_price=limit
        )

    def _closing(self, context: StrategyContext) -> bool:
        """Whether the session is late enough that the book should be flattened.

        The upper bound matters: a night session belongs to the *next* trading
        day, so its 21:00-23:00 bars are the first bars of that day, not the last.
        Comparing on time alone would read them as past the cut-off.
        """
        after = time.fromisoformat(str(self.parameters.get("flat_after", "14:00")))
        now = context.timestamp.time()
        return after <= now <= time(20, 0)
