# Contributing

欢迎 issue 与 pull request。这个项目是 MIT 许可的开源框架，改动的门槛不在于代码风格，
而在于**回测结果必须仍然可复现、且不引入未来信息**。

## 本地开发

需要 Python 3.11 或更高版本。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[parquet,dev]"
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
.venv/bin/python -m pip check
```

`dev` extra 里就是 `pytest`、`ruff`、`build`，与 CI 用的是同一套。

## 改动流程

1. fork 仓库，从 `main` 切一个短生命周期分支。
2. 一次只做一件事，并补回归测试。
3. 跑完上面的本地检查（`ruff check` 与 `pytest -q` 必须全绿）。
4. 提 pull request，说明改了什么行为、为什么。
5. CI 三个 Python 版本都绿、评审通过后合并。

期货相关的改动必须覆盖这些回归点：T-1 主力路由、换月移仓与成本、结算盯市、
涨跌停与保证金拒单、同配置可复现。任何可能让策略提前看到未来主力或未来行情的改动，
需要在 PR 里明确说明为什么不构成 look-ahead。

`futures_backtest/contrib/` 是示例级代码，不属于框架契约、不承诺稳定性；
往里加策略的门槛低，但它不该被当作正式 API 使用。

## 发版流程

发布由 tag 触发，仓库里不存任何 PyPI token（用的是 PyPI Trusted Publishing）：

1. 更新 `pyproject.toml` 里的 `version`。
2. 在 `CHANGELOG.md` 写清本次改动。
3. 合并发版 pull request。
4. 在合并提交上打带注释的 tag，例如 `git tag -a v0.1.0 -m 'v0.1.0'` 并推送。
5. `release.yml` 会校验 tag 与 `version` 一致，构建 sdist/wheel 并发到 PyPI。

tag 与 `pyproject.toml` 的版本不一致时流水线会直接失败，避免打错 tag 发出错版本。
补丁号用于兼容修复，次版本号用于兼容的新功能，主版本号用于有意的不兼容变更。
公开 API 的弃用至少保留一个次版本。

使用方应在 `requirements.txt` 里**固定版本**（例如 `futures-backtest==0.1.0`），
否则历史回测结果会随框架漂移。

## 使用方怎么写自己的策略

框架只交付引擎。真实策略写在你自己的项目里，放一个可导入的模块：

```text
my-desk/
├── run.yaml
├── data/                # 或者配置成 ipquant_mysql
└── strategies.py        # class HoldOne(BaseStrategy)
```

```yaml
strategy:
  path: strategies:HoldOne
  parameters: {underlying: RB, lots: 1}
```

CLI 会把**配置文件所在目录**加入 `sys.path`，所以 `run.yaml` 与 `strategies.py` 同级时，
在任何工作目录下执行都能导入：

```bash
futures-backtest run --config /path/to/my-desk/run.yaml
```

策略打包成自己的可安装包（`pip install -e .`）同样可用，那种情况下 `path` 就是包名，
例如 `mydesk.strategies:HoldOne`。本仓库 `examples/` 下的配置与样例数据**不随 wheel
分发**；随包分发的只有 `futures_backtest.contrib.strategies` 里的示例策略。
