# Crypto Quant

面向币安 USDT 本位永续合约的确定性研究、组合回测、模拟账户、市场事件和策略意图系统。支持多空与手动币种白名单；**没有任何真实订单提交、修改或撤销实现**。

## 当前完成范围

```text
Data Engine
    ↓
Market Engine
    ↓
Strategy Engine
    ↓
StrategyIntent 持久日志
```

同时具备独立的历史回测、组合模拟账户和不会提交订单的 shadow intent。

尚未完成：

```text
Risk Engine
Portfolio Engine
Execution Engine
交易所订单状态机
账户对账
生产熔断编排
```

因此当前代码不能直接连接真实资金执行交易。

## 安全边界

- 只实现研究、模拟、行情事件和待审查策略意图。
- 不读取交易权限密钥，不包含真实下单网络函数。
- `CQ_MAX_LEVERAGE=100` 是配置硬上限，不是目标杠杆。
- Strategy Engine 不输出数量、杠杆、保证金或订单类型。
- 缺少行情、周期屏障、版本一致性或关键消费者时，系统失败关闭。
- 缺少币安账户签名杠杆档位时，研究规则退回保守占位并保留警告。
- 项目不承诺盈利，不应在风险、组合、执行和对账层完成前接入真实资金。

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
- 校验与持久化成功后才发布。
- CCXT与SQLite阻塞工作移出asyncio事件循环。
- Runtime、Stream、Data Engine和订阅者健康快照。

### Strategy Engine

- 回测与实时共用纯函数：

  ```python
  evaluate_strategy(StrategyContext) -> StrategyDecision
  ```

- `1h` 趋势、`15m` 执行的严格多周期闭合屏障。
- 等待高周期期间仍按每个低周期事件的决策时间切片，禁止未来数据。
- 策略实例锁定策略ID、代码版本、参数指纹和高低周期。
- 确定性的 `StrategyIntent`，包含来源行情、失效价、原因和结构证据。
- 策略输出中没有数量、杠杆或订单字段。
- 评估、意图和检查点单事务写入。
- 同一市场事件产生不同决策时直接拒绝。
- 持久 outbox，恢复已落库但尚未进入关键队列的意图。
- 永久 `storage_sequence` 与耐久消费者游标。
- 默认风险重放只读取 `published` 意图。
- 历史恢复信号标记为 `suppressed`，不会进入实时交易链路。
- 首次启动只评估新鲜收盘；陈旧历史只建立基线。
- 默认发现未处理历史时失败，显式支持 `REPLAY` 和 `RESET`。

### 研究与模拟账户

- 回测和模拟账户共用 `EventEngine`。
- 信号收盘确认，下一根K线开盘模拟成交。
- 保守的全仓强平、止损、止盈顺序。
- 手续费、滑点、资金费率、标记价格和维持保证金。
- 所有币种共享现金、未实现盈亏和组合风险预算。
- 单币风险、组合风险、方向敞口、日亏、回撤、连亏和全局停止。
- 多币种全局时间线，不重复使用本金。

### 策略与验证

- 形式化缠论包含处理、确认分型、笔、中枢、线段、背驰和一类/三类点。
- 缠论确认分型、缠论三类点、唐奇安突破、EMA回踩。
- 策略注册表记录版本、来源、许可证和参数网格。
- 扩展式 walk-forward，最后20%历史隔离。
- 最终测试、双倍成本和蒙特卡洛顺序压力测试。
- 严格JSON，不输出 `NaN` 或无穷值。

这里的缠论规则是本项目公开、固定、可测试的形式化版本，不声称是所有流派的唯一解释。

### 工程与控制界面

- SQLite WAL：行情、资金费率、账户状态、任务、策略版本、评估和意图日志。
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

白名单单进程模拟账户和不提交的影子订单意图：

```bash
crypto-quant portfolio-paper --shadow --once
```

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

## 验证

```bash
ruff format --check src tests
ruff check src tests
pytest -q
```

当前执行环境可能无法访问币安域名。离线单元、集成、静态和打包验证不等于真实行情联调；部署网络必须另外执行公开行情烟雾测试和长期影子验证。

## 文档

- [总架构](docs/ARCHITECTURE.md)
- [Data Engine](docs/DATA_ENGINE.md)
- [Market Engine](docs/MARKET_ENGINE.md)
- [Strategy Engine](docs/STRATEGY_ENGINE.md)
- [风险模型](docs/RISK_MODEL.md)
- [策略研究协议](docs/STRATEGY_RESEARCH.md)
- [安全部署](SECURITY.md)
- [变更记录](CHANGELOG.md)

## 风险声明

永续合约可能在极短时间内损失全部保证金。回测与模拟结果会受到数据质量、手续费、滑点、标记价格、维持保证金档位、成交优先级和过拟合影响。本项目是研究工具，不是投资建议。
