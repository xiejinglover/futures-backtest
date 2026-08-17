from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

import pandas as pd


class BacktestDataError(RuntimeError):
    """Raised when input data or a runtime invariant violates the contract."""


class EventKind(StrEnum):
    BAR = "BAR"
    ORDER_TRIGGER = "ORDER_TRIGGER"
    ROLL = "ROLL"
    SETTLE = "SETTLE"


@dataclass(frozen=True)
class ContractInfo:
    symbol: str
    underlying: str
    multiplier: float
    tick_size: float
    exchange: str | None = None
    expire_date: date | None = None


@dataclass(frozen=True)
class Bar:
    symbol: str
    underlying: str
    timestamp: datetime
    trading_day: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    open_interest: float | None = None
    upper_limit: float | None = None
    lower_limit: float | None = None


@dataclass(frozen=True)
class TargetPosition:
    """What a strategy asks for: a net lot count on an *underlying*.

    Positive is long, negative is short, zero is flat. The strategy never names a
    month contract; the router resolves one.

    ``limit_price`` turns the resulting order into a limit order. ``stop_price``
    makes it a stop-market order, or a stop-limit order when both prices are set.
    ``time_in_force`` is ``DAY`` by default; ``GTC`` survives trading-day closes.
    """

    underlying: str
    net_lots: int
    limit_price: float | None = None
    stop_price: float | None = None
    time_in_force: str = "DAY"

    def __post_init__(self) -> None:
        if not self.underlying:
            raise ValueError("TargetPosition.underlying must not be empty")
        if int(self.net_lots) != self.net_lots:
            raise ValueError("TargetPosition.net_lots must be a whole number of lots")
        for name, price in (
            ("limit_price", self.limit_price),
            ("stop_price", self.stop_price),
        ):
            if price is not None and (not math.isfinite(price) or price <= 0):
                raise ValueError(
                    f"TargetPosition.{name} must be a positive finite price, got {price!r}"
                )
        if self.time_in_force not in ("DAY", "GTC"):
            raise ValueError("TargetPosition.time_in_force must be 'DAY' or 'GTC'")


@dataclass(frozen=True)
class Order:
    trading_day: date
    timestamp: datetime
    underlying: str
    symbol: str
    side: str  # "buy" | "sell"
    offset: str  # "open" | "close" | "close_today"
    lots: int
    reference_price: float
    reason: str  # "signal" | "roll_out" | "roll_in" | "expiry"
    limit_price: float | None = None  # None means market order
    stop_price: float | None = None
    time_in_force: str = "DAY"
    order_id: str = ""


@dataclass(frozen=True)
class Fill:
    trading_day: date
    timestamp: datetime
    underlying: str
    symbol: str
    side: str
    offset: str
    requested_lots: int
    filled_lots: int
    price: float
    commission: float
    slippage_ticks: float
    realized_pnl: float
    status: str  # "filled" | "partial" | "rejected"
    reason: str
    reject_reason: str | None = None
    order_id: str = ""


@dataclass
class Position:
    """One side of one contract. Long and short are tracked separately."""

    symbol: str
    underlying: str
    direction: str  # "long" | "short"
    lots: int = 0
    today_lots: int = 0
    average_price: float = 0.0
    last_price: float = 0.0

    @property
    def yesterday_lots(self) -> int:
        return self.lots - self.today_lots

    def signed_lots(self) -> int:
        return self.lots if self.direction == "long" else -self.lots


@dataclass
class RollLog:
    trading_day: date
    underlying: str
    from_symbol: str
    to_symbol: str
    net_lots: int
    close_price: float
    open_price: float
    commission: float
    slippage_cost: float
    realized_pnl: float


@dataclass
class AccountSnapshot:
    """Read-only account view handed to strategies and written to the NAV file."""

    trading_day: date
    cash: float
    margin: float
    available: float
    equity: float
    unrealized_pnl: float
    net_lots: dict[str, int] = field(default_factory=dict)


class DataAdapter(Protocol):
    """Turns an external source into the tables of docs/data-contract.md."""

    def metadata(self) -> AdapterMetadata: ...

    def load_table(self, name: str) -> pd.DataFrame: ...


@dataclass(frozen=True)
class AdapterMetadata:
    adapter: str
    data_version: str
    fingerprint: str
    as_of: date | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    run_path: Path
    data_version: str
    status: str
    metrics: dict[str, Any]
