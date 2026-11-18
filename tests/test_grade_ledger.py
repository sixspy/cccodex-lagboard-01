import sqlite3

import app as app_module


def _mode(db, source):
    c = sqlite3.connect(db)
    m = c.execute("SELECT output_mode FROM sources WHERE name=?", (source,)).fetchone()[0]
    c.close()
    return m


def test_rulebook_locked_to_jia_path_for_today(client, admin, _db):
    r = client.get("/api/rules", headers=admin)
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "甲"
    assert body["locked"] is True
    assert body["rule_day"] == app_module.today()
    assert body["lock_sha"] == app_module.rule_lock_sha("甲", app_module.today())


def test_rulebook_change_rejected_and_deviation_recorded(client, admin):
    r = client.post("/api/rules", headers=admin, json={"path": "乙"})
    assert r.status_code == 409
    assert r.json()["code"] == "分级偏了"
    d = client.get("/api/deviations", headers=admin).json()["items"]
    assert any(x["kind"] == "分级偏了" and "改口" in x["detail"] for x in d)
    # 册子仍是甲路径当日锁定，不因请求改口
    assert client.get("/api/rules", headers=admin).json()["path"] == "甲"


def test_request_with_yi_path_rejected_as_deviation(client, admin):
    r = client.post("/api/arrivals", headers=admin, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00",
        "ingest_clock": "2026-11-16T10:05:00", "path": "乙"})
    assert r.status_code == 409 and r.json()["code"] == "分级偏了"
    assert client.get("/api/deviations", headers=admin).json()["items"]


def test_request_level_mismatch_rejected(client, admin):
    r = client.post("/api/arrivals", headers=admin, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00",
        "ingest_clock": "2026-11-16T10:05:00", "level": "违约"})
    assert r.status_code == 409 and r.json()["code"] == "分级偏了"


def test_cross_tier_write_updates_both_arrival_and_source_mode(client, admin, _db):
    """跨两档：10 分正常 → 130 分违约，输出态必须真的切到冻结（非页面颜色）。"""
    assert _mode(_db.DB_PATH, "订单源") == "可写"
    r = client.post("/api/arrivals", headers=admin, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00",
        "ingest_clock": "2026-11-16T12:10:00"})
    assert r.status_code == 200, r.text
    j = r.json()
    assert (j["lag_minutes"], j["level"], j["output_mode"]) == (130, "违约", "冻结")
    assert _mode(_db.DB_PATH, "订单源") == "冻结"
    # 五处读数一致
    src = [s for s in client.get("/api/sources", headers=admin).json()["items"] if s["name"] == "订单源"][0]
    assert src["output_mode"] == "冻结" and src["last_level"] == "违约"
    arr = client.get(f"/api/late/{j['arrival_id']}", headers=admin).json()
    assert arr["level"] == "违约" and arr["consistent"] is True
    wm = [w for w in client.get("/api/watermark", headers=admin).json()["items"] if w["source"] == "订单源"][0]
    assert wm["last_level"] == "违约" and wm["effective_mode"] == "冻结"
    rec = client.get("/api/reconcile", headers=admin).json()
    assert rec["consistent"] is True, rec


def test_warn_tier_is_degraded_readonly(client, admin, _db):
    r = client.post("/api/arrivals", headers=admin, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00",
        "ingest_clock": "2026-11-16T10:20:00"})
    assert r.status_code == 200
    assert r.json()["level"] == "告警" and r.json()["output_mode"] == "降级只读"
    assert _mode(_db.DB_PATH, "订单源") == "降级只读"


def test_catchup_restores_and_keeps_views_aligned(client, admin, _db):
    r = client.post("/api/arrivals", headers=admin, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00",
        "ingest_clock": "2026-11-16T11:00:00"})
    aid = r.json()["arrival_id"]
    assert _mode(_db.DB_PATH, "订单源") == "冻结"
    up = client.post("/api/catchup/订单源", headers=admin).json()
    assert up["output_mode"] == "可写" and up["restored_after_arrival_id"] == aid
    items = client.get("/api/catchups", headers=admin).json()["items"]
    assert items and items[0]["source"] == "订单源" and items[0]["restored_mode"] == "可写"
    wm = [w for w in client.get("/api/watermark", headers=admin).json()["items"] if w["source"] == "订单源"][0]
    assert wm["restored"] is True and wm["effective_mode"] == "可写"
    assert client.get("/api/reconcile", headers=admin).json()["consistent"] is True


def test_no_shadow_arrival_table_exists(_db):
    """甲路径不得同时落乙路径的影子表：甲乙不得混用。"""
    c = sqlite3.connect(_db.DB_PATH)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    c.close()
    assert not any("shadow" in t.lower() or "影子" in t for t in tables), tables
    assert "arrivals_shadow" not in tables


def test_failed_write_leaves_no_arrival_and_no_mode_change(client, admin, _db):
    c = sqlite3.connect(_db.DB_PATH)
    c.execute("""CREATE TRIGGER boom BEFORE UPDATE OF output_mode ON sources
                 WHEN NEW.name='订单源'
                 BEGIN SELECT RAISE(ABORT,'mode write broken'); END""")
    c.commit(); c.close()
    before = sqlite3.connect(_db.DB_PATH).execute(
        "SELECT COUNT(*) FROM arrivals WHERE source='订单源'").fetchone()[0]
    r = client.post("/api/arrivals", headers=admin, json={
        "source": "订单源", "source_clock": "2026-11-16T10:00:00",
        "ingest_clock": "2026-11-16T11:00:00"})
    assert r.status_code == 503 and r.json()["rolled_back"] is True
    c = sqlite3.connect(_db.DB_PATH)
    after = c.execute("SELECT COUNT(*) FROM arrivals WHERE source='订单源'").fetchone()[0]
    mode = c.execute("SELECT output_mode FROM sources WHERE name='订单源'").fetchone()[0]
    c.close()
    assert after == before and mode == "可写"


def test_rulebook_rolls_to_new_day_but_keeps_jia_path(_db, client, admin):
    """跨天只滚动 rule_day 与 lock_sha，路径仍锁定甲，不改口。"""
    import sqlite3

    c = sqlite3.connect(_db.DB_PATH)
    c.execute("UPDATE grade_rules SET rule_day='2026-01-01', lock_sha=? WHERE id=1",
              (app_module.rule_lock_sha("甲", "2026-01-01"),))
    c.commit(); c.close()
    _db.init_db()  # 模拟重启时的册子校验
    rules = client.get("/api/rules", headers=admin).json()
    assert rules["path"] == "甲"
    assert rules["rule_day"] == app_module.today()
    assert rules["lock_sha"] == app_module.rule_lock_sha("甲", app_module.today())
    assert client.get("/api/deviations", headers=admin).json()["items"] == []


def test_tampered_rulebook_records_deviation_without_rewriting(_db, client, admin):
    import sqlite3

    c = sqlite3.connect(_db.DB_PATH)
    c.execute("UPDATE grade_rules SET path='乙' WHERE id=1")
    c.commit(); c.close()
    _db.init_db()  # 模拟重启时的册子校验，只记偏差不改回
    assert client.get("/api/health").json()["grade_path"] == "乙"  # 库内被改，不自动洗白
    devs = client.get("/api/deviations", headers=admin).json()["items"]
    assert any("与锁定口径不符" in d["detail"] for d in devs)
