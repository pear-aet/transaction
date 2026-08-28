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
