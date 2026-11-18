import json
import sqlite3
from pathlib import Path

import app as app_module

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "late_grade_cross_tier.json"


def _seed_cross_tier_mismatch(app_mod):
    """夹具：迟到分钟 130 已跨两档，源目录输出态却仍是可写，到达行等级还写着正常。"""
    f = json.loads(FIXTURE.read_text(encoding="utf-8"))
    conn = sqlite3.connect(app_mod.DB_PATH)
    conn.execute("UPDATE sources SET output_mode=? WHERE name=?", (f["stale_output_mode"], f["source"]))
    conn.execute("DELETE FROM arrivals WHERE source=?", (f["source"],))
    conn.execute(
        "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
        (f["source"], f["source_clock"], f["ingest_clock"], f["lag_minutes"], f["observed_level"]),
    )
    conn.commit()
    conn.close()
    return f


def test_cross_tier_mismatch_is_reproduced_and_detected(_db, client, admin):
    """复现题面错切：迟到分钟跨档，源输出态未切，五处读数对不上。"""
    f = _seed_cross_tier_mismatch(_db)
    r = client.get("/api/reconcile", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["consistent"] is False
    assert any(
        m.get("source") == f["source"] and m.get("kind") == "源输出态与时限档不符"
        for m in body["mismatches"]
    ), body
    # 到达行自身等级也必须被判定为与迟到分钟不符
    assert any(m.get("kind") == "到达等级与迟到分钟不符" for m in body["mismatches"]), body


def test_reconcile_detects_no_mismatch_when_state_aligned(_db, client, admin):
    r = client.get("/api/reconcile", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["consistent"] is True
    assert r.json()["mismatches"] == []
