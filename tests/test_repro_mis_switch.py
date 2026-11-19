"""错切复现：迟到分钟已跨档（违约）而源输出态仍是可写（撕裂）。

要复现并修掉三件事：
1. 撕裂态必须被读数标出（band_conflict），不能装作对齐；
2. 缺值班令牌的分级写必须整笔拒写、不切档、记越权（甲路径）；
3. 有效值班令牌的分级写必须让迟到档与源输出态重新对齐。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_mod

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "lag_cross_grade_stale_mode.json").read_text("utf-8"))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_mod, "DB_PATH", tmp_path / "repro.db")
    app_mod.SESSIONS.clear()
    app_mod.init_db()
    conn = app_mod.connect()
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM arrivals"); conn.execute("DELETE FROM sources")
        for t in ("grade_events", "duty_tokens"):
            try:
                conn.execute(f"DELETE FROM {t}")
            except Exception:
                pass
        for s in FIXTURE["sources"]:
            conn.execute("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)",
                         (s["name"], s["sla_minutes"], s["output_mode"]))
        for a in FIXTURE["seed_arrivals"]:
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         (a["source"], a["source_clock"], a["ingest_clock"], a["lag_minutes"], a["level"]))
        conn.execute("COMMIT")
    finally:
        conn.close()
    with TestClient(app_mod.app, raise_server_exceptions=False) as c:
        yield c


def _login(c):
    return c.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]


def test_repro_stale_band_is_flagged(client):
    h = {"Authorization": "Bearer " + _login(client)}
    row = client.get("/api/sources", headers=h).json()["items"][0]
    # 夹具现状：late 70 分（违约）但输出态 可写 —— 撕裂。
    assert row["lag_minutes"] == FIXTURE["observed_bug"]["stale_before"]["lag_minutes"]
    assert row["output_mode"] == FIXTURE["observed_bug"]["stale_before"]["output_mode"]
    assert row["band_conflict"] is True, (
        "撕裂态（迟到档与源输出态不一致）未被读数标出：band_conflict="
        + repr(row.get("band_conflict"))
    )


def test_repro_write_without_duty_token_is_refused_not_half_written(client):
    h = {"Authorization": "Bearer " + _login(client)}
    a = FIXTURE["attempt"]
    r = client.post("/api/arrivals", headers=h, json={
        "source": a["source"], "source_clock": a["source_clock"], "ingest_clock": a["ingest_clock"]})
    assert r.status_code == 403, f"错切复现：缺值班令牌仍被受理 HTTP {r.status_code} {r.text[:200]}"
    rows = client.get("/api/arrivals", headers=h).json()["items"]
    assert not any(x["ingest_clock"] == a["ingest_clock"] for x in rows), "错切复现：缺令牌仍落了到达行"
    assert client.get("/api/delay/" + a["source"], headers=h).json()["output_mode"] == "可写", (
        "错切复现：缺令牌仍切了源输出态"
    )
    d = client.get("/api/delay/" + a["source"], headers=h).json()
    assert d["effective_mechanism"] == FIXTURE["expected_after"]["detail_effective"], (
        f"迟到详情未标出令牌拒写生效：{d.get('effective_mechanism')!r}"
    )


def test_repro_duty_write_realigns_band_and_mode(client):
    h = {"Authorization": "Bearer " + _login(client)}
    duty = client.post("/api/duty/token", headers=h, json={"operator": "值班甲"}).json()["duty_token"]
    a = FIXTURE["attempt"]
    r = client.post("/api/arrivals", headers={**h, "X-Duty-Token": duty}, json={
        "source": a["source"], "source_clock": a["source_clock"], "ingest_clock": a["ingest_clock"]})
    assert r.status_code == 200, r.text
    assert r.json()["output_mode"] == FIXTURE["expected_after"]["write_with_duty"]["source_output_mode_after"]
    row = client.get("/api/sources", headers=h).json()["items"][0]
    assert row["band_conflict"] is False, "有效值班令牌写入后迟到档与源输出态仍未对齐"
