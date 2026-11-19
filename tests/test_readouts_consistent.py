"""五处读数（源目录/到达列表/迟到详情/补采附页/水位卡片）必须逐源对得上。"""

import pytest
from fastapi.testclient import TestClient

import app as app_mod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "DB_PATH", tmp_path / "t.db")
    app_mod.SESSIONS.clear()
    app_mod.init_db()
    with TestClient(app_mod.app, raise_server_exceptions=False) as c:
        yield c


def _h(client):
    tok = client.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    return {"Authorization": "Bearer " + tok}


def test_five_readouts_agree_per_source(client):
    h = _h(client)
    duty = client.post("/api/duty/token", headers=h, json={"operator": "值班甲"}).json()["duty_token"]
    hd = {**h, "X-Duty-Token": duty}
    r = client.post("/api/arrivals", headers=hd, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T11:10:00"})
    assert r.status_code == 200, r.text

    src_dir = {s["name"]: s for s in client.get("/api/sources", headers=h).json()["items"]}
    snap = client.get("/api/snapshot", headers=h).json()
    snap_by = {s["name"]: s for s in snap["items"]}
    wm = client.get("/api/watermark", headers=h).json()
    wm_by = {s["name"]: s for s in wm["items"]}
    arrivals = client.get("/api/arrivals", headers=h).json()["items"]

    for name, a in src_dir.items():
        detail = client.get(f"/api/delay/{name}", headers=h).json()
        for label, other in (("快照", snap_by[name]), ("水位", wm_by[name]), ("迟到详情", detail)):
            for key in ("sla_minutes", "lag_minutes", "level", "output_mode", "mode_prev"):
                assert a[key] == other[key], f"{name} 的 {key} 在源目录与{label}间不一致：{a[key]!r} vs {other[key]!r}"

    # 到达列表最后一条与源目录读数一致
    latest = next(x for x in arrivals if x["source"] == "订单源")
    assert latest["lag_minutes"] == src_dir["订单源"]["lag_minutes"]
    assert latest["level"] == src_dir["订单源"]["level"]

    # 水位卡片与源目录汇总一致
    assert wm["cards"]["frozen"] == sum(1 for s in src_dir.values() if s["output_mode"] == "冻结")
    assert wm["cards"]["sources"] == len(src_dir)
    # 补采附页（snapshot.catchups）所挂源必须在源目录存在
    for c in snap["catchups"]:
        assert c["source"] in src_dir


def test_detail_marks_which_mechanism_effective(client):
    h = _h(client)
    # 缺值班令牌 => 越权 => 详情标「令牌拒写」
    client.post("/api/arrivals", headers=h, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T11:10:00"})
    d = client.get("/api/delay/订单源", headers=h).json()
    assert d["effective_mechanism"] == "令牌拒写"
    assert d["rollback_status"].startswith("未启用")
    # 源输出态保持未切
    assert d["output_mode"] == "可写"


def test_policy_locked_and_deviation_rejected(client):
    h = _h(client)
    p = client.get("/api/grade/policy", headers=h).json()
    assert p["path"] == "甲" and p["locked"] == 1
    # 当天改口一律拒绝并记「分级偏了」
    assert client.put("/api/grade/policy", headers=h, json={"path": "乙"}).status_code == 423
    # 请求自带其它口径也拒绝
    r = client.post("/api/arrivals", headers=h, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00",
        "ingest_clock": "2026-11-16T11:10:00", "policy_path": "乙"})
    assert r.status_code == 423
    kinds = [e["kind"] for e in client.get("/api/grade/events", headers=h).json()["items"]]
    assert kinds.count("分级偏了") >= 2


def test_catchup_after_breach_is_not_flagged_torn(client):
    h = _h(client)
    duty = client.post("/api/duty/token", headers=h, json={"operator": "值班甲"}).json()["duty_token"]
    hd = {**h, "X-Duty-Token": duty}
    r = client.post("/api/arrivals", headers=hd, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T11:10:00"})
    assert r.status_code == 200
    assert client.get("/api/delay/订单源", headers=h).json()["band_conflict"] is False
    client.post("/api/catchup/订单源", headers=h)
    d = client.get("/api/delay/订单源", headers=h).json()
    assert d["band_conflict"] is False, "补齐恢复后被误报撕裂"
    assert d["output_mode"] == "可写" and d["mode_prev"] == "冻结"
