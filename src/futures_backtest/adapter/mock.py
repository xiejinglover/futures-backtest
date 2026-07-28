from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd

from ..config import DataConfig
from ..types import AdapterMetadata, BacktestDataError
from .base import TABLE_SPECS, option


class MockAdapter:
    """Reads the contract tables from a directory of CSV or Parquet files.

    One format per table: a table present as both ``bars.csv`` and
    ``bars.parquet`` is a configuration mistake rather than a merge.
    """

    name = "mock"

    def __init__(self, config: DataConfig):
        self.config = config
        root = option(config, "root", required=True)
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise BacktestDataError(f"data.options.root is not a directory: {self.root}")

    def _path(self, name: str) -> Path | None:
        candidates = [
            path
            for path in (self.root / f"{name}.csv", self.root / f"{name}.parquet")
            if path.exists()
        ]
        if len(candidates) > 1:
            raise BacktestDataError(f"table {name} exists as both CSV and Parquet")
        return candidates[0] if candidates else None

    def _files(self) -> list[Path]:
        found = []
        for name in TABLE_SPECS:
            path = self._path(name)
            if path is not None:
                found.append(path)
        return sorted(found)

    def metadata(self) -> AdapterMetadata:
        digest = hashlib.sha256()
        for path in self._files():
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        fingerprint = digest.hexdigest()
        version = self.config.data_version or f"mock-{fingerprint[:12]}"
        bars = self.load_table("bars")
        as_of = pd.to_datetime(bars["trading_day"]).max().date() if not bars.empty else None
        return AdapterMetadata(
            adapter=self.name,
            data_version=version,
            fingerprint=fingerprint,
            as_of=as_of,
            details={"root": str(self.root), "files": [path.name for path in self._files()]},
        )

    def load_table(self, name: str) -> pd.DataFrame | None:
        if name not in TABLE_SPECS:
            raise BacktestDataError(f"unknown table {name}")
        path = self._path(name)
        if path is None:
            if TABLE_SPECS[name].required:
                raise BacktestDataError(f"missing required table {name} under {self.root}")
            return None
        if path.suffix == ".csv":
            return pd.read_csv(path)
        return pd.read_parquet(path)
