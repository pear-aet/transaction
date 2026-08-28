# 系统架构

## 1. 当前安全边界

当前确定性链路已经实现到本地影子和模拟执行：

```text
Data Engine
    ↓
Market Engine
    ↓
Strategy Engine
    ↓
StrategyIntent 持久日志
    ↓
Risk Engine
    ↓
RiskDecision 持久日志
    ↓
Execution Engine
    ↓
ExecutionCommand / Order / Update 持久日志
    ↓
Shadow 或 Local Simulation
```

当前仍未实现：

```text
真实交易所订单提交
真实改单与撤单
交易权限 API Key
交易所用户数据流
真实余额与持仓对账
生产级 Portfolio Engine
真实资金启用审批
```

因此系统仍然**没有真实交易权限**。Execution Engine 的加入只把风险批准后的影子计划、本地模拟订单状态、恢复和对账做成了正式、可持久化、可测试的层。

## 2. 总体组件关系

```text
Binance public REST / WebSocket
          │
          ▼
┌────────────────────────┐
│      Data Engine       │
│ 校验、增量同步、缺口修复 │
└────────────┬───────────┘
             │ 已持久化市场事实
             ▼
┌────────────────────────┐
│      Market Engine     │
│ 标准事件、排序、去重、背压│
└────────────┬───────────┘
             │ MarketEvent
             ▼
┌────────────────────────┐
│    Strategy Runtime    │
│ 关键订阅、耐久恢复、周期屏障│
└────────────┬───────────┘
             ▼
┌────────────────────────┐
│    Strategy Engine     │
│ 纯评估、时间切片、确定意图 │
└────────────┬───────────┘
             │ 单事务
             ▼
┌────────────────────────┐
│     StrategyStore      │
│ 评估、检查点、意图 outbox │
└────────────┬───────────┘
             │ persisted StrategyIntent
             ▼
┌────────────────────────┐
│      Risk Runtime      │
│ 关键订阅 + 耐久追赶      │
└────────────┬───────────┘
             ▼
┌────────────────────────┐
│       Risk Engine      │
│ 账户/行情快照、风险和杠杆 │
└────────────┬───────────┘
             │ 单事务
             ▼
┌────────────────────────┐
│       RiskStore        │
│ 决策、评估、批准 outbox   │
└────────────┬───────────┘
             │ approved RiskDecision
             ▼
┌────────────────────────┐
│   Execution Runtime    │
│ 关键订阅、耐久追赶、恢复   │
└────────────┬───────────┘
             ▼
┌────────────────────────┐
│    Execution Engine    │
│ 来源复核、行情复核、规划   │
└────────────┬───────────┘
             │ 单事务
             ▼
┌────────────────────────┐
│     ExecutionStore     │
│ 命令、订单、outbox、更新日志│
└────────────┬───────────┘
             │
      ┌──────┴────────┐
      ▼               ▼
 Shadow Record   Local SimulatedBroker
 无 broker 能力   无网络写入、确定性状态更新
```

三条硬边界：

1. Strategy Engine 不决定数量、杠杆、保证金或订单类型；
2. Risk Engine 不提交订单；
3. Execution Engine 当前不包含任何真实交易所写能力。

## 3. Data Engine

Data Engine 是市场事实的唯一写入口，负责：

- 合约K线、标记价格K线和资金费率；
- 过滤未闭合K线；
- OHLCV合法性、时间顺序、重复和缺口校验；
- 历史增量同步；
- WebSocket断线重连；
- 实时K线出现缺口时先用REST补齐，再保存当前K线；
- 已闭合K线和资金费率的不可变写入。

策略、风险和执行模块不得私自维护另一套历史K线定义。

详见 [Data Engine](DATA_ENGINE.md)。

## 4. Market Engine

Market Engine 把已验证市场事实转换为统一事件：

```text
candle_closed
mark_price
funding_rate
```

每条事件包含：

- 确定性事件ID；
- 逻辑事件时间；
- 交易所来源时间；
- 接收时间；
- 币种和周期分区；
- 进程内全局序号；
- 严格版本化载荷。

同一收盘时点，高周期K线优先于低周期K线排序。关键消费者使用 `BLOCK`；只有可替代展示数据才能显式丢旧或断开。

详见 [Market Engine](MARKET_ENGINE.md)。

## 5. Strategy Engine

Strategy Engine 只消费已持久化的合约闭合K线事件。每个实例固定绑定：

- 策略ID；
- 代码版本；
- 参数指纹；
- 高周期；
- 低周期。

核心入口：

```python
evaluate_strategy(StrategyContext) -> StrategyDecision
```

### 5.1 多周期闭合屏障

在1小时/15分钟配置下：

- 普通15分钟收盘使用最近已闭合1小时K线；
- 整点15分钟收盘必须等待同一整点1小时K线；
- 高周期缺失时低周期事件进入有上限待决队列；
- 超过上限直接失败，不使用陈旧趋势继续产生入场意图。

### 5.2 时间边界

高低周期历史严格按当前低周期事件自己的决策时间切片。未来K线、重复K线、顺序倒退、内部缺口和已闭合K线修订都会失败关闭。

### 5.3 原子结果

一次评估在同一个SQLite事务中写入：

```text
strategy_evaluations
strategy_intents（可为空）
strategy_checkpoints
```

同一市场事件重算出不同结果会被视为确定性破坏。

详见 [Strategy Engine](STRATEGY_ENGINE.md)。

## 6. StrategyIntent

StrategyIntent 只表达：

```text
方向意图
决策时间
参考价
失效价
来源行情
策略版本与参数
结构证据
```

它不表达：

```text
下单数量
杠杆
保证金
订单类型
成交状态
```

每条意图有永久 `storage_sequence`。历史恢复产生的意图标记为 `suppressed`，默认风险消费者只读取 `published` 意图。

StrategyIntent 的 `pending` 状态是耐久消费者硬屏障：不能因为它暂时不匹配终态过滤器就跳到后续记录。

## 7. Risk Engine

Risk Engine 是策略意图与执行计划之间的唯一风险审批边界。

核心入口：

```python
evaluate_risk(RiskContext) -> RiskDecision
```

输入包括：

- 已持久化 StrategyIntent；
- 不可变账户风险快照；
- 标记价格和交易规则快照；
- 版本化 RiskPolicy。

### 7.1 风险维度

入场审批同时限制：

- 单笔风险；
- 单币种风险；
- 组合总风险；
- 相关性分组风险；
- 总名义价值；
- 单方向名义价值；
- 可用余额；
- 交易所最小数量和最小名义价值；
- 维持保证金档位；
- 波动率缩放；
- 动态杠杆；
- 全仓强平缓冲；
- 日亏、回撤和连续亏损；
- 意图、账户和行情快照年龄；
- 参考价偏离。

### 7.2 决策有效期

每个批准决策都有确定性 `valid_until`。有效期由策略意图、账户快照和行情快照中最早失效者决定。

过期决策不能发布、恢复或重新进入待处理状态。

### 7.3 风险降低型退出

退出使用批准的完整数量并强制 `reduce_only=true`。账户熔断和陈旧账户/行情快照不能阻止减仓，但陈旧或未来策略意图仍然拒绝。

### 7.4 原子持久化和 outbox

RiskStore 在一个事务中写入：

```text
risk_instances
risk_evaluations
risk_decisions
```

批准决策初始进入 `pending` outbox；拒绝决策作为 `recorded` 审计事实。批准状态只允许：

```text
pending -> published
pending -> expired
```

Risk Engine 在向关键执行队列成功发布后才标记 `published`。

详见 [Risk Engine](RISK_ENGINE.md)。

## 8. RiskDecision

RiskDecision 表达：

```text
批准或拒绝
批准动作
数量和名义价值
杠杆和保证金
止损
reduce_only
风险预算与审批后敞口
账户、行情、策略和政策来源
valid_until
```

Execution Engine 不能修改这些风险边界。它只能在执行行情和执行策略允许范围内生成具体本地计划。

每条决策有永久 `storage_sequence`。默认执行消费者只耐久读取 `PUBLISHED + APPROVED` 决策。

## 9. Execution Engine

Execution Engine 是风险批准与本地订单状态之间的唯一规划边界。

核心入口：

```python
build_execution_command(ExecutionContext) -> ExecutionCommand
```

ExecutionContext 包含：

- 已持久化 RiskDecision；
- ExecutionMarketSnapshot；
- 版本化 ExecutionPolicy；
- 执行实例 ID；
- 评估时间。

### 9.1 来源复核

在规划和分发前复核：

- RiskStore 中是否存在相同 storage_sequence；
- 完整 RiskDecision 语义是否一致；
- 账户、币种、状态和批准动作是否匹配；
- 数量、价格、止损、杠杆、保证金和 reduce-only 是否一致；
- 执行有效期是否未超过风险有效期。

传入对象即使形式合法，只要不同于 RiskStore 不可变事实，也会失败关闭。

### 9.2 执行行情复核

规划检查：

- 未来行情；
- 陈旧行情；
- 买卖价差；
- 风险审批价偏离；
- bid/ask/mark 的方向性使用。

执行行情只用于判断当前本地计划是否仍然可接受，不能扩大 RiskDecision 批准的数量或风险。

### 9.3 命令和订单腿

入场命令包含：

```text
1 个主订单
1 个绑定主订单的 reduce-only 保护性止损
```

退出命令只包含一个 reduce-only 主订单。

命令 ID、订单 ID、客户端订单 ID 和模拟 broker ID 都是确定性的。

详见 [Execution Engine](EXECUTION_ENGINE.md)。

## 10. ExecutionStore

ExecutionStore 使用 SQLite WAL 保存：

```text
execution_instances
execution_commands
execution_orders
execution_order_updates
```

### 10.1 原子规划

执行实例、命令和全部初始订单腿在同一个事务中写入。任何 broker 操作都发生在持久化之后。

### 10.2 命令状态

```text
recorded      shadow 记录
pending       simulation 待本地分发
dispatched    本地 broker 已接收
completed     本地订单生命周期完成
expired       分发前超过有效期
failed        broker、保护腿或对账失败
```

### 10.3 订单更新

每条 OrderUpdate 先追加不可变日志，再更新当前订单快照。更新 ID 重复时必须保持完全相同语义。

## 11. Shadow 模式

Shadow 模式：

- 没有 broker；
- 命令持久化为 `recorded`；
- 不进入分发 outbox；
- 不产生 broker order ID 或成交；
- 只展示经过 Risk Engine 批准并已持久化的 ExecutionCommand。

旧 CLI 中的原始 Strategy Signal 只作为未风控意图展示：

```text
risk_approved=false
executable=false
orders=[]
```

它不能被误认为正式订单。

## 12. Simulation 模式

SimulatedBroker 是确定性的进程内适配器：

- 不连接交易所；
- 不读取交易密钥；
- 不包含网络写函数；
- 只接受已持久化的 simulation 命令；
- 显式产生分发、确认、部分成交、完全成交、拒绝和取消更新；
- 主订单部分或完全成交后激活保护腿；
- 所有对象强制 `submitted=false` 和 `network_write=false`。

模拟 broker 不会自动假设所有订单立刻成交。成交必须由测试、研究驱动器或未来独立撮合模型显式输入。

## 13. Execution Runtime

Execution Runtime：

1. 先订阅 RiskDecisionBus 关键 `BLOCK` 队列；
2. 恢复 ExecutionStore pending outbox；
3. 从 RiskReplayLedger 做单条耐久追赶；
4. 进入实时消费；
5. 周期冲刷 outbox 和上游追赶。

上游游标只在 ExecutionCommand 持久化后前进。即使本地 broker 随后失败，命令仍可从执行 outbox 恢复，无需重新规划和重新取行情。

多个 outbox 冲刷调用由同一异步锁串行化，避免重复模拟分发。

## 14. 上游发布竞态

Risk Engine 的顺序是：

```text
持久化批准决策
    ↓
发布到关键队列
    ↓
RiskStore 标记 published
```

Execution Runtime 可能在最后一步前收到对象，因此：

- 可以先持久化命令；
- RiskStore 源状态仍为 pending 时禁止 broker 分发；
- published 后周期恢复自动继续；
- expired 时执行命令也过期；
- 不会利用实时队列先到绕过耐久发布事实。

## 15. 订单状态机与保护腿

订单状态只允许显式单调转换：

```text
planned -> dispatched -> acknowledged
acknowledged -> partially_filled -> filled
acknowledged/partially_filled -> cancel_pending -> canceled
活动态 -> rejected / expired / failed
```

终态不能恢复。累计成交、金额和费用不能倒退。broker order ID 一旦建立不能替换。

保护腿被拒绝、过期、失败，或在存在主订单成交时被取消，执行命令进入 failed。

## 16. 对账

对账函数接收某个命令的完整 broker 账本快照，按确定性客户端订单 ID 比较。

明确失败包括：

- 本地活动订单在完整账本缺失；
- broker 出现本地未知订单；
- planned 订单被 broker 提前激活；
- broker order ID 改变；
- 状态或成交数据倒退；
- 非法终态变化。

合法向前变化转换为确定性 OrderUpdate。结构性差异会把命令标记为 failed 并抛出可审计错误，不会隐式创建、改单或撤单。

当前只对本地模拟 broker 账本进行对账。真实交易所只读账本适配器仍未实现。

## 17. 研究与模拟账户 EventEngine

`engine.py` 中的 EventEngine 是历史回测和模拟账户模型，不是 Market Engine、Risk Runtime 或 Execution Runtime。

它仍处理：

- 历史K线驱动；
- 手续费、滑点和资金费率；
- 全仓账户权益；
- 保守强平、止损和止盈顺序；
- 多币种共享资金。

EventEngine 的入场仓位计算已经路由到与 Risk Engine 相同的纯风险函数，但它的K线撮合模型仍是研究模拟，不等于 Execution Runtime 的订单状态日志。

## 18. 交付语义

当前实现保证：

```text
市场事实：不可变
市场事件：持久化后发布
策略评估/意图/检查点：单事务
策略意图 outbox：至少一次 + 幂等
风险评估/决策：单事务
风险批准 outbox：至少一次 + 有效期
执行命令/初始订单：单事务
执行命令 outbox：持久化后本地分发
订单更新：追加日志 + 当前快照单事务
耐久消费者：永久序号 + 固定过滤器 + pending 硬屏障
```

系统不声称跨所有进程、数据库和未来交易所 API 实现 exactly-once。每层通过确定性 ID、永久序号、幂等写入和对账缩小重复与丢失边界。

## 19. 故障原则

以下情况失败关闭：

- 市场数据缺口或历史修订；
- 事件、策略、风险或执行时间倒退；
- 策略上下文含未来数据；
- 高周期屏障无法满足；
- 策略、风险或执行实例身份改变；
- 同一来源事实重算出不同语义；
- 关键消费者缺失；
- RiskDecision 与 RiskStore 不一致；
- 执行行情陈旧、未来或偏离过大；
- 风险源尚未 published；
- 订单状态或累计成交倒退；
- 保护腿失效；
- 对账出现缺单、陌生单或身份冲突。

失败后不能通过手工改库继续同一实例。

## 20. 真实交易限制

本仓库当前不存在：

```text
真实订单提交函数
真实改单函数
真实撤单函数
交易权限密钥读取路径
签名交易请求
真实交易所用户数据流
```

真实交易必须等待：

- 独立 Portfolio Engine；
- 最小权限密钥管理；
- 真实交易所只读和写适配器；
- 速率限制、签名、时间同步和幂等重试；
- 用户数据流与REST双通道对账；
- 未知订单隔离；
- 余额和持仓对账；
- 人工批准与紧急停止；
- 长时间影子验证；
- 独立安全评审。

运行和恢复细节见 [Execution Engine 运行与恢复](EXECUTION_ENGINE_OPERATIONS.md)。
