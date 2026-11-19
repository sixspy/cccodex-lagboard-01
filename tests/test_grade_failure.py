"""迟到分级写路径 —— 先行的两条失败测（实现前必须见红）。

本文件只覆盖两件事：
1. 分级写失败：缺值班令牌必须拒写到达行（不落半截），正式侧输出态保持未切，并写越权审计。
2. 双人抢交失败：两名采集值班同时交同一源到达，只落一条到达行，后到者只记「迟到分级争用」。
"""

import threading

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


def _login(c):
    r = c.post("/api/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200, r.text
    return r.json()


def _auth(tok):
    return {"Authorization": "Bearer " + tok}


def _duty(c, tok, operator):
    """领值班令牌；实现前该端点不存在，退回仅登录态（用于复现失败）。"""
    r = c.post("/api/duty/token", headers=_auth(tok), json={"operator": operator})
    if r.status_code == 200:
        return {**_auth(tok), "X-Duty-Token": r.json()["duty_token"]}
    return _auth(tok)


PAYLOAD = {
    "source": "订单源",  # SLA 15 分钟；70 分钟迟到 => 违约 => 冻结
    "source_clock": "2026-11-16T10:00:00",
    "ingest_clock": "2026-11-16T11:10:00",
}


def _source_modes(c, h):
    return {s["name"]: s["output_mode"] for s in c.get("/api/sources", headers=h).json()["items"]}


def test_grade_failure_without_duty_token_never_half_writes(client):
    j = _login(client)
    h = _auth(j["token"])
    modes_before = _source_modes(client, h)
    count_before = len(client.get("/api/arrivals", headers=h).json()["items"])

    # 故意不带值班令牌：分级写必须整体拒绝。
    r = client.post("/api/arrivals", headers=h, json=PAYLOAD)
    assert r.status_code == 403, (
        f"缺值班令牌的分级写必须拒写，实得 HTTP {r.status_code}: {r.text[:300]}"
    )

    # 不得出现半截成功：到达行不落库，正式侧输出态保持未切档。
    assert len(client.get("/api/arrivals", headers=h).json()["items"]) == count_before, (
        "拒写后仍落了到达行 —— 半截成功写库"
    )
    assert _source_modes(client, h)["订单源"] == modes_before["订单源"], (
        "拒写后正式侧输出态被切档 —— 半截成功写库"
    )

    ev = client.get("/api/grade/events", headers=h)
    assert ev.status_code == 200, f"越权审计不可读: {ev.status_code}"
    rows = ev.json()["items"]
    assert any(e["kind"] == "越权" and e["source"] == "订单源" for e in rows), (
        "缺值班令牌被拒写后没有越权审计记录"
    )


def test_two_duty_collectors_contend_single_arrival(client):
    j = _login(client)
    h = _auth(j["token"])
    h_a = _duty(client, j["token"], "值班甲")
    h_b = _duty(client, j["token"], "值班乙")

    c1 = TestClient(app_mod.app, raise_server_exceptions=False)
    c2 = TestClient(app_mod.app, raise_server_exceptions=False)
    barrier = threading.Barrier(2)
    results = []

    def grab(c, headers):
        barrier.wait()
        results.append(c.post("/api/arrivals", headers=headers, json=PAYLOAD))

    t1 = threading.Thread(target=grab, args=(c1, h_a))
    t2 = threading.Thread(target=grab, args=(c2, h_b))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert len(results) == 2

    conn = app_mod.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM arrivals WHERE source=? AND source_clock=? AND ingest_clock=?",
            (PAYLOAD["source"], PAYLOAD["source_clock"], PAYLOAD["ingest_clock"]),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, f"同一源到达被写重复：{len(rows)} 行（应排队后只留一行）"

    ev = client.get("/api/grade/events", headers=h)
    items = ev.json()["items"] if ev.status_code == 200 else []
    contend = [e for e in items if e["kind"] == "迟到分级争用"]
    assert len(contend) == 1, f"后到者未记「迟到分级争用」（现存 {len(contend)} 条）"

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], f"抢交应一成一拒（200/409），实得 {codes}"
