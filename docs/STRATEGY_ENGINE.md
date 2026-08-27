# Strategy Engine

## 1. 职责边界

Strategy Engine 只负责把已经验证、已经收盘、已经持久化的市场事件转换为可审计的策略意图。

它负责：

- 维护每个币种的高周期和低周期已闭合K线窗口；
- 在严格决策时点上执行纯策略函数；
- 执行 `1h` 趋势、`15m` 触发的多周期闭合屏障；
- 生成确定性的 `StrategyIntent`；
- 原子保存评估记录、检查点和意图；
- 通过持久 outbox 恢复“已落库但尚未入总线”的意图；
- 为后续 Risk Engine 提供永久递增的意图序号和耐久重放游标。

它不负责：

- 计算下单数量；
- 决定杠杆；
- 分配保证金；
- 校验组合风险；
- 创建、修改或撤销订单；
- 判断真实成交。

因此 `StrategyIntent` 中不存在 `quantity`、`leverage`、`margin`、`order_type` 或交易所订单字段。

## 2. 数据流

```text
MarketEventBus
    │
    │ 只订阅 contract + candle_closed
    │
    ▼
StrategyRuntime（BLOCK关键订阅者）
    │
    ▼
StrategyEngine
    ├─ 检查币种、周期、序号和闭合时间
    ├─ 更新高/低周期窗口
    ├─ 执行多周期闭合屏障
    ├─ 按 decision_time 切断未来数据
    └─ 调用 evaluate_strategy(StrategyContext)
              │
              ▼
       StrategyDecision
              │
              ▼
       StrategyIntent
              │
              ▼
StrategyStore 单事务
    ├─ strategy_evaluations
    ├─ strategy_checkpoints
    └─ strategy_intents（永久 storage_sequence）
              │
              ▼
StrategyIntentBus
    │ 仅在匹配的关键 BLOCK 消费者成功入队后标记 published
    ▼
Risk Engine（下一阶段）
```

市场数据必须先通过 Data Engine 与 Market Engine 的校验、补洞和持久化。Strategy Engine 不接受未闭合K线，也不直接解析交易所原始 WebSocket JSON。

## 3. 决策时间与未来数据

### 3.1 统一定义

对于一根开盘时间为 `t`、周期长度为 `Δ` 的K线：

```text
decision_time = t + Δ
```

`StrategyContext` 只允许包含满足以下条件的K线：

```text
candle.open_time + candle_interval <= decision_time
```

任何收盘时间晚于 `decision_time` 的高周期或低周期K线都会被拒绝。

### 3.2 等待期间仍不能看到未来

当低周期K线先到、高周期K线后到时，低周期事件会进入待决队列。高周期事件到达后，Strategy Engine 不是把“当前全部历史”直接交给策略，而是再次按该低周期事件自己的 `decision_time` 切片。

因此，即使等待期间已经收到后续15分钟K线，较早的待决事件仍然看不到这些后续数据。

## 4. 1小时/15分钟闭合屏障

对于一个15分钟决策时点 `T`，所需的1小时闭合边界为：

```text
required_high_close = floor(T / 1h) × 1h
```

例子：

```text
20:45 的15分钟收盘 → 允许使用截至20:00闭合的1小时K线
21:00 的15分钟收盘 → 必须等待21:00闭合的1小时K线
21:15 的15分钟收盘 → 允许使用截至21:00闭合的1小时K线
```

如果21:00的15分钟K线先到，而21:00的1小时K线尚未到达，策略不能先运行。低周期事件只会进入待决队列。

待决队列有硬上限。高周期事件长期缺失并使队列超过上限时，系统抛出 `StrategyBarrierError` 并停止该运行链路，而不是继续使用陈旧趋势。

## 5. 纯策略函数

实时与回测共用：

```python
evaluate_strategy(StrategyContext) -> StrategyDecision
```

旧的 `decide(...)` 只是兼容适配器，它会构造同一个 `StrategyContext` 后调用 `evaluate_strategy`。

`StrategyContext` 包含：

- 截止决策时点已经闭合的低周期K线；
- 截止决策时点已经闭合的高周期K线；
- 完整策略配置；
- 决策时间；
- 当时的持仓方向。

策略函数不访问数据库、网络、系统时间、账户余额或随机数。相同上下文必须产生相同决策。

当前注册策略：

- `chan_fractal`；
- `chan_third_point`；
- `donchian`；
- `ema_pullback`。

每个策略都有固定 `strategy_id`、代码版本、来源、许可证和最小预热长度。

## 6. StrategyIntent 契约

每条意图至少包含：

```text
intent_id
strategy_id
strategy_version
instance_id
symbol
action
decision_time
reference_price
stop_price
reason
source_event_id
source_sequence
high_timeframe
low_timeframe
parameter_hash
evidence
storage_sequence
sequence
schema_version
```

### 6.1 两种序号

`storage_sequence`：

- SQLite永久自增序号；
- 进程重启后仍然存在；
- Risk Engine 应以它作为耐久重放和检查点依据。

`sequence`：

- 当前进程内 StrategyIntentBus 的发布顺序；
- 只用于进程内排队和监控；
- 不能替代 `storage_sequence`。

### 6.2 确定性ID

`intent_id` 由以下语义字段生成：

- 策略ID与版本；
- 实例ID；
- 币种；
- 动作；
- 决策时间；
- 参考价与失效价；
- 原因；
- 来源市场事件ID；
- 参数指纹。

创建时间、总线序号和存储序号不参与ID。因此，进程重启后对同一决策重算，仍会得到同一 `intent_id`。

### 6.3 失效价约束

```text
enter_long  → stop_price 必须低于 reference_price
enter_short → stop_price 必须高于 reference_price
exit        → 不允许携带 stop_price
```

策略信号时间戳还必须与来源低周期K线的开盘时间完全相同，防止把一个旧信号错误绑定到新的市场事件。

## 7. 参数与版本锁定

每个 `(instance_id, symbol)` 检查点同时锁定：

```text
strategy_id
strategy_version
parameter_hash
high_timeframe
low_timeframe
```

如果同一个实例名称下修改策略代码版本、入场模型、回看长度、分型间隔、止损缓冲或周期，启动会直接失败。

修改策略规则必须使用新的 `instance_id`。这可以防止新规则从旧规则的检查点继续运行，并把两套策略的历史混在一起。

风险参数和杠杆参数不进入策略参数指纹，因为它们属于后续 Risk Engine，而不是 Strategy Engine。

## 8. 原子评估日志

每个低周期决策由同一个SQLite事务写入：

1. `strategy_evaluations`：该市场事件产生了什么决策；
2. `strategy_intents`：如果有信号，保存完整意图；
3. `strategy_checkpoints`：推进该实例/币种的最后决策位置。

事务失败时三者全部回滚。

同一决策时点再次提交时，系统会比较：

- 市场事件ID；
- 策略签名；
- 评估模式；
- 是否产生意图；
- 意图ID与语义。

如果同一市场事件第一次有信号、第二次无信号，或者意图内容发生改变，系统抛出 `StrategyIntentConflict`，不会把差异当成普通重复消息。

## 9. 意图交付状态

每条持久意图只有三种状态：

### `pending`

已与检查点原子落库，但尚未成功进入匹配的关键 StrategyIntentBus 订阅者。

### `published`

已经成功进入至少一个匹配的关键、阻塞式订阅队列。

这只表示“入进程内关键队列”，不表示：

- 风险审批通过；
- 已创建订单；
- 已成交；
- 已获得交易所确认。

### `suppressed`

历史恢复过程中重算出的旧意图。它保留在永久审计日志中，但不会进入实时风险或执行链路。

## 10. 关键订阅与背压

StrategyIntentBus 支持：

- `BLOCK`；
- `DROP_OLDEST`；
- `DISCONNECT`。

关键消费者必须：

```text
critical = true
policy = BLOCK
```

Risk Engine 和未来的执行控制链路必须是关键消费者。

Dashboard 等展示消费者必须显式设置 `critical=false`，才允许使用 `DROP_OLDEST`。

如果某条意图没有匹配的关键消费者：

- 不分配成功发布结果；
- 不进入去重集合；
- 不标记为 `published`；
- 保留在持久 outbox；
- Strategy Engine 抛出 `StrategyDeliveryError`。

## 11. 启动与恢复

### 11.1 先订阅，再启动

StrategyRuntime 先以关键 `BLOCK` 模式订阅 MarketEventBus，然后才执行历史加载和恢复。启动期间到达的市场收盘事件会排在订阅队列中，不会落入“加载完成前的空窗”。

### 11.2 首次启动

首次启动没有检查点：

- 如果最新低周期收盘仍处于配置的新鲜窗口内，立即评估一次；
- 如果历史已经过旧，只建立 `baseline` 检查点，不补发陈旧信号；
- 无论哪种情况，都必须满足高周期闭合屏障。

### 11.3 默认恢复：FAIL

如果数据库中的低周期历史已经超过检查点，默认抛出 `StrategyReplayRequired`。系统不会猜测停机期间的持仓状态，也不会直接跳过未处理事件。

### 11.4 REPLAY

显式选择 `REPLAY` 时，调用方必须提供历史时点持仓查询函数。

每个遗漏事件都会：

- 使用该事件自己的决策时点切片；
- 重新执行相同纯策略函数；
- 写入不可变评估和意图日志；
- 将产生的意图标记为 `suppressed`；
- 不发布到实时 StrategyIntentBus。

因此恢复不会拿数小时前的入场意图去开当前仓位。

### 11.5 RESET

显式选择 `RESET` 时，检查点推进到最新低周期收盘并记录 `reset` 模式。遗漏决策不会重算。

这是有数据损失含义的人工恢复方式，不应成为默认启动参数。

## 12. 下游耐久重放

`StrategyReplayLedger` 为下一层提供持久游标。

默认 `DurableIntentFilter` 只允许：

```text
delivery_state = published
```

因此 Risk Engine 默认看不到 `suppressed` 历史意图。

审计消费者如果需要历史补算记录，必须显式加入：

```text
delivery_state = suppressed
```

耐久消费者名称会绑定过滤器哈希。相同消费者名称不能更换币种、动作、实例或交付状态过滤条件。

批次读取返回：

- 开始序号；
- 读取时的高水位；
- 实际扫描到的序号；
- 匹配意图；
- 被过滤跳过的数量。

下游只有在整批匹配意图全部处理成功后，才能提交 `scanned_through`。旧批次或倒退序号会被拒绝。

## 13. 交付语义

当前边界提供：

```text
评估事务：原子
意图ID：确定性
持久日志：不可变
outbox到进程内关键队列：至少一次
下游耐久重放：至少一次
```

系统不声称跨 Strategy Engine、Risk Engine、数据库和未来交易所之间实现 exactly-once。

下游处理器必须使用 `intent_id` 或 `storage_sequence` 保持幂等，并在自身状态成功持久化后提交耐久游标。

## 14. 监控指标

Strategy Engine 健康快照包含：

- 是否完成bootstrap；
- 市场事件接收、忽略和重复数量；
- 高/低周期事件数量；
- 屏障等待和待决队列深度；
- 预热跳过、实际评估和信号数量；
- 意图持久化、发布和抑制数量；
- outbox恢复数量；
- replay/reset数量；
- 最近市场事件、决策和意图ID；
- 最近错误。

StrategyIntentBus 还包含：

- 关键与非关键订阅者；
- 队列深度和容量；
- 阻塞次数；
- 丢弃与断开数量；
- 缺少关键匹配的拒绝次数；
- 最新进程内序号。

以下情况应阻止后续新开仓：

- `StrategyBarrierError`；
- `StrategyReplayRequired`；
- `StrategyCheckpointError`；
- `StrategyIntentConflict`；
- `StrategyDeliveryError`；
- 持久 outbox 长时间不为零；
- 关键消费者断开或持续积压。

## 15. 安全边界

本阶段仍不包含任何真实订单提交代码。StrategyIntent 是待 Risk Engine 审查的交易意图，不是订单。

在 Risk Engine、Portfolio Engine、Execution Engine、账户对账、熔断和长期影子验证完成前，不应把策略意图直接映射到交易所API。
