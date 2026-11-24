from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_home() -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"message": "Welcome to Agent Builder!"}


def test_health_ok() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"
    assert body["data"]["project"] == "Agent Builder"


def test_health_alias_on_root() -> None:
    """根路径 /health 与 /api/health 返回同构数据，供部署探针使用。"""
    assert client.get("/health").json()["data"]["status"] == "ok"
