"""A strategy living in the user's own project, next to the config that names it.

This is the shape a real desk uses: the framework is installed from PyPI, and the
strategy is your code. The CLI puts the config file's directory on ``sys.path``, so
``strategy.path: strategies:BreakoutWithStop`` resolves from any working directory.

Ready-made examples ship inside the package as
``futures_backtest.contrib.strategies``; this file exists to show the other path.
"""

from __future__ import annotations

from futures_backtest import BaseStrategy, StrategyContext, TargetPosition


class BreakoutWithStop(BaseStrategy):
    """Buy an N-day high, leave on an M-day low, sized in lots.

    Note what is absent: no contract codes, no roll handling, no fee arithmetic.
    The strategy states the exposure it wants on the underlying and stops there.
    """

    def on_bar(self, context: StrategyContext) -> TargetPosition | None:
        underlying = self.parameters.get("underlying", context.underlyings[0])
        entry_window = int(self.parameters.get("entry_window", 4))
        exit_window = int(self.parameters.get("exit_window", 2))
        lots = int(self.parameters.get("lots", 1))

        history = context.history(underlying)
        daily = history.groupby("trading_day").agg(high=("high", "max"), low=("low", "min"))
        if len(daily) <= entry_window:
            return None

        bar = context.bar(underlying)
        if bar is None:
            return None
        held = context.net_lots(underlying)

        if held <= 0 and bar.close >= float(daily["high"].iloc[-entry_window - 1 : -1].max()):
            return TargetPosition(underlying=underlying, net_lots=lots)
        if held > 0 and bar.close <= float(daily["low"].iloc[-exit_window - 1 : -1].min()):
            return TargetPosition(underlying=underlying, net_lots=0)
        return None
