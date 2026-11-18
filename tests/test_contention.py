import threading

import app as app_module


def _login(client, user="admin", pwd="123456"):
    r = client.post("/api/login", json={"username": user, "password": pwd})
    assert r.status_code == 200, r.text
    return "Bearer " + r.json()["token"]


def test_two_collectors_racing_same_arrival_queue_and_record_contention(_db, client):
    """两名采集值班同时抢交同一源到达：后到只记迟到分级争用，不得双写。"""
    h1 = _login(client)
    h2 = _login(client)
    statuses = {}
    barrier = threading.Barrier(2)

    payload = {"source": "订单源", "source_clock": "2026-11-16T12:00:00", "ingest_clock": "2026-11-16T12:10:00"}

    def submit(name, token):
        barrier.wait()
        r = client.post("/api/arrivals", headers={"Authorization": token}, json=payload)
        statuses[name] = (r.status_code, r.json())

    t1 = threading.Thread(target=submit, args=("甲", h1))
    t2 = threading.Thread(target=submit, args=("乙", h2))
    for t in (t1, t2):
        t.start()
    for t in (t1, t2):
        t.join()

    ok = [v for v in statuses.values() if v[0] == 200]
    lost = [v for v in statuses.values() if v[0] == 409]
    assert len(ok) == 1, statuses
    assert len(lost) == 1, statuses
    assert lost[0][1]["code"] == "迟到分级争用"

    conn = _db.DB_PATH
    import sqlite3
    c = sqlite3.connect(conn)
    n = c.execute(
        "SELECT COUNT(*) FROM arrivals WHERE source=? AND source_clock=? AND ingest_clock=?",
        (payload["source"], payload["source_clock"], payload["ingest_clock"]),
    ).fetchone()[0]
    c.close()
    assert n == 1, "同一源到达被双写"
    r = client.get("/api/contention", headers={"Authorization": h1})
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "订单源"
