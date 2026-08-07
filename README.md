# futures-backtest

[![test](https://github.com/xiejinglover/futures-backtest/actions/workflows/ci.yml/badge.svg)](https://github.com/xiejinglover/futures-backtest/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/futures-backtest.svg)](https://pypi.org/project/futures-backtest/)
[![Python](https://img.shields.io/pypi/pyversions/futures-backtest.svg)](https://pypi.org/project/futures-backtest/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

期货回测框架：**策略只出品种信号，框架完成适配、主力路由、换月移仓、撮合与盯市。**

> **English summary.** A futures backtesting framework where strategies emit
> *underlying-level* target positions (`TargetPosition("RB", +2)`) and the framework
> produces *contract-level* fills. On trading day T it routes to the dominant contract
> as known at T-1 — never tomorrow's — so no look-ahead leaks in through the roll, and
> it rolls positions automatically when the dominant changes. Exchange-style daily
> settlement, per-contract margin, tick alignment, price limits and today/yesterday
> close fees are all handled by the engine, not by your strategy. Docs are in Chinese
> because the domain vocabulary is (主力合约, 平今, 涨跌停).

需求与设计的权威文档是 [`docs/features.md`](docs/features.md)，数据契约见
[`docs/data-contract.md`](docs/data-contract.md)。

## 它解决什么问题

量化模型常基于主力连续序列建模，但真实成交必须落在具体月份合约，而下单时并不知道
"今天收盘后会认定谁是新主力"。框架把两条轨道分开：

```text
研究轨（策略可见）              交易轨（Router / Matcher / Account）
──────────────────              ────────────────────────────────────
品种层 bar 视图                 dominant_map（历史已公布的主力映射）
→ TargetPosition("RB", +2)      → 交易日 T 使用 T-1 日的主力 symbol
                                → 成交、手续费、保证金、盯市都落在该 symbol
                                → 主力相对昨日变化时自动 ROLL（平旧开新）
```

策略里不需要出现月份合约、不需要预测明天的主力、不需要自己平旧开新。

## 安装

需要 Python 3.11 或更高版本。

```bash
python -m pip install futures-backtest
```

可选 extra：`parquet`（Parquet 数据源）、`mysql`（`ipquant_mysql` 适配器）。

```bash
python -m pip install "futures-backtest[parquet,mysql]"
```

本仓库开发：`python -m pip install -e ".[parquet,dev]"`，协作流程见
[`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 运行

装完包即可跑：包内随附两个示例策略
（`futures_backtest.contrib.strategies`），只需要一份配置和一份数据。

```bash
futures-backtest validate --config examples/mock_daily.yaml
futures-backtest run --config examples/mock_daily.yaml
python -m futures_backtest run --config examples/mock_daily.yaml
```

`contrib` 里的策略是**示例级**代码，不属于框架契约、不承诺稳定性，仅供上手与对照，
真实策略请写在你自己的项目里。

CLI 会把**配置文件所在目录**加入 `sys.path`，因此 `strategy.path` 可以直接写使用方
项目里与配置同级的模块，在任何工作目录下都能跑；
[`examples/own_strategy.yaml`](examples/own_strategy.yaml) 走的就是这条路径。

Python API：

```python
from futures_backtest import BacktestConfig, run_backtest

result = run_backtest(BacktestConfig.model_validate(payload))
print(result.metrics["total_return"])
```

仓库自带一份 mock 数据（1 个品种、2 个合约、一次主力切换），
`examples/mock_daily.yaml` 可直接运行，用来演示格式与换月行为，不代表真实收益。

## 策略接口

策略只面向 `underlying` 工作，输出目标净手数：

```python
from futures_backtest import BaseStrategy, StrategyContext, TargetPosition


class BuyAndHold(BaseStrategy):
    def on_bar(self, context: StrategyContext) -> TargetPosition | None:
        underlying = self.parameters["underlying"]
        if context.bars_seen < self.parameters.get("warmup", 1):
            return None
        return TargetPosition(underlying=underlying, net_lots=self.parameters["lots"])
```

约定：

- 决策单位是品种，不是月份合约；
- 策略不查主力、不拼合约代码、不算手续费、不做移仓；
- 需要知道"框架现在在交易哪个合约"时，用 `context.trading_symbol(underlying)` 只读查询；
- `context.history(...)` 只返回当前 bar 及之前的数据，越界即抛错。

## 模块职责

| 模块 | 职责 | 策略是否感知 |
|---|---|---|
| `adapter/` | 从外部源读数，产出框架标准数据 | 否 |
| `router.py` | 品种信号 → 具体合约订单；换月移仓 | 否（可只读查询） |
| `matcher.py` | 按 bar 撮合、tick 对齐、涨跌停、保证金检查 | 否 |
| `account.py` | 合约持仓、保证金、结算价盯市 | 只看摘要 |
| `performance.py` | 成交、换月日志、资金曲线、绩效指标 | 回测结束后查看 |
| `scheduler.py` | `BAR` / `ROLL` / `SETTLE` 事件推进 | 否 |

## 数据来源

框架不内置行情库。`MockAdapter` 读本地 CSV/Parquet；`IpquantMysqlAdapter` 读外部
`ipquant` 库的既有表并映射成内部契约。换数据源只新增 Adapter，不改策略与引擎。

```yaml
data:
  adapter: ipquant_mysql
  options: {dsn: "mysql+pymysql://user:password@host:3306/ipquant"}
  underlyings: [RB, CU]
  start: 2023-01-01
  end: 2024-12-31
```

需要 `python -m pip install 'futures-backtest[mysql]'`。第三方 provider 也可以写成
`adapter: your_package.adapters:build`，工厂函数接收 `DataConfig` 并返回实现
`DataAdapter` 的对象。

## 关键配置项

| 配置 | 含义 |
|---|---|
| `routing.dominant_lag` | 交易日 T 使用 T-N 认定的主力；默认 1 |
| `routing.roll_timing` | 换月在新主力生效日的 `next_open` 还是 `same_close` 成交 |
| `routing.allow_signals_on_roll_day` | 换月日是否仍接受新信号；否则记入 `skipped_targets.csv` |
| `routing.lookahead_dominant` | 研究用宽松模式，必须与 `dominant_lag: 0` 同时设置，结果打标 |
| `routing.force_close_before_expiry_days` | 距最后交易日不足 N 天则强制平仓 |
| `execution.market_fill` | 信号在 `next_open` 还是 `same_close` 成交 |
| `execution.slippage_ticks` | 滑点，按最小变动价位计，方向始终不利于自己 |
| `execution.on_margin_short` | 保证金不足时 `reject` 还是 `scale`（缩手数） |
| `execution.enforce_price_limits` | 涨停不可买、跌停不可卖 |
| `execution.limit_fill_rule` | 限价单成交判定：`penetrate`（击穿才算，默认）或 `touch`（触及即算） |
| `execution.check_margin_on_submit` | 挂单时即按限价估算保证金，覆盖不了就拒单；默认开 |
| `execution.volume_participation` | 单笔最多吃掉一根 bar 成交量的比例；默认 `None`（不限） |
| `data.bar_freq` | `1d`（默认）/ `1m` / `5m` / `15m` / `30m` / `1h`；框架不重采样 |

### 日内回测（T+0）

`bar_freq` 设成日内周期即可在一天内任意时点开平仓——期货本来就没有 T+1 锁定，缺的
只是一天内的第二个决策点。每根 bar 都会调 `on_bar`，返回 `None` 表示不动作；要节流
用 `context.bars_seen` 自理。框架**不做重采样**，数据源给什么周期就回放什么周期，
配了日内周期却喂日线数据会直接报错。

每根 bar 的顺序：检验挂单簿 → 上一根 bar 的市价目标按本 bar 开盘价成交 → `on_bar`
决策 → 目标变了才撤旧单重挂。当日首根 bar 额外做今仓滚昨仓、`next_open` 换月与
到期强平，末根 bar 额外做 `same_close` 换月、撤光挂单与日终结算。结算仍是每交易日
一次。日线数据下首根即末根，行为与之前完全一致。

夜盘属于下一交易日，因此数据的 `trading_day` 必须按交易所口径标注，今昨仓切换点才
会落在夜盘开盘那一刻，见 [docs/data-contract.md](docs/data-contract.md)。

想用日线信号驱动分钟级执行，给 `context.history()` 传 `freq`：

```python
daily = context.history("RB", bars=20, symbol=context.trading_symbol("RB"), freq="1d")
```

游标所在的那个周期按已走完的部分返回——盘中确实知道"今日至此的最高价"，不是未来函数。

### 限价单

策略在 `TargetPosition` 上给出 `limit_price` 即为限价单，不给就是市价单：

```python
bar = context.bar("RB")
return TargetPosition(
    underlying="RB",
    net_lots=2,
    limit_price=bar.close - 2 * context.tick_size("RB"),
)
```

委托**存续到当日收盘**，逐根 bar 检验：跳空开在限价之内按开盘价成交，否则看最高/
最低价是否按 `limit_fill_rule` 达到限价。收盘仍未成交即撤单，信号作废并记入
`skipped_targets.csv`（`reason=limit_not_reached`），不顺延到下一日，也不支持
跨日 GTC。

目标没变就不重挂——否则持仓目标型策略每根 bar 重发同一目标会把挂单每分钟撤一次
重挂，成交率就成了 bar 周期的函数而非市场的函数。目标变了会撤旧单重挂
（`reason=superseded`），`same_close` 换月也会撤掉旧合约上的在途单
（`reason=cancelled_on_roll`）。

限价只作用于路由到当前主力合约的信号单，换月、到期强平以及旧合约上的残留清理单一律
走市价。因为决策当天的 bar 不能同时用来证明成交，`limit_price` 与
`execution.market_fill: same_close` 组合会直接报错。

## 输出

每次运行在 `output.root/<run_id>/` 下写：`orders.csv`、`fills.csv`、`rolls.csv`、
`events.csv`（`BAR` / `ROLL` / `SETTLE`）、`nav.csv`、`skipped_targets.csv`、
`trades.csv`、`metrics.json`、`metadata.json`、`config.json`。

`nav.csv` 的 `unrealized_pnl` 在日终通常为 0：结算把持仓成本重置为结算价，浮动盈亏
每天通过 `settlement_variation` 落进现金，这与交易所每日无负债结算一致。

`trades.csv` 是回合级记录（入场/出场时刻与价格、持仓时长、毛利、分摊手续费），
按合约与方向 FIFO 配对得到。**它与 `fills.csv` 口径不同，不可逐笔相减**：结算每天
把持仓成本重置为结算价，所以 `fills.csv` 的 `realized_pnl` 只含最后一段，之前各段
在 `nav.csv` 的 `settlement_variation` 里；`trades.csv` 是入场到出场的全程。只有
`sum(realized_pnl) + sum(settlement_variation) - sum(commission)` 才等于权益变化。

## 当前边界

- 一根 bar 的 OHLC 说不出高低点谁先发生。引擎按距开盘价远近推断路径（近的极值先
  到），这是**假设**，缩短周期只能减轻不能消除。
- 没有排队模型。`penetrate` 与 `volume_participation` 都只是排队失败与深度的粗糙
  代理，不表达排队位置。
- 挂单时校验的保证金**不冻结**，价格大幅不利变动后成交时仍可能不足。
- 不支持止损单与跨日 GTC，两者都需要显式的 TIF 字段。
- `events.csv` 行数随 bar 数线性增长，1m 一年约 6 万行。
- 策略看到的是**被路由合约的真实 bar**。用于特征的复权连续序列（signal view）属于
  Phase 2，届时仍不参与撮合。
- 主力映射表最好比回测区间多一段历史，否则回测首日没有"前一日认定"可用，框架会退用
  当日记录并在 `metadata.json` 的 `dominant_warmup_fallbacks` 里记下这一次妥协。
- 日终结算后若权益盖不住保证金，回测会**报错中止**（信息里给出权益与保证金），而不是
  模拟强制平仓。
- 未做：实盘下单、参数优化平台、盘口级部分成交。
