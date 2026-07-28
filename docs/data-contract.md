# 框架内部数据契约

Adapter 负责把外部数据源转成本文档描述的标准表。Router / Matcher / Account 只依赖
这些表，策略完全不接触原始表结构。字段名固定，额外字段会保留但不参与撮合。

`MockAdapter` 从一个目录读取同名 CSV 或 Parquet 文件：

```text
sample_data/
├── bars.csv
├── contracts.csv
├── dominant_map.csv
├── settles.csv
├── charges.csv        # 可省略，缺失时按零费率
└── margins.csv        # 可省略，缺失时按 margins_default
```

同一张表不能同时存在 CSV 和 Parquet。

## 1. `bars` — 驱动回测与撮合

主键 `(symbol, datetime)`。

| 字段 | 必需 | 类型 | 含义 |
|---|---:|---|---|
| `symbol` | 是 | string | 具体月份合约，如 `RB2410` |
| `underlying` | 是 | string | 品种代码，如 `RB` |
| `datetime` | 是 | datetime | bar 结束时间 |
| `trading_day` | 是 | date | 归属交易日；夜盘归属次日 |
| `open` `high` `low` `close` | 是 | float | 有限且大于 0 |
| `volume` | 是 | number | 有限且大于等于 0 |
| `open_interest` | 否 | number | 持仓量 |
| `upper_limit` | 否 | float/null | 当日涨停价；空值表示不做检查 |
| `lower_limit` | 否 | float/null | 当日跌停价；空值表示不做检查 |

`datetime` 与 `trading_day` 分离，明确夜盘归属。`volume=0` 的 bar 拒绝成交。

## 2. `contracts` — 合约元数据

主键 `symbol`。

| 字段 | 必需 | 类型 | 含义 |
|---|---:|---|---|
| `symbol` | 是 | string | 合约代码 |
| `underlying` | 是 | string | 品种代码 |
| `multiplier` | 是 | float | 合约乘数（点值），大于 0 |
| `tick_size` | 是 | float | 最小变动价位，大于 0 |
| `exchange` | 否 | string | 交易所 |
| `expire_date` | 否 | date | 最后交易日 |

每个出现在 `bars` 里的 `symbol` 都必须在此表中，且 `underlying` 必须一致。

## 3. `dominant_map` — 主力映射

主键 `(trading_day, underlying)`。这是 Router 决定"交易谁"的唯一依据。

| 字段 | 必需 | 类型 | 含义 |
|---|---:|---|---|
| `trading_day` | 是 | date | 该主力认定所属的交易日 |
| `underlying` | 是 | string | 品种代码 |
| `dominant_symbol` | 是 | string | 当日认定的主力合约 |

**框架默认用 T-1 的记录决定 T 日交易哪个合约**（`routing.dominant_lag: 1`）。
换月事件由相邻交易日的 `dominant_symbol` 变化推导，不需要外部单独提供 roll 表。

因此这张表**应该比回测区间多一段历史**：回测首日也需要"前一日的认定"。如果表里
没有区间之前的记录，框架只能退而使用首日当天的记录，并把这次妥协写进运行元数据的
`dominant_warmup_fallbacks`，而不是假装它是干净的 T-1 路由。

## 4. `settles` — 日终结算价

主键 `(symbol, trading_day)`。用于 `SETTLE` 事件盯市。

| 字段 | 必需 | 类型 | 含义 |
|---|---:|---|---|
| `symbol` | 是 | string | 合约代码 |
| `trading_day` | 是 | date | 交易日 |
| `settle_price` | 是 | float | 结算价，有限且大于 0 |

持仓合约在某交易日缺结算价时，回退到该合约当日最后一根 bar 的 `close`，
并在运行元数据里累计 `settle_fallbacks` 计数。

## 5. `charges` — 手续费

可按品种或按合约给出，可随日期变化。至少一列费率必须存在。

| 字段 | 必需 | 类型 | 含义 |
|---|---:|---|---|
| `underlying` | 二选一 | string | 品种级费率 |
| `symbol` | 二选一 | string | 合约级费率，优先于品种级 |
| `trading_day` | 否 | date | 生效日；空值表示长期有效 |
| `open_fee_rate` | 否 | float | 开仓按成交额比例 |
| `open_fee_per_lot` | 否 | float | 开仓按手固定费用 |
| `close_fee_rate` | 否 | float | 平昨按成交额比例 |
| `close_fee_per_lot` | 否 | float | 平昨按手固定费用 |
| `close_today_fee_rate` | 否 | float | 平今按成交额比例；缺失时回退平昨口径 |
| `close_today_fee_per_lot` | 否 | float | 平今按手固定费用；缺失时回退平昨口径 |

同一笔成交的费用 = `notional * rate + lots * per_lot`，两种口径可同时存在。
选取规则：先匹配 `symbol`，否则匹配 `underlying`；再取 `trading_day <= 当日` 的最后一条。

## 6. `margins` — 保证金率

字段与选取规则同 `charges`。

| 字段 | 必需 | 类型 | 含义 |
|---|---:|---|---|
| `underlying` | 二选一 | string | 品种级 |
| `symbol` | 二选一 | string | 合约级 |
| `trading_day` | 否 | date | 生效日 |
| `long_margin_rate` | 是 | float | 多头保证金率，`(0, 1]` |
| `short_margin_rate` | 否 | float | 空头保证金率；缺失时等于多头 |

单个合约的保证金占用 = `lots * multiplier * price * margin_rate`，
其中 `price` 为盘中最新成交价、日终为结算价。

## 7. 校验与常见错误

`futures-backtest validate --config ...` 会检查并输出适配器、数据指纹、区间与品种：

- `missing required table` ：文件名或根目录错误。
- `missing columns` ：缺少上表中的必需字段。
- `duplicate keys` ：同一主键出现多行。
- `must be finite and positive` ：价格、乘数、tick 为空或小于等于 0。
- `bar symbol missing from contracts` ：`bars` 出现未登记的合约。
- `dominant symbol has no contract` ：主力映射指向未登记的合约。
- `dominant map does not cover` ：回测区间内某交易日缺主力记录。
- `margin rate must fall in (0, 1]` ：保证金率越界。

## 8. 数据版本与可复现

`data.data_version` 应在任何输入数据变化时变更。省略时 `MockAdapter` 会对全部输入
文件内容计算 SHA-256 并生成版本号，写入运行目录的 `metadata.json`，
使同配置 + 同数据的两次运行可比对。
