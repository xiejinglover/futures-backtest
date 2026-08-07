"""Write a fixed set of backtests into a directory so two revisions can be diffed.

Used to prove that a refactor left the daily results untouched. Not part of the
package: run it from two checkouts with ``PYTHONPATH=src:.`` and compare the trees.
"""

from __future__ import annotations

import sys
from pathlib import Path

from futures_backtest.scheduler import run_backtest
from tests.support import DictAdapter, config_for, trading_days, two_contract_tables

CASES = (
    ("hold_next_open", "tests.support:HoldTwoLots", "next_open", {}),
    ("hold_same_close", "tests.support:HoldTwoLots", "same_close", {}),
    ("flip_next_open", "tests.support:FlipStrategy", "next_open", {}),
    ("limit_missing", "tests.support:LimitStrategy", "next_open", {"offset_ticks": 8}),
    ("limit_marketable", "tests.support:LimitStrategy", "next_open", {"offset_ticks": -4}),
)


def main(out: Path) -> None:
    for name, strategy, market_fill, parameters in CASES:
        days = trading_days(8)
        tables = two_contract_tables(days, days[4])
        config = config_for(
            tables,
            strategy=strategy,
            parameters=parameters,
            execution={"market_fill": market_fill},
            output_root=out / name,
        )
        run_backtest(config, DictAdapter(tables), run_id="golden")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
