# Crypto Quant 0.2

面向币安 USDT 本位永续合约的确定性研究、组合回测、全仓模拟盘和影子验证系统。支持多空和手动币种白名单；**没有任何实盘下单实现**。

## 安全边界

- 只实现回测、模拟盘和不会提交订单的 shadow intent。
- 不读取交易权限密钥，不包含创建/撤销真实订单的代码。
- `CQ_MAX_LEVERAGE=100` 是币种配置硬上限，不是目标杠杆。实际值由止损风险、组合风险、币种档位、同方向敞口和强平缓冲共同限制。
- 缺少币安账户对应的签名杠杆档位时，规则层退回保守的 20 倍/1% 维持保证金占位；结果会保留可信度警告。
- 缺少标记价格K线时，强平模拟退回合约价格并报警。
- 项目不承诺盈利，也不应在未完成长期影子验证前连接真实资金。

## 已实现

### 统一交易与账户引擎

- 回测和模拟盘共用 `EventEngine`，避免两套实现漂移。
- 信号在K线收盘后确认，下一根K线开盘成交。
- 同一根K线保守排序：全仓强平检查 → 止损 → 止盈。
- 所有币种共享一个现金余额、未实现盈亏、资金费率和组合风险预算。
- 单币风险、总组合风险、同方向名义敞口、日亏、最大回撤、连续亏损和全局停止。
- 实际资金费率或统一的8小时回退费率；回测与模拟盘一致。

### 币安合约数据和规则

- 合约K线、标记价格K线、资金费率历史及增量分页。
- 去重、未闭合K线过滤、缺口和时间顺序校验。
- `PRICE_FILTER`、`LOT_SIZE`、`MIN_NOTIONAL` 和币种最大杠杆。
- 账户可用时读取 Notional/Leverage Brackets；否则使用保守研究占位。
- 指数退避、限频交给 CCXT，应用层再进行有限重试。

币安官方参考：[Exchange Information](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information)、[Mark Price Klines](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price-Kline-Candlestick-Data)、[Funding Rate History](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History)、[Notional and Leverage Brackets](https://developers.binance.com/docs/derivatives/usds-margined-futures/account/rest-api/Notional-and-Leverage-Brackets)。

### 策略与验证

- 1小时缠论结构趋势与15分钟执行，周期可配置。
- 确定性包含处理、确认分型、笔、中枢、线段、背驰强度和一类/三类买卖点。
- 缠论确认分型、缠论三类点、唐奇安突破、EMA回踩策略。
- 策略注册表记录版本、来源、许可证和参数网格。
- 每个币种独立使用扩展式 walk-forward；最后20%历史完全隔离。
- 只对胜出者运行一次最终测试，再做双倍成本和蒙特卡洛顺序压力测试。
- 严格 JSON，不输出 `Infinity`、`-Infinity` 或 `NaN`。

这里的缠论规则是本项目公开、固定、可测试的形式化版本，不声称是所有缠论流派的唯一解释。

### 工程与手机端

- SQLite WAL：增量行情、资金费率、统一账户状态、事件、任务和策略版本。
- 事件ID防重放、原子状态保存、后台任务、状态查询和取消。
- JSON结构化日志、就绪指标、全局停止开关。
- 至少32字符 API Token、恒定时间比较、每IP基础限流。
- 可安装 PWA，支持手机浏览器查看账户、任务、策略结果和紧急停止。
- 非 root Docker、只读容器、移除 Linux capabilities、健康检查。
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

## 命令

单币回测：

```bash
crypto-quant backtest --symbol BTC/USDT:USDT --days 180 --entry-model chan_fractal
```

单币严格选优：

```bash
crypto-quant select --symbol BTC/USDT:USDT --days 365
```

整份白名单共享资金组合回测：

```bash
crypto-quant portfolio-backtest --days 365
```

整份白名单单进程模拟盘，并输出不会提交的影子订单意图：

```bash
crypto-quant portfolio-paper --shadow --once
```

启动 PWA：

```bash
export CQ_API_TOKEN="上一步生成的强Token"
crypto-quant serve --host 0.0.0.0 --port 8000
```

公网部署必须由反向代理提供 HTTPS。不要把无 TLS 的 Uvicorn 端口直接暴露到公网。

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
| `CQ_RISK_PER_TRADE` | `0.01` | 单次计划风险 |
| `CQ_MAX_SYMBOL_RISK` | `0.015` | 单币风险上限 |
| `CQ_MAX_PORTFOLIO_RISK` | `0.03` | 全账户未平仓风险上限 |
| `CQ_MAX_DIRECTIONAL_NOTIONAL` | `20` | 单方向名义敞口/权益上限 |
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

当前执行环境不能访问 `fapi.binance.com`，因此公开币安端到端烟雾测试必须在可访问币安的网络中再执行。离线单元、集成、静态和打包验证不等于真实行情联调。

## 文档

- [系统架构](docs/ARCHITECTURE.md)
- [风险模型](docs/RISK_MODEL.md)
- [策略研究协议](docs/STRATEGY_RESEARCH.md)
- [安全部署](SECURITY.md)
- [变更记录](CHANGELOG.md)

## 风险声明

永续合约可能在极短时间内损失全部保证金。回测结果会受到数据质量、手续费、滑点、标记价格、档位、成交优先级和过拟合影响。本项目是研究工具，不是投资建议。
