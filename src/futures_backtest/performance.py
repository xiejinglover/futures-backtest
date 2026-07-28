from __future__ import annotations

import json
import math
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .config import BacktestConfig

TRADING_DAYS_PER_YEAR = 252


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime, Path)):
        return str(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if hasattr(value, "item"):
        return value.item()
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    raise TypeError(type(value).__name__)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _drawdown(equity: pd.Series) -> tuple[float, dict[str, Any]]:
    curve = equity / equity.cummax() - 1
    if curve.empty:
        return 0.0, {}
    trough = int(curve.to_numpy().argmin())
    peak = int(equity.iloc[: trough + 1].to_numpy().argmax())
    recovery = next(
        (index for index in range(trough, len(equity)) if equity.iloc[index] >= equity.iloc[peak]),
        None,
    )
    return float(curve.min()), {
        "peak_index": peak,
        "trough_index": trough,
        "recovery_index": recovery,
        "recovered": recovery is not None,
    }


def compute_metrics(frames: dict[str, pd.DataFrame], initial_cash: float) -> dict[str, Any]:
    nav = frames["nav"]
    fills = frames["fills"]
    rolls = frames["rolls"]

    if nav.empty:
        return {"status": "empty", "initial_cash": initial_cash}

    nav = nav.sort_values("trading_day").reset_index(drop=True)
    equity = nav["equity"].astype(float)
    returns = equity.pct_change().dropna()
    total_return = float(equity.iloc[-1] / initial_cash - 1)
    days = max(1, len(equity))
    annualized = (
        float((1 + total_return) ** (TRADING_DAYS_PER_YEAR / days) - 1)
        if total_return > -1
        else -1.0
    )
    volatility = (
        float(returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)) if len(returns) > 1 else 0.0
    )
    sharpe = (
        float(returns.mean() / returns.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
        if len(returns) > 1 and returns.std(ddof=1) > 0
        else 0.0
    )
    downside = returns[returns < 0]
    sortino = (
        float(returns.mean() / downside.std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR))
        if len(downside) > 1 and downside.std(ddof=1) > 0
        else 0.0
    )
    max_drawdown, drawdown_profile = _drawdown(equity)

    filled = fills[fills["filled_lots"] > 0] if not fills.empty else fills
    rejected = fills[fills["filled_lots"] == 0] if not fills.empty else fills
    commission = float(filled["commission"].sum()) if not filled.empty else 0.0
    roll_commission = float(rolls["commission"].sum()) if not rolls.empty else 0.0
    roll_slippage = float(rolls["slippage_cost"].sum()) if not rolls.empty else 0.0
    roll_cost = roll_commission + roll_slippage
    total_pnl = float(equity.iloc[-1] - initial_cash)

    metrics: dict[str, Any] = {
        "status": "ok",
        "initial_cash": float(initial_cash),
        "final_equity": float(equity.iloc[-1]),
        "total_return": total_return,
        "annualized_return": annualized,
        "annualized_volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "drawdown_profile": drawdown_profile,
        "trading_days": int(len(equity)),
        "first_trading_day": nav["trading_day"].iloc[0],
        "last_trading_day": nav["trading_day"].iloc[-1],
        "realized_pnl": float(nav["realized_pnl_cum"].iloc[-1]),
        "total_pnl": total_pnl,
        "commission": commission,
        "orders": int(len(frames["orders"])),
        "fills": int(len(filled)),
        "rejections": int(len(rejected)),
        "rejection_reasons": (
            rejected["reject_reason"].value_counts().to_dict() if not rejected.empty else {}
        ),
        "rolls": int(len(rolls)),
        "roll_commission": roll_commission,
        "roll_slippage_cost": roll_slippage,
        "roll_cost": roll_cost,
        "roll_cost_share_of_pnl": (roll_cost / abs(total_pnl)) if total_pnl else None,
        "average_margin_ratio": float((nav["margin"] / nav["equity"]).mean()),
        "peak_margin_ratio": float((nav["margin"] / nav["equity"]).max()),
        "skipped_targets": int(len(frames["skipped_targets"])),
    }
    if not filled.empty:
        closes = filled[filled["offset"].isin(["close", "close_today"])]
        wins = closes[closes["realized_pnl"] > 0]
        metrics["closed_trades"] = int(len(closes))
        metrics["win_rate"] = float(len(wins) / len(closes)) if len(closes) else 0.0
        gross_profit = float(wins["realized_pnl"].sum())
        gross_loss = float(-closes[closes["realized_pnl"] < 0]["realized_pnl"].sum())
        metrics["profit_factor"] = (gross_profit / gross_loss) if gross_loss > 0 else None
    return metrics


def write_outputs(
    run_path: Path,
    config: BacktestConfig,
    frames: dict[str, pd.DataFrame],
    metrics: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    run_path.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_csv(run_path / f"{name}.csv", index=False)
        if config.output.write_parquet and not frame.empty:
            frame.to_parquet(run_path / f"{name}.parquet", index=False)
    write_json(run_path / "metrics.json", metrics)
    write_json(run_path / "metadata.json", metadata)
    write_json(run_path / "config.json", json.loads(config.model_dump_json()))
