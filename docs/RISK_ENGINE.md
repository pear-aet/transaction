# Risk Engine

Risk Engine 是 `StrategyIntent` 与未来 Execution Engine 之间的唯一风险审批边界。

```text
StrategyStore durable log
          |
          v
RiskRuntime durable catch-up + critical live subscription
          |
          v
RiskEngine
  account snapshot + market snapshot + RiskPolicy
          |
          v
pure evaluate_risk(RiskContext)
          |
          v
RiskStore atomic decision/evaluation commit
          |
          +--> rejected -> recorded audit fact
          |
          +--> approved -> pending outbox
                            |
                            v
                    RiskDecisionBus
                    critical BLOCK execution subscriber
```

本层不提交、修改或撤销交易所订单。

## 1. 非协商边界

1. 输入必须是已经持久化并带 `storage_sequence` 的 `StrategyIntent`。
2. 策略只能表达方向、参考价和失效价，不能指定数量、杠杆或保证金。
3. 风险计算必须是纯函数：

```python
evaluate_risk(RiskContext) -> RiskDecision
```

4. 同一风险实例与同一策略意图只能产生一个不可变风险决策。
5. 批准决策必须先持久化，之后才能进入执行队列。
6. 拒绝决策只做审计，不进入执行队列。
7. 已过期批准决策不能发布、恢复或重新变为待投递。
8. 减仓退出可以穿透账户熔断和陈旧账户/行情快照，但不能穿透陈旧或来自未来的策略意图。
9. 关键执行消费者必须使用阻塞式背压，不能丢弃批准决策。
10. Risk Engine 不拥有交易所下单权限。

## 2. 输入契约

### 2.1 StrategyIntent

Risk Engine 使用：

- `intent_id` 与永久 `storage_sequence`；
- `symbol`；
- `action`；
- `decision_time`；
- `reference_price`；
- `stop_price`；
- 策略版本和参数指纹。

Risk Engine 不信任策略给出的任何仓位建议。当前契约中也不存在数量、杠杆、保证金或订单类型字段。

### 2.2 AccountRiskSnapshot

账户快照包含：

- 账户ID与快照ID；
- 观测时间；
- 权益与可用余额；
- 当前维持保证金；
- 日初权益与历史峰值；
- 连续亏损次数；
- 当前持仓的方向、数量、标记价、初始风险和风险档位；
- 全局、当日和回撤熔断状态。

同一快照中禁止出现同币种重复仓位。

### 2.3 MarketRiskSnapshot

市场快照包含：

- 标记价格；
- 交易规则；
- tick size、step size、最小数量、最小名义价值；
- 最大杠杆；
- 连续且有序的维持保证金档位；
- 相关性分组；
- 可选年化波动率。

市场规则的币种必须与策略意图一致。

### 2.4 RiskPolicy

风险政策固定绑定：

- 单笔、单币种、组合和相关性分组风险；
- 总名义价值和单方向名义价值上限；
- 最大杠杆；
- 手续费、滑点、强平费与最低强平缓冲；
- 信号、账户和行情快照最大年龄；
- 最大参考价偏离；
- 日亏、账户回撤和连续亏损阈值；
- 波动率目标与最低风险缩放。

政策通过 SHA-256 指纹固定到风险实例。修改政策必须使用新的 `engine_instance_id`。

## 3. 评估顺序

入场审批按以下顺序执行：

1. 检查账户熔断、权益和可用余额；
2. 拒绝已有同币种仓位；
3. 检查意图与快照是否来自未来或已经陈旧；
4. 检查日亏、峰值回撤和连续亏损；
5. 检查参考价与当前标记价偏离；
6. 校验交易规则和维持保证金档位连续性；
7. 按保守方向计算滑点后审批价格；
8. 按保守方向取整止损价；
9. 计算单单位风险；
10. 计算风险预算和各风险剩余额度；
11. 对每个交易所风险档位计算可行候选；
12. 按 step size 向下取整数量；
13. 校验最小数量、最小名义价值、可用余额、杠杆和强平缓冲；
14. 生成不可变批准或拒绝决策。

任何一步不满足条件即失败关闭。

## 4. 风险与数量计算

### 4.1 单单位风险

```text
long price risk  = adverse fill - rounded stop
short price risk = rounded stop - adverse fill

risk per unit = price risk
              + fill price * (2 * fee rate + slippage rate)
```

数量使用完整往返费用，避免只按止损距离低估风险。

### 4.2 波动率缩放

```text
volatility scale = clamp(
    volatility target / observed annualized volatility,
    minimum scale,
    1.0
)
```

没有波动率数据时使用 `1.0`，不会虚构估计值。

### 4.3 风险预算

```text
base budget      = equity * risk_per_trade * volatility_scale
symbol room      = equity * max_symbol_risk    - current symbol risk
portfolio room   = equity * max_portfolio_risk - total open risk
group room       = equity * max_group_risk     - correlation group risk

risk budget = min(base budget, symbol room, portfolio room, group room)
```

任一剩余额度非正数即拒绝。

### 4.4 数量上限

候选名义价值同时受以下上限约束：

- 风险预算换算出的数量；
- 总名义价值剩余额度；
- 同方向名义价值剩余额度；
- 账户权益与杠杆上限；
- 可用余额与杠杆上限；
- 强平缓冲推导出的名义价值上限；
- 当前交易所维持保证金档位上限。

最终数量按交易所 `step_size` 向下取整，取整后初始风险不得超过批准预算。

## 5. 动态杠杆

Risk Engine 不把100倍当作目标，只把它当成绝对上限。

实际杠杆上限取以下最小值：

```text
政策最大杠杆
交易所最大杠杆
止损几何 + 费用 + 维持保证金 + 强平缓冲推导上限
波动率缩放后的杠杆上限
```

随后根据可用余额计算满足保证金要求的最小整数杠杆。若该杠杆超过上限，拒绝交易。

杠杆不会改变批准的初始风险预算，只改变保证金占用。

## 6. 全仓强平缓冲

审批后计算：

```text
projected equity = current equity - entry fee
maintenance after = current maintenance + new position maintenance

gross after = current gross notional + new notional

liquidation buffer ratio =
    (projected equity
     - maintenance after
     - gross after * liquidation fee rate)
    / gross after
```

结果必须不低于政策的 `min_liquidation_buffer`。

这是一条组合级约束，不是单仓近似。

## 7. 退出审批

`StrategyAction.EXIT` 只允许关闭现有同币种仓位：

- 自动映射为 `CLOSE_LONG` 或 `CLOSE_SHORT`；
- 使用完整现有数量；
- 必须 `reduce_only=true`；
- 不要求新增保证金；
- 不携带新止损价；
- 降低组合风险、名义价值和维持保证金。

账户熔断或账户/行情快照陈旧不会阻止风险降低型退出。但意图本身陈旧、来自未来或账户中没有该仓位时仍然拒绝。

## 8. RiskDecision

风险决策包含：

- 确定性 `decision_id`；
- 风险政策版本和指纹；
- 账户、行情与策略意图来源ID；
- 批准或拒绝状态；
- 批准动作；
- 数量、名义价值、杠杆和保证金；
- 止损价与审批价格；
- 各风险维度审批后数值；
- 强平缓冲；
- `valid_until`；
- 审计证据。

契约强制：

- `ENTER_LONG` 只能批准 `OPEN_LONG`；
- `ENTER_SHORT` 只能批准 `OPEN_SHORT`；
- `EXIT` 只能批准对应减仓关闭动作；
- 批准入场必须有保证金、止损和 `approved_entry` 原因；
- 批准退出必须为 reduce-only、零新增保证金和 `approved_exit` 原因；
- 拒绝项不能残留数量、名义价值、杠杆、保证金、风险预算或初始风险；
- 原因代码不能重复。

`evaluated_at` 和队列序号不参与决策语义ID，因此相同输入得到相同决策ID。

## 9. 持久化状态机

`RiskStore` 在一个事务中写入：

```text
risk_instances
risk_decisions
risk_evaluations
```

批准决策状态：

```text
pending -> published
pending -> expired
```

拒绝决策状态：

```text
recorded
```

禁止：

- `expired -> published`；
- `published -> expired`；
- 有效期结束前提前标记过期；
- 有效期结束后标记发布；
- 同一风险实例和意图写入不同决策；
- 中途修改账户或政策身份。

## 10. 实时投递

`RiskDecisionBus` 只接受：

- `approved`；
- 已持久化；
- 未过期；
- 尚未携带进程内发布序号的决策。

关键执行消费者必须：

```text
critical = true
backpressure = BLOCK
```

没有匹配的关键执行消费者时，批准决策不会进入任何队列，Risk Engine 抛出可见故障，数据库记录保持 `pending`。

Dashboard等非关键消费者可以显式选择 `drop_oldest` 或 `disconnect`。

## 11. 启动与恢复

Risk Engine 启动顺序：

1. 验证关键执行消费者存在；
2. 读取自身 `pending` outbox；
3. 未过期批准项重新投递；
4. 过期批准项转为 `expired`；
5. RiskRuntime 订阅实时 StrategyIntentBus；
6. 从 StrategyStore 耐久游标追上未处理 `published` 意图；
7. 进入实时循环。

同一策略意图再次出现时，Risk Engine 先查询已有决策：

- 不重新获取账户或行情快照；
- 不重新计算；
- 若仍为 pending，则只重试投递；
- 返回同一个不可变决策。

## 12. 耐久执行游标

`RiskReplayLedger` 为未来 Execution Engine 提供永久游标。

默认只暴露：

```text
delivery_state = published
```

拒绝和过期记录只有审计消费者显式修改过滤器才能读取。

游标保证：

- 过滤器指纹固定；
- 快照高水位固定；
- 不能提交陈旧批次；
- 不能跳出批次扫描范围；
- 不能超过决策日志最新序号；
- 不能倒退。

## 13. EventEngine 兼容层

研究和模拟账户的 `EventEngine` 不再维护第二套入场仓位公式。

它现在：

1. 把合法旧信号转换为确定性 `StrategyIntent`；
2. 从模拟账户状态构造 `AccountRiskSnapshot`；
3. 从当前开盘价与市场规则构造 `MarketRiskSnapshot`；
4. 调用相同的 `evaluate_risk()`；
5. 只按批准决策中的数量、审批价格、止损和初始风险开仓。

旧信号若连 StrategyIntent 契约都不满足，会记录 `invalid_strategy_intent` 并失败关闭。风险计算器本身抛出的异常不会被伪装成策略错误。

## 14. 健康与监控

必须监控：

- 收到、重复、批准和拒绝的意图数量；
- 决策持久化、发布和过期数量；
- pending outbox深度；
- 关键执行消费者数量与队列深度；
- 背压、断开和投递失败；
- 上游策略游标和下游执行游标；
- 最后意图、最后决策和最后错误。

以下情况应阻止新的入场审批：

- Risk Engine未完成启动；
- 关键执行消费者不存在；
- pending outbox持续增长；
- 账户或行情快照陈旧；
- 持久化失败；
- 策略或风险游标异常。

## 15. 当前边界

本阶段完成到：

```text
StrategyIntent
    -> RiskDecision
    -> persistent approved outbox
    -> critical execution-facing bus
    -> durable execution replay cursor
```

尚未实现：

- 真实交易所订单提交；
- 订单状态机；
- 部分成交与撤单重试；
- 交易所账户对账；
- 多策略净额和目标组合优化；
- 生产级 Execution Engine。

详见 [Risk Engine Acceptance Gates](RISK_ENGINE_ACCEPTANCE.md)。
