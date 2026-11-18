import sqlite3

import app as app_module
from helpers import fresh_legacy_db


def test_legacy_duplicate_arrivals_migrate_to_contention(tmp_path, monkeypatch):
    db = tmp_path / "legacy.db"
    fresh_legacy_db(db)
    monkeypatch.setattr(app_module, "DB_PATH", db)
    app_module.init_db()
    c = sqlite3.connect(db)
    n = c.execute("SELECT COUNT(*) FROM arrivals").fetchone()[0]
    rows = c.execute("SELECT winner,loser FROM contention").fetchall()
    idx = c.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ux_arrivals_triple'").fetchone()
    c.close()
    assert n == 1, "旧库重复到达未去重"
    assert len(rows) == 1 and rows[0][1] == "legacy"
    assert idx is not None
