"""The shipped example strategies must stay loadable by the path in the example configs."""

from __future__ import annotations

import pandas as pd
import pytest

from futures_backtest import load_strategy, run_backtest
from tests.support import DictAdapter, config_for, intraday_tables, trading_days

PATHS = [
    "futures_backtest.contrib.strategies:BuyAndHoldUnderlying",
    "futures_backtest.contrib.strategies:MovingAverageCross",
    "futures_backtest.contrib.strategies:IntradayRangeBreakout",
]


@pytest.mark.parametrize("path", PATHS)
def test_contrib_strategies_load_from_an_installed_package(path):
    strategy = load_strategy(path, {"underlying": "RB", "lots": 1})
    assert strategy.parameters["underlying"] == "RB"
    assert callable(strategy.on_bar)


def test_the_intraday_example_strategy_goes_home_flat_every_day(tmp_path):
    """What the example is there to show: a second decision, and no overnight risk."""
    days = trading_days(6)
    # Bars at 09:00 through 13:00: two form the range, one can break it, and the
    # 12:00 cut-off still leaves a bar for the exit to fill on.
    tables = intraday_tables(days, days[3], shape=(0, 6, 14, 2, -6), step_minutes=60)
    config = config_for(
        tables,
        output_root=tmp_path,
        strategy="futures_backtest.contrib.strategies:IntradayRangeBreakout",
        parameters={"underlying": "RB", "lots": 1, "opening_bars": 2, "flat_after": "12:00"},
    )
    config.data.bar_freq = "1h"
    result = run_backtest(config, DictAdapter(tables))

    fills = pd.read_csv(result.run_path / "fills.csv")
    traded = fills[fills["filled_lots"] > 0]
    assert not traded.empty
    # Everything it opened, it closed the same day.
    assert set(traded[traded["offset"].str.startswith("close")]["offset"]) == {"close_today"}
    nav = pd.read_csv(result.run_path / "nav.csv")
    assert (nav["margin"] == 0).all()
