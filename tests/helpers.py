import sqlite3


def fresh_legacy_db(path):
    """构造未迁移旧库：无 actor、无 restored 列、无唯一索引，且已有重复到达。"""
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE sources(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
        sla_minutes INTEGER NOT NULL, output_mode TEXT NOT NULL);
    CREATE TABLE arrivals(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
        source_clock TEXT NOT NULL, ingest_clock TEXT NOT NULL,
        lag_minutes INTEGER NOT NULL, level TEXT NOT NULL);
    CREATE TABLE catchups(id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL,
        status TEXT NOT NULL, restored_mode TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
    INSERT INTO sources(name,sla_minutes,output_mode) VALUES ('订单源',15,'可写');
    INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level)
        VALUES ('订单源','2026-11-16T10:00:00','2026-11-16T10:10:00',10,'正常'),
               ('订单源','2026-11-16T10:00:00','2026-11-16T10:10:00',10,'正常');
    """)
    c.commit(); c.close()
