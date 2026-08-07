"""The intraday loop: several decisions a day, orders that outlive a bar."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from futures_backtest import run_backtest
from tests.support import DictAdapter, config_for, intraday_tables, make_dataset, trading_days


@pytest.fixture()
def tables():
    days = trading_days(6)
    return intraday_tables(days, days[3])


def _run(tmp_path: Path, tables, **kwargs):
    config = config_for(tables, output_root=tmp_path, **kwargs)
    config.data.bar_freq = "5m"
    return run_backtest(config, DictAdapter(tables))


def _frame(result, name: str) -> pd.DataFrame:
    return pd.read_csv(result.run_path / f"{name}.csv")


def test_a_position_opened_and_closed_in_one_day_pays_the_close_today_rate(tmp_path, tables):
    """Nothing about futures locks a position overnight; this is the whole point."""
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:IntradayTurnStrategy",
        parameters={"open_slot": 1, "close_slot": 3},
    )
    fills = _frame(result, "fills")
    signals = fills[fills["reason"] == "signal"]
    closes = signals[signals["offset"].str.startswith("close")]
    assert not closes.empty
    assert set(closes["offset"]) == {"close_today"}
    # The close-today rate in the fixture is ten times the ordinary one.
    opens = signals[signals["offset"] == "open"]
    assert closes["commission"].sum() > opens["commission"].sum() * 5


def test_a_position_held_overnight_closes_against_yesterdays_lots(tmp_path, tables):
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:IntradayTurnStrategy",
        parameters={"open_slot": 3, "close_slot": 0},
    )
    fills = _frame(result, "fills")
    # The roll day is excluded on purpose: the roll reopens on the new contract
    # that morning, so closing it an hour later really is a close_today.
    roll_days = set(_frame(result, "rolls")["trading_day"])
    closes = fills[
        (fills["reason"] == "signal")
        & fills["offset"].str.startswith("close")
        & ~fills["trading_day"].isin(roll_days)
    ]
    assert len(closes) == 4
    assert set(closes["offset"]) == {"close"}


def test_settlement_still_happens_once_a_day_not_once_a_bar(tmp_path, tables):
    result = _run(tmp_path, tables)
    days = sorted(set(tables["bars"]["trading_day"]))
    assert len(_frame(result, "nav")) == len(days)
    events = _frame(result, "events")
    assert len(events[events["kind"] == "SETTLE"]) == len(days)
    # One bar event per bar, which is what makes a second decision possible.
    assert len(events[events["kind"] == "BAR"]) == len(set(tables["bars"]["datetime"]))


def test_a_resting_limit_fills_on_a_later_bar_of_the_same_day(tmp_path, tables):
    """The default path falls back through 3497 only on each day's last bar."""
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:FixedLimitStrategy",
        parameters={"limit_price": 3497.0, "lots": 1},
    )
    fills = _frame(result, "fills")
    filled = fills[fills["filled_lots"] > 0]
    assert not filled.empty
    first = filled.iloc[0]
    assert first["price"] == pytest.approx(3497.0)
    # Placed on the day's second bar, filled on its fifth.
    assert datetime.fromisoformat(first["timestamp"]).time().minute == 20


def test_an_unchanged_target_leaves_its_order_working_instead_of_re_placing_it(
    tmp_path, tables
):
    """Otherwise the fill rate would be a function of the bar period."""
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:FixedLimitStrategy",
        parameters={"limit_price": 3000.0, "lots": 1},
    )
    orders = _frame(result, "orders")
    days = sorted(set(tables["bars"]["trading_day"]))
    # One order a day, not one a bar: the target never changes, and only the
    # close empties the book.
    assert len(orders) == len(days)
    skipped = _frame(result, "skipped_targets")
    assert set(skipped["reason"]) == {"limit_not_reached"}
    assert len(skipped) == len(orders)


def test_a_changed_target_cancels_the_order_the_old_one_left_working(tmp_path, tables):
    """LimitStrategy reprices off each close, so every bar supersedes the last."""
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:LimitStrategy",
        parameters={"offset_ticks": 40, "lots": 1},
    )
    skipped = _frame(result, "skipped_targets")
    assert "superseded" in set(skipped["reason"])


def test_a_roll_cancels_what_is_still_working_on_the_contract_it_leaves(tmp_path, tables):
    """A close-time roll is the one moment an order can outlive its contract."""
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:FixedLimitStrategy",
        parameters={"limit_price": 3000.0, "lots": 1},
        routing={"roll_timing": "same_close"},
    )
    skipped = _frame(result, "skipped_targets")
    assert "cancelled_on_roll" in set(skipped["reason"])


def test_history_is_cut_at_the_cursor_and_matches_a_plain_filter(tables):
    dataset = make_dataset(tables, bar_freq="5m")
    for cutoff in list(dataset.timestamps)[::7]:
        expected = dataset.bars[
            (dataset.bars["datetime"] <= cutoff) & (dataset.bars["underlying"] == "RB")
        ].reset_index(drop=True)
        pd.testing.assert_frame_equal(dataset.history("RB", cutoff), expected)
        assert dataset.history("RB", cutoff, bars=3).equals(expected.tail(3).reset_index(drop=True))


def test_history_folds_into_daily_periods_and_the_running_day_keeps_updating(tables):
    dataset = make_dataset(tables, bar_freq="5m")
    days = sorted(set(tables["bars"]["trading_day"]))
    stamps = dataset.timestamps_of_day(days[1])

    part = dataset.history("RB", stamps[2], symbol="RB2405", freq="1d")
    raw = dataset.history("RB", stamps[2], symbol="RB2405")
    today = raw[raw["trading_day"] == days[1]]
    assert len(part) == 2
    assert part.iloc[-1]["high"] == pytest.approx(today["high"].max())
    assert part.iloc[-1]["close"] == pytest.approx(today["close"].iloc[-1])

    # Knowing the session high so far is not look-ahead; it has to grow.
    whole = dataset.history("RB", stamps[-1], symbol="RB2405", freq="1d")
    assert whole.iloc[-1]["high"] >= part.iloc[-1]["high"]
    assert whole.iloc[-1]["open"] == pytest.approx(part.iloc[-1]["open"])
    assert whole.iloc[-1]["volume"] == pytest.approx(
        dataset.history("RB", stamps[-1], symbol="RB2405")
        .query("trading_day == @days[1]")["volume"]
        .sum()
    )


def test_a_round_trip_is_reported_with_its_entry_exit_and_holding_time(tmp_path, tables):
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:IntradayTurnStrategy",
        parameters={"open_slot": 1, "close_slot": 3},
    )
    trades = _frame(result, "trades")
    assert not trades.empty
    first = trades.iloc[0]
    # Opened on the third bar, closed on the fifth, five minutes apart.
    assert first["holding_minutes"] == pytest.approx(10.0)
    assert first["net_pnl"] == pytest.approx(
        first["gross_pnl"] - first["commission"]
    )
    fills = _frame(result, "fills")
    assert trades["lots"].sum() == fills[fills["offset"].str.startswith("close")][
        "filled_lots"
    ].sum()
    assert result.metrics["round_trips"] == len(trades)


def test_a_round_trip_held_overnight_does_not_match_the_fill_it_closed_on(tmp_path, tables):
    """The two figures are on different bases, and the difference is expected.

    Settlement resets the position's average price every evening, so a closing
    fill reports only the last leg; the earlier legs sit in settlement_variation
    on the nav. A round trip is the whole move, entry to exit.
    """
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:IntradayTurnStrategy",
        parameters={"open_slot": 3, "close_slot": 0},
    )
    trades = _frame(result, "trades")
    overnight = trades[trades["entry_day"] != trades["exit_day"]]
    assert not overnight.empty

    fills = _frame(result, "fills")
    closes = fills[(fills["reason"] == "signal") & fills["offset"].str.startswith("close")]
    assert overnight["gross_pnl"].iloc[0] != pytest.approx(closes["realized_pnl"].iloc[0])

    nav = _frame(result, "nav")
    total = (
        fills["realized_pnl"].sum()
        + nav["settlement_variation"].sum()
        - fills["commission"].sum()
    )
    assert result.metrics["final_equity"] - result.metrics["initial_cash"] == pytest.approx(total)
