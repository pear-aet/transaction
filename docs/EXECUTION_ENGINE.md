# Execution Engine

Execution Engine 是已批准 `RiskDecision` 与未来真实交易所适配器之间的唯一执行边界。

当前阶段只支持：

```text
RiskStore durable approved decision
          |
          v
ExecutionRuntime durable catch-up + critical live subscription
          |
          v
ExecutionEngine
  execution market snapshot + ExecutionPolicy
          |
          v
pure build_execution_command(ExecutionContext)
          |
          v
ExecutionStore atomic command + order-leg commit
          |
          +--> shadow -> recorded audit fact
          |
          +--> simulation -> pending durable outbox
                                  |
                                  v
                         local SimulatedBroker
                                  |
                                  v
                    append-only order update journal
                                  |
                                  v
                      command-state reconciliation
```

本层没有交易所认证、真实下单、改单或撤单能力。

## 1. 非协商边界

1. 输入必须是已经持久化、状态为 `APPROVED` 的 `RiskDecision`。
2. Execution Engine 不能重新决定方向、数量、止损、杠杆或保证金。
3. 同一执行实例与同一风险决策只能产生一个不可变执行命令。
4. 执行命令和初始订单腿必须先原子持久化，之后才能进入任何分发过程。
5. Shadow 模式永久记录 `submitted=false` 和 `network_write=false`。
6. Simulation 模式只允许本地确定性 broker，不允许网络客户端。
7. 订单状态、累计成交数量、累计成交金额和累计费用只能单调前进。
8. 终态订单不能重新激活。
9. 保护性止损的累计成交量不能超过主订单已经形成的实际敞口。
10. 已经存在持久化提交证据的命令不能因进程重启或 TTL 到期而被误判为从未提交。
11. 父命令每次状态变化必须写入持久化审计日志。
12. 真实交易所适配器必须在独立阶段接受新的安全审查，不能直接扩展当前本地 broker。

## 2. 输入契约

### 2.1 RiskDecision

Execution Engine 重新核验：

- `decision_id` 与永久 `storage_sequence`；
- 账户ID与币种；
- 批准动作；
- 数量；
- 参考价格；
- 止损价格；
- 杠杆；
- 保证金；
- `reduce_only`；
- `valid_until`；
- RiskStore 中保存的完整不可变语义。

运行时收到的对象必须与 RiskStore 中同序号记录完全一致。调用方不能伪造一个看似合法但并非 RiskStore 原始事实的风险决策。

### 2.2 ExecutionMarketSnapshot

执行行情快照包含：

- 快照ID；
- 币种；
- 观测时间；
- 标记价格；
- 可选买一价；
- 可选卖一价；
- 上游序号。

买入优先使用卖一价，卖出优先使用买一价；盘口缺失时才回退标记价格。

以下情况失败关闭：

- 快照来自未来；
- 快照超过最大允许年龄；
- 币种与风险决策不同；
- 执行价格相对风险审批价格偏离超过政策上限。

### 2.3 ExecutionPolicy

执行政策固定绑定：

- `policy_id`；
- 版本；
- `shadow` 或 `simulation` 模式；
- 行情最大年龄；
- 最大执行价格偏离；
- 命令 TTL；
- 是否强制保护性止损；
- 客户端订单ID前缀。

政策通过 SHA-256 指纹固定到执行实例。修改政策或模式必须使用新的 `engine_instance_id`。

## 3. 执行命令

### 3.1 确定性身份

命令ID由以下不可变语义生成：

```text
execution instance
execution policy fingerprint
source risk decision ID and storage sequence
market snapshot ID
mode
approved action
quantity
validity window
selected execution price
```

同一输入必须生成相同命令ID。语义不同却复用同一身份时，存储层直接拒绝。

每个订单腿也有确定性：

```text
command ID + leg type -> order ID
command ID + leg type -> exchange-safe client order ID
```

重启、重复投递和耐久重放不会创建第二组逻辑订单。

### 3.2 入场命令

入场必须包含：

```text
PRIMARY market order
PROTECTIVE_STOP stop-market order
```

约束：

- 主订单方向必须与批准动作一致；
- 主订单数量必须等于风险批准数量；
- 主订单不得 `reduce_only`；
- 保护性订单方向必须与主订单相反；
- 保护性订单必须 `reduce_only`；
- 保护性订单必须绑定主订单激活关系；
- 保护性触发价必须等于风险批准止损价。

保护性订单不会在主订单形成敞口前提前激活。

### 3.3 退出命令

退出只包含一个主订单：

```text
PRIMARY market order, reduce_only=true
```

退出命令不能附带新止损、不能申请新保证金，也不能增加敞口。

### 3.4 不可执行命令

风险批准过期、行情陈旧、行情来自未来或执行价格偏离过大时，系统仍会持久化一个明确处置结果，但：

```text
quantity = 0
leverage = 0
required_margin = 0
orders = []
```

不会静默丢弃输入，也不会留下无法解释的游标空洞。

## 4. 双层状态模型

订单状态与父命令状态是两个不同维度。

### 4.1 订单状态

```text
planned
  -> dispatched
  -> acknowledged
  -> partially_filled
  -> filled

acknowledged / partially_filled
  -> cancel_pending
  -> canceled

submitted states
  -> rejected / expired / failed
```

终态包括：

```text
filled
canceled
rejected
expired
failed
```

终态不能回到活动状态。

### 4.2 父命令投递状态

```text
shadow:
recorded

simulation:
pending -> dispatched -> completed
        \-> expired
        \-> failed
```

父命令状态不能凭调用方口头声明推进，必须满足持久化订单事实：

- `pending -> dispatched`：主订单已有提交或确认状态；
- `dispatched -> completed`：退出主订单已完全成交；入场命令的主订单和保护性关闭腿均已完成；
- `pending -> expired`：所有订单仍处于 `planned`，即没有提交证据；
- `pending/dispatched -> failed`：必须提供明确失败原因。

## 5. 原子持久化

`ExecutionStore.commit_command()` 在一个 `BEGIN IMMEDIATE` 事务中写入：

```text
execution instance identity
execution command
all initial order legs
source RiskDecision identity
initial delivery state
```

要么全部成功，要么全部回滚。

不会出现：

```text
命令已存在，但订单腿缺失
订单腿已存在，但命令缺失
同一 RiskDecision 生成两条活动命令
同一 client order ID 指向两个逻辑订单
```

命令持久化完成后才允许本地 broker 分发。

## 6. 订单更新日志

每个 `OrderUpdate` 包含：

- 确定性更新ID；
- 订单ID；
- 事件时间；
- 新状态；
- 累计成交数量；
- 累计成交金额；
- 累计费用；
- 平均成交价；
- broker订单ID；
- 原始证据。

更新先通过状态机和数学不变量，再与当前订单快照和追加日志原子提交。

重复更新ID：

- 语义完全相同：幂等返回；
- 语义不同：`ExecutionUpdateConflict`。

数学约束：

```text
cumulative filled <= requested quantity
cumulative quote = cumulative filled * average fill price
filled / quote / fee never decrease
partial-fill update must increase filled quantity
broker order ID cannot change
```

## 7. 保护性敞口约束

完整风险批准数量不是任何时刻都已经存在的真实敞口。

例如主订单批准数量为 `10`，目前只成交 `4`：

```text
maximum protective cumulative fill = 4
```

保护性止损不能成交 `10`，否则会把剩余 `6` 变成反向仓位。

因此存储层在接受保护性成交更新前，读取同命令主订单的累计成交量，并强制：

```text
protective cumulative fill <= primary cumulative fill
```

该约束位于持久化边界，不能由 broker 或调用者绕过。

## 8. Outbox 与崩溃恢复

### 8.1 正常路径

```text
persist pending command and planned orders
        |
        v
local broker emits dispatched / acknowledged updates
        |
        v
persist order updates
        |
        v
mark parent command dispatched
```

### 8.2 关键崩溃窗口

最危险的窗口是：

```text
订单提交证据已经落库
        |
        X process crashes
        |
父命令仍是 pending
```

重启时，系统必须先检查订单事实，而不是先看命令 TTL。

若发现任一订单已不再是 `planned`：

1. 使用持久化订单的 `updated_at` 恢复实际分发时间；
2. 校验该时间没有超过命令有效期；
3. 把父命令恢复为 `dispatched`；
4. 写入 `pending_order_recovery` 审计证据；
5. 根据当前订单状态继续收敛父命令。

因此，已经提交的订单不会因为进程重启后当前时间超过 TTL 而被错误标记为未执行过期。

### 8.3 Active-command reconciliation

启动时还会扫描所有 `dispatched` 命令：

- 主订单或保护性订单失败时，父命令收敛为 `failed`；
- 退出主订单完全成交时，父命令收敛为 `completed`；
- 入场保护性关闭腿完全成交时，父命令收敛为 `completed`；
- 未满足终态条件时保持 `dispatched`。

这修复了“订单终态已落库，但父命令更新前崩溃”的第二类窗口。

## 9. 风险决策耐久消费

`ExecutionRuntime` 采用：

```text
subscribe live first
        |
        v
read durable RiskDecision batches
        |
        v
plan and persist command
        |
        v
commit upstream cursor
        |
        v
continue live consumption
```

耐久批大小固定为 `1`，确保一个失败决策不会被同批后续决策越过。

上游 `RiskDecision` 仍是 `pending` 时构成硬屏障；Execution Runtime 不会把它当作过滤不匹配记录跳过。

游标只有在执行处置已经持久化后才能推进。

## 10. 父命令审计日志

`execution_command_transitions` 追加记录：

```text
command ID
source RiskDecision ID
event time
prior state
new state
reason
evidence
```

该日志与父命令状态更新处于同一事务中。

因此可以回答：

- 命令何时分发；
- 是正常本地 broker 分发还是崩溃恢复；
- 命令为何失败或过期；
- 哪个风险决策产生了该状态变化。

## 11. Shadow 与本地模拟

### 11.1 Shadow

Shadow 模式只生成和持久化计划：

```text
status = shadow_recorded
submitted = false
network_write = false
```

没有 broker，不进入分发状态。

### 11.2 Simulation

`SimulatedBroker`：

- 不导入网络库；
- 不连接交易所；
- 不读取交易API密钥；
- 对相同命令生成确定性 update ID；
- 支持 dispatch、acknowledge、partial fill、fill、保护腿激活、取消和拒绝测试；
- 所有结果仍必须通过同一订单契约和存储边界。

模拟 broker 不是未来实盘 broker 的安全证明。真实适配器必须独立实现网络隔离、签名、重试、限频、交易所对账、人工授权和 kill switch。

## 12. 机械安全门禁

测试会解析全部 `execution*.py` 的 Python AST，禁止出现：

```text
ccxt
requests
httpx
aiohttp
websockets
socket
```

并禁止调用：

```text
create_order
edit_order
cancel_order
submit_order
place_order
amend_order
```

这不是字符串约定，而是 CI 中的可执行边界。

## 13. 健康状态

Execution Engine 健康快照包括：

- 收到和重复风险决策数；
- 已规划和已持久化命令数；
- shadow记录数；
- pending、dispatched、completed、expired、failed数量；
- 订单更新数；
- 保护腿激活数；
- 启动重对账数量；
- 上游硬屏障次数；
- 最老 pending 年龄；
- 最近决策、命令和错误；
- `network_write_enabled=false`。

## 14. 当前阶段故意不解决的问题

Execution Engine 当前不负责：

- 组合级持仓净额；
- 多策略对同一币种的冲突合并；
- 跨命令 OCO 关系；
- 账户真实余额、保证金和仓位对账；
- 意外交易所订单发现；
- 真实下单、改单和撤单；
- API密钥托管与权限隔离；
- 人工审批、紧急停止和灾难恢复演练。

这些问题不能通过继续往 Execution Engine 堆逻辑解决。

下一阶段应建立 **Portfolio / Account Engine**，统一维护：

```text
positions
balances
reserved margin
realized and unrealized PnL
fees and funding
per-symbol net exposure
command-to-position attribution
portfolio snapshots
reconciliation facts
```

只有账户与组合状态成为唯一可信事实后，才有资格设计真实交易所适配器。
