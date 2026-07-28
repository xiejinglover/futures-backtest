from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DataConfig(StrictModel):
    adapter: str = "mock"
    options: dict[str, Any] = Field(default_factory=dict)
    underlyings: list[str] = Field(min_length=1)
    start: date
    end: date
    bar_freq: Literal["1d", "1m"] = "1d"
    data_version: str | None = None
    history_bars: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_dates(self) -> DataConfig:
        if self.start > self.end:
            raise ValueError("data.start must be <= data.end")
        if len(set(self.underlyings)) != len(self.underlyings):
            raise ValueError("data.underlyings must be unique")
        return self


class PortfolioConfig(StrictModel):
    initial_cash: float = Field(gt=0)
    margins_default: float = Field(default=0.1, gt=0, le=1)


class RoutingConfig(StrictModel):
    dominant_lag: int = Field(default=1, ge=0)
    roll_timing: Literal["next_open", "same_close"] = "next_open"
    allow_signals_on_roll_day: bool = True
    lookahead_dominant: bool = False
    force_close_before_expiry_days: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_routing(self) -> RoutingConfig:
        if self.lookahead_dominant and self.dominant_lag != 0:
            raise ValueError(
                "routing.lookahead_dominant=true requires routing.dominant_lag=0; "
                "the two settings describe the same relaxation"
            )
        if self.dominant_lag == 0 and not self.lookahead_dominant:
            raise ValueError(
                "routing.dominant_lag=0 lets a same-day dominant decision explain "
                "same-day fills; set routing.lookahead_dominant=true to accept that "
                "look-ahead explicitly"
            )
        return self


class ExecutionConfig(StrictModel):
    market_fill: Literal["next_open", "same_close"] = "next_open"
    slippage_ticks: float = Field(default=1.0, ge=0)
    on_margin_short: Literal["reject", "scale"] = "reject"
    enforce_price_limits: bool = True


class StrategyConfig(StrictModel):
    path: str = Field(min_length=1)
    parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_path(self) -> StrategyConfig:
        if ":" not in self.path:
            raise ValueError("strategy.path must look like 'module.sub:ClassName'")
        return self


class OutputConfig(StrictModel):
    root: Path = Path("backtests")
    seed: int = 20260728
    write_parquet: bool = False


class BacktestConfig(StrictModel):
    data: DataConfig
    portfolio: PortfolioConfig
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    strategy: StrategyConfig
    output: OutputConfig = Field(default_factory=OutputConfig)


def load_config(path: str | Path) -> BacktestConfig:
    source = Path(path).expanduser().resolve()
    with source.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    config = BacktestConfig.model_validate(payload)
    base = source.parent
    root = config.data.options.get("root")
    if root is not None and not Path(root).is_absolute():
        config.data.options["root"] = str((base / str(root)).resolve())
    if not config.output.root.is_absolute():
        config.output.root = (Path.cwd() / config.output.root).resolve()
    return config
