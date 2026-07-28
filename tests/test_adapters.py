from __future__ import annotations

from datetime import date

import pytest

from futures_backtest import DataConfig, IpquantMysqlAdapter, MockAdapter, create_adapter
from futures_backtest.adapter.ipquant import QUERIES
from futures_backtest.types import BacktestDataError
from tests.support import SAMPLE_DATA, trading_days, two_contract_tables


def _mock_config(root=SAMPLE_DATA, **overrides):
    payload = {
        "adapter": "mock",
        "options": {"root": str(root)},
        "underlyings": ["RB"],
        "start": date(2024, 4, 1),
        "end": date(2024, 4, 12),
    }
    payload.update(overrides)
    return DataConfig(**payload)


def test_mock_adapter_fingerprint_tracks_file_content(tmp_path):
    tables = two_contract_tables(trading_days(4), trading_days(4)[2])
    for name, frame in tables.items():
        frame.to_csv(tmp_path / f"{name}.csv", index=False)
    config = _mock_config(tmp_path, start=trading_days(4)[0], end=trading_days(4)[-1])

    first = MockAdapter(config).metadata()
    assert first.data_version.startswith("mock-")

    edited = tables["bars"].copy()
    edited.loc[0, "close"] = edited.loc[0, "close"] + 1
    edited.to_csv(tmp_path / "bars.csv", index=False)
    assert MockAdapter(config).metadata().fingerprint != first.fingerprint


def test_an_explicit_data_version_wins_over_the_fingerprint():
    metadata = MockAdapter(_mock_config(data_version="rb-2024-04")).metadata()
    assert metadata.data_version == "rb-2024-04"
    assert metadata.fingerprint  # still recorded for auditing


def test_a_table_in_two_formats_is_a_configuration_error(tmp_path):
    tables = two_contract_tables(trading_days(4), trading_days(4)[2])
    for name, frame in tables.items():
        frame.to_csv(tmp_path / f"{name}.csv", index=False)
    tables["bars"].to_parquet(tmp_path / "bars.parquet", index=False)
    config = _mock_config(tmp_path, start=trading_days(4)[0], end=trading_days(4)[-1])
    with pytest.raises(BacktestDataError, match="both CSV and Parquet"):
        MockAdapter(config).load_table("bars")


def test_parquet_and_csv_produce_the_same_tables(tmp_path):
    tables = two_contract_tables(trading_days(4), trading_days(4)[2])
    csv_root = tmp_path / "csv"
    parquet_root = tmp_path / "parquet"
    csv_root.mkdir()
    parquet_root.mkdir()
    for name, frame in tables.items():
        frame.to_csv(csv_root / f"{name}.csv", index=False)
        frame.to_parquet(parquet_root / f"{name}.parquet", index=False)

    days = trading_days(4)
    from_csv = MockAdapter(_mock_config(csv_root, start=days[0], end=days[-1])).load_table("bars")
    from_parquet = MockAdapter(
        _mock_config(parquet_root, start=days[0], end=days[-1])
    ).load_table("bars")
    assert len(from_csv) == len(from_parquet)
    assert list(from_csv["symbol"]) == list(from_parquet["symbol"])


def test_a_bad_root_is_reported_early():
    with pytest.raises(BacktestDataError, match="not a directory"):
        MockAdapter(_mock_config(SAMPLE_DATA / "nope"))


def test_unknown_adapter_names_are_rejected():
    with pytest.raises(BacktestDataError, match="unknown data.adapter"):
        create_adapter(_mock_config(adapter="postgres"))


def test_a_module_factory_adapter_can_be_configured():
    config = _mock_config(adapter="tests.test_adapters:factory")
    assert isinstance(create_adapter(config), MockAdapter)


def factory(config: DataConfig) -> MockAdapter:
    return MockAdapter(config)


def test_ipquant_adapter_maps_the_documented_tables():
    config = DataConfig(
        adapter="ipquant_mysql",
        options={"dsn": "mysql+pymysql://user:pw@host/ipquant"},
        underlyings=["RB"],
        start=date(2024, 4, 1),
        end=date(2024, 4, 12),
    )
    adapter = IpquantMysqlAdapter(config)

    assert "future_daily_market_data" in adapter._query("bars")
    assert "future_1m_market_data_all" in IpquantMysqlAdapter(
        DataConfig(**{**config.model_dump(), "bar_freq": "1m"})
    )._query("bars")
    assert "future_dominant_contracts" in QUERIES["dominant_map"]
    assert "future_contract_info" in QUERIES["contracts"]
    assert "future_charge" in QUERIES["charges"]
    assert "future_margin_rate" in QUERIES["margins"]
    assert "future_set_price" in QUERIES["settles"]

    metadata = adapter.metadata()
    assert metadata.adapter == "ipquant_mysql"
    # The credentials must not leak into the reproducibility fingerprint.
    assert "user" not in metadata.fingerprint and "pw" not in metadata.fingerprint


def test_ipquant_adapter_requires_a_dsn():
    with pytest.raises(BacktestDataError, match="data.options.dsn is required"):
        IpquantMysqlAdapter(
            DataConfig(
                adapter="ipquant_mysql",
                underlyings=["RB"],
                start=date(2024, 4, 1),
                end=date(2024, 4, 12),
            )
        )


def test_ipquant_adapter_returns_contract_tables_against_a_live_database():
    """Runs only where a real ipquant database is configured."""
    import os

    dsn = os.environ.get("IPQUANT_DSN")
    if not dsn:
        pytest.skip("set IPQUANT_DSN to exercise the ipquant adapter end to end")
    pytest.importorskip("sqlalchemy")

    config = DataConfig(
        adapter="ipquant_mysql",
        options={"dsn": dsn},
        underlyings=[os.environ.get("IPQUANT_UNDERLYING", "RB")],
        start=date.fromisoformat(os.environ.get("IPQUANT_START", "2024-04-01")),
        end=date.fromisoformat(os.environ.get("IPQUANT_END", "2024-04-30")),
    )
    from futures_backtest import MarketDataset

    dataset = MarketDataset(config, IpquantMysqlAdapter(config))
    assert dataset.describe()["bars"] > 0


def test_the_optional_mysql_extra_is_reported_when_absent(monkeypatch):
    config = DataConfig(
        adapter="ipquant_mysql",
        options={"dsn": "mysql+pymysql://user:pw@host/ipquant"},
        underlyings=["RB"],
        start=date(2024, 4, 1),
        end=date(2024, 4, 12),
    )
    adapter = IpquantMysqlAdapter(config)

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def fail_on_sqlalchemy(name, *args, **kwargs):
        if name == "sqlalchemy":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fail_on_sqlalchemy)
    with pytest.raises(BacktestDataError, match=r"futures-backtest\[mysql\]"):
        adapter._connect()
