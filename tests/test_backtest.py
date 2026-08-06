"""End-to-end checks for the acceptance criteria in docs/features.md section 9."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pandas as pd
import pytest

from futures_backtest import run_backtest, validate_config
from futures_backtest.types import BacktestDataError
from tests import support
from tests.support import (
    DictAdapter,
    config_for,
    trading_days,
    two_contract_tables,
)


def _run(tmp_path: Path, tables, **kwargs):
    config = config_for(tables, output_root=tmp_path, **kwargs)
    return run_backtest(config, DictAdapter(tables))


def _frame(result, name: str) -> pd.DataFrame:
    return pd.read_csv(result.run_path / f"{name}.csv")


@pytest.fixture()
def tables():
    days = trading_days(8)
    return two_contract_tables(days, days[4])


def test_a_strategy_never_names_a_month_contract(tmp_path, tables):
    """Criterion 1: the underlying-only strategy completes a continuous-series backtest."""
    source = inspect.getsource(support.HoldTwoLots)
    assert "RB2405" not in source and "RB2410" not in source

    result = _run(tmp_path, tables)
    assert result.metrics["status"] == "ok"
    assert result.metrics["trading_days"] == 8


def test_fills_and_positions_land_on_specific_contracts(tmp_path, tables):
    """Criterion 2, part one: exposure is always on a month contract."""
    result = _run(tmp_path, tables)
    fills = _frame(result, "fills")
    assert set(fills["symbol"]) <= {"RB2405", "RB2410"}
    assert fills["symbol"].str.match(r"RB\d{4}").all()


def test_the_roll_has_its_own_log_and_a_non_zero_cost(tmp_path, tables):
    """Criterion 2, part two: rolling is logged separately and it costs money."""
    result = _run(tmp_path, tables)
    rolls = _frame(result, "rolls")
    assert len(rolls) == 1
    entry = rolls.iloc[0]
    assert entry["from_symbol"] == "RB2405"
    assert entry["to_symbol"] == "RB2410"
    assert entry["net_lots"] == 2
    assert entry["commission"] > 0
    assert entry["slippage_cost"] > 0
    assert result.metrics["roll_cost"] == pytest.approx(
        entry["commission"] + entry["slippage_cost"]
    )

    fills = _frame(result, "fills")
    roll_fills = fills[fills["reason"].isin(["roll_out", "roll_in"])]
    assert list(roll_fills["symbol"]) == ["RB2405", "RB2410"]
    assert list(roll_fills["offset"]) == ["close", "open"]


def test_the_roll_happens_a_day_after_the_dominant_switch(tmp_path, tables):
    """Criterion 3: T-1 routing, so the switch day itself still trades the old contract."""
    days = sorted(set(tables["bars"]["trading_day"]))
    switch_day = days[4]
    result = _run(tmp_path, tables)

    rolls = _frame(result, "rolls")
    assert pd.to_datetime(rolls.iloc[0]["trading_day"]).date() == days[5]

    fills = _frame(result, "fills")
    fills["trading_day"] = pd.to_datetime(fills["trading_day"]).dt.date
    on_switch_day = fills[fills["trading_day"] == switch_day]
    assert set(on_switch_day["symbol"]) <= {"RB2405"}


def test_the_strategy_sees_the_routed_contract_and_only_that(tmp_path, tables):
    config = config_for(tables, output_root=tmp_path)
    from futures_backtest import Scheduler, build_dataset
    from futures_backtest.strategy import load_strategy

    dataset = build_dataset(config.data, DictAdapter(tables))
    strategy = load_strategy(config.strategy.path, config.strategy.parameters)
    Scheduler(config, dataset, strategy).run()

    days = sorted(set(tables["bars"]["trading_day"]))
    expected = ["RB2405"] * 5 + ["RB2410"] * 3
    assert strategy.seen_symbols == expected
    assert len(strategy.seen_symbols) == len(days)


def test_lookahead_mode_rolls_a_day_earlier_and_is_flagged(tmp_path, tables):
    """Criterion 3, part two: the relaxed mode is possible but marked."""
    days = sorted(set(tables["bars"]["trading_day"]))
    strict = _run(tmp_path / "strict", tables)
    relaxed = _run(
        tmp_path / "relaxed",
        tables,
        routing={"dominant_lag": 0, "lookahead_dominant": True},
    )

    strict_roll = pd.to_datetime(_frame(strict, "rolls").iloc[0]["trading_day"]).date()
    relaxed_roll = pd.to_datetime(_frame(relaxed, "rolls").iloc[0]["trading_day"]).date()
    assert strict_roll == days[5]
    assert relaxed_roll == days[4]

    metadata = json.loads((relaxed.run_path / "metadata.json").read_text())
    assert metadata["lookahead_dominant"] is True
    assert "look-ahead" in metadata["warning"]
    strict_metadata = json.loads((strict.run_path / "metadata.json").read_text())
    assert strict_metadata["lookahead_dominant"] is False


def test_replacing_the_adapter_does_not_touch_the_strategy(tmp_path, tables):
    """Criterion 4: same strategy, same numbers, a different data source."""
    from_dict = _run(tmp_path / "dict", tables)

    root = tmp_path / "files"
    root.mkdir()
    for name, frame in tables.items():
        frame.to_csv(root / f"{name}.csv", index=False)
    config = config_for(tables, output_root=tmp_path / "csv")
    config.data.adapter = "mock"
    config.data.options = {"root": str(root)}
    from_files = run_backtest(config)

    assert from_files.metrics["final_equity"] == pytest.approx(from_dict.metrics["final_equity"])
    assert from_files.metrics["rolls"] == from_dict.metrics["rolls"]


def test_the_same_configuration_reproduces_the_same_result(tmp_path, tables):
    """Criterion 5: reproducibility."""
    first = _run(tmp_path / "a", tables)
    second = _run(tmp_path / "b", tables)

    assert first.data_version == second.data_version
    assert dict(first.metrics) == dict(second.metrics)
    for name in ("fills", "orders", "rolls", "nav", "events"):
        pd.testing.assert_frame_equal(_frame(first, name), _frame(second, name))


def test_settlement_variation_explains_the_equity_curve(tmp_path, tables):
    result = _run(tmp_path, tables)
    nav = _frame(result, "nav")
    fills = _frame(result, "fills")

    expected = (
        result.metrics["initial_cash"]
        + nav["settlement_variation"].sum()
        + fills["realized_pnl"].sum()
        - fills["commission"].sum()
    )
    assert nav["equity"].iloc[-1] == pytest.approx(expected)
    assert nav["margin"].gt(0).any()
    assert nav["available"].ge(0).all()


def test_targets_can_be_skipped_on_a_roll_day(tmp_path, tables):
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:FlipStrategy",
        routing={"allow_signals_on_roll_day": False},
    )
    skipped = _frame(result, "skipped_targets")
    assert len(skipped) == 1
    assert skipped.iloc[0]["reason"] == "roll_day"
    assert result.metrics["skipped_targets"] == 1


def test_a_limit_the_next_bar_never_reaches_loses_the_signal(tmp_path, tables):
    """The fixture trends up, so a bid under yesterday's close is never hit."""
    result = _run(
        tmp_path,
        tables,
        strategy="tests.support:LimitStrategy",
        parameters={"offset_ticks": 2},
    )
    fills = _frame(result, "fills")
    assert (fills["reject_reason"] == "limit_not_reached").all()
    assert (fills["filled_lots"] == 0).all()

    skipped = _frame(result, "skipped_targets")
    assert set(skipped["reason"]) == {"limit_not_reached"}
    assert len(skipped) == len(fills)
    assert result.metrics["final_equity"] == pytest.approx(result.metrics["initial_cash"])


def test_a_marketable_limit_fills_at_the_open_and_beats_the_market_order(tmp_path, tables):
    limited = _run(
        tmp_path / "limit",
        tables,
        strategy="tests.support:LimitStrategy",
        parameters={"offset_ticks": -10},
    )
    fills = _frame(limited, "fills")
    signals = fills[fills["reason"] == "signal"]
    assert not signals.empty
    assert (signals["status"] == "filled").all()
    assert (signals["slippage_ticks"] == 0).all()

    market = _run(tmp_path / "market", tables)
    market_signals = _frame(market, "fills")
    market_signals = market_signals[market_signals["reason"] == "signal"]
    # Same bar, same lots: the limit saves exactly the one tick of slippage.
    assert signals.iloc[0]["price"] == pytest.approx(market_signals.iloc[0]["price"] - 1)


def test_a_limit_price_cannot_be_filled_at_the_same_close(tmp_path, tables):
    with pytest.raises(BacktestDataError, match="look-ahead"):
        _run(
            tmp_path,
            tables,
            strategy="tests.support:LimitStrategy",
            execution={"market_fill": "same_close"},
        )


def test_history_is_cut_off_at_the_current_bar(tmp_path, tables):
    config = config_for(tables, strategy="tests.support:PeekingStrategy", output_root=tmp_path)
    from futures_backtest import Scheduler, build_dataset
    from futures_backtest.strategy import load_strategy

    dataset = build_dataset(config.data, DictAdapter(tables))
    strategy = load_strategy(config.strategy.path, config.strategy.parameters)
    Scheduler(config, dataset, strategy).run()
    # Two contracts per day, so the visible history grows by two rows per bar.
    assert strategy.rows_seen == [2 * (index + 1) for index in range(8)]


def test_a_target_on_an_unconfigured_underlying_is_rejected(tmp_path, tables):
    with pytest.raises(BacktestDataError, match="not in data.underlyings"):
        _run(tmp_path, tables, strategy="tests.support:StrayStrategy")


def test_validate_reports_the_roll_schedule_without_trading(tables):
    days = sorted(set(tables["bars"]["trading_day"]))
    report = validate_config(config_for(tables), DictAdapter(tables))
    assert report["status"] == "ok"
    assert report["dominant_lag"] == 1
    assert report["rolls"] == [
        {
            "trading_day": days[5],
            "underlying": "RB",
            "roll_to": "RB2410",
            "roll_from": "RB2405",
        }
    ]


def test_intraday_frequency_is_refused_rather_than_sampled(tmp_path, tables):
    config = config_for(tables, output_root=tmp_path)
    config.data.bar_freq = "1m"
    with pytest.raises(BacktestDataError, match="not supported yet"):
        run_backtest(config, DictAdapter(tables))
    with pytest.raises(BacktestDataError, match="Phase 2"):
        validate_config(config, DictAdapter(tables))


def test_outputs_include_every_artefact(tmp_path, tables):
    result = _run(tmp_path, tables)
    for name in (
        "orders.csv",
        "fills.csv",
        "rolls.csv",
        "events.csv",
        "nav.csv",
        "skipped_targets.csv",
        "metrics.json",
        "metadata.json",
        "config.json",
    ):
        assert (result.run_path / name).exists(), name
    events = _frame(result, "events")
    assert set(events["kind"]) == {"BAR", "ROLL", "SETTLE"}
