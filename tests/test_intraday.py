"""The intraday loop: several decisions a day, orders that outlive a bar."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from futures_backtest import BacktestDataError, run_backtest
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


def test_an_unchanged_target_leaves_its_order_working_instead_of_re_placing_it(tmp_path, tables):
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


def test_a_partial_fill_keeps_the_same_order_working_for_its_remainder(tmp_path, tables):
    tables["bars"]["volume"] = 100
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:FixedLimitStrategy",
        parameters={"limit_price": 4000.0, "lots": 3},
        execution={"volume_participation": 0.01},
    )
    orders = _frame(result, "orders")
    first_order = orders.iloc[0]["order_id"]
    fills = _frame(result, "fills")
    parts = fills[(fills["order_id"] == first_order) & (fills["filled_lots"] > 0)]
    assert list(parts["filled_lots"]) == [1, 1, 1]
    assert parts["filled_lots"].sum() == 3


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


def test_a_gtc_stop_survives_the_close_and_fills_the_next_day(tmp_path, tables):
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:FixedStopStrategy",
        parameters={"stop_price": 3520.0, "lots": 1, "time_in_force": "GTC"},
    )
    orders = _frame(result, "orders")
    stop_orders = orders[orders["stop_price"].notna()]
    assert len(stop_orders) == 1
    fills = _frame(result, "fills")
    stop_fill = fills[
        (fills["order_id"] == stop_orders.iloc[0]["order_id"]) & (fills["filled_lots"] > 0)
    ]
    assert len(stop_fill) == 1
    assert (
        pd.to_datetime(stop_fill.iloc[0]["trading_day"]).date()
        > pd.to_datetime(stop_orders.iloc[0]["trading_day"]).date()
    )
    events = _frame(result, "events")
    triggers = events[events["kind"] == "ORDER_TRIGGER"]
    assert list(triggers["order_id"].dropna()) == [stop_orders.iloc[0]["order_id"]]


def test_a_market_target_waits_for_its_own_underlyings_next_bar(tmp_path, tables):
    rb = tables["bars"]
    ag_bars = rb[rb["symbol"] == "RB2405"].copy()
    ag_bars["symbol"] = "AG2405"
    ag_bars["underlying"] = "AG"
    ag_bars["datetime"] = ag_bars["datetime"] + timedelta(minutes=7)
    tables["bars"] = pd.concat([rb, ag_bars], ignore_index=True)

    ag_contract = tables["contracts"].iloc[[0]].copy()
    ag_contract["symbol"] = "AG2405"
    ag_contract["underlying"] = "AG"
    tables["contracts"] = pd.concat([tables["contracts"], ag_contract], ignore_index=True)
    ag_dominant = tables["dominant_map"].drop_duplicates("trading_day").copy()
    ag_dominant["underlying"] = "AG"
    ag_dominant["dominant_symbol"] = "AG2405"
    tables["dominant_map"] = pd.concat([tables["dominant_map"], ag_dominant], ignore_index=True)
    ag_settles = tables["settles"][tables["settles"]["symbol"] == "RB2405"].copy()
    ag_settles["symbol"] = "AG2405"
    tables["settles"] = pd.concat([tables["settles"], ag_settles], ignore_index=True)

    config = config_for(
        tables,
        strategy="tests.support:TargetAgOnceStrategy",
        output_root=tmp_path,
    )
    config.data.bar_freq = "5m"
    config.data.underlyings = ["RB", "AG"]
    result = run_backtest(config, DictAdapter(tables))
    fills = _frame(result, "fills")
    ag_fill = fills[(fills["underlying"] == "AG") & (fills["filled_lots"] > 0)].iloc[0]
    assert datetime.fromisoformat(ag_fill["timestamp"]).time().minute == 7


def test_a_roll_waits_until_both_contracts_have_a_bar(tmp_path, tables):
    days = sorted(set(tables["bars"]["trading_day"]))
    roll_day = days[4]
    first = min(tables["bars"].loc[tables["bars"]["trading_day"] == roll_day, "datetime"])
    tables["bars"] = tables["bars"][
        ~(
            (tables["bars"]["symbol"] == "RB2405")
            & (tables["bars"]["trading_day"] == roll_day)
            & (tables["bars"]["datetime"] == first)
        )
    ]
    result = _run(tmp_path, tables)
    fills = _frame(result, "fills")
    roll_fills = fills[
        (fills["reason"].isin(["roll_out", "roll_in"]))
        & (pd.to_datetime(fills["trading_day"]).dt.date == roll_day)
    ]
    assert len(roll_fills) == 2
    assert {datetime.fromisoformat(value).time().minute for value in roll_fills["timestamp"]} == {5}


def test_a_roll_fails_closed_when_no_slot_has_both_contracts(tmp_path, tables):
    days = sorted(set(tables["bars"]["trading_day"]))
    roll_day = days[4]
    tables["bars"] = tables["bars"][
        ~((tables["bars"]["symbol"] == "RB2405") & (tables["bars"]["trading_day"] == roll_day))
    ]
    with pytest.raises(BacktestDataError, match="no time slot had tradable bars"):
        _run(tmp_path, tables)


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


def test_rolled_history_materializes_only_the_requested_tail(tables):
    dataset = make_dataset(tables, bar_freq="5m")
    days = sorted(set(tables["bars"]["trading_day"]))
    cutoff = dataset.timestamps_of_day(days[-1])[-1]

    whole = dataset.history("RB", cutoff, symbol="RB2405", freq="1d")
    tail = dataset.history("RB", cutoff, bars=2, symbol="RB2405", freq="1d")

    pd.testing.assert_frame_equal(tail, whole.tail(2).reset_index(drop=True))


def test_rolled_history_can_rewind_without_leaking_future_data(tables):
    dataset = make_dataset(tables, bar_freq="5m")
    days = sorted(set(tables["bars"]["trading_day"]))
    late = dataset.timestamps_of_day(days[-1])[-1]
    early = dataset.timestamps_of_day(days[1])[2]

    dataset.history("RB", late, bars=2, symbol="RB2405", freq="1d")
    rewound = dataset.history("RB", early, bars=2, symbol="RB2405", freq="1d")
    expected_raw = dataset.history("RB", early, symbol="RB2405")
    expected = expected_raw.groupby("trading_day", sort=False).agg(
        symbol=("symbol", "first"),
        underlying=("underlying", "first"),
        datetime=("datetime", "last"),
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        open_interest=("open_interest", "last"),
    )
    expected.insert(3, "trading_day", expected.index)
    expected = expected.reset_index(drop=True)

    pd.testing.assert_frame_equal(rewound, expected, check_dtype=False)


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
    assert first["net_pnl"] == pytest.approx(first["gross_pnl"] - first["commission"])
    fills = _frame(result, "fills")
    assert (
        trades["lots"].sum() == fills[fills["offset"].str.startswith("close")]["filled_lots"].sum()
    )
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
        fills["realized_pnl"].sum() + nav["settlement_variation"].sum() - fills["commission"].sum()
    )
    assert result.metrics["final_equity"] - result.metrics["initial_cash"] == pytest.approx(total)
