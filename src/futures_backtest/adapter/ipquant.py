from __future__ import annotations

import hashlib
from typing import Any

import pandas as pd

from ..config import DataConfig
from ..types import AdapterMetadata, BacktestDataError
from .base import TABLE_SPECS, option

# Column mapping from the external ipquant schema (docs/features.md section 10) onto
# the framework contract. Only these tables are read; nothing else leaks upward.
DAILY_BAR_QUERY = """
SELECT symbol, underlying, trade_date AS `datetime`, trade_date AS trading_day,
       open, high, low, close, volume, open_interest,
       upper_limit, lower_limit
FROM future_daily_market_data
WHERE underlying IN :underlyings AND trade_date BETWEEN :start AND :end
"""

MINUTE_BAR_QUERY = """
SELECT symbol, underlying, bar_time AS `datetime`, trading_day,
       open, high, low, close, volume, open_interest
FROM {table}
WHERE underlying IN :underlyings AND trading_day BETWEEN :start AND :end
"""

QUERIES = {
    "contracts": """
        SELECT symbol, underlying, multiplier, tick_size, exchange, expire_date
        FROM future_contract_info
        WHERE underlying IN :underlyings
    """,
    "dominant_map": """
        SELECT trading_day, underlying, dominant_symbol
        FROM future_dominant_contracts
        WHERE underlying IN :underlyings AND trading_day BETWEEN :start AND :end
    """,
    "settles": """
        SELECT symbol, trading_day, settle_price
        FROM future_set_price
        WHERE underlying IN :underlyings AND trading_day BETWEEN :start AND :end
    """,
    "charges": """
        SELECT underlying, symbol, trading_day,
               open_fee_rate, open_fee_per_lot,
               close_fee_rate, close_fee_per_lot,
               close_today_fee_rate, close_today_fee_per_lot
        FROM future_charge
        WHERE underlying IN :underlyings
    """,
    "margins": """
        SELECT underlying, symbol, trading_day,
               long_margin_rate, short_margin_rate
        FROM future_margin_rate
        WHERE underlying IN :underlyings
    """,
}


class IpquantMysqlAdapter:
    """Maps the external ``ipquant`` MySQL tables onto the framework contract.

    The external library and its database are deliberately outside this repository;
    this adapter only knows table and column names. The dominant map is read as
    published, and the router still applies ``routing.dominant_lag`` on top, so a
    same-day dominant row in the source cannot silently explain a same-day fill.
    """

    name = "ipquant_mysql"

    def __init__(self, config: DataConfig):
        self.config = config
        self.dsn = str(option(config, "dsn", required=True))
        self.minute_table = str(
            option(config, "minute_table", "future_1m_market_data_all")
        )
        self._engine: Any | None = None

    def _connect(self) -> Any:
        if self._engine is None:
            try:
                from sqlalchemy import create_engine
            except ModuleNotFoundError as error:  # pragma: no cover - optional extra
                raise BacktestDataError(
                    "the ipquant_mysql adapter needs the 'mysql' extra: "
                    "python -m pip install 'futures-backtest[mysql]'"
                ) from error
            self._engine = create_engine(self.dsn)
        return self._engine

    def _params(self) -> dict[str, Any]:
        return {
            "underlyings": tuple(self.config.underlyings),
            "start": self.config.start,
            "end": self.config.end,
        }

    def _query(self, name: str) -> str:
        if name == "bars":
            if self.config.bar_freq == "1d":
                return DAILY_BAR_QUERY
            return MINUTE_BAR_QUERY.format(table=self.minute_table)
        return QUERIES[name]

    def metadata(self) -> AdapterMetadata:
        digest = hashlib.sha256()
        digest.update(self.dsn.split("@")[-1].encode("utf-8"))
        digest.update(str(self._params()).encode("utf-8"))
        digest.update(self.config.bar_freq.encode("utf-8"))
        fingerprint = digest.hexdigest()
        version = self.config.data_version or f"ipquant-{fingerprint[:12]}"
        return AdapterMetadata(
            adapter=self.name,
            data_version=version,
            fingerprint=fingerprint,
            as_of=self.config.end,
            details={"bar_freq": self.config.bar_freq, "minute_table": self.minute_table},
        )

    def load_table(self, name: str) -> pd.DataFrame | None:
        if name not in TABLE_SPECS:
            raise BacktestDataError(f"unknown table {name}")
        from sqlalchemy import text

        with self._connect().connect() as connection:
            statement = text(self._query(name)).bindparams(**self._params())
            return pd.read_sql(statement, connection)
