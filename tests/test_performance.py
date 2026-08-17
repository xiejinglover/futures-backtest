"""Metric definitions pinned against a nav curve short enough to do by hand.

The formulas these assert are tabulated in docs/features.md 5.7.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Any

import pandas as pd
import pytest

from futures_backtest.performance import compute_metrics

# Three trading days, chosen so both daily returns are exact: +10% then -5%.
INITIAL_CASH = 1_000_000.0
EQUITY = [1_020_000.0, 1_122_000.0, 1_065_900.0]

FILL_COLUMNS = [
    "trading_day",
    "offset",
    "filled_lots",
    "commission",
    "realized_pnl",
    "reject_reason",
]


def nav_frame(equity: list[float]) -> pd.DataFrame:
    days = [date(2024, 4, 1 + index) for index in range(len(equity))]
    return pd.DataFrame(
        {
            "trading_day": days,
            "equity": equity,
            "margin": [100_000.0] * len(equity),
            "realized_pnl_cum": [0.0] * len(equity),
        }
    )


def fill_rows(rows: list[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(rows)
    for column in FILL_COLUMNS:
        if column not in frame.columns:
            frame[column] = None
    return frame[FILL_COLUMNS]


def metrics_for(
    equity: list[float] | None = None,
    fills: pd.DataFrame | None = None,
    trades: pd.DataFrame | None = None,
) -> dict[str, Any]:
    frames = {
        "nav": nav_frame(equity if equity is not None else EQUITY),
        "fills": fills if fills is not None else fill_rows([]),
        "rolls": pd.DataFrame(),
        "orders": pd.DataFrame(),
        "skipped_targets": pd.DataFrame(),
        "trades": trades if trades is not None else pd.DataFrame(),
    }
    return compute_metrics(frames, INITIAL_CASH)


def test_the_four_headline_numbers_match_the_arithmetic():
    metrics = metrics_for()

    # 1_065_900 / 1_000_000 - 1
    assert metrics["total_return"] == pytest.approx(0.0659)
    # Geometric, over the three trading days on the nav: 1.0659 ** (252 / 3) - 1.
    assert metrics["trading_days"] == 3
    assert metrics["annualized_return"] == pytest.approx(1.0659**84 - 1)
    # The two returns are +0.10 and -0.05, so mean 0.025 and sample stdev
    # 0.075 * sqrt(2). The first day's +2% over initial cash is not one of them.
    assert metrics["annualized_volatility"] == pytest.approx(0.075 * math.sqrt(504))
    assert metrics["sharpe"] == pytest.approx(math.sqrt(14))


def test_sharpe_is_not_annualized_return_over_annualized_volatility():
    """Deliberate, not a bug: see docs/features.md 5.7 before "fixing" this.

    The ratio compares a geometric return against an arithmetic dispersion, and
    over n days against n-1 returns. Here it is 33x the actual Sharpe.
    """
    metrics = metrics_for()
    ratio = metrics["annualized_return"] / metrics["annualized_volatility"]

    assert metrics["sharpe"] == pytest.approx(math.sqrt(14))
    assert ratio == pytest.approx(125.852435, rel=1e-6)
    assert metrics["sharpe"] != pytest.approx(ratio)


def test_win_rate_and_profit_factor_count_closing_fills_only():
    fills = fill_rows(
        [
            # An opening fill realizes nothing and must not count as a loss.
            {"offset": "open", "filled_lots": 2, "commission": 1.0, "realized_pnl": 0.0},
            {"offset": "close", "filled_lots": 1, "commission": 1.0, "realized_pnl": 300.0},
            {"offset": "close_today", "filled_lots": 1, "commission": 1.0, "realized_pnl": 200.0},
            {"offset": "close", "filled_lots": 1, "commission": 1.0, "realized_pnl": -100.0},
            # Rejected: no lots changed hands, so it is not a losing trade either.
            {
                "offset": "close",
                "filled_lots": 0,
                "commission": 0.0,
                "realized_pnl": 0.0,
                "reject_reason": "insufficient_margin",
            },
        ]
    )
    metrics = metrics_for(fills=fills)

    assert metrics["closed_trades"] == 3
    assert metrics["win_rate"] == pytest.approx(2 / 3)
    assert metrics["profit_factor"] == pytest.approx(500 / 100)


def test_profit_factor_is_none_rather_than_infinite_without_a_losing_close():
    fills = fill_rows(
        [{"offset": "close", "filled_lots": 1, "commission": 1.0, "realized_pnl": 300.0}]
    )
    assert metrics_for(fills=fills)["profit_factor"] is None


def test_the_two_win_rates_answer_different_questions():
    """``win_rate`` is the settlement basis, ``round_trip_win_rate`` the economic one.

    A round trip that made money overall can still leave a losing fill behind,
    because settlement resets the basis every evening and splits the move.
    """
    fills = fill_rows(
        [
            {"offset": "close", "filled_lots": 1, "commission": 1.0, "realized_pnl": -100.0},
            {"offset": "close", "filled_lots": 1, "commission": 1.0, "realized_pnl": 300.0},
        ]
    )
    trades = pd.DataFrame(
        [
            {"net_pnl": 400.0, "holding_minutes": 30.0},
            {"net_pnl": 250.0, "holding_minutes": 90.0},
        ]
    )
    metrics = metrics_for(fills=fills, trades=trades)

    assert metrics["win_rate"] == pytest.approx(0.5)
    assert metrics["round_trip_win_rate"] == pytest.approx(1.0)
    assert metrics["average_holding_minutes"] == pytest.approx(60.0)
