def test_lag_freeze_and_catchup(client, admin):
    assert client.get("/api/health").json()["product"] == "源到达SLA"
    r = client.post("/api/arrivals", headers=admin, json={"source": "订单源", "source_clock": "2026-11-16T10:00:00", "ingest_clock": "2026-11-16T11:00:00"})
    assert r.status_code == 200 and r.json()["output_mode"] == "冻结"
    assert client.post("/api/catchup/订单源", headers=admin).json()["output_mode"] == "可写"
