# 系统架构

## 1. 当前安全边界

当前生产链路实现到：

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
RiskDecision 持久日志与关键执行队列
```

尚未实现：

```text
Execution Engine
交易所订单状态机
部分成交与撤单重试
账户与交易所对账
真实订单提交、修改和撤销
```

因此当前系统仍没有真实交易权限。

## 2. 总体组件关系

```text
Binance REST / WebSocket
          │
          ▼
┌─────────────────────┐
│     Data Engine     │
│ 校验、增量同步、补洞 │
└──────────┬──────────┘
           │ 已持久化闭合数据
           ▼
┌─────────────────────┐
│    Market Engine    │
│ 标准事件、排序、去重 │
│ 背压、统一进程内序号 │
└──────────┬──────────┘
           │ MarketEvent
           ▼
┌─────────────────────┐
│   Strategy Runtime  │
│ 关键BLOCK行情订阅者  │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│   Strategy Engine   │
│ 多周期屏障、时间切片 │
│ 纯函数评估、恢复控制 │
└──────────┬──────────┘
           │ 单事务
           ▼
┌─────────────────────┐
│    StrategyStore    │
│ 评估、检查点、意图日志│
└──────────┬──────────┘
           │ pending outbox
           ▼
┌─────────────────────┐
│ StrategyIntentBus   │
│ 关键匹配、阻塞式背压 │
└──────────┬──────────┘
           │ persisted StrategyIntent
           ▼
┌─────────────────────┐
│     RiskRuntime     │
│ 实时订阅 + 耐久追赶   │
└──────────┬──────────┘
           ▼
┌─────────────────────┐
│     Risk Engine     │
│ 账户/行情快照 + 政策 │
│ 纯审批、仓位和杠杆   │
└──────────┬──────────┘
           │ 单事务
           ▼
┌─────────────────────┐
│      RiskStore      │
│ 决策、评估、过期状态 │
└──────────┬──────────┘
           │ approved pending outbox
           ▼
┌─────────────────────┐
│  RiskDecisionBus    │
│ 关键执行BLOCK订阅者  │
└──────────┬──────────┘
           │ approved RiskDecision
           ▼
    Future Execution Engine
```

## 3. Data Engine

Data Engine 是市场事实的唯一写入口，负责：

- 合约K线、标记价格K线和资金费率；
- 过滤未闭合K线；
- OHLCV合法性、时间顺序、重复与缺口校验；
- 历史增量同步；
- WebSocket断线重连；
- 实时K线出现缺口时先REST补洞，再保存当前K线；
- 已闭合K线与资金费率的不可变写入。

策略和风险模块不得自行请求交易所行情或维护另一套K线定义。

## 4. Market Engine

Market Engine 把已验证数据转换为统一事件：

```text
candle_closed
mark_price
funding_rate
```

每条事件有确定性ID、逻辑事件时间、来源时间、接收时间、分区、版本化载荷和进程内序号。

同一收盘时点，高周期K线优先于低周期K线排序。关键消费者使用 `BLOCK`；只有可替代展示数据才能使用丢旧或断开策略。

详见 [Market Engine](MARKET_ENGINE.md)。

## 5. Strategy Engine

Strategy Engine 只消费已持久化的合约闭合K线事件。每个实例固定绑定策略ID、代码版本、参数指纹和高低周期。

核心入口：

```python
evaluate_strategy(StrategyContext) -> StrategyDecision
```

### 5.1 多周期屏障

1小时/15分钟配置下，整点15分钟收盘必须等待同一整点的1小时收盘；高周期缺失时低周期事件进入有上限的待决队列，不能使用旧趋势将就运行。

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
数量
杠杆
保证金
订单类型
成交状态
```

每条意图有永久 `storage_sequence`。历史补算意图标记为 `suppressed`，默认风险消费者只能耐久读取 `published` 意图。

## 7. Risk Engine

Risk Engine 是策略意图与执行层之间的唯一审批边界。

核心入口：

```python
evaluate_risk(RiskContext) -> RiskDecision
```

输入包括：

- 已持久化 StrategyIntent；
- 不可变账户风险快照；
- 标记价格与交易规则快照；
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
- 信号、账户与行情快照年龄；
- 参考价偏离。

### 7.2 决策有效期

每个批准决策都有确定性 `valid_until`。有效期由策略意图、账户快照和行情快照三者中最早失效者决定。

过期决策不能发布、恢复或重新进入待处理状态。

### 7.3 风险降低型退出

退出使用完整现有数量并强制 `reduce_only=true`。账户熔断和陈旧账户/行情快照不能阻止减仓，但陈旧或未来策略意图仍然拒绝。

### 7.4 原子持久化

RiskStore 在一个事务中写入：

```text
risk_instances
risk_decisions
risk_evaluations
```

批准项进入：

```text
pending -> published
pending -> expired
```

拒绝项永久为：

```text
recorded
```

同一风险实例与同一策略意图只能有一个不可变决策。账户或政策身份改变必须使用新实例ID。

详见 [Risk Engine](RISK_ENGINE.md)。

## 8. 风险投递与耐久恢复

RiskDecisionBus 只接受已经持久化、批准、未过期且未携带进程内序号的决策。

关键执行消费者必须使用：

```text
critical = true
backpressure = BLOCK
```

没有匹配的关键消费者时，批准决策保持 `pending` 并抛出可见故障。

Risk Engine 启动时：

1. 验证关键执行消费者；
2. 重试仍有效的 pending 决策；
3. 把过期 pending 决策改为 expired；
4. 订阅实时 StrategyIntentBus；
5. 通过 StrategyReplayLedger 追赶未处理意图；
6. 进入实时循环。

下游 `RiskReplayLedger` 默认只向未来 Execution Engine 暴露 `published` 决策，拒绝和过期记录必须通过显式审计过滤器读取。

## 9. 研究与模拟账户 EventEngine

`engine.py` 中的 EventEngine 仍负责回测和模拟账户状态，但不再维护第二套入场风险公式。

有效旧信号会被转换为确定性 StrategyIntent，随后构造账户与市场快照并调用同一个 `evaluate_risk()`。EventEngine 只执行 RiskDecision 中的：

- 数量；
- 审批价格；
- 止损；
- 初始风险；
- 批准杠杆。

不合法旧信号在 StrategyIntent 契约处失败关闭。风险计算器异常不会被误报为策略信号错误。

回测的下一根开盘执行、标记价格强平、止损优先、资金费率和组合共享现金语义保持不变。

## 10. 交付语义

当前实现的保证：

```text
市场事实：不可变
市场事件：进程内有序、明确背压
策略评估与意图：单事务 + 确定性ID
策略意图耐久重放：永久序号 + 过滤器锁定游标
风险评估与决策：单事务 + 确定性ID
批准决策outbox：持久化后投递 + 有效期
风险决策耐久重放：永久序号 + 过滤器锁定游标
```

系统不声称跨所有进程、数据库和未来交易所API实现 exactly-once。每一层通过确定性ID、永久序号和消费者幂等实现至少一次边界。

## 11. 故障原则

以下情况一律失败关闭：

- 市场数据缺口或历史修订；
- 市场、策略或风险分区时间倒退；
- 策略上下文包含未来或不连续K线；
- 高周期收盘缺失；
- 策略或风险实例版本、参数、账户或政策漂移；
- 同一输入重算出不同决策；
- 策略意图或风险决策没有匹配关键消费者；
- 风险意图、账户或行情快照陈旧；
- 交易所风险档位不连续；
- 风险、名义价值、保证金或强平缓冲不足；
- 耐久游标过滤器改变、批次陈旧或越界。

失败后不得继续批准新的入场。

## 12. 真实交易限制

`shadow.py` 仍只能生成 `submitted=false` 的订单意图，代码中不存在真实交易所提交函数。

真实交易必须等待 Execution Engine、订单状态机、部分成交处理、撤单重试、账户对账和生产熔断编排完成，并作为独立安全评审项目处理。
