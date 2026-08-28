# 系统架构

## 1. 当前安全边界

当前代码实现到：

```text
Data Engine
    ↓
Market Engine
    ↓
Strategy Engine
    ↓
StrategyIntent 持久日志
```

同时保留独立的研究/模拟账户引擎：

```text
历史K线
    ↓
纯策略函数
    ↓
EventEngine（费用、滑点、资金费率、仓位和全仓账户模拟）
```

尚未完成的生产链路包括：

```text
Risk Engine
Portfolio Engine
Execution Engine
交易所订单状态机
账户与交易所对账
完整监控和自动熔断编排
```

因此系统仍然没有真实订单提交、修改或撤销能力。

## 2. 组件关系

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
           │ StrategyIntent
           ▼
      Future Risk Engine
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

策略和风险模块不得自行请求交易所行情或自行维护另一套K线定义。

## 4. Market Engine

Market Engine 把已验证数据转换为统一事件：

```text
candle_closed
mark_price
funding_rate
```

每条事件有：

- 确定性事件ID；
- 逻辑事件时间；
- 交易所来源时间；
- 接收时间；
- 币种和周期分区；
- 进程内全局序号；
- 严格版本化载荷。

同一收盘时点，高周期K线优先于低周期K线排序。但实时交易所流仍可能先到达低周期事件，因此 Strategy Engine 还必须执行自己的多周期闭合屏障。

关键市场消费者使用 `BLOCK`；Dashboard等可替代展示数据才能使用丢旧或断开策略。

详见 [Market Engine](MARKET_ENGINE.md)。

## 5. Strategy Engine

Strategy Engine 只消费：

```text
price_type = contract
event_kind = candle_closed
```

每个策略实例固定绑定：

- 策略ID；
- 策略代码版本；
- 策略参数指纹；
- 高周期；
- 低周期。

同一个实例不能在中途切换规则并继续旧检查点。

### 5.1 多周期闭合屏障

在1小时/15分钟配置下：

- 普通15分钟收盘使用最近已经闭合的1小时K线；
- 整点15分钟收盘必须等待同一整点的1小时K线；
- 高周期缺失时低周期事件进入有上限的待决队列；
- 超过上限直接失败，不使用陈旧趋势。

### 5.2 无未来数据

策略调用前，高低周期历史都按当前低周期事件自己的决策时点切片。等待高周期期间到达的后续低周期K线不会泄漏给较早决策。

### 5.3 纯评估入口

```python
evaluate_strategy(StrategyContext) -> StrategyDecision
```

实时和回测共用该入口。策略函数不访问网络、数据库、系统时间、账户资金或随机数。

### 5.4 原子结果

每次低周期评估在一个SQLite事务中写入：

```text
strategy_evaluations
strategy_intents（可为空）
strategy_checkpoints
```

同一市场事件如果重算出不同决策，会被当成确定性破坏而拒绝。

详见 [Strategy Engine](STRATEGY_ENGINE.md)。

## 6. StrategyIntent 与后续风险层

StrategyIntent 表达：

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

这些必须由未来 Risk Engine、Portfolio Engine 和 Execution Engine 决定。

每条意图有永久 `storage_sequence`。未来 Risk Engine 应从 StrategyStore 耐久重放，并只在自身状态成功持久化后提交消费者游标。

历史补算产生的意图被标记为 `suppressed`，默认风险消费者只能重放 `published` 意图。

## 7. 研究与模拟账户 EventEngine

`engine.py` 中的 `EventEngine` 是回测和模拟账户引擎，不是 Market Engine，也不是 Strategy Engine。

它处理：

1. 读取下一根已闭合小周期K线；
2. 执行上一根收盘后产生的待处理信号；
3. 用标记价格检查全仓维持保证金；
4. 用合约K线按强平、止损、止盈的保守顺序检查退出；
5. 应用资金费率；
6. 更新账户权益、日亏、回撤和连续亏损状态；
7. 只暴露当时已经闭合的大周期K线；
8. 调用与实时相同的纯策略评估入口；
9. 生成下一根开盘才允许执行的研究信号。

多币种组合回测把所有币种K线合并为全局时间序列，并按 `(timestamp, symbol)` 稳定排序。所有仓位共享 `EngineState.cash`，不会重复使用本金。

## 8. 交付语义

当前实现的保证是：

```text
市场事实写入：不可变
市场事件进程内发布：有序、至少一次边界
策略评估与检查点：单事务
策略意图ID：确定性
策略outbox到关键队列：至少一次
策略意图耐久重放：永久序号 + 过滤器锁定游标
```

系统不声称跨全部进程、数据库、未来交易所API实现 exactly-once。

每一层都必须使用确定性ID和持久序号保证幂等。

## 9. 故障原则

以下情况一律失败关闭：

- 市场数据缺口不能完整修复；
- 已闭合市场事实发生历史修订；
- 市场或策略分区时间倒退；
- 策略上下文包含未来K线；
- 所需高周期收盘缺失；
- 策略实例版本或参数发生改变；
- 同一行情事件重算出不同决策；
- 策略意图没有匹配的关键消费者；
- 启动发现未处理历史且未显式选择恢复模式。

失败后不得继续产生新的入场意图。

## 10. 真实交易限制

`shadow.py` 仍然只能生成 `submitted=false` 的订单意图，模块中不存在网络提交函数。

真实交易必须等待 Risk Engine、Portfolio Engine、Execution Engine、订单状态机、对账和熔断层全部完成，并作为独立安全评审项目处理。
