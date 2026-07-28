# Changelog

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
