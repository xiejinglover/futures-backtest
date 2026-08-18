# Changelog

## 未发布

## 0.2.1 - 2026-08-18

- **分钟级聚合提速**：`history(freq=..., bars=N)` 改为增量折叠，每条源 bar 只处理一次，
  并且只构造请求的末尾 N 条聚合结果；60 日分钟数据的逐 bar 日线查询约提速 42 倍。
- 普通 `history()` 避免不必要的全量索引复制，同时保留连续索引返回约定。

## 0.2.0 - 2026-08-18

- **盘中条件单**：`TargetPosition` 新增 `stop_price` 与 `time_in_force`，支持 bar 级
  stop-market / stop-limit 及 DAY / GTC；跳空按开盘触发，否则按 stop 触发并对
  stop-market 施加滑点。订单与成交输出新增稳定的 `order_id`。
- **异步订单生命周期**：市价目标只在本品种下一根可用 bar 消费，其他品种的全局时间槽
  不再导致丢单；条件单按具体合约跨 bar 保存，部分成交保留剩余手数。
- **异步移仓**：`next_open` 换月会等待新旧合约首次同时有可交易 bar；全天无双边报价
  则明确报错，不再要求全市场首个时间槽恰好齐全。
- **日内回测（T+0）**：`data.bar_freq` 放开为 `1d / 1m / 5m / 15m / 30m / 1h`，
  调度器改为逐 bar 回放，一天内可在任意时点开平仓。首根 bar 做今仓滚昨仓、换月与
  到期强平，末根 bar 做收盘换月、撤单与结算，结算仍每交易日一次。日线数据下每天
  一根 bar，产出与 0.1.0 逐字节一致。框架不重采样，配了日内周期却喂日线数据会报错。
- **挂单簿**：限价单存续到当日收盘并逐根 bar 检验，不再是"当根 bar 有效"。目标没变
  就不重挂，变了撤旧重挂（`superseded`），`same_close` 换月撤旧合约在途单
  （`cancelled_on_roll`）。
- **`trades.csv`**：回合级记录，含入场/出场时刻与价格、持仓时长、毛利与分摊手续费；
  指标新增 `round_trips` / `round_trip_win_rate` / `average_holding_minutes`。
  注意它与 `fills.csv` 是两套口径，见 README。
- **`history(freq=...)`**：把原始 bar 聚合成更粗周期，供日线信号 + 分钟执行的策略；
  按交易日而非自然日划分，游标所在周期按已走完的部分返回。
- **`history()` 提速**：改为在预排序切片上二分，从每次全表过滤变为对数查找。
- **行为变更** — 成交价压回 `[bar.low, bar.high]`：滑点不再能把成交推到 bar 之外。
  日线上罕见，1m 上收在极值是常态。
- **行为变更** — `execution.check_margin_on_submit`（默认开）：覆盖不了的限价单在
  挂单阶段即以 `insufficient_margin` 拒掉，不再挂上去等成交时才发现。
- **新增** `execution.volume_participation`：单笔最多吃掉一根 bar 成交量的比例，
  超出部分 `partial`，压到 0 手以 `no_liquidity` 拒单；默认关闭。
- **移除** `data.history_bars`：从未被任何代码读取的死配置。
- **新增日内示例**：`examples/mock_intraday.yaml` + `examples/sample_data_intraday/`
  （15m、含夜盘、5 个交易日、一次换月），策略
  `futures_backtest.contrib.strategies:IntradayRangeBreakout` 开盘区间突破、收盘前
  平光。已接入 CI。

## 0.1.0

首个版本：品种层信号进、具体合约成交出的日频期货回测框架，以 MIT 许可开源，
发布到 PyPI（`pip install futures-backtest`）。

- **策略接口**：`on_bar(context) -> TargetPosition | None`，只面向 `underlying`；
  `context.history` 拒绝返回当前 bar 之后的数据，`context.trading_symbol` 只读查询
  当前被路由的合约。
- **主力路由**：默认 `dominant_lag: 1`，交易日 T 使用 T-1 认定的主力；
  `lookahead_dominant` 必须与 `dominant_lag: 0` 同时显式设置，且结果元数据打标。
  区间之前没有主力历史时退用首日记录，并记入 `dominant_warmup_fallbacks`。
- **换月**：由主力映射的日变化推导，`next_open` 或 `same_close` 成交，平旧开新写入
  独立的 `rolls.csv`，成本计入 `metrics.roll_cost`；换月日可选择不接受新信号。
- **撮合**：tick 对齐（方向永远不利于自己）、涨跌停拒单、零成交量拒单、整数手、
  保证金不足 `reject` 或 `scale`、开仓/平昨/平今分别计费。
- **账户**：多空分别记账，开仓占用保证金而非扣除全额名义本金，日终按结算价盯市并把
  变动落进现金。
- **数据适配**：`MockAdapter`（目录 CSV/Parquet）、`IpquantMysqlAdapter`（映射外部
  `ipquant` 库既有表）、`module:factory` 自定义 provider。
- **输出**：`orders` / `fills` / `rolls` / `events` / `nav` / `skipped_targets` 与
  `metrics.json`、`metadata.json`、`config.json`；同配置同数据可复现。
- **边界**：仅日线回放，`bar_freq: 1m` 直接报错而非静默采样；结算后权益盖不住保证金
  时中止回测而不模拟强平。
- **随包示例**：`futures_backtest.contrib.strategies` 提供 `BuyAndHoldUnderlying` 与
  `MovingAverageCross`，`pip install` 后配一份 YAML 即可运行；示例级代码，无稳定性承诺。
- **工程化**：MIT LICENSE 与完整包元数据、`py.typed`、`ruff` 规则、GitHub Actions
  在 Python 3.11/3.12/3.13 上跑测试与示例、tag 触发经 PyPI Trusted Publishing 发布。
