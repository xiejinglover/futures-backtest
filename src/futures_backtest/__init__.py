"""Futures backtesting: underlying-level signals in, contract-level fills out."""

from .account import Account
from .adapter import IpquantMysqlAdapter, MockAdapter, create_adapter
from .config import (
    BacktestConfig,
    DataConfig,
    ExecutionConfig,
    OutputConfig,
    PortfolioConfig,
    RoutingConfig,
    StrategyConfig,
    load_config,
)
from .dataset import MarketDataset, build_dataset
from .matcher import Matcher
from .performance import compute_metrics
from .router import Router
from .scheduler import Scheduler, run_backtest, validate_config
from .strategy import BaseStrategy, Strategy, StrategyContext, load_strategy
from .types import (
    AccountSnapshot,
    AdapterMetadata,
    BacktestDataError,
    BacktestResult,
    Bar,
    ContractInfo,
    DataAdapter,
    EventKind,
    Fill,
    Order,
    Position,
    RollLog,
    TargetPosition,
)

__version__ = "0.1.0"

__all__ = [
    "Account",
    "AccountSnapshot",
    "AdapterMetadata",
    "BacktestConfig",
    "BacktestDataError",
    "BacktestResult",
    "Bar",
    "BaseStrategy",
    "ContractInfo",
    "DataAdapter",
    "DataConfig",
    "EventKind",
    "ExecutionConfig",
    "Fill",
    "IpquantMysqlAdapter",
    "MarketDataset",
    "Matcher",
    "MockAdapter",
    "Order",
    "OutputConfig",
    "PortfolioConfig",
    "Position",
    "RollLog",
    "Router",
    "RoutingConfig",
    "Scheduler",
    "Strategy",
    "StrategyConfig",
    "StrategyContext",
    "TargetPosition",
    "__version__",
    "build_dataset",
    "compute_metrics",
    "create_adapter",
    "load_config",
    "load_strategy",
    "run_backtest",
    "validate_config",
]
