from __future__ import annotations

import bisect
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from .adapter import cross_validate, load_tables
from .config import DataConfig
from .types import (
    AdapterMetadata,
    BacktestDataError,
    Bar,
    ContractInfo,
    DataAdapter,
)

_PERIODS = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "60min", "1d": ""}

_ROLLUP_COLUMNS = (
    "symbol",
    "underlying",
    "datetime",
    "trading_day",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
)


def _period_keys(frame: pd.DataFrame, freq: str) -> Any:
    """Which coarse period each row belongs to.

    A day is the exchange's trading day rather than the calendar day, so a night
    session folds into the session it belongs to instead of the evening it fell on.
    """
    if freq == "1d":
        return frame["trading_day"].to_numpy()
    return frame["datetime"].dt.floor(_PERIODS[freq]).to_numpy()


class _Rollup:
    """Streaming OHLCV aggregation over a slice, folded once and reused."""

    def __init__(self, keys: Any):
        self.keys = keys
        self.end = 0
        self.order: list[tuple[str, Any]] = []
        self.records: dict[tuple[str, Any], dict[str, Any]] = {}

    def upto(self, frame: pd.DataFrame, end: int, bars: int | None = None) -> pd.DataFrame:
        if end <= 0 or (bars is not None and bars <= 0):
            return pd.DataFrame(columns=list(_ROLLUP_COLUMNS))
        if end < self.end:
            self.end, self.order, self.records = 0, [], {}
        for index in range(self.end, end):
            _fold(self.records, self.order, self.keys[index], frame.iloc[index])
        self.end = end

        keys = self.order[-bars:] if bars is not None else self.order
        return pd.DataFrame([self.records[key] for key in keys], columns=list(_ROLLUP_COLUMNS))


def _fold(
    into: dict[tuple[str, Any], dict[str, Any]],
    order: list[tuple[str, Any]],
    period: Any,
    row: Any,
) -> None:
    key = (str(row.symbol), period)
    slot = into.get(key)
    if slot is None:
        into[key] = {
            "symbol": str(row.symbol),
            "underlying": str(row.underlying),
            "datetime": row.datetime,
            "trading_day": row.trading_day,
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
            "volume": float(row.volume),
            "open_interest": row.open_interest,
        }
        order.append(key)
        return
    slot["datetime"] = row.datetime
    slot["trading_day"] = row.trading_day
    slot["high"] = max(slot["high"], float(row.high))
    slot["low"] = min(slot["low"], float(row.low))
    slot["close"] = float(row.close)
    slot["volume"] += float(row.volume)
    slot["open_interest"] = row.open_interest


def _fee_lookup(
    frame: pd.DataFrame | None,
) -> dict[str, list[tuple[date | None, dict[str, float]]]]:
    """Index charge/margin rows by scope key, keeping their effective-date order."""
    lookup: dict[str, list[tuple[date | None, dict[str, float]]]] = {}
    if frame is None or frame.empty:
        return lookup
    value_columns = [
        column
        for column in frame.columns
        if column not in ("underlying", "symbol", "trading_day")
    ]
    for row in frame.itertuples(index=False):
        record = {
            column: float(getattr(row, column))
            for column in value_columns
            if pd.notna(getattr(row, column, None))
        }
        effective = getattr(row, "trading_day", None)
        if effective is not None and pd.isna(effective):
            effective = None
        for scope in ("symbol", "underlying"):
            key = getattr(row, scope, None)
            if key is None or pd.isna(key):
                continue
            lookup.setdefault(f"{scope}:{key}", []).append((effective, record))
    for entries in lookup.values():
        entries.sort(key=lambda item: (item[0] is not None, item[0] or date.min))
    return lookup


def _resolve(
    lookup: dict[str, list[tuple[date | None, dict[str, float]]]],
    symbol: str,
    underlying: str,
    day: date,
) -> dict[str, float]:
    for key in (f"symbol:{symbol}", f"underlying:{underlying}"):
        entries = lookup.get(key)
        if not entries:
            continue
        chosen: dict[str, float] | None = None
        for effective, record in entries:
            if effective is None or effective <= day:
                chosen = record
        if chosen is not None:
            return chosen
    return {}


class MarketDataset:
    """Normalized, indexed market data plus the no-look-ahead accessors on top.

    Every read is keyed by a trading day or timestamp the caller already reached;
    ``history`` refuses to return rows past the cursor the scheduler set.
    """

    def __init__(self, config: DataConfig, adapter: DataAdapter):
        self.config = config
        self.adapter = adapter
        self.metadata: AdapterMetadata = adapter.metadata()
        tables = load_tables(adapter)
        cross_validate(tables)

        self.contracts: dict[str, ContractInfo] = {
            str(row.symbol): ContractInfo(
                symbol=str(row.symbol),
                underlying=str(row.underlying),
                multiplier=float(row.multiplier),
                tick_size=float(row.tick_size),
                exchange=str(row.exchange) if pd.notna(getattr(row, "exchange", None)) else None,
                expire_date=getattr(row, "expire_date", None)
                if pd.notna(getattr(row, "expire_date", None))
                else None,
            )
            for row in tables["contracts"].itertuples(index=False)
        }

        wanted = set(config.underlyings)
        bars = tables["bars"]
        bars = bars[bars["underlying"].isin(wanted)]
        bars = bars[
            (bars["trading_day"] >= config.start) & (bars["trading_day"] <= config.end)
        ]
        if bars.empty:
            raise BacktestDataError(
                "no bars for the requested underlyings inside "
                f"{config.start}..{config.end}"
            )
        self.bars = bars.sort_values(["datetime", "symbol"]).reset_index(drop=True)

        self._bar_by_key: dict[tuple[str, pd.Timestamp], Bar] = {}
        self._bars_by_symbol_day: dict[tuple[str, date], list[Bar]] = {}
        self._bars_by_timestamp: dict[pd.Timestamp, dict[str, Bar]] = {}
        self._timestamps_by_day: dict[date, list[pd.Timestamp]] = {}
        for row in self.bars.itertuples(index=False):
            bar = Bar(
                symbol=str(row.symbol),
                underlying=str(row.underlying),
                timestamp=row.datetime.to_pydatetime(),
                trading_day=row.trading_day,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
                open_interest=float(row.open_interest)
                if pd.notna(getattr(row, "open_interest", None))
                else None,
                upper_limit=float(row.upper_limit)
                if pd.notna(getattr(row, "upper_limit", None))
                else None,
                lower_limit=float(row.lower_limit)
                if pd.notna(getattr(row, "lower_limit", None))
                else None,
            )
            self._bar_by_key[(bar.symbol, row.datetime)] = bar
            self._bars_by_symbol_day.setdefault((bar.symbol, bar.trading_day), []).append(bar)
            self._bars_by_timestamp.setdefault(row.datetime, {})[bar.symbol] = bar
            slots = self._timestamps_by_day.setdefault(bar.trading_day, [])
            if not slots or slots[-1] != row.datetime:
                slots.append(row.datetime)

        self.trading_days: list[date] = sorted(set(self.bars["trading_day"]))
        self.timestamps: list[pd.Timestamp] = sorted(set(self.bars["datetime"]))
        self.bars_per_day = max(len(item) for item in self._bars_by_symbol_day.values())
        self.intraday = config.bar_freq != "1d"
        if not self.intraday and self.bars_per_day > 1:
            raise BacktestDataError(
                "data.bar_freq is 1d but some contract has several bars in one "
                "trading day; set bar_freq to the real frequency of the source"
            )
        if self.intraday and self.bars_per_day == 1:
            raise BacktestDataError(
                f"data.bar_freq is {config.bar_freq} but no contract has more than one "
                "bar in any trading day; the source looks daily, set bar_freq to 1d"
            )

        dominant = tables["dominant_map"]
        dominant = dominant[dominant["underlying"].isin(wanted)]
        self._dominant: dict[str, list[tuple[date, str]]] = {}
        for row in dominant.itertuples(index=False):
            self._dominant.setdefault(str(row.underlying), []).append(
                (row.trading_day, str(row.dominant_symbol))
            )
        for entries in self._dominant.values():
            entries.sort(key=lambda item: item[0])

        settles = tables["settles"]
        self._settles: dict[tuple[str, date], float] = {
            (str(row.symbol), row.trading_day): float(row.settle_price)
            for row in settles.itertuples(index=False)
        }

        self._charges = _fee_lookup(tables.get("charges"))
        self._margins = _fee_lookup(tables.get("margins"))
        self.settle_fallbacks = 0
        self._slices: dict[tuple[str, str | None], tuple[pd.DataFrame, Any]] = {}
        self._rollups: dict[tuple[str, str | None, str], _Rollup] = {}

    # -- calendar ---------------------------------------------------------

    def previous_trading_day(self, day: date, lag: int = 1) -> date | None:
        index = bisect.bisect_left(self.trading_days, day)
        target = index - lag
        if target < 0:
            return None
        return self.trading_days[target]

    def bar(self, symbol: str, timestamp: pd.Timestamp) -> Bar | None:
        return self._bar_by_key.get((symbol, timestamp))

    def last_bar_of_day(self, symbol: str, day: date) -> Bar | None:
        bars = self._bars_by_symbol_day.get((symbol, day))
        return bars[-1] if bars else None

    def timestamps_of_day(self, day: date) -> list[pd.Timestamp]:
        """Every bar timestamp belonging to ``day``, in order.

        A daily dataset yields exactly one entry per day, which is what collapses
        the intraday loop back onto the original one-bar-per-day behaviour.
        """
        return self._timestamps_by_day.get(day, [])

    def bars_at(self, timestamp: pd.Timestamp) -> dict[str, Bar]:
        """Bars of every contract that traded at ``timestamp``.

        Contracts without a bar at this instant are simply absent; the scheduler
        treats them as untradable rather than reusing a stale price.
        """
        return dict(self._bars_by_timestamp.get(timestamp, {}))

    # -- routing inputs ---------------------------------------------------

    def dominant_symbol(self, underlying: str, day: date) -> str | None:
        """Dominant contract *as published for* ``day``.

        Callers pass the already-lagged trading day; this method performs no
        shifting of its own so the look-ahead rule lives in exactly one place
        (``Router``).
        """
        entries = self._dominant.get(underlying)
        if not entries:
            return None
        days = [item[0] for item in entries]
        index = bisect.bisect_right(days, day) - 1
        if index < 0:
            return None
        return entries[index][1]

    def dominant_coverage(self) -> None:
        for underlying in self.config.underlyings:
            entries = self._dominant.get(underlying)
            if not entries:
                raise BacktestDataError(f"dominant map does not cover underlying {underlying}")
            first = entries[0][0]
            required = self.trading_days[0]
            if first > required:
                raise BacktestDataError(
                    f"dominant map does not cover {underlying} on {required}; "
                    f"earliest record is {first}"
                )

    # -- valuation --------------------------------------------------------

    def settle_price(self, symbol: str, day: date, fallback: float | None = None) -> float:
        price = self._settles.get((symbol, day))
        if price is not None:
            return price
        bar = self.last_bar_of_day(symbol, day)
        if bar is not None:
            self.settle_fallbacks += 1
            return bar.close
        if fallback is not None:
            self.settle_fallbacks += 1
            return fallback
        raise BacktestDataError(f"cannot settle {symbol} on {day}: no settle price or bar")

    def charge(self, symbol: str, day: date) -> dict[str, float]:
        info = self.contracts[symbol]
        return _resolve(self._charges, symbol, info.underlying, day)

    def margin_rate(self, symbol: str, day: date, direction: str, default: float) -> float:
        info = self.contracts[symbol]
        record = _resolve(self._margins, symbol, info.underlying, day)
        if direction == "short" and "short_margin_rate" in record:
            return float(record["short_margin_rate"])
        if "long_margin_rate" in record:
            return float(record["long_margin_rate"])
        return default

    # -- strategy-facing history -----------------------------------------

    def _slice(self, underlying: str, symbol: str | None) -> tuple[pd.DataFrame, Any]:
        """Cached rows for one underlying (optionally one contract), plus their times.

        ``self.bars`` is already ordered by ``(datetime, symbol)``, so filtering
        preserves that order and no re-sort is needed.
        """
        key = (underlying, symbol)
        cached = self._slices.get(key)
        if cached is None:
            frame = self.bars[self.bars["underlying"] == underlying]
            if symbol is not None:
                frame = frame[frame["symbol"] == symbol]
            frame = frame.reset_index(drop=True)
            cached = (frame, frame["datetime"].to_numpy())
            self._slices[key] = cached
        return cached

    def history(
        self,
        underlying: str,
        cutoff: datetime,
        bars: int | None = None,
        symbol: str | None = None,
        freq: str | None = None,
    ) -> pd.DataFrame:
        """Rows up to and including ``cutoff``; never anything later.

        Binary search on a cached, pre-sorted slice: an intraday run asks this
        once per bar, so scanning the whole table each time would make the
        backtest quadratic in the number of bars.

        ``freq`` folds the rows into a coarser period, for a strategy that wants
        a daily signal while trading minute bars. The period that ``cutoff``
        falls inside is returned as it stands so far, which is not look-ahead:
        halfway through a session one really does know the session's high to
        that point.
        """
        frame, times = self._slice(underlying, symbol)
        end = int(np.searchsorted(times, pd.Timestamp(cutoff).to_datetime64(), side="right"))
        if freq is not None:
            return self._rollup(underlying, symbol, freq, end, bars)
        start = max(0, end - bars) if bars is not None else 0
        result = frame.iloc[start:end]
        if start:
            result = result.copy(deep=False)
            result.index = pd.RangeIndex(len(result))
        return result

    def _rollup(
        self,
        underlying: str,
        symbol: str | None,
        freq: str,
        end: int,
        bars: int | None = None,
    ) -> pd.DataFrame:
        """Aggregate the first ``end`` rows of a slice into ``freq`` periods.

        Each source row is folded once as the cursor advances. When ``bars`` is
        set, only that many aggregate rows are materialized for the caller.
        """
        if freq not in _PERIODS:
            raise BacktestDataError(
                f"history(freq={freq!r}) is not a known period; use one of "
                f"{', '.join(sorted(_PERIODS))}"
            )
        frame, _ = self._slice(underlying, symbol)
        key = (underlying, symbol, freq)
        state = self._rollups.get(key)
        if state is None:
            state = _Rollup(_period_keys(frame, freq))
            self._rollups[key] = state
        return state.upto(frame, end, bars)

    def describe(self) -> dict[str, Any]:
        return {
            "adapter": self.metadata.adapter,
            "data_version": self.metadata.data_version,
            "fingerprint": self.metadata.fingerprint,
            "as_of": self.metadata.as_of,
            "underlyings": list(self.config.underlyings),
            "bar_freq": self.config.bar_freq,
            "trading_days": len(self.trading_days),
            "first_trading_day": self.trading_days[0],
            "last_trading_day": self.trading_days[-1],
            "symbols": sorted({str(symbol) for symbol in self.bars["symbol"]}),
            "bars": int(len(self.bars)),
        }


def build_dataset(config: DataConfig, adapter: DataAdapter | None = None) -> MarketDataset:
    from .adapter import create_adapter

    return MarketDataset(config, adapter or create_adapter(config))
