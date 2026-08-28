# Execution Engine 运行与恢复

## 1. 运行前提

当前 Execution Engine 只能运行在：

```text
shadow
simulation
```

任何部署都必须满足：

- 不提供交易权限 API Key；
- 不注入真实订单网络客户端；
- broker 模块中没有 HTTP、WebSocket 或 CCXT 写调用；
- 健康快照中的 `network_write_enabled` 必须始终为 `false`；
- RiskStore、ExecutionStore 和耐久游标使用可持久化磁盘；
- 系统时钟不能明显倒退；
- 执行实例 ID、账户 ID、模式和策略版本由部署配置固定。

如果目标是连接真实交易所，本手册不适用。真实执行需要新的阶段、独立权限模型和安全评审。

## 2. 推荐启动顺序

```text
1. 初始化 Data / Market / Strategy / Risk Store
2. 启动 ExecutionStore
3. 创建 ExecutionPolicy
4. 创建 MarketSnapshotProvider
5. shadow: broker=None
   simulation: broker=SimulatedBroker()
6. 创建 ExecutionEngine
7. 创建 ExecutionRuntime
8. 启动 runtime.run()
```

Execution Runtime 启动时会：

1. 先订阅 RiskDecisionBus 的关键 `BLOCK` 队列；
2. 恢复 ExecutionStore 中的 `pending` outbox；
3. 从 RiskReplayLedger 做耐久追赶；
4. 进入实时消费循环；
5. 按固定周期继续冲刷 outbox 与耐久追赶。

不得先追赶后订阅，否则可能漏掉追赶水位之后、实时订阅之前到达的决策。

## 3. 模式选择

### 3.1 Shadow

适用：

- 验证风险决策到执行计划的映射；
- 检查数量、订单方向、保护腿和客户端订单 ID；
- 观察真实行情条件下的计划产生频率；
- 不需要任何成交模拟。

配置要求：

```python
policy = ExecutionPolicy(mode=ExecutionMode.SHADOW)
broker = None
```

Shadow 命令持久化为 `recorded`，不进入 outbox。

### 3.2 Simulation

适用：

- 验证 outbox 恢复；
- 验证主订单、部分成交、保护腿、取消、拒绝和对账；
- 测试异常恢复和幂等性；
- 为未来撮合模型提供订单状态契约。

配置要求：

```python
policy = ExecutionPolicy(mode=ExecutionMode.SIMULATION)
broker = SimulatedBroker()
```

SimulatedBroker 是本地对象，不连接交易所。

## 4. 数据库文件

建议在同一持久卷中保存但使用清晰命名：

```text
risk.db
execution.db
```

如果多个 Store 共用同一 SQLite 文件，也必须保留：

```text
WAL
foreign_keys=ON
busy_timeout
稳定文件锁语义
```

不要把数据库放在容器临时层或会被自动清理的目录。

## 5. ExecutionStore 关键表

```text
execution_instances
execution_commands
execution_orders
execution_order_updates
```

排障时首先查看：

```sql
SELECT delivery_state, count(*)
FROM execution_commands
GROUP BY delivery_state;
```

查看待恢复命令：

```sql
SELECT storage_sequence, command_id, source_decision_id, symbol, delivery_state
FROM execution_commands
WHERE delivery_state = 'pending'
ORDER BY storage_sequence;
```

查看某个命令订单：

```sql
SELECT order_id, client_order_id, leg_type, state, broker_order_id, updated_at
FROM execution_orders
WHERE command_id = ?
ORDER BY leg_type, order_id;
```

查看订单更新历史：

```sql
SELECT storage_sequence, update_id, prior_state, new_state, event_time
FROM execution_order_updates
WHERE order_id = ?
ORDER BY storage_sequence;
```

数据库只用于诊断，不应直接手工修改状态。手工 SQL 更新会绕过状态机与审计日志。

## 6. 正常恢复场景

### 6.1 风险决策已持久化但尚未 published

现象：

- ExecutionCommand 已进入 `pending`；
- outbox 不分发；
- `source_barriers` 增加。

处理：

- 不要删除命令；
- 检查 Risk Engine 是否仍在完成关键队列发布；
- RiskStore 源记录变为 `published` 后，周期冲刷会自动恢复；
- 如果源记录最终变为 `expired`，执行命令也应过期。

### 6.2 命令已持久化，broker 分发前进程退出

现象：

- 命令状态为 `pending`；
- 订单状态仍为 `planned`；
- 上游耐久游标可能已经前进，因为命令已经安全落库。

处理：

- 使用相同 ExecutionStore、实例 ID、账户、策略和模式重启；
- bootstrap 直接冲刷旧 outbox；
- 不重新规划，不重新获取执行行情，不生成新的 command ID。

### 6.3 broker 生成更新后、应用全部更新前进程退出

SimulatedBroker 的 update ID 是确定性的。重启后重复应用同一更新：

- 完全相同语义返回 duplicate；
- 同 ID 不同语义触发 `ExecutionUpdateConflict`；
- 不会重复增加累计成交。

### 6.4 上游耐久追赶被 pending 风险记录阻塞

现象：

- 后续风险记录已经 published；
- Execution Runtime 游标仍停在前序；
- 没有生成后续命令。

这是正确行为。前序记录未进入终态时，游标不能越过。周期追赶会在屏障释放后按顺序继续。

## 7. 过期处理

执行命令有效期不超过 RiskDecision 的 `valid_until`。

如果命令在分发前过期：

```text
pending -> expired
```

不得：

- 延长已有命令有效期；
- 重新使用旧执行行情；
- 把 expired 改回 pending；
- 通过新 storage_sequence 伪装成新命令。

需要重新交易必须由新的市场事实、策略意图和风险决策重新走完整链路。

## 8. 保护腿故障

主订单发生部分或全部成交后，保护腿必须激活。

以下情况命令进入 failed：

- 保护腿 rejected；
- 保护腿 expired；
- 保护腿 failed；
- 已有主订单成交时保护腿被 canceled；
- 保护腿身份或激活父订单不一致；
- 对账发现保护腿缺失或未知替代订单。

当前系统没有真实交易所撤单或应急平仓能力，因此 failed 只代表明确故障事实。真实资金环境必须另有人工和自动应急处置层，不能复用本地模拟逻辑假装风险已解除。

## 9. 对账操作

对账输入必须是某个命令的**完整 broker 订单账本快照**，不是增量事件片段。

```python
report = await engine.reconcile_broker_ledger(
    command_id,
    observed_orders,
    observed_at=timestamp,
)
```

合法向前变化会生成确定性 OrderUpdate；结构性差异会：

```text
命令 -> failed
抛出 ExecutionReconciliationError
保留 ReconciliationReport
```

必须报警的差异：

- 本地活动订单在完整账本缺失；
- 出现未知客户端订单 ID；
- planned 订单被 broker 提前激活；
- broker order ID 改变；
- 状态倒退；
- 成交数量、成交金额或费用倒退；
- 终态恢复为活动态。

对账失败后不得通过手工改库继续运行同一个执行实例。

## 10. 停止流程

推荐停止顺序：

1. 停止新的 Strategy/Risk 输入；
2. 调用 `ExecutionRuntime.stop()` 关闭关键订阅；
3. 等待 `run()` 返回；
4. 读取最终健康快照；
5. 备份 SQLite 主文件、WAL 和 SHM 文件；
6. 停止进程。

不要强制终止正在提交 SQLite 事务的进程。即使 WAL 能恢复，频繁硬杀会增加排障复杂度。

## 11. 健康检查

至少监控：

```text
runtime.running
runtime.failures
engine.failures
store.pending_commands
store.failed_commands
store.expired_commands
oldest_pending_age_ms
source_barriers
reconciliation_failures
upstream replay offset
critical subscription 状态
network_write_enabled
```

建议报警条件：

- `network_write_enabled != false`：立即停止部署；
- pending 年龄持续超过策略有效期；
- failed 命令新增；
- reconciliation_failures 新增；
- 上游游标长期不前进且存在 published 风险记录；
- critical subscriber 消失；
- SQLite 锁超时或磁盘空间不足。

## 12. 实例身份变更

以下任一变化都必须使用新 `engine_instance_id`：

- account_id；
- ExecutionPolicy ID 或版本；
- ExecutionPolicy 指纹；
- shadow/simulation 模式。

禁止在旧实例上切换模式。例如不能把已经积累 shadow 命令的实例直接改成 simulation。

## 13. 备份与恢复验证

备份前应执行：

```sql
PRAGMA wal_checkpoint(FULL);
PRAGMA integrity_check;
```

恢复副本后至少验证：

- `integrity_check` 返回 `ok`；
- execution_commands 与 execution_orders 外键完整；
- 每个 update 的 order_id 可解析；
- 没有终态命令回到 pending；
- 耐久游标不超过 RiskStore 最新序号；
- 使用 shadow 模式做只读启动演练；
- `network_write_enabled=false`。

## 14. 禁止操作

不要：

- 直接把网络交易客户端注入 SimulatedBroker；
- 在本阶段读取 Binance 交易权限密钥；
- 手工把 `submitted` 改为 true；
- 跳过 RiskStore 来源复核；
- 越过 pending 上游游标屏障；
- 删除失败命令来恢复“绿色”指标；
- 手工重写累计成交数据；
- 对不完整 broker 快照执行完整对账；
- 将单元测试通过解释为真实交易可用。

## 15. 上线真实资金前仍缺少

```text
真实交易所只读账户适配器
真实订单提交/改单/撤单适配器
最小权限密钥管理
签名、时间同步、限速和幂等重试
用户数据流与 REST 双通道对账
未知订单隔离
持仓和余额对账
人工批准与紧急停止
独立 Portfolio Engine
长时间 shadow 观测与故障演练
真实交易安全评审
```

在这些能力完成前，系统只能用于研究、影子计划和本地模拟。
