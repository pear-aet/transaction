from __future__ import annotations

import os
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .backtest import run_backtest
from .config import StrategyConfig
from .data import CcxtDataSource, days_ago_ms, timeframe_ms
from .engine import default_market_rules
from .jobs import JobManager
from .observability import configure_logging
from .selection import select_strategy_for_symbol
from .storage import Repository
from .strategy import strategy_registry


class ResearchRequest(BaseModel):
    symbol: str = "BTC/USDT:USDT"
    days: int = Field(180, ge=30, le=1460)
    initial_cash: float = Field(10_000, gt=0, le=100_000_000)
    risk_per_trade: float = Field(0.01, gt=0, le=0.05)
    max_leverage: float = Field(100, ge=1, le=100)
    entry_model: str = "chan_fractal"
    entry_lookback: int = Field(20, ge=5, le=200)


class StopRequest(BaseModel):
    enabled: bool


def _symbols() -> tuple[str, ...]:
    raw = os.getenv("CQ_SYMBOLS", "BTC/USDT:USDT,ETH/USDT:USDT")
    return tuple(value.strip() for value in raw.split(",") if value.strip())


def _authorize(x_api_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("CQ_API_TOKEN", "")
    if len(expected) < 32:
        raise HTTPException(
            status_code=503,
            detail="CQ_API_TOKEN must be configured with at least 32 characters",
        )
    if x_api_token is None or not secrets.compare_digest(x_api_token, expected):
        raise HTTPException(status_code=401, detail="invalid API token")


def _config(request: ResearchRequest) -> StrategyConfig:
    if request.symbol not in _symbols():
        raise HTTPException(status_code=400, detail="symbol is not in CQ_SYMBOLS whitelist")
    try:
        return replace(
            StrategyConfig.from_env(),
            exchange="binanceusdm",
            symbol=request.symbol,
            initial_cash=request.initial_cash,
            risk_per_trade=request.risk_per_trade,
            max_leverage=request.max_leverage,
            entry_model=request.entry_model,
            entry_lookback=request.entry_lookback,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


_services_lock = threading.Lock()
_repository: Repository | None = None
_jobs: JobManager | None = None


def _services() -> tuple[Repository, JobManager]:
    global _repository, _jobs
    with _services_lock:
        if _repository is None:
            data_dir = Path(os.getenv("CQ_DATA_DIR", "data"))
            _repository = Repository(data_dir / "crypto_quant.db")
            _jobs = JobManager(_repository, workers=int(os.getenv("CQ_JOB_WORKERS", "2")))
    assert _jobs is not None
    return _repository, _jobs


def _incremental_candles(
    repository: Repository,
    source: CcxtDataSource,
    symbol: str,
    timeframe: str,
    since: int,
    max_candles: int,
    price_type: str,
):
    cached = repository.load_candles(symbol, timeframe, since, price_type)
    cursor = cached[-1].timestamp + timeframe_ms(timeframe) if cached else since
    try:
        fetched = (
            source.fetch_mark_candles(symbol, timeframe, cursor, max_candles=max_candles)
            if price_type == "mark"
            else source.fetch_candles(symbol, timeframe, cursor, max_candles=max_candles)
        )
    except Exception:
        if cached:
            return cached
        raise
    if fetched:
        repository.upsert_candles(symbol, timeframe, fetched, price_type)
    return repository.load_candles(symbol, timeframe, since, price_type)


def _market(config: StrategyConfig, days: int):
    repository, _ = _services()
    source = CcxtDataSource("binanceusdm", "swap")
    since = days_ago_ms(days)
    high_since = since - 60 * 86_400_000
    low_limit = min(150_000, days * 86_400_000 // timeframe_ms(config.low_timeframe) + 20)
    high_limit = min(
        30_000,
        (days + 60) * 86_400_000 // timeframe_ms(config.high_timeframe) + 20,
    )
    low = _incremental_candles(
        repository, source, config.symbol, config.low_timeframe, since, low_limit, "contract"
    )
    high = _incremental_candles(
        repository,
        source,
        config.symbol,
        config.high_timeframe,
        high_since,
        high_limit,
        "contract",
    )
    try:
        marks = _incremental_candles(
            repository, source, config.symbol, config.low_timeframe, since, low_limit, "mark"
        )
    except Exception:
        marks = repository.load_candles(config.symbol, config.low_timeframe, since, "mark")
    try:
        funding = source.fetch_funding_rates(config.symbol, since, 1000, 20_000)
    except Exception:
        funding = []
    if funding:
        repository.upsert_funding(config.symbol, funding)
    funding = repository.load_funding(config.symbol, since)
    try:
        rules = source.fetch_market_rules(config.symbol)
    except Exception:
        rules = default_market_rules(config)
    return low, high, funding, marks, rules


app = FastAPI(
    title="Crypto Quant",
    version="0.2.0",
    description=(
        "Backtest, portfolio paper trading and shadow validation for Binance USDT perpetuals."
    ),
)
configure_logging(os.getenv("CQ_LOG_LEVEL", "INFO"))

_requests: dict[str, deque[float]] = defaultdict(deque)


@app.middleware("http")
async def rate_limit(request: Request, call_next: Callable) -> Response:
    if request.url.path.startswith("/api/"):
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        bucket = _requests[key]
        while bucket and bucket[0] < now - 60:
            bucket.popleft()
        if len(bucket) >= int(os.getenv("CQ_RATE_LIMIT_PER_MINUTE", "60")):
            return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
        bucket.append(now)
    return await call_next(request)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "mode": "research-paper-only"}


@app.get("/api/readiness", dependencies=[Depends(_authorize)])
def readiness() -> dict[str, Any]:
    repository, _ = _services()
    return {"status": "ready", "storage": repository.metrics()}


@app.get("/api/config", dependencies=[Depends(_authorize)])
def public_config() -> dict[str, object]:
    config = StrategyConfig.from_env()
    return {
        "exchange": "binanceusdm",
        "symbols": _symbols(),
        "high_timeframe": config.high_timeframe,
        "low_timeframe": config.low_timeframe,
        "margin_mode": "cross",
        "market_type": "swap",
        "live_trading": False,
        "testnet_order_submission": False,
        "max_portfolio_risk": config.max_portfolio_risk,
    }


@app.get("/api/strategies", dependencies=[Depends(_authorize)])
def strategies() -> dict[str, Any]:
    repository, _ = _services()
    return {
        "registry": [asdict(spec) for spec in strategy_registry()],
        "selected_versions": repository.list_strategy_versions(),
    }


def _submit(request: ResearchRequest, kind: str) -> dict[str, str]:
    config = _config(request)
    _, manager = _services()

    def task(cancelled: Callable[[], bool]) -> dict[str, Any]:
        low, high, funding, marks, rules = _market(config, request.days)
        if cancelled():
            return {"cancelled": True}
        if kind == "backtest":
            result = run_backtest(low, high, config, funding, marks, rules)
            return {
                "config": config.to_dict(),
                "bars": {
                    "low": len(low),
                    "high": len(high),
                    "funding": len(funding),
                    "mark": len(marks),
                },
                "result": result.to_dict(),
            }
        result = select_strategy_for_symbol(low, high, config, funding, market_rules=rules)
        if result.selected:
            repository, _ = _services()
            now = int(time.time() * 1000)
            repository.save_strategy_version(
                config.symbol,
                result.selected.strategy_id,
                result.selected.version,
                {"lookback": result.selected.lookback},
                {
                    "validation_score": result.selected.score,
                    "final_test": result.final_test,
                    "stress_test": result.stress_test,
                },
                now + 30 * 86_400_000,
                True,
            )
        return {"config": config.to_dict(), "selection": result.to_dict()}

    identifier = manager.submit(kind, request.model_dump(), task)
    return {"job_id": identifier, "status": "queued"}


@app.post("/api/backtest", status_code=202, dependencies=[Depends(_authorize)])
def backtest(request: ResearchRequest) -> dict[str, str]:
    return _submit(request, "backtest")


@app.post("/api/select", status_code=202, dependencies=[Depends(_authorize)])
def select(request: ResearchRequest) -> dict[str, str]:
    return _submit(request, "select")


@app.get("/api/jobs", dependencies=[Depends(_authorize)])
def list_jobs(limit: int = 20) -> dict[str, Any]:
    repository, _ = _services()
    try:
        return {"jobs": repository.list_jobs(limit)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/jobs/{job_id}", dependencies=[Depends(_authorize)])
def get_job(job_id: str) -> dict[str, Any]:
    repository, _ = _services()
    job = repository.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(_authorize)])
def cancel_job(job_id: str) -> dict[str, bool]:
    repository, _ = _services()
    return {"cancel_requested": repository.cancel_job(job_id, int(time.time() * 1000))}


@app.get("/api/portfolio", dependencies=[Depends(_authorize)])
def portfolio() -> dict[str, Any]:
    repository, _ = _services()
    state = repository.load_engine_state("paper", StrategyConfig.from_env().initial_cash)
    return state.to_dict()


@app.post("/api/risk/global-stop", dependencies=[Depends(_authorize)])
def global_stop(request: StopRequest) -> dict[str, bool]:
    repository, _ = _services()
    state = repository.load_engine_state("paper", StrategyConfig.from_env().initial_cash)
    state.global_stop = request.enabled
    repository.save_engine_state("paper", state, int(time.time() * 1000))
    repository.append_event(
        "paper", int(time.time() * 1000), "global_stop_changed", {"enabled": request.enabled}
    )
    return {"global_stop": state.global_stop}


_web = Path(__file__).with_name("web")
app.mount("/", StaticFiles(directory=_web, html=True), name="web")
