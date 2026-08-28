# Crypto Quant

面向币安 USDT 本位永续合约的确定性研究、组合回测、市场事件、策略意图、风险审批和本地执行模拟系统。支持多空与手动币种白名单；**没有任何真实订单提交、修改或撤销能力**。

## 当前完成范围

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
Shadow / Local Simulation
```

Execution Engine 当前只支持：

```text
shadow：持久化风险批准后的正式执行计划，不分发 broker
simulation：本地确定性 broker、订单状态机、恢复与对账
```

尚未完成：

```text
生产级 Portfolio Engine
真实交易所只读账户/订单适配器
真实下单、改单和撤单
交易权限密钥管理
真实余额、持仓和订单对账
生产熔断与人工审批编排
```

因此当前代码不能直接连接真实资金执行交易。

## 安全边界

- 不读取交易权限密钥，不包含真实交易所写函数。
- Execution 命令、订单和模拟更新在契约层强制 `submitted=false`、`network_write=false`。
- Shadow 模式没有 broker；SimulatedBroker 没有网络客户端。
- Strategy Engine 不输出数量、杠杆、保证金或订单类型。
- Risk Engine 是数量、杠杆、保证金和组合限制的唯一审批边界。
- Execution Engine 必须复核 RiskStore 中的不可变 RiskDecision，不能信任脱离存储的对象。
- `CQ_MAX_LEVERAGE=100` 是配置硬上限，不是目标杠杆。
- 缺少行情、周期屏障、版本一致性、关键消费者、风险发布事实、保护腿或完整对账时，系统失败关闭。
- 项目不承诺盈利，不应在真实交易所适配、权限、持仓对账和安全评审完成前接入真实资金。

## 已实现

### Data Engine

- 币安 USDT 永续合约K线、标记价格K线和资金费率。
- 历史增量分页、时间同步和有限重试。
- 过滤未闭合K线。
- OHLCV、重复、排序、负成交量和缺口校验。
- 实时K线缺口先REST补齐，再保存当前K线。
- 已闭合K线和资金费率不可变写入，历史修订失败关闭。
- WebSocket断线重连和健康状态。

### Market Engine

- 统一 `candle_closed`、`mark_price`、`funding_rate` 事件。
- 逻辑收盘时间、交易所来源时间和接收时间分离。
- 确定性事件ID、严格载荷、分区键和进程内全局序号。
- 同一收盘时点高周期优先排序。
- 精确重复抑制与陈旧事件拒绝。
- 关键消费者阻塞式背压，展示消费者显式丢旧或断开。
- 校验和持久化成功后才发布。
- CCXT与SQLite阻塞工作移出asyncio事件循环。

### Strategy Engine

- 回测与实时共用纯函数：

  ```python
  evaluate_strategy(StrategyContext) -> StrategyDecision
  ```

- `1h` 趋势、`15m` 执行的严格多周期闭合屏障。
- 等待高周期期间仍按每个低周期事件自己的决策时间切片，禁止未来数据。
- 策略实例锁定策略ID、代码版本、参数指纹和高低周期。
- 确定性的 `StrategyIntent`，包含来源行情、失效价、原因和结构证据。
- 策略输出中没有数量、杠杆或订单字段。
- 评估、意图和检查点单事务写入。
- 持久 outbox、永久 storage sequence 和固定过滤器耐久游标。
- `pending` 是硬游标屏障；历史恢复意图标记为 `suppressed`。

### Risk Engine

- 纯风险入口：

  ```python
  evaluate_risk(RiskContext) -> RiskDecision
  ```

- 不可变策略意图、账户快照、市场规则和版本化风险政策。
- 单笔、单币、组合、相关性分组、总名义价值和方向敞口限制。
- 手续费、滑点、止损、交易所步长、最小订单和维持保证金档位。
- 波动率缩放、动态杠杆、可用余额和全仓强平缓冲。
- 日亏、账户回撤、连续亏损、陈旧/未来数据和参考价偏离检查。
- 风险降低型退出强制 reduce-only。
- 决策、评估和风险实例单事务持久化。
- 批准决策持久 outbox：`pending -> published` 或 `pending -> expired`。
- 执行关键队列、永久 storage sequence、耐久游标和启动恢复。
- EventEngine 入场仓位计算复用相同纯风险函数。

### Execution Engine

- 严格的 ExecutionPolicy、市场快照、命令、订单和更新契约。
- 完整复核 RiskStore 中的风险决策语义和来源序号。
- 检查执行行情年龄、未来时间、价差和风险审批价偏离。
- 确定性命令ID、订单ID、客户端订单ID和模拟 broker ID。
- 入场包含主订单和绑定主订单的 reduce-only 保护性止损。
- 退出只生成 reduce-only 主订单。
- 执行实例、命令和初始订单腿单事务持久化。
- 命令 outbox：recorded、pending、dispatched、completed、expired、failed。
- 单调订单状态机和追加式 OrderUpdate 审计日志。
- 并发 outbox 串行化，避免重复本地分发。
- RiskDecision 实时订阅与耐久追赶；上游游标仅在命令落库后前进。
- RiskStore 源记录尚未 published 时禁止分发。
- broker 失败后保留 pending 命令，重启直接恢复而不重新规划。
- 本地 SimulatedBroker 支持分发、确认、部分成交、完全成交、拒绝和取消。
- 主订单部分或全部成交后激活保护腿。
- 保护腿拒绝、失败、过期或危险取消会把命令标记为失败。
- 完整模拟 broker 账本对账；缺单、陌生单、身份冲突和状态倒退失败关闭。
- 正式 shadow 报告只接受已持久化、已风控 ExecutionCommand。

### 研究与模拟账户

- 回测和模拟账户共用 EventEngine。
- 信号收盘确认，下一根K线开盘模拟成交。
- 保守的全仓强平、止损、止盈顺序。
- 手续费、滑点、资金费率、标记价格和维持保证金。
- 所有币种共享现金、未实现盈亏和组合风险预算。
- 单币风险、组合风险、方向敞口、日亏、回撤、连亏和全局停止。
- 多币种全局时间线，不重复使用本金。

EventEngine 是K线研究撮合模型；Execution Runtime 是持久化订单状态模型。两者不能混为真实交易所成交。

### 策略与验证

- 形式化缠论包含处理、确认分型、笔、中枢、线段、背驰和一类/三类点。
- 缠论确认分型、缠论三类点、唐奇安突破、EMA回踩。
- 策略注册表记录版本、来源、许可证和参数网格。
- 扩展式 walk-forward，最后20%历史隔离。
- 最终测试、双倍成本和蒙特卡洛顺序压力测试。
- 严格JSON，不输出 `NaN` 或无穷值。

这里的缠论规则是本项目公开、固定、可测试的形式化版本，不声称是所有流派的唯一解释。

### 工程与控制界面

- SQLite WAL：行情、资金费率、账户状态、任务、策略、风险和执行日志。
- PWA控制界面，支持手机查看研究、账户、任务和紧急停止。
- 至少32字符 API Token、恒定时间比较和基础限流。
- JSON结构化日志和就绪指标。
- 非root Docker、只读容器、移除Linux capabilities和健康检查。
- GitHub Actions：Python 3.11/3.12、Ruff、Pytest、pip-audit、Bandit和Docker构建。

## 安装

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
cp .env.example .env
```

生成强 Token：

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 研究命令

单币回测：

```bash
crypto-quant backtest --symbol BTC/USDT:USDT --days 180 --entry-model chan_fractal
```

单币严格选优：

```bash
crypto-quant select --symbol BTC/USDT:USDT --days 365
```

白名单共享资金组合回测：

```bash
crypto-quant portfolio-backtest --days 365
```

白名单单进程模拟账户和旧兼容影子意图报告：

```bash
crypto-quant portfolio-paper --shadow --once
```

该 CLI 的旧 shadow 输出不会把未经过 Risk Engine 的 Signal 伪装成正式订单。正式影子执行计划必须通过 Execution Engine 持久化后读取。

启动控制界面：

```bash
export CQ_API_TOKEN="上一步生成的强Token"
crypto-quant serve --host 0.0.0.0 --port 8000
```

公网部署必须由反向代理提供 HTTPS。不要把无TLS的Uvicorn端口直接暴露到公网。

Docker：

```bash
export CQ_API_TOKEN="上一步生成的强Token"
docker compose up --build
```

## 关键配置

| 环境变量 | 默认值 | 作用 |
|---|---:|---|
| `CQ_SYMBOLS` | BTC、ETH | 手动合约白名单 |
| `CQ_HIGH_TIMEFRAME` | `1h` | 趋势周期 |
| `CQ_LOW_TIMEFRAME` | `15m` | 执行周期 |
| `CQ_RISK_PER_TRADE` | `0.01` | 研究账户单次计划风险 |
| `CQ_MAX_SYMBOL_RISK` | `0.015` | 研究账户单币风险上限 |
| `CQ_MAX_PORTFOLIO_RISK` | `0.03` | 研究账户组合风险上限 |
| `CQ_MAX_DIRECTIONAL_NOTIONAL` | `20` | 研究账户方向名义敞口上限 |
| `CQ_MAX_LEVERAGE` | `100` | 配置硬上限，非目标值 |
| `CQ_MIN_LIQUIDATION_BUFFER` | `0.02` | 强平前最小缓冲 |
| `CQ_REQUIRE_MARK_PRICE` | `true` | 标记价格缺失时报警 |
| `CQ_API_TOKEN` | 无 | 受保护接口必填，至少32字符 |

ExecutionPolicy 当前通过代码构造并固定到执行实例。不要把 shadow 实例原地切换为 simulation；模式或策略身份变化必须使用新的 engine instance ID。

## 验证

```bash
ruff format --check src tests
ruff check src tests
pytest -q
```

完整CI还执行：

```text
Python 3.11
Python 3.12
pip-audit
Bandit medium/high
Docker build
```

离线单元、集成、静态和打包验证不等于真实行情或真实订单联调。部署网络必须另外执行公开行情烟雾测试和长期影子验证。

## 文档

- [总架构](docs/ARCHITECTURE.md)
- [Data Engine](docs/DATA_ENGINE.md)
- [Market Engine](docs/MARKET_ENGINE.md)
- [Strategy Engine](docs/STRATEGY_ENGINE.md)
- [Risk Engine](docs/RISK_ENGINE.md)
- [Execution Engine](docs/EXECUTION_ENGINE.md)
- [Execution 运行与恢复](docs/EXECUTION_ENGINE_OPERATIONS.md)
- [Execution 验收门槛](docs/EXECUTION_ENGINE_ACCEPTANCE.md)
- [风险模型](docs/RISK_MODEL.md)
- [策略研究协议](docs/STRATEGY_RESEARCH.md)
- [安全部署](SECURITY.md)
- [变更记录](CHANGELOG.md)

## 风险声明

永续合约可能在极短时间内损失全部保证金。回测、影子和模拟结果会受到数据质量、手续费、滑点、标记价格、维持保证金档位、成交优先级和过拟合影响。本项目是研究和本地模拟工具，不是投资建议，也未获准用于真实资金交易。
