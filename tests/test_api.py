from fastapi.testclient import TestClient

from crypto_quant.api import app


def test_health_does_not_enable_live_trading() -> None:
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mode": "research-paper-only"}


def test_protected_api_requires_a_real_token(monkeypatch) -> None:
    monkeypatch.delenv("CQ_API_TOKEN", raising=False)
    assert TestClient(app).get("/api/config").status_code == 503
    monkeypatch.setenv("CQ_API_TOKEN", "x" * 32)
    assert TestClient(app).get("/api/config", headers={"x-api-token": "bad"}).status_code == 401
    response = TestClient(app).get("/api/config", headers={"x-api-token": "x" * 32})
    assert response.status_code == 200
    assert response.json()["live_trading"] is False
