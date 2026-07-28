"""In-memory fixtures so unit tests do not need files or a database."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from futures_backtest import (
    Account,
    BacktestConfig,
    DataConfig,
    MarketDataset,
    Matcher,
    Router,
)
from futures_backtest.types import AdapterMetadata

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_DATA = REPO_ROOT / "examples" / "sample_data"


class DictAdapter:
    """A ``DataAdapter`` backed by dataframes handed in by the test."""

    name = "dict"

    def __init__(self, tables: dict[str, pd.DataFrame], data_version: str = "test-v1"):
        self.tables = tables
        self.data_version = data_version

    def metadata(self) -> AdapterMetadata:
        return AdapterMetadata(
            adapter=self.name,
            data_version=self.data_version,
            fingerprint=self.data_version,
            as_of=None,
        )

    def load_table(self, name: str) -> pd.DataFrame | None:
        frame = self.tables.get(name)
        return None if frame is None else frame.copy()


def bar_rows(
    symbol: str,
    underlying: str,
    days: list[date],
    closes: list[float],
    *,
    volume: float = 100000,
    limits: tuple[float, float] | None = None,
    spread: float = 5.0,
) -> list[dict[str, Any]]:
    rows = []
    for day, close in zip(days, closes, strict=True):
        open_price = close - spread
        rows.append(
            {
                "symbol": symbol,
                "underlying": underlying,
                "datetime": datetime.combine(day, datetime.min.time()) + timedelta(hours=15),
                "trading_day": day,
                "open": open_price,
                "high": max(open_price, close) + 2,
                "low": min(open_price, close) - 2,
                "close": close,
                "volume": volume,
                "open_interest": 200000,
                "upper_limit": limits[0] if limits else None,
                "lower_limit": limits[1] if limits else None,
            }
        )
    return rows


def two_contract_tables(
    days: list[date],
    switch_day: date,
    near_closes: list[float] | None = None,
    far_closes: list[float] | None = None,
) -> dict[str, pd.DataFrame]:
    """One underlying, two contracts, and one dominant switch on ``switch_day``."""
    near_closes = near_closes or [3500 + 10 * index for index in range(len(days))]
    far_closes = far_closes or [3560 + 10 * index for index in range(len(days))]
    bars = bar_rows("RB2405", "RB", days, near_closes) + bar_rows(
        "RB2410", "RB", days, far_closes
    )
    contracts = pd.DataFrame(
        [
            {
                "symbol": "RB2405",
                "underlying": "RB",
                "multiplier": 10,
                "tick_size": 1,
                "exchange": "SHFE",
                "expire_date": days[-1] + timedelta(days=30),
            },
            {
                "symbol": "RB2410",
                "underlying": "RB",
                "multiplier": 10,
                "tick_size": 1,
                "exchange": "SHFE",
                "expire_date": days[-1] + timedelta(days=180),
            },
        ]
    )
    dominant = pd.DataFrame(
        [
            {
                "trading_day": day,
                "underlying": "RB",
                "dominant_symbol": "RB2410" if day >= switch_day else "RB2405",
            }
            for day in days
        ]
    )
    settles = pd.DataFrame(
        [
            {
                "symbol": row["symbol"],
                "trading_day": row["trading_day"],
                "settle_price": row["close"],
            }
            for row in bars
        ]
    )
    charges = pd.DataFrame(
        [
            {
                "underlying": "RB",
                "open_fee_rate": 0.0001,
                "close_fee_rate": 0.0001,
                "close_today_fee_rate": 0.001,
            }
        ]
    )
    margins = pd.DataFrame(
        [{"underlying": "RB", "long_margin_rate": 0.1, "short_margin_rate": 0.12}]
    )
    return {
        "bars": pd.DataFrame(bars),
        "contracts": contracts,
        "dominant_map": dominant,
        "settles": settles,
        "charges": charges,
        "margins": margins,
    }


def trading_days(count: int = 6, start: date = date(2024, 4, 1)) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


def make_dataset(tables: dict[str, pd.DataFrame], **overrides: Any) -> MarketDataset:
    days = sorted(set(tables["bars"]["trading_day"]))
    payload: dict[str, Any] = {
        "adapter": "dict",
        "underlyings": ["RB"],
        "start": days[0],
        "end": days[-1],
    }
    payload.update(overrides)
    return MarketDataset(DataConfig(**payload), DictAdapter(tables))


def make_parts(
    tables: dict[str, pd.DataFrame],
    initial_cash: float = 500000,
    **execution: Any,
) -> tuple[MarketDataset, Account, Router, Matcher]:
    dataset = make_dataset(tables)
    config = config_for(tables, initial_cash=initial_cash, execution=execution)
    account = Account(dataset, initial_cash, config.portfolio.margins_default)
    return dataset, account, Router(dataset, config.routing), Matcher(dataset, config.execution)


def config_for(
    tables: dict[str, pd.DataFrame],
    *,
    initial_cash: float = 500000,
    strategy: str = "tests.support:HoldTwoLots",
    parameters: dict[str, Any] | None = None,
    routing: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    output_root: Path | None = None,
) -> BacktestConfig:
    days = sorted(set(tables["bars"]["trading_day"]))
    return BacktestConfig.model_validate(
        {
            "data": {
                "adapter": "dict",
                "underlyings": ["RB"],
                "start": days[0],
                "end": days[-1],
            },
            "portfolio": {"initial_cash": initial_cash, "margins_default": 0.1},
            "routing": routing or {},
            "execution": execution or {},
            "strategy": {"path": strategy, "parameters": parameters or {}},
            "output": {"root": str(output_root or (REPO_ROOT / "backtests" / "tests"))},
        }
    )


class HoldTwoLots:
    """Underlying-level strategy used by the golden run; it never names a contract."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})
        self.seen_symbols: list[str] = []

    def on_bar(self, context):  # noqa: ANN001 - context type lives in the package
        from futures_backtest import TargetPosition

        self.seen_symbols.append(context.trading_symbol("RB"))
        return TargetPosition(underlying="RB", net_lots=int(self.parameters.get("lots", 2)))


class FlipStrategy:
    """Long on even bars, short on odd ones, to exercise reversals."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})

    def on_bar(self, context):  # noqa: ANN001
        from futures_backtest import TargetPosition

        lots = int(self.parameters.get("lots", 1))
        return TargetPosition(
            underlying="RB", net_lots=lots if context.bars_seen % 2 else -lots
        )


class StrayStrategy:
    """Targets an underlying that is not configured; the framework must refuse."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})

    def on_bar(self, context):  # noqa: ANN001
        from futures_backtest import TargetPosition

        return TargetPosition(underlying="CU", net_lots=1)


class PeekingStrategy:
    """Tries to read data past the current bar; the context must refuse."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})
        self.rows_seen: list[int] = []

    def on_bar(self, context):  # noqa: ANN001
        history = context.history("RB")
        self.rows_seen.append(int(len(history)))
        assert history["datetime"].max() <= pd.Timestamp(context.timestamp)
        return None
