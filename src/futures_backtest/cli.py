from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import load_config
from .performance import _json_default
from .scheduler import run_backtest, validate_config
from .types import BacktestDataError


def _print(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))


def _make_importable(directory: Path) -> None:
    """Let ``strategy.path`` name a module that sits next to the config file.

    An installed console script starts with no project directory on ``sys.path``,
    so a team's own ``mydesk.strategies`` would be unimportable. Resolving against
    the config's own directory keeps a config working from any current directory.
    """
    entry = str(directory)
    if entry not in sys.path:
        sys.path.insert(0, entry)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="futures-backtest",
        description="Underlying-level signals in, contract-level fills out.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("validate", "load data and check the contract without trading"),
        ("run", "run the backtest and write the artefacts"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--config", required=True, help="path to the YAML config")

    arguments = parser.parse_args(argv)
    try:
        _make_importable(Path(arguments.config).expanduser().resolve().parent)
        config = load_config(arguments.config)
        if arguments.command == "validate":
            _print(validate_config(config))
            return 0
        result = run_backtest(config)
        _print(
            {
                "run_id": result.run_id,
                "run_path": str(result.run_path),
                "data_version": result.data_version,
                "metrics": result.metrics,
            }
        )
        return 0
    except BacktestDataError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
