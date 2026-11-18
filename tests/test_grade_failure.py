import sqlite3

import app as app_module


def _break_output_mode_write(app_mod, source):
    conn = sqlite3.connect(app_mod.DB_PATH)
    conn.execute(
        f"""CREATE TRIGGER boom BEFORE UPDATE OF output_mode ON sources
            WHEN NEW.name = '{source}'
            BEGIN SELECT RAISE(ABORT, 'mode write broken'); END"""
    )
    conn.commit()
    conn.close()


def test_half_success_write_is_rejected_and_rolled_back(_db, client, admin):
    """分级写库中途失败：整笔回退，禁止半截成功（到达行不得留下）。"""
    _break_output_mode_write(_db, "支付源")
    before = _db.DB_PATH and sqlite3.connect(_db.DB_PATH)
    n_before = before.execute("SELECT COUNT(*) FROM arrivals").fetchone()[0]
    mode_before = before.execute("SELECT output_mode FROM sources WHERE name='支付源'").fetchone()[0]
    before.close()

    r = client.post(
        "/api/arrivals",
        headers=admin,
        json={"source": "支付源", "source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T10:40:00"},
    )
    assert r.status_code == 503, r.text
    body = r.json()
    assert body["rolled_back"] is True
    assert body["code"] == "整笔回退"

    conn = sqlite3.connect(_db.DB_PATH)
    n_after = conn.execute("SELECT COUNT(*) FROM arrivals").fetchone()[0]
    mode_after = conn.execute("SELECT output_mode FROM sources WHERE name='支付源'").fetchone()[0]
    conn.close()
    assert n_after == n_before, "半截成功：到达行已落库"
    assert mode_after == mode_before


def test_reconcile_catches_drift_between_catalog_and_detail(_db, client, admin):
    """写库侧被旁路修改时，五处读数须能对出来（不是只在页面改色）。"""
    import sqlite3

    conn = sqlite3.connect(_db.DB_PATH)
    conn.execute("UPDATE sources SET output_mode='可写' WHERE name='物流源'")
    conn.commit()
    conn.close()
    rec = client.get("/api/reconcile", headers=admin).json()
    assert rec["consistent"] is False
    kinds = {(m["kind"], m["source"]) for m in rec["mismatches"]}
    assert ("源输出态与时限档不符", "物流源") in kinds
    assert ("水位卡片与时限档不符", "物流源") in kinds


def test_watermark_and_catalog_agree_after_failed_write(_db, client, admin):
    import sqlite3

    conn = sqlite3.connect(_db.DB_PATH)
    conn.execute("""CREATE TRIGGER boom2 BEFORE UPDATE OF output_mode ON sources
                    WHEN NEW.name='物流源'
                    BEGIN SELECT RAISE(ABORT,'mode write broken'); END""")
    conn.commit()
    conn.close()
    r = client.post("/api/arrivals", headers=admin, json={
        "source": "物流源", "source_clock": "2026-11-16T07:30:00",
        "ingest_clock": "2026-11-16T09:40:00"})
    assert r.status_code == 503
    rec = client.get("/api/reconcile", headers=admin).json()
    assert rec["consistent"] is True, rec
