from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

import pandas as pd

from ..config import DataConfig
from ..types import BacktestDataError, DataAdapter


@dataclass(frozen=True)
class TableSpec:
    name: str
    required: bool
    required_columns: tuple[str, ...]
    optional_columns: tuple[str, ...]
    keys: tuple[str, ...]
    date_columns: tuple[str, ...] = ()
    datetime_columns: tuple[str, ...] = ()
    positive_columns: tuple[str, ...] = ()
    non_negative_columns: tuple[str, ...] = ()


TABLE_SPECS: dict[str, TableSpec] = {
    "bars": TableSpec(
        name="bars",
        required=True,
        required_columns=(
            "symbol", "underlying", "datetime", "trading_day",
            "open", "high", "low", "close", "volume",
        ),
        optional_columns=("open_interest", "upper_limit", "lower_limit"),
        keys=("symbol", "datetime"),
        date_columns=("trading_day",),
        datetime_columns=("datetime",),
        positive_columns=("open", "high", "low", "close"),
        non_negative_columns=("volume",),
    ),
    "contracts": TableSpec(
        name="contracts",
        required=True,
        required_columns=("symbol", "underlying", "multiplier", "tick_size"),
        optional_columns=("exchange", "expire_date"),
        keys=("symbol",),
        date_columns=("expire_date",),
        positive_columns=("multiplier", "tick_size"),
    ),
    "dominant_map": TableSpec(
        name="dominant_map",
        required=True,
        required_columns=("trading_day", "underlying", "dominant_symbol"),
        optional_columns=(),
        keys=("trading_day", "underlying"),
        date_columns=("trading_day",),
    ),
    "settles": TableSpec(
        name="settles",
        required=True,
        required_columns=("symbol", "trading_day", "settle_price"),
        optional_columns=(),
        keys=("symbol", "trading_day"),
        date_columns=("trading_day",),
        positive_columns=("settle_price",),
    ),
    "charges": TableSpec(
        name="charges",
        required=False,
        required_columns=(),
        optional_columns=(
            "underlying", "symbol", "trading_day",
            "open_fee_rate", "open_fee_per_lot",
            "close_fee_rate", "close_fee_per_lot",
            "close_today_fee_rate", "close_today_fee_per_lot",
        ),
        keys=(),
        date_columns=("trading_day",),
        non_negative_columns=(
            "open_fee_rate", "open_fee_per_lot",
            "close_fee_rate", "close_fee_per_lot",
            "close_today_fee_rate", "close_today_fee_per_lot",
        ),
    ),
    "margins": TableSpec(
        name="margins",
        required=False,
        required_columns=("long_margin_rate",),
        optional_columns=(
            "underlying", "symbol", "trading_day", "short_margin_rate",
        ),
        keys=(),
        date_columns=("trading_day",),
    ),
}

RATE_COLUMNS = ("long_margin_rate", "short_margin_rate")


def normalize_table(name: str, frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce a raw adapter table into the contract's dtypes and validate it."""
    spec = TABLE_SPECS[name]
    frame = frame.copy()
    missing = [column for column in spec.required_columns if column not in frame.columns]
    if missing:
        raise BacktestDataError(f"table {name} missing columns: {sorted(missing)}")

    for column in spec.date_columns:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce").dt.date
    for column in spec.datetime_columns:
        if column in frame.columns:
            frame[column] = pd.to_datetime(frame[column], errors="coerce")

    for column in ("symbol", "underlying", "dominant_symbol", "exchange"):
        if column in frame.columns:
            frame[column] = frame[column].astype("string").str.strip()
            if column in spec.required_columns and frame[column].isna().any():
                raise BacktestDataError(f"table {name} has empty {column} values")

    numeric = set(
        spec.positive_columns
        + spec.non_negative_columns
        + RATE_COLUMNS
        + ("open_interest", "upper_limit", "lower_limit", "multiplier", "tick_size")
    )
    for column in numeric & set(frame.columns):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    for column in spec.positive_columns:
        if column not in frame.columns:
            continue
        values = frame[column]
        if values.isna().any() or not ((values > 0) & values.notna()).all():
            raise BacktestDataError(f"table {name}.{column} must be finite and positive")
    for column in spec.non_negative_columns:
        if column not in frame.columns:
            continue
        values = frame[column].fillna(0.0)
        if (values < 0).any():
            raise BacktestDataError(f"table {name}.{column} must not be negative")
        frame[column] = values

    for column in spec.date_columns + spec.datetime_columns:
        if column in spec.required_columns and frame[column].isna().any():
            raise BacktestDataError(f"table {name}.{column} has unparsable values")

    if spec.keys:
        duplicated = frame.duplicated(subset=list(spec.keys))
        if duplicated.any():
            sample = frame.loc[duplicated, list(spec.keys)].head(3).to_dict("records")
            raise BacktestDataError(f"table {name} duplicate keys: {sample}")
        frame = frame.sort_values(list(spec.keys)).reset_index(drop=True)

    if name == "margins":
        if "long_margin_rate" not in frame.columns:
            raise BacktestDataError("table margins missing columns: ['long_margin_rate']")
        for column in RATE_COLUMNS:
            if column not in frame.columns:
                continue
            values = frame[column].dropna()
            if not values.empty and (((values <= 0) | (values > 1)).any()):
                raise BacktestDataError(f"margin rate must fall in (0, 1]: {column}")

    if name in ("charges", "margins") and not frame.empty:
        has_scope = ("underlying" in frame.columns) or ("symbol" in frame.columns)
        if not has_scope:
            raise BacktestDataError(
                f"table {name} needs an 'underlying' or 'symbol' column to scope rows"
            )

    return frame


def load_tables(adapter: DataAdapter) -> dict[str, pd.DataFrame]:
    """Load and normalize every contract table the adapter can provide."""
    tables: dict[str, pd.DataFrame] = {}
    for name, spec in TABLE_SPECS.items():
        try:
            frame = adapter.load_table(name)
        except BacktestDataError:
            if spec.required:
                raise
            frame = None
        if frame is None:
            if spec.required:
                raise BacktestDataError(f"missing required table {name}")
            continue
        tables[name] = normalize_table(name, frame)
    return tables


def cross_validate(tables: dict[str, pd.DataFrame]) -> None:
    bars = tables["bars"]
    contracts = tables["contracts"]
    dominant = tables["dominant_map"]

    known = dict(zip(contracts["symbol"], contracts["underlying"], strict=True))
    unknown = sorted(set(bars["symbol"]) - set(known))
    if unknown:
        raise BacktestDataError(f"bar symbol missing from contracts: {unknown[:5]}")
    mismatched = sorted(
        {
            symbol
            for symbol, underlying in zip(bars["symbol"], bars["underlying"], strict=True)
            if known[symbol] != underlying
        }
    )
    if mismatched:
        raise BacktestDataError(
            f"bars and contracts disagree on underlying: {mismatched[:5]}"
        )
    orphan = sorted(set(dominant["dominant_symbol"]) - set(known))
    if orphan:
        raise BacktestDataError(f"dominant symbol has no contract: {orphan[:5]}")


def create_adapter(config: DataConfig) -> DataAdapter:
    """Resolve ``data.adapter`` into an adapter instance.

    Built-in names are ``mock`` and ``ipquant_mysql``; anything containing ``:``
    is imported as ``module:factory`` and called with the ``DataConfig``.
    """
    name = config.adapter
    if ":" in name:
        module_name, _, attribute = name.partition(":")
        factory = getattr(importlib.import_module(module_name), attribute)
        return factory(config)
    if name == "mock":
        from .mock import MockAdapter

        return MockAdapter(config)
    if name == "ipquant_mysql":
        from .ipquant import IpquantMysqlAdapter

        return IpquantMysqlAdapter(config)
    raise BacktestDataError(
        f"unknown data.adapter {name!r}; use 'mock', 'ipquant_mysql' or 'module:factory'"
    )


def option(config: DataConfig, key: str, default: Any = None, *, required: bool = False) -> Any:
    if key in config.options:
        return config.options[key]
    if required:
        raise BacktestDataError(f"data.options.{key} is required by adapter {config.adapter}")
    return default
