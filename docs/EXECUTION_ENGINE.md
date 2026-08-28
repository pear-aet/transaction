# Execution Engine

## 1. 作用与边界

Execution Engine 是 `RiskDecision` 与订单生命周期之间的唯一执行规划边界。

当前实现只支持：

```text
shadow
simulation
```

当前实现明确不支持：

```text
真实交易所下单
真实改单
真实撤单
交易权限 API Key
任何交易所网络写请求
```

`shadow` 只生成并持久化经过风险审批的执行计划；`simulation` 只调用进程内 `SimulatedBroker`，不会连接交易所。

所有执行命令、订单和模拟更新都强制携带：

```json
{
  "submitted": false,
  "network_write": false
}
```

这两个字段在契约层校验，不依赖调用者自觉。

## 2. 处理链路

```text
RiskStore 中已持久化 RiskDecision
              │
              ▼
┌────────────────────────┐
│   Execution Runtime    │
│ 关键订阅 + 耐久追赶     │
└────────────┬───────────┘
             │ approved RiskDecision
             ▼
┌────────────────────────┐
│    Execution Engine    │
│ 来源复核、行情复核、规划 │
└────────────┬───────────┘
             │ 单事务
             ▼
┌────────────────────────┐
│     ExecutionStore     │
│ 命令、订单、outbox、日志 │
└────────────┬───────────┘
             │
      shadow │ simulation
             │
       ┌─────┴─────────────┐
       ▼                   ▼
持久化影子报告       Local SimulatedBroker
无 broker 能力       无网络、确定性订单更新
```

Execution Runtime 先订阅实时 `RiskDecisionBus`，再从 `RiskReplayLedger` 做耐久追赶，关闭实时与恢复之间的竞态窗口。

## 3. 输入要求

Execution Engine 只接受：

- `RiskDecisionStatus.APPROVED`；
- 已写入 RiskStore 且具有正数 `storage_sequence`；
- 账户 ID 与执行实例一致；
- RiskStore 中的完整语义与传入对象一致；
- 风险决策尚未过期；
- 风险决策方向、数量、止损、杠杆、保证金和 `reduce_only` 可被原样约束到执行命令。

输入决策即使字段形式合法，只要与 RiskStore 中同序号事实不一致，也会失败关闭。

## 4. 执行行情快照

规划时需要 `ExecutionMarketSnapshot`：

```text
symbol
observed_at
mark_price
bid_price
ask_price
```

规划器检查：

- 行情不能来自未来；
- 行情不能超过执行策略允许的最大年龄；
- 买入使用 ask、卖出使用 bid；缺少盘口时才回退 mark；
- 买卖价差不能超过策略上限；
- 执行价相对风险审批价不能超过允许偏离；
- 命令有效期不得超过上游 RiskDecision 的 `valid_until`。

行情检查失败时生成不可执行处置，不会进入模拟 broker。

## 5. ExecutionPolicy

ExecutionPolicy 固定执行实例的行为，核心参数包括：

- `mode`：`shadow` 或 `simulation`；
- 行情最大年龄；
- 最大允许价差；
- 相对风险审批价的最大偏离；
- 客户端订单 ID 前缀；
- 策略 ID、版本和确定性策略指纹。

同一个 `engine_instance_id` 不能在旧数据上切换账户、策略版本、策略指纹或模式。需要改变这些字段时必须使用新的执行实例 ID。

## 6. ExecutionCommand

ExecutionCommand 记录：

```text
来源 RiskDecision ID 与 storage_sequence
账户和币种
风险审批动作
参考价、审批价与执行规划价
数量、杠杆和所需保证金
止损与 reduce_only
创建时间与 valid_until
执行策略身份
订单腿
确定性证据
```

命令 ID 由不可变风险决策、执行策略和执行快照语义确定性生成。

永久存储序号不参与命令语义，因此恢复后的同一命令不会因为数据库序号不同而改变身份。

## 7. 订单腿

### 7.1 入场

入场命令包含：

1. 一个主订单；
2. 一个绑定主订单 ID 的保护性止损订单。

保护性止损：

- `reduce_only=true`；
- 初始状态为 `planned`；
- 只有主订单发生部分成交或完全成交后才激活；
- 激活后由模拟 broker 进入 `dispatched -> acknowledged`；
- 保护腿被拒绝、过期、失败，或在已有成交时被取消，会把命令标记为失败。

### 7.2 退出

风险降低型退出只生成一个主订单：

```text
reduce_only=true
无新止损
无新增保证金
数量等于 RiskDecision 批准数量
```

Execution Engine 不允许退出命令增加敞口。

## 8. 订单状态机

订单状态包括：

```text
planned
    ↓
dispatched
    ↓
acknowledged
    ├──→ partially_filled ──→ filled
    ├──→ cancel_pending ────→ canceled
    ├──→ rejected
    ├──→ expired
    └──→ failed
```

约束包括：

- 状态只能沿允许路径单调前进；
- 终态不能回到活动态；
- 累计成交数量、成交金额和费用不能倒退；
- 成交数量不能超过请求数量；
- `partially_filled` 必须实际增加累计成交量；
- 累计成交金额必须与累计数量和平均成交价一致；
- broker order ID 一旦建立不能被替换；
- 相同 update ID 只能对应完全相同的更新语义。

## 9. ExecutionStore

ExecutionStore 使用 SQLite WAL，保存：

```text
execution_instances
execution_commands
execution_orders
execution_order_updates
```

### 9.1 原子命令提交

一个事务内完成：

- 固定或校验执行实例身份；
- 写入 ExecutionCommand；
- 写入全部初始订单腿；
- 建立命令 outbox 状态。

模拟 broker 只能在事务提交成功后看到并处理命令。

### 9.2 命令投递状态

```text
recorded      shadow 已记录
pending       simulation 待本地分发
dispatched    主订单已由本地 broker 接收
completed     订单生命周期完整结束
expired       分发前超过 valid_until
failed        broker、保护腿或对账失败
```

允许的主要转换：

```text
pending -> dispatched -> completed
pending -> expired
pending -> failed
dispatched -> failed
```

状态转换带时间单调校验。失败原因在重复写入时也不能改变。

### 9.3 更新日志

每条 OrderUpdate 先追加到不可变日志，再更新当前订单快照。重复 update ID 返回已有事实；同 ID 不同语义直接冲突。

## 10. Shadow 模式

Shadow 模式：

- 不配置 broker；
- 命令落库后状态为 `recorded`；
- 不进入 outbox；
- 不产生 broker order ID；
- 不生成成交；
- `execution_shadow_report()` 只接受已持久化的 shadow ExecutionCommand。

旧 `shadow.py` 仍保留 CLI 兼容入口，但未经过 Risk Engine 的原始 Signal 只会显示为：

```text
legacy_strategy_signals
risk_approved=false
executable=false
orders=[]
```

它们不能再被误称为正式订单。

## 11. Simulation 模式

SimulatedBroker 是进程内确定性适配器：

- 没有 HTTP、WebSocket、CCXT 写调用或签名逻辑；
- 不读取交易权限密钥；
- 只接受已持久化的 `DISPATCH_PENDING` 命令；
- 生成确定性的 broker order ID 与 update ID；
- 支持分发、确认、部分成交、完全成交、拒绝和取消；
- 所有更新继续强制 `submitted=false` 和 `network_write=false`。

模拟成交必须由测试、研究驱动器或未来独立的撮合模型显式输入，broker 不会凭空把所有订单立即成交。

## 12. 上游发布屏障

Risk Engine 会先将批准决策持久化，再向关键队列发布，最后把 RiskStore 状态标记为 `published`。实时消费者可能在最后一步之前收到对象。

Execution Engine 因此执行双重检查：

1. 可以先把命令持久化为 `pending`；
2. 在 RiskStore 源记录仍为 `pending` 时，outbox 停止分发；
3. 源记录变为 `published` 后，周期恢复会自动继续；
4. 源记录已 `expired` 时，执行命令也进入 `expired`；
5. 不会因为实时消息先到而绕过上游发布事实。

## 13. 耐久恢复

Execution Runtime：

- 使用关键 `BLOCK` 订阅；
- 默认只消费 `PUBLISHED + APPROVED` 风险决策；
- 过滤器与消费者名称锁定；
- `pending` 风险记录是硬游标屏障；
- replay batch 固定为单条，确保命令持久化与游标提交的故障边界清晰；
- 上游游标只在命令持久化后前进；
- broker 分发失败时命令保持 `pending`，重启后直接恢复，不重新规划、不重新取行情；
- 周期任务同时冲刷执行 outbox 和上游耐久追赶；
- 多个并发 outbox 调用由同一异步锁串行化。

## 14. 对账

`reconcile_execution_orders()` 接收某个命令的完整 broker 订单账本快照。

对账按确定性客户端订单 ID 比较：

- 已分发本地订单在完整 broker 账本中缺失；
- broker 出现本地未知订单；
- 本地仍为 `planned`，broker 却已出现；
- broker order ID 变化；
- 状态倒退；
- 成交数量、金额或费用倒退；
- 非法终态变化。

可合法向前推进的差异被转换为确定性 OrderUpdate。任何结构性差异都会：

1. 生成可审计的 ReconciliationReport；
2. 将执行命令标记为 `failed`；
3. 抛出 `ExecutionReconciliationError`；
4. 不隐式创建、修改或取消任何订单。

当前 broker 为本地模拟 broker，因此对账用于验证持久化状态与完整模拟账本。真实交易所对账需要新的只读交易所适配层和单独安全评审。

## 15. 健康与指标

Execution Engine 健康快照包含：

- 收到与重复风险决策数；
- 规划、持久化、影子记录和待处理命令数；
- 分发、过期、完成与失败命令数；
- OrderUpdate 数；
- 保护腿激活数；
- 对账运行、更新和失败数；
- 最近风险决策、执行命令和错误；
- outbox 深度和最老 pending 年龄；
- 上游耐久游标；
- 固定 `network_write_enabled=false`。

## 16. 当前未实现

以下能力不在本阶段：

- 真实 Binance 下单、改单和撤单；
- 交易权限 API Key 管理；
- 交易所用户数据流；
- 真实订单速率限制与签名重试；
- 真实交易所只读订单账本适配器；
- 真实账户余额和持仓对账；
- 未知订单的人工处置工作流；
- 生产级 Portfolio Engine；
- 真实资金启用审批。

这些能力不能通过把 `SimulatedBroker` 替换成网络客户端直接获得，必须作为新的安全阶段实现和评审。
