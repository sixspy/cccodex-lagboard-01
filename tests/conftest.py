import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    """每个用例一份独立 SQLite 文件库，挂在 data 目录同构路径上。"""
    import app as app_module

    monkeypatch.setattr(app_module, "DB_PATH", tmp_path / "t.db")
    app_module.init_db()
    return app_module


@pytest.fixture
def client(_db):
    return TestClient(_db.app)


@pytest.fixture
def admin(client):
    r = client.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}
