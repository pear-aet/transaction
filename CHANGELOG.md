# Changelog

## Unreleased

### Market Engine

- 新增版本化 `candle_closed`、`mark_price` 和 `funding_rate` 事件。
- 分离逻辑收盘时间、交易所来源时间和接收时间。
- 新增确定性事件ID、全局进程序号、分区陈旧检查和重复抑制。
- 新增显式 `block`、`drop_oldest`、`disconnect` 背压策略。
- 关键行情消费者禁止静默丢事件。
- 校验、缺口修复和不可变持久化成功后才发布事件。
- CCXT和SQLite操作移出asyncio事件循环。
- 新增Runtime、Stream、Data Engine和订阅者健康快照。

### Strategy Engine

- 新增无副作用 `evaluate_strategy(StrategyContext)`，回测与实时共用。
- 新增1小时/15分钟多周期闭合屏障和决策时点历史切片。
- 新增确定性、版本化 `StrategyIntent`；策略输出不包含数量或杠杆。
- 新增策略实例的ID、代码版本、参数指纹和周期锁定。
- 新增不可变评估日志、事务检查点和持久意图outbox。
- 新增永久 `storage_sequence` 与过滤器锁定的耐久重放游标。
- 新增关键阻塞式意图消费者约束。
- 历史补算意图标记为 `suppressed`，默认风险重放不可见。
- 首次启动只评估新鲜收盘；默认发现历史缺口时失败关闭。
- 新增FAIL、REPLAY和RESET恢复模式。
- 新增决策不确定性、参数漂移、未来数据、周期屏障、启动竞态、outbox恢复和耐久重放测试。

### Risk Engine

- 新增纯函数 `evaluate_risk(RiskContext)` 和严格版本化 `RiskDecision`。
- 新增不可变账户、持仓、市场规则和风险政策快照。
- 新增单笔、单币、组合、相关性分组、总名义价值和方向敞口限制。
- 新增手续费、滑点、止损、交易所步长、最小订单和维持保证金档位处理。
- 新增波动率缩放、动态杠杆、可用余额和全仓强平缓冲检查。
- 新增日亏、回撤、连续亏损、陈旧/未来数据和参考价偏离拒绝。
- 新增风险降低型 reduce-only 退出。
- 新增风险实例、评估、决策和批准outbox单事务持久化。
- 新增 `pending -> published` / `pending -> expired` 单调状态。
- 新增关键执行队列、永久storage sequence、耐久游标和启动恢复。
- 新增决策来源重算、ID/有效期复核和执行数学不变量。
- EventEngine 入场仓位计算迁移到相同纯风险函数。

### Execution Engine

- 新增严格的 ExecutionPolicy、执行行情、命令、订单和更新契约。
- 所有影子和模拟对象强制 `submitted=false`、`network_write=false`。
- 新增 RiskStore 完整来源语义复核和上游published硬屏障。
- 新增执行行情年龄、未来时间、价差和风险审批价偏离检查。
- 新增确定性命令ID、订单ID、客户端订单ID和本地模拟broker ID。
- 新增主订单与激活绑定的 reduce-only 保护性止损计划。
- 新增执行实例、命令、初始订单腿和outbox单事务持久化。
- 新增 recorded、pending、dispatched、completed、expired、failed 命令状态。
- 新增单调订单状态机和追加式 OrderUpdate 审计日志。
- 新增并发outbox串行化、分发失败重启恢复和不重新规划语义。
- 新增 RiskDecision 关键订阅、耐久追赶和周期恢复。
- 新增无网络 SimulatedBroker，支持确认、部分成交、完全成交、拒绝和取消。
- 新增部分/全部成交后的保护腿激活和保护失效故障传播。
- 新增完整本地broker账本对账；缺单、陌生单、身份变化和状态倒退失败关闭。
- 旧shadow CLI不再把未风控Signal描述为正式订单；正式影子报告只接受已持久化ExecutionCommand。
- 新增Execution设计、运行恢复和验收门槛文档。
- 仍然没有真实下单、改单、撤单、交易权限密钥或交易所网络写能力。

## 0.2.0 - 2026-08-27

- 统一回测和模拟盘事件引擎。
- 新增多币种全仓账户、组合风险和全局停止。
- 新增标记价格、币安过滤规则和分层维持保证金适配。
- 新增 SQLite 增量仓库、事件、任务和状态恢复。
- 新增确定性线段、背驰和一类/三类点。
- 选优升级为隔离最终测试的 expanding walk-forward。
- 新增成本与蒙特卡洛压力测试、PWA、认证、限流、日志和 CI。
- 影子模式明确禁止订单提交；仍无实盘功能。
- CI 按 Python 版本解析兼容依赖，关闭矩阵 fail-fast，并以 Bandit 取代私有仓库不可用的 CodeQL。
