# Portfolio / Account Engine Acceptance Gates

Portfolio / Account Engine 在所有门禁满足前不得离开草稿状态。

## 1. 范围与安全边界

- 输入只来自已持久化的 ExecutionCommand、ExecutionOrder、OrderUpdate、MarketEvent 和 FundingRate 事实。
- 当前阶段只维护 shadow / simulation 账户事实。
- 不得引入交易所账户认证、余额查询写路径、真实下单、改单或撤单。
- 不得读取交易权限 API Key。
- 账户层不得重新决定策略方向、风险批准数量、杠杆或订单类型。
- 账户层是余额、持仓、费用、资金费率、已实现与未实现盈亏的唯一可信写入口。
- 旧 EventEngine 不得继续维护与新账本并行的第二套资金与持仓公式。

## 2. 账户模式

- 首版固定为 USDT 结算、单账户、全仓、单向持仓模式。
- 每个账户与币种最多存在一个净持仓。
- 同一币种不能同时持有独立多仓和空仓。
- 非 reduce-only 成交不能跨过零轴直接翻转持仓。
- reduce-only 成交不能增加敞口、改变方向或超过当前可关闭数量。
- 多账户、逐仓和双向持仓只能在新版本中显式实现，不能通过隐式字段兼容。

## 3. 不可变会计流水

- 所有货币变化必须由不可变 JournalTransaction 表达。
- 每个 JournalTransaction 包含确定性 transaction ID、来源、事件时间、账户、结算资产和至少两个 posting。
- 每个 posting 只能有 debit 或 credit，且金额必须有限、非负并符合最小精度。
- 每个 transaction 的 debit 总额必须等于 credit 总额。
- 重复 transaction ID 与相同语义幂等；相同 ID 不同语义失败关闭。
- 余额不得通过绕过 journal 的直接 SQL 或对象赋值改变。
- Journal、派生余额、持仓更新和上游游标必须在一个 SQLite 事务中提交。

## 4. 会计科目

至少明确维护：

```text
cash
command_margin
position_margin
command_fee_reserve
external_capital
realized_pnl
fees
funding_pnl
```

- cash、command_margin、position_margin 和 command_fee_reserve 构成结算资产。
- external_capital 记录净注资与提款，不得与交易盈亏混合。
- realized_pnl 允许收入和亏损双向记账。
- fees 与 funding_pnl 必须与现金变化逐笔对账。
- 内部保证金转移不能改变总权益。

## 5. 初始资金与外部现金流

- 账户初始化必须通过确定性入金 transaction，不得直接设置 cash。
- 相同初始化 transaction 重放不得二次增加资金。
- 提款不得使可用现金或总资产为负。
- 外部现金流与交易盈亏必须可独立统计。

## 6. ExecutionCommand 预留

- 每个 simulation 入场命令在任何成交前先创建唯一 command reservation。
- 预留至少覆盖批准 required_margin 与政策定义的费用缓冲。
- 预留必须从 cash 转移到 command_margin / command_fee_reserve。
- 同一命令不能重复预留。
- 同一账户与币种不能同时存在冲突的入场预留。
- 退出命令必须锁定可关闭数量，防止多个 reduce-only 命令合计超卖。
- shadow 命令只形成审计事实，不占用模拟账户资金。
- 过期、失败或未成交取消命令必须释放全部未使用预留。
- 已经形成持仓的保证金不能随命令结束错误释放回 cash。

## 7. 成交增量

- Account Engine 只处理累计 OrderUpdate 的正增量，不把累计值重复计账。
- 成交增量由当前累计成交减去最后已记账累计成交确定。
- 成交增量、成交金额和费用增量不得为负。
- 同一 update ID 重放必须幂等。
- 新 update ID 但累计值不增长时不得重复产生 journal。
- 成交金额增量与累计平均价变化必须使用差分后的 quote value，而不是简单乘最新均价。

## 8. 开仓记账

- 非 reduce-only 主订单成交只能建立或增加同方向净持仓。
- 首次成交建立 position，后续同命令部分成交按成交金额加权更新平均开仓价。
- command_margin 按已成交比例转入 position_margin。
- 实际费用优先消耗 command_fee_reserve，超出部分才允许从 cash 支付。
- 费用不足不得静默制造负现金；必须失败关闭或进入显式账户故障状态。
- position 必须保留来源 command、RiskDecision 和 StrategyIntent 的可审计归因。

## 9. 平仓记账

- reduce-only 成交方向必须与现有持仓相反。
- 平仓数量不得超过持仓数量减去其他活动退出预留。
- 已实现盈亏按实际平仓成交价和持仓平均成本计算。
- 多头：`qty * (exit - entry)`；空头：`qty * (entry - exit)`。
- 已实现盈亏、平仓费用和释放的 position_margin 必须在同一事务结算。
- 部分平仓保持原平均开仓价；完全平仓删除活动持仓并生成不可变 closed-position 事实。
- reduce-only 订单不能跨零轴翻转方向。

## 10. 命令内 OCO 对账

- Account Engine 必须按 ExecutionStore 的最终累计成交，而不是订单请求数量记账。
- 主订单部分成交后保护腿成交，只关闭实际已形成的敞口。
- OCO 取消的未成交剩余量不产生持仓、费用或盈亏。
- 已完成入场命令的主累计成交必须等于保护累计成交时，账户净持仓应为零。
- ExecutionStore 与 Account Engine 对同一命令的累计成交差异必须成为显式 reconciliation failure。

## 11. 持仓模型

每个活动持仓至少包含：

```text
account_id
symbol
side
quantity
average_entry_price
mark_price
position_margin
initial_risk
maintenance_margin_rate
opened_at
updated_at
version
source command / risk / strategy attribution
```

- 所有数值必须有限且满足正数或非负约束。
- quantity 为零的活动持仓禁止持久化。
- 持仓 version 只能单调增加。
- 同一事件时间下出现不同持仓语义视为确定性冲突。

## 12. 标记价格与未实现盈亏

- 标记价格必须来自 canonical `mark_price` 或明确的闭合 mark candle 事实。
- 每个币种标记价格时间只能单调前进。
- 完全相同重复快照幂等。
- 同时间不同价格视为历史修订并失败关闭。
- 未来快照和超过政策年龄的快照不能生成可供 Risk Engine 使用的健康账户快照。
- 未实现盈亏使用标记价格，不使用合约最新成交价代替。

## 13. 资金费率

- 资金费率事件必须有确定性来源 ID 和时间。
- 同一资金费率事件只能应用一次。
- 资金费用基于事件时点活动持仓数量与明确价格基准。
- 多头在正费率下支付，空头在正费率下收取。
- 支付与收取均通过 balanced journal 结算。
- 已平仓前后边界、同时间成交与 funding 的排序必须有确定规则并测试。

## 14. 维持保证金与权益

- 每个持仓的维持保证金使用对应 MarketRules 档位计算。
- 组合 maintenance margin 等于所有持仓要求之和。
- settled assets 等于 cash + command_margin + position_margin + command_fee_reserve。
- equity 等于 settled assets + unrealized PnL。
- available balance 必须扣除命令预留和持仓保证金，不得重复扣减。
- 账户权益、科目余额、journal 汇总和持仓派生值必须可相互对账。
- 非有限、负保证金、负不可用资产或会计恒等式破坏必须失败关闭。

## 15. AccountSnapshot

- Account Engine 输出不可变、版本化、确定性 ID 的 AccountSnapshot。
- 快照包含账户观察时间、权益、可用余额、维持保证金、持仓、日初权益、峰值权益、连续亏损和熔断状态。
- 相同账户事实必须产生相同 snapshot ID。
- 快照必须能无损转换为 Risk Engine 的 AccountRiskSnapshot。
- Risk Engine 不得再依赖调用方临时拼装账户数据。
- 快照只有在 execution、market 和 funding 游标均达到声明水位后才可发布。

## 16. 日亏、回撤与连续亏损

- 日切边界固定为 UTC 日，不依赖进程本地时区。
- 新交易日的 day_start_equity 必须由持久化快照确定。
- peak_equity 只能单调增加，除非显式账户重置产生新 account epoch。
- 连续亏损只在完整关闭后的净交易结果上更新。
- 入金、提款、费用和资金费率不能被误判为一笔交易胜负。
- 熔断状态必须持久化并进入 AccountSnapshot。

## 17. 耐久消费与游标

- Execution command 与 order-update 消费者必须有独立、固定过滤器的耐久游标。
- 命令必须在任何相关成交前完成预留或明确记录无需预留的处置。
- 未处理 command 构成 order-update 游标硬屏障。
- 上游记录只有在 journal、持仓、预留和账户检查点原子提交后才能推进。
- 批次水位、过滤器哈希、过期批次和游标倒退都必须校验。
- 启动时先建立实时订阅或冻结高水位，再追赶历史，不能产生 live/replay 缝隙。

## 18. 重启恢复

- 账户状态必须只依赖持久化 journal、positions、reservations、marks 和 cursors 重建。
- 进程在成交 journal 与游标提交之间崩溃，重放不得重复计账。
- 进程在命令预留与成交之间崩溃，预留不得丢失或重复。
- 进程在完全平仓与 closed-position 写入之间崩溃，事务必须全部回滚或全部存在。
- 启动时必须核验派生余额与 journal 汇总一致。
- 不一致时账户进入 halted 状态，不能继续向 Risk Engine 发布健康快照。

## 19. 对账

至少检查：

- ExecutionStore 累计成交与已记账累计成交；
- command reservation 与未完成命令；
- position quantity 与开平仓成交净额；
- position margin 与命令批准保证金分配；
- journal 资产余额与账户缓存；
- closed-position 盈亏与 journal realized_pnl；
- mark price 时间与账户快照时间。

任何差异必须形成持久化 reconciliation finding。对账不得通过隐式修改历史流水自动“修平”。

## 20. 旧 EventEngine 迁移

- 回测和模拟盘必须调用同一 position accounting 与 journal 纯函数。
- 旧 `EngineState.cash`、`positions`、`funding_accrued` 和 `trades` 不能继续作为独立真相长期存在。
- 迁移过程中允许只读兼容视图，不允许双写两套不同公式。
- 相同成交序列在 EventEngine 与 Portfolio Engine 中必须产生相同现金、持仓、费用、资金费率和净盈亏。

## 21. 可观测性

健康状态至少暴露：

```text
journal transactions and postings
cash and restricted assets
active positions
open command reservations
processed command/update/funding counts
latest execution and market cursors
oldest unprocessed event age
account snapshot age
reconciliation failures
halted state and last error
```

## 22. 质量与安全门禁

- Ruff 格式化与静态检查通过。
- 完整测试在 Python 3.11 和 Python 3.12 通过。
- `pip-audit` 通过。
- Bandit 中高风险扫描通过，不能用 `# nosec` 绕过。
- Docker build 通过。
- 所有会计边界都有反例测试，而不只是正常路径测试。
- 不得为了绿灯降低精度、禁用测试或放宽会计恒等式。
- Portfolio / Account Engine 模块不得包含真实交易所账户写方法或网络客户端。
