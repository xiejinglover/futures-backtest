from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from futures_backtest import DataConfig, MockAdapter
from futures_backtest.adapter import normalize_table
from futures_backtest.types import BacktestDataError
from tests.support import (
    SAMPLE_DATA,
    DictAdapter,
    make_dataset,
    trading_days,
    two_contract_tables,
)


def test_sample_data_loads_through_the_mock_adapter():
    config = DataConfig(
        adapter="mock",
        options={"root": str(SAMPLE_DATA)},
        underlyings=["RB"],
        start=date(2024, 4, 1),
        end=date(2024, 4, 12),
    )
    from futures_backtest import MarketDataset

    dataset = MarketDataset(config, MockAdapter(config))
    described = dataset.describe()
    assert described["symbols"] == ["RB2405", "RB2410"]
    assert described["trading_days"] == 10
    assert described["data_version"].startswith("mock-")


def test_missing_column_is_reported_by_name():
    frame = pd.DataFrame([{"symbol": "RB2405", "underlying": "RB", "multiplier": 10}])
    with pytest.raises(BacktestDataError, match=r"missing columns: \['tick_size'\]"):
        normalize_table("contracts", frame)


def test_duplicate_keys_are_rejected():
    tables = two_contract_tables(trading_days(4), trading_days(4)[2])
    tables["bars"] = pd.concat([tables["bars"], tables["bars"].head(1)])
    with pytest.raises(BacktestDataError, match="duplicate keys"):
        make_dataset(tables)


def test_non_positive_price_is_rejected():
    tables = two_contract_tables(trading_days(4), trading_days(4)[2])
    tables["bars"].loc[0, "close"] = 0
    with pytest.raises(BacktestDataError, match="must be finite and positive"):
        make_dataset(tables)


def test_dominant_symbol_without_a_contract_is_rejected():
    tables = two_contract_tables(trading_days(4), trading_days(4)[2])
    tables["dominant_map"].loc[0, "dominant_symbol"] = "RB2501"
    with pytest.raises(BacktestDataError, match="dominant symbol has no contract"):
        make_dataset(tables)


def test_bar_symbol_absent_from_contracts_is_rejected():
    tables = two_contract_tables(trading_days(4), trading_days(4)[2])
    tables["contracts"] = tables["contracts"].head(1)
    with pytest.raises(BacktestDataError, match="bar symbol missing from contracts"):
        make_dataset(tables)


def test_margin_rate_outside_the_unit_interval_is_rejected():
    tables = two_contract_tables(trading_days(4), trading_days(4)[2])
    tables["margins"].loc[0, "long_margin_rate"] = 1.5
    with pytest.raises(BacktestDataError, match=r"margin rate must fall in \(0, 1\]"):
        make_dataset(tables)


def test_charges_prefer_the_symbol_scope_and_the_latest_effective_row():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    tables["charges"] = pd.DataFrame(
        [
            {"underlying": "RB", "open_fee_rate": 0.0001},
            {"symbol": "RB2405", "open_fee_rate": 0.0002},
            {"symbol": "RB2405", "trading_day": days[2], "open_fee_rate": 0.0009},
        ]
    )
    dataset = make_dataset(tables)
    assert dataset.charge("RB2405", days[0])["open_fee_rate"] == 0.0002
    assert dataset.charge("RB2405", days[2])["open_fee_rate"] == 0.0009
    assert dataset.charge("RB2410", days[2])["open_fee_rate"] == 0.0001


def test_short_margin_rate_falls_back_to_the_long_rate():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    tables["margins"] = pd.DataFrame([{"underlying": "RB", "long_margin_rate": 0.08}])
    dataset = make_dataset(tables)
    assert dataset.margin_rate("RB2405", days[0], "short", 0.1) == 0.08


def test_history_never_returns_bars_past_the_cutoff():
    days = trading_days(5)
    dataset = make_dataset(two_contract_tables(days, days[3]))
    cutoff = dataset.timestamps[2]
    history = dataset.history("RB", cutoff.to_pydatetime())
    assert history["datetime"].max() == cutoff
    assert set(history["symbol"]) == {"RB2405", "RB2410"}


def test_settle_price_falls_back_to_the_close_and_counts_it():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    tables["settles"] = tables["settles"][tables["settles"]["trading_day"] != days[1]]
    dataset = make_dataset(tables)
    bar = dataset.last_bar_of_day("RB2405", days[1])
    assert dataset.settle_price("RB2405", days[1]) == bar.close
    assert dataset.settle_fallbacks == 1


def test_optional_tables_may_be_absent():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    del tables["charges"]
    del tables["margins"]
    dataset = make_dataset(tables)
    assert dataset.charge("RB2405", days[0]) == {}
    assert dataset.margin_rate("RB2405", days[0], "long", 0.11) == 0.11


def test_required_table_absence_is_reported():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    del tables["settles"]
    with pytest.raises(BacktestDataError, match="missing required table settles"):
        make_dataset(tables)


def test_intraday_bars_labelled_as_daily_are_rejected():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    extra = tables["bars"].head(2).copy()
    extra["datetime"] = extra["datetime"] - pd.Timedelta(hours=4)
    tables["bars"] = pd.concat([tables["bars"], extra], ignore_index=True)
    with pytest.raises(BacktestDataError, match="several bars in one"):
        make_dataset(tables)


def test_adapter_without_bars_in_range_is_reported():
    days = trading_days(4)
    tables = two_contract_tables(days, days[2])
    config = DataConfig(
        adapter="dict",
        underlyings=["RB"],
        start=date(2030, 1, 1),
        end=date(2030, 1, 31),
    )
    from futures_backtest import MarketDataset

    with pytest.raises(BacktestDataError, match="no bars for the requested underlyings"):
        MarketDataset(config, DictAdapter(tables))
