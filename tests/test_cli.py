from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from futures_backtest.cli import main
from tests.support import REPO_ROOT

EXAMPLES = REPO_ROOT / "examples"


def _redirect_output(config_path: Path, destination: Path) -> Path:
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["output"]["root"] = str(destination)
    payload["data"]["options"]["root"] = str(EXAMPLES / payload["data"]["options"]["root"])
    target = destination / config_path.name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return target


@pytest.mark.parametrize("name", ["mock_daily.yaml", "mock_ma_cross.yaml"])
def test_the_shipped_examples_validate_and_run(tmp_path, capsys, name):
    config_path = _redirect_output(EXAMPLES / name, tmp_path / name.replace(".yaml", ""))

    assert main(["validate", "--config", str(config_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "ok"
    assert report["rolls"], "the sample data must contain a roll"

    assert main(["run", "--config", str(config_path)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["metrics"]["status"] == "ok"
    assert result["metrics"]["rolls"] == 1
    assert Path(result["run_path"]).joinpath("metrics.json").exists()


def test_a_strategy_module_beside_the_config_is_importable(tmp_path, capsys, monkeypatch):
    """A pip-installed console script has no project directory on sys.path."""
    project = tmp_path / "desk"
    (project / "mydesk").mkdir(parents=True)
    (project / "mydesk" / "__init__.py").write_text("", encoding="utf-8")
    (project / "mydesk" / "strategies.py").write_text(
        "from futures_backtest import BaseStrategy, TargetPosition\n\n\n"
        "class HoldOne(BaseStrategy):\n"
        "    def on_bar(self, context):\n"
        "        return TargetPosition(underlying='RB', net_lots=1)\n",
        encoding="utf-8",
    )
    payload = yaml.safe_load((EXAMPLES / "mock_daily.yaml").read_text(encoding="utf-8"))
    payload["data"]["options"]["root"] = str(EXAMPLES / "sample_data")
    payload["strategy"] = {"path": "mydesk.strategies:HoldOne"}
    payload["output"]["root"] = str(tmp_path / "out")
    config_path = project / "run.yaml"
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    # Run from an unrelated directory: only the config's own folder should matter.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    assert main(["run", "--config", str(config_path)]) == 0
    assert json.loads(capsys.readouterr().out)["metrics"]["status"] == "ok"


def test_a_broken_config_exits_with_a_message(tmp_path, capsys):
    config_path = tmp_path / "broken.yaml"
    config_path.write_text("data: {}\n", encoding="utf-8")
    assert main(["run", "--config", str(config_path)]) == 1
    assert "error:" in capsys.readouterr().err


def test_a_missing_data_root_exits_as_a_data_error(tmp_path, capsys):
    payload = yaml.safe_load((EXAMPLES / "mock_daily.yaml").read_text(encoding="utf-8"))
    payload["data"]["options"]["root"] = str(tmp_path / "absent")
    payload["output"]["root"] = str(tmp_path / "out")
    config_path = tmp_path / "missing.yaml"
    config_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    assert main(["validate", "--config", str(config_path)]) == 2
    assert "not a directory" in capsys.readouterr().err
