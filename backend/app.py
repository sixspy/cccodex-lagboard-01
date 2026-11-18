import hashlib
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 迟到分级互斥落库写法：本仓锁定甲路径。
GRADE_PATH = "甲"
GRADE_LOCKED = 1
PATH_ALIASES = {
    "甲": "甲", "jia": "甲", "甲路径": "甲", "same-tx": "甲", "同事务": "甲",
    "乙": "乙", "yi": "乙", "乙路径": "乙", "shadow": "乙", "影子": "乙",
}
MODE_OF_LEVEL = {"正常": "可写", "告警": "降级只读", "违约": "冻结"}

# sqlite 单文件写路径串行化（同进程）+ BEGIN IMMEDIATE（跨进程）。
GRADE_LOCK = threading.RLock()


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def session():
    """短连接：用完即关，避免 WAL 下句柄泄漏；异常沿调用方处理。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def today() -> str:
    return datetime.now().astimezone().date().isoformat()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def rule_lock_sha(path: str, rule_day: str) -> str:
    return sha(f"late-grade|path={path}|day={rule_day}|tiers=SLA,2*SLA|modes=可写,降级只读,冻结")


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


def columns(conn, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}


def ensure_rule_locked(conn):
    """迟到分级册：首次按当日锁定；当天不许改口。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS grade_rules(
        id INTEGER PRIMARY KEY CHECK (id = 1),
        path TEXT NOT NULL,
        rule_day TEXT NOT NULL,
        locked INTEGER NOT NULL,
        lock_sha TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    row = conn.execute("SELECT * FROM grade_rules WHERE id=1").fetchone()
    if row is None:
        day = today()
        conn.execute(
            "INSERT INTO grade_rules(id,path,rule_day,locked,lock_sha,created_at) VALUES (1,?,?,?,?,?)",
            (GRADE_PATH, day, GRADE_LOCKED, rule_lock_sha(GRADE_PATH, day), now_iso()),
        )
        return
    # 已锁册不允许改口：口径或路径漂移立即落偏差，不改册。
    expected = rule_lock_sha(GRADE_PATH, row["rule_day"])
    if row["path"] != GRADE_PATH or row["locked"] != 1 or row["lock_sha"] != expected:
        record_deviation(
            conn, source=None, actor="system",
            detail=f"迟到分级册与锁定口径不符：path={row['path']} day={row['rule_day']}",
        )
        return
    # 锁定按“当日”生效：跨天滚动到新当日并重算 lock_sha，路径与档位口径不变。
    day = today()
    if row["rule_day"] != day:
        conn.execute("UPDATE grade_rules SET rule_day=?, lock_sha=? WHERE id=1",
                     (day, rule_lock_sha(GRADE_PATH, day)))


def _dedupe_legacy_arrivals(conn):
    """旧库若已存在同源到达重复行，先留最早一条，其余记为迟到分级争用，再加唯一索引。"""
    dups = conn.execute(
        "SELECT source, source_clock, ingest_clock, COUNT(*) n FROM arrivals "
        "GROUP BY source, source_clock, ingest_clock HAVING n > 1").fetchall()
    for d in dups:
        rows = conn.execute(
            "SELECT * FROM arrivals WHERE source=? AND source_clock=? AND ingest_clock=? ORDER BY id",
            (d["source"], d["source_clock"], d["ingest_clock"])).fetchall()
        winner = rows[0]
        for loser in rows[1:]:
            conn.execute(
                "INSERT INTO contention(source,source_clock,ingest_clock,winner,loser,lag_minutes,level,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (d["source"], d["source_clock"], d["ingest_clock"], winner["actor"] or "seed",
                 loser["actor"] or "legacy", loser["lag_minutes"], loser["level"], now_iso()))
            conn.execute("DELETE FROM arrivals WHERE id=?", (loser["id"],))


def init_db():
    with session() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL,
          actor TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contention(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          winner TEXT NOT NULL,
          loser TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS grade_deviations(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          source TEXT,
          actor TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        # 旧库补列（不重建表）
        if "actor" not in columns(conn, "arrivals"):
            conn.execute("ALTER TABLE arrivals ADD COLUMN actor TEXT NOT NULL DEFAULT ''")
        if "restored_after_arrival_id" not in columns(conn, "sources"):
            conn.execute("ALTER TABLE sources ADD COLUMN restored_after_arrival_id INTEGER")
        _dedupe_legacy_arrivals(conn)
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS ux_arrivals_triple
                        ON arrivals(source, source_clock, ingest_clock)""")
        ensure_rule_locked(conn)
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,actor) VALUES (?,?,?,?,?,?)",
                         ("支付源", "2026-11-16T08:00:00", "2026-11-16T08:35:00", 35, "告警", "seed"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,actor) VALUES (?,?,?,?,?,?)",
                         ("物流源", "2026-11-16T07:00:00", "2026-11-16T09:10:00", 130, "违约", "seed"))


def level_of(lag: int, sla: int) -> str:
    """唯一分级函数：只认迟到分钟与源时限，不由页面/请求体反推。"""
    if lag <= sla:
        return "正常"
    if lag <= sla * 2:
        return "告警"
    return "违约"


def mode_of(level: str) -> str:
    return MODE_OF_LEVEL[level]


def record_deviation(conn, *, source, actor, detail):
    conn.execute(
        "INSERT INTO grade_deviations(kind,source,actor,detail,created_at) VALUES (?,?,?,?,?)",
        ("分级偏了", source, actor, detail, now_iso()),
    )


def parse_clocks(source_clock: str, ingest_clock: str) -> int:
    try:
        a = datetime.fromisoformat(source_clock)
        b = datetime.fromisoformat(ingest_clock)
    except ValueError:
        raise HTTPException(422, "时钟格式不合法")
    lag = int((b - a).total_seconds() // 60)
    return max(lag, 0)


def normalize_path(raw: str | None) -> str | None:
    if raw is None:
        return None
    return PATH_ALIASES.get(str(raw).strip().lower(), str(raw).strip())


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def _s():
    init_db()


@app.get("/api/health")
def health():
    with session() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
        rule = c.execute("SELECT path,rule_day,locked FROM grade_rules WHERE id=1").fetchone()
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n,
            "db": str(DB_PATH), "grade_path": rule["path"] if rule else None,
            "rule_day": rule["rule_day"] if rule else None,
            "rule_locked": bool(rule["locked"]) if rule else False}


@app.post("/api/login")
def login(body: LoginIn):
    with session() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?",
                        (body.username, sha(body.password))).fetchone()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/rules")
def get_rules(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        r = c.execute("SELECT * FROM grade_rules WHERE id=1").fetchone()
    return {"items": [dict(r)], "path": r["path"], "rule_day": r["rule_day"],
            "locked": bool(r["locked"]), "lock_sha": r["lock_sha"]}


@app.post("/api/rules")
def change_rule(request: Request, authorization: str | None = Header(default=None)):
    """改口迟到分级册一律拒绝并记「分级偏了」。"""
    actor = require_user(authorization)
    with session() as c:
        record_deviation(c, source=None, actor=actor, detail="请求改口迟到分级册（当天不许改口）")
    return JSONResponse(status_code=409, content={
        "code": "分级偏了", "detail": "迟到分级册当天锁定，禁止改口", "recorded": True,
    })


@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]
        for r in rows:
            last = c.execute("SELECT * FROM arrivals WHERE source=? ORDER BY id DESC LIMIT 1",
                             (r["name"],)).fetchone()
            r["last_arrival_id"] = last["id"] if last else None
            r["last_level"] = last["level"] if last else None
            r["restored_after_arrival_id"] = r.get("restored_after_arrival_id")
    return {"items": rows}


class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str
    level: str | None = None
    path: str | None = None
    rule_day: str | None = None
    actor: str | None = None


def _actor_name(username: str, token: str, body_actor: str | None) -> str:
    if body_actor:
        return f"{username}/{body_actor}"
    return f"{username}#{token[-6:]}"


def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(401, "未登录")
    return authorization.removeprefix("Bearer ").strip()


@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    username = require_user(authorization)
    token = _bearer(authorization)
    actor = _actor_name(username, token, body.actor)

    # 锁定口径校验：偏离说明册即拒绝并记「分级偏了」。
    with session() as c:
        rule = c.execute("SELECT * FROM grade_rules WHERE id=1").fetchone()
        req_path = normalize_path(body.path)
        if req_path is not None and req_path != rule["path"]:
            record_deviation(c, source=body.source, actor=actor,
                             detail=f"请求路径 {body.path} 偏离锁定路径 {rule['path']}")
            return JSONResponse(status_code=409, content={
                "code": "分级偏了", "detail": "迟到分级只允许锁定路径 甲，甲乙不得混用", "recorded": True})
        if body.rule_day is not None and body.rule_day != rule["rule_day"]:
            record_deviation(c, source=body.source, actor=actor,
                             detail=f"请求改口 rule_day={body.rule_day}，锁定为 {rule['rule_day']}")
            return JSONResponse(status_code=409, content={
                "code": "分级偏了", "detail": "当天不许改口迟到分级册", "recorded": True})
        if normalize_path(rule["path"]) != GRADE_PATH or not rule["locked"]:
            record_deviation(c, source=body.source, actor=actor, detail="迟到分级册未按甲路径锁定")
            return JSONResponse(status_code=409, content={
                "code": "分级偏了", "detail": "迟到分级册未锁定", "recorded": True})

        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
        if not src:
            raise HTTPException(404, "未知源")

        lag = parse_clocks(body.source_clock, body.ingest_clock)
        level = level_of(lag, src["sla_minutes"])
        mode = mode_of(level)
        if body.level is not None and body.level != level:
            record_deviation(c, source=body.source, actor=actor,
                             detail=f"请求等级 {body.level} 与迟到 {lag} 分推算 {level} 不符")
            return JSONResponse(status_code=409, content={
                "code": "分级偏了", "detail": "请求等级偏离迟到分钟分级", "recorded": True})

    # —— 甲路径：到达行与源输出态同一事务改写，失败整笔回退 ——
    with GRADE_LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            dup = conn.execute(
                "SELECT * FROM arrivals WHERE source=? AND source_clock=? AND ingest_clock=?",
                (body.source, body.source_clock, body.ingest_clock),
            ).fetchone()
            if dup is not None:
                conn.execute("ROLLBACK")
                _record_contention(conn, body, dup, actor, lag, level)
                return JSONResponse(status_code=409, content={
                    "code": "迟到分级争用",
                    "detail": "同一源到达已有值班先交，后到只记迟到分级争用",
                    "winner": dup["actor"], "recorded": True})

            cur = conn.execute(
                "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level,actor) "
                "VALUES (?,?,?,?,?,?)",
                (body.source, body.source_clock, body.ingest_clock, lag, level, actor))
            arrival_id = cur.lastrowid
            conn.execute(
                "UPDATE sources SET output_mode=?, restored_after_arrival_id=NULL WHERE name=?",
                (mode, body.source))
            check = conn.execute("SELECT output_mode FROM sources WHERE name=?", (body.source,)).fetchone()
            if check is None or check["output_mode"] != mode:
                raise sqlite3.IntegrityError("源输出态回读不一致")
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as e:
            _rollback_quietly(conn)
            if "UNIQUE" in str(e).upper():
                dup = conn.execute(
                    "SELECT * FROM arrivals WHERE source=? AND source_clock=? AND ingest_clock=?",
                    (body.source, body.source_clock, body.ingest_clock)).fetchone()
                _record_contention(conn, body, dup, actor, lag, level)
                return JSONResponse(status_code=409, content={
                    "code": "迟到分级争用", "detail": "并发抢交同一源到达，后到只记争用",
                    "winner": dup["actor"] if dup else "", "recorded": True})
            return JSONResponse(status_code=503, content={
                "code": "整笔回退", "rolled_back": True,
                "detail": f"到达行与源输出态同一事务改写失败，已整笔回退：{e}",
                "arrival_id": None})
        except Exception as e:  # noqa: BLE001 —— 任何失败都不得半截成功
            _rollback_quietly(conn)
            return JSONResponse(status_code=503, content={
                "code": "整笔回退", "rolled_back": True,
                "detail": f"到达行与源输出态同一事务改写失败，已整笔回退：{e}",
                "arrival_id": None})
        finally:
            conn.close()

    return {"arrival_id": arrival_id, "lag_minutes": lag, "level": level,
            "output_mode": mode, "grade_path": GRADE_PATH, "actor": actor}


def _rollback_quietly(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _record_contention(conn, body, dup, actor, lag, level):
    """后到者不写业务数据，只记迟到分级争用。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO contention(source,source_clock,ingest_clock,winner,loser,lag_minutes,level,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (body.source, body.source_clock, body.ingest_clock,
             dup["actor"] if dup else "", actor, lag, level, now_iso()))
        conn.execute("COMMIT")
    except sqlite3.Error:
        _rollback_quietly(conn)


@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]
    return {"items": rows}


@app.get("/api/late")
def late_list(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT a.*, s.sla_minutes, s.output_mode FROM arrivals a "
            "JOIN sources s ON s.name=a.source ORDER BY a.id DESC").fetchall()]
    items = []
    for r in rows:
        expected = level_of(r["lag_minutes"], r["sla_minutes"])
        items.append({**r, "expected_level": expected, "expected_mode": mode_of(expected),
                      "consistent": r["level"] == expected})
    return {"items": items}


@app.get("/api/late/{arrival_id}")
def late_detail(arrival_id: int, authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        r = c.execute("SELECT a.*, s.sla_minutes, s.output_mode FROM arrivals a "
                      "JOIN sources s ON s.name=a.source WHERE a.id=?", (arrival_id,)).fetchone()
        if not r:
            raise HTTPException(404, "到达不存在")
    d = dict(r)
    expected = level_of(d["lag_minutes"], d["sla_minutes"])
    d["expected_level"] = expected
    d["expected_mode"] = mode_of(expected)
    d["consistent"] = d["level"] == expected
    return d


@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = now_iso()
    with GRADE_LOCK:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            src = conn.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
            if not src:
                conn.execute("ROLLBACK")
                raise HTTPException(404, "未知源")
            last = conn.execute("SELECT id FROM arrivals WHERE source=? ORDER BY id DESC LIMIT 1",
                                (source,)).fetchone()
            last_id = last["id"] if last else 0
            conn.execute("UPDATE sources SET output_mode='可写', restored_after_arrival_id=? WHERE name=?",
                         (last_id, source))
            conn.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                         (source, "成功", "可写", "补齐后恢复可写", now))
            conn.execute("COMMIT")
        except HTTPException:
            raise
        except sqlite3.Error:
            _rollback_quietly(conn)
            return JSONResponse(status_code=503, content={"code": "整笔回退", "rolled_back": True})
        finally:
            conn.close()
    return {"ok": True, "output_mode": "可写", "restored_after_arrival_id": last_id, "created_at": now}


@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}


@app.get("/api/contention")
def contention_list(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM contention ORDER BY id DESC").fetchall()]}


@app.get("/api/deviations")
def deviations_list(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        return {"items": [dict(r) for r in c.execute("SELECT * FROM grade_deviations ORDER BY id DESC").fetchall()]}


@app.get("/api/watermark")
def watermark(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with session() as c:
        srcs = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]
        items = []
        for s in srcs:
            last = c.execute("SELECT * FROM arrivals WHERE source=? ORDER BY id DESC LIMIT 1",
                             (s["name"],)).fetchone()
            last_level = last["level"] if last else None
            restored = (last is not None and s.get("restored_after_arrival_id") is not None
                        and s["restored_after_arrival_id"] >= last["id"])
            effective_mode = "可写" if restored else s["output_mode"]
            items.append({
                "source": s["name"], "sla_minutes": s["sla_minutes"],
                "last_arrival_id": last["id"] if last else None,
                "last_lag_minutes": last["lag_minutes"] if last else None,
                "last_level": last_level,
                "output_mode": s["output_mode"],
                "restored": restored,
                "effective_mode": effective_mode,
                "arrival_count": c.execute("SELECT COUNT(*) c FROM arrivals WHERE source=?",
                                           (s["name"],)).fetchone()["c"],
                "late_count": c.execute("SELECT COUNT(*) c FROM arrivals WHERE source=? AND level<>'正常'",
                                        (s["name"],)).fetchone()["c"],
            })
        return {"items": items,
                "frozen": sum(1 for i in items if i["effective_mode"] == "冻结"),
                "degraded": sum(1 for i in items if i["effective_mode"] == "降级只读")}


@app.get("/api/reconcile")
def reconcile(authorization: str | None = Header(default=None)):
    """五处读数对账：源目录 / 到达列表 / 迟到详情 / 补采附页 / 水位卡片。

    五处均按同一迟到分级函数派生后相互比对，不只比页面颜色。
    """
    require_user(authorization)
    with session() as c:
        srcs = [dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]
        mismatches = []

        # 读数①源目录
        catalog = {s["name"]: s["output_mode"] for s in srcs}

        # 读数②到达列表（含每源最新一条）
        last_by_source = {}
        for name in catalog:
            row = c.execute("SELECT * FROM arrivals WHERE source=? ORDER BY id DESC LIMIT 1",
                            (name,)).fetchone()
            last_by_source[name] = dict(row) if row else None

        # 读数③迟到详情：逐条按迟到分钟重算
        detail = {}
        for a in c.execute("SELECT * FROM arrivals ORDER BY id").fetchall():
            sla = c.execute("SELECT sla_minutes FROM sources WHERE name=?", (a["source"],)).fetchone()
            if sla is None:
                continue
            detail[a["id"]] = level_of(a["lag_minutes"], sla["sla_minutes"])

        # 读数④补采附页：每源最新一次补齐
        catchup_last = {}
        for name in catalog:
            row = c.execute("SELECT * FROM catchups WHERE source=? ORDER BY id DESC LIMIT 1",
                            (name,)).fetchone()
            catchup_last[name] = dict(row) if row else None

        # 读数⑤水位卡片
        for name, s in ((x["name"], x) for x in srcs):
            last = last_by_source[name]
            restored = (s.get("restored_after_arrival_id") is not None
                        and last is not None
                        and s["restored_after_arrival_id"] >= last["id"])
            effective_mode = "可写" if restored else catalog[name]

            # 逐条：到达列表的 level 与迟到详情重算必须一致
            for a in c.execute("SELECT * FROM arrivals WHERE source=? ORDER BY id", (name,)).fetchall():
                exp = detail.get(a["id"])
                if exp is not None and a["level"] != exp:
                    mismatches.append({
                        "kind": "到达等级与迟到分钟不符", "source": name,
                        "arrival_id": a["id"], "lag_minutes": a["lag_minutes"],
                        "actual_level": a["level"], "expected_level": exp,
                        "expected_mode": mode_of(exp)})

            if last is None:
                continue

            expected_mode = "可写" if restored else mode_of(detail.get(last["id"], last["level"]))
            # 源目录 vs 水位卡片 effective_mode
            if catalog[name] != expected_mode:
                mismatches.append({
                    "kind": "源输出态与时限档不符", "source": name,
                    "actual_mode": catalog[name], "expected_mode": expected_mode,
                    "last_arrival_id": last["id"], "last_lag_minutes": last["lag_minutes"],
                    "last_level": last["level"]})
            if effective_mode != expected_mode:
                mismatches.append({
                    "kind": "水位卡片与时限档不符", "source": name,
                    "actual_mode": effective_mode, "expected_mode": expected_mode,
                    "last_arrival_id": last["id"]})
            # 补采附页：最后一条补齐若声称可写，源目录必须已可写且早于最新到达
            cup = catchup_last.get(name)
            if restored and cup is not None and cup["restored_mode"] != catalog[name]:
                mismatches.append({
                    "kind": "补采附页与源目录不符", "source": name,
                    "actual_mode": catalog[name], "restored_mode": cup["restored_mode"]})

        return {"consistent": len(mismatches) == 0, "mismatches": mismatches,
                "views": ["源目录", "到达列表", "迟到详情", "补采附页", "水位卡片"]}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
