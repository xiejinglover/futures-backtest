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
    bars = bar_rows("RB2405", "RB", days, near_closes) + bar_rows("RB2410", "RB", days, far_closes)
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


def intraday_tables(
    days: list[date],
    switch_day: date,
    *,
    shape: tuple[float, ...] = (0, 6, 12, 4, -4),
    step_minutes: int = 5,
    volume: float = 100000,
) -> dict[str, pd.DataFrame]:
    """The two-contract fixture cut into several bars per trading day.

    ``shape`` is each bar's close as an offset from the day's opening price, so a
    test can steer the path within the day: the default rises and then falls back
    through where it started, which is what a resting bid needs to be filled.
    """
    tables = two_contract_tables(days, switch_day)
    rows = []
    for symbol, base in (("RB2405", 3500), ("RB2410", 3560)):
        for index, day in enumerate(days):
            opening = base + 10 * index
            previous = 0.0
            start = datetime.combine(day, datetime.min.time()) + timedelta(hours=9)
            for slot, offset in enumerate(shape):
                open_price = opening + previous
                close = opening + offset
                rows.append(
                    {
                        "symbol": symbol,
                        "underlying": "RB",
                        "datetime": start + timedelta(minutes=step_minutes * slot),
                        "trading_day": day,
                        "open": open_price,
                        "high": max(open_price, close) + 1,
                        "low": min(open_price, close) - 1,
                        "close": close,
                        "volume": volume,
                        "open_interest": 200000,
                        "upper_limit": None,
                        "lower_limit": None,
                    }
                )
                previous = offset
    bars = pd.DataFrame(rows)
    tables["bars"] = bars
    tables["settles"] = pd.DataFrame(
        [
            {
                "symbol": str(symbol),
                "trading_day": day,
                "settle_price": float(group["close"].iloc[-1]),
            }
            for (symbol, day), group in bars.groupby(["symbol", "trading_day"], sort=True)
        ]
    )
    return tables


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
        return TargetPosition(underlying="RB", net_lots=lots if context.bars_seen % 2 else -lots)


class LimitStrategy:
    """Holds a fixed net position, but asks for it with a day limit order.

    ``offset_ticks`` is subtracted from the last close, so a positive value asks
    for a better price than the market last traded at and a negative one is
    already marketable when the next bar opens.
    """

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})

    def on_bar(self, context):  # noqa: ANN001
        from futures_backtest import TargetPosition

        bar = context.bar("RB")
        if bar is None:
            return None
        offset = float(self.parameters.get("offset_ticks", 2))
        return TargetPosition(
            underlying="RB",
            net_lots=int(self.parameters.get("lots", 2)),
            limit_price=bar.close - offset * context.tick_size("RB"),
        )


class FixedLimitStrategy:
    """Asks for the same lots at the same absolute price on every single bar.

    Re-emitting an unchanged target is what a position-target strategy does, so
    this is the case where the order has to stay working rather than be re-placed.
    """

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})

    def on_bar(self, context):  # noqa: ANN001
        from futures_backtest import TargetPosition

        return TargetPosition(
            underlying="RB",
            net_lots=int(self.parameters.get("lots", 2)),
            limit_price=float(self.parameters["limit_price"]),
        )


class FixedStopStrategy:
    """Keeps one fixed conditional target working for lifecycle tests."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})

    def on_bar(self, context):  # noqa: ANN001
        from futures_backtest import TargetPosition

        return TargetPosition(
            underlying="RB",
            net_lots=int(self.parameters.get("lots", 1)),
            limit_price=self.parameters.get("limit_price"),
            stop_price=float(self.parameters["stop_price"]),
            time_in_force=str(self.parameters.get("time_in_force", "DAY")),
        )


class IntradayTurnStrategy:
    """Opens at one slot of the trading day and flattens at another, every day."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})

    def on_bar(self, context):  # noqa: ANN001
        from futures_backtest import TargetPosition

        slots = int(self.parameters.get("bars_per_day", 5))
        slot = (context.bars_seen - 1) % slots
        if slot == int(self.parameters.get("open_slot", 1)):
            return TargetPosition(underlying="RB", net_lots=int(self.parameters.get("lots", 2)))
        if slot == int(self.parameters.get("close_slot", 3)):
            return TargetPosition(underlying="RB", net_lots=0)
        return None


class StrayStrategy:
    """Targets an underlying that is not configured; the framework must refuse."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})

    def on_bar(self, context):  # noqa: ANN001
        from futures_backtest import TargetPosition

        return TargetPosition(underlying="CU", net_lots=1)


class TargetAgOnceStrategy:
    """Submit an AG target from a global slot where only RB has a bar."""

    def __init__(self, parameters: dict[str, Any] | None = None):
        self.parameters = dict(parameters or {})
        self.sent = False

    def on_bar(self, context):  # noqa: ANN001
        from futures_backtest import TargetPosition

        if self.sent:
            return None
        self.sent = True
        return TargetPosition(underlying="AG", net_lots=1)


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
