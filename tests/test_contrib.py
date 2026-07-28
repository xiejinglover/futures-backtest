"""The shipped example strategies must stay loadable by the path in the example configs."""

from __future__ import annotations

import pytest

from futures_backtest import load_strategy

PATHS = [
    "futures_backtest.contrib.strategies:BuyAndHoldUnderlying",
    "futures_backtest.contrib.strategies:MovingAverageCross",
]


@pytest.mark.parametrize("path", PATHS)
def test_contrib_strategies_load_from_an_installed_package(path):
    strategy = load_strategy(path, {"underlying": "RB", "lots": 1})
    assert strategy.parameters["underlying"] == "RB"
    assert callable(strategy.on_bar)
