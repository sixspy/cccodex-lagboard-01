import hashlib, secrets, sqlite3
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
POLICY_DOC = ROOT / "docs" / "迟到分级说明.md"
SESSIONS: dict[str, str] = {}

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

# 迟到分级说明册锁定口径：本仓选定甲路径（值班令牌鉴权），乙路径不启用。
POLICY_PATH = "甲"
POLICY_LABEL = "甲路径 · 值班令牌鉴权"
POLICY_EFFECTIVE_DATE = "2026-11-19"
BANDS = (("正常", 1), ("告警", 2), ("违约", None))
MODE_OF = {"正常": "可写", "告警": "降级只读", "违约": "冻结"}


def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def policy_doc_sha() -> str:
    return hashlib.sha256(POLICY_DOC.read_bytes()).hexdigest() if POLICY_DOC.exists() else ""


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


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


app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def init_db():
    conn = connect()
    try:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL,
          mode_prev TEXT NOT NULL DEFAULT '可写'
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_arrivals_identity
          ON arrivals(source, source_clock, ingest_clock);
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS duty_tokens(
          token TEXT PRIMARY KEY,
          operator TEXT NOT NULL,
          username TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS grade_events(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          kind TEXT NOT NULL,
          operator TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS grade_policy(
          id INTEGER PRIMARY KEY CHECK (id = 1),
          path TEXT NOT NULL,
          sla_multiplier_warn INTEGER NOT NULL,
          sla_multiplier_breach INTEGER NOT NULL,
          effective_date TEXT NOT NULL,
          doc_sha TEXT NOT NULL,
          locked INTEGER NOT NULL
        );
        """)
        # 兼容旧库：补 mode_prev 列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sources)").fetchall()}
        if "mode_prev" not in cols:
            conn.execute("ALTER TABLE sources ADD COLUMN mode_prev TEXT NOT NULL DEFAULT '可写'")
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO sources(name,sla_minutes,output_mode,mode_prev) VALUES (?,?,?,?)",
                [("订单源", 15, "可写", "可写"), ("支付源", 20, "降级只读", "可写"), ("物流源", 30, "冻结", "可写")],
            )
            conn.execute(
                "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                ("支付源", "2026-11-16T08:00:00", "2026-11-16T08:35:00", 35, "告警"),
            )
            conn.execute(
                "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                ("物流源", "2026-11-16T07:00:00", "2026-11-16T09:10:00", 130, "违约"),
            )
        if conn.execute("SELECT COUNT(*) c FROM grade_policy").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO grade_policy VALUES (1,?,?,?,?,?,?)",
                (POLICY_PATH, 1, 2, POLICY_EFFECTIVE_DATE, policy_doc_sha(), 1),
            )
    finally:
        conn.close()


def level_of(lag, sla):
    if lag <= sla:
        return "正常"
    if lag <= sla * 2:
        return "告警"
    return "违约"


def mode_of(level):
    return MODE_OF[level]


def log_event(conn, source, kind, operator, detail):
    conn.execute(
        "INSERT INTO grade_events(source,kind,operator,detail,created_at) VALUES (?,?,?,?,?)",
        (source, kind, operator, detail, datetime.now().isoformat(timespec="seconds")),
    )


def snapshot(conn):
    """五处读数的唯一来源：源目录、到达列表、迟到详情、补采附页、水位卡片都由它派生。"""
    rows = []
    for s in conn.execute("SELECT * FROM sources ORDER BY id").fetchall():
        last = conn.execute(
            "SELECT * FROM arrivals WHERE source=? ORDER BY id DESC LIMIT 1", (s["name"],)
        ).fetchone()
        ev = conn.execute(
            "SELECT kind,detail FROM grade_events WHERE source=? ORDER BY id DESC LIMIT 1", (s["name"],)
        ).fetchone()
        kinds = {
            r["kind"]: r["c"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) c FROM grade_events WHERE source=? GROUP BY kind", (s["name"],)
            ).fetchall()
        }
        # 撕裂态：最近到达所推档位与源实际输出态不一致。
        # 若最后一次改变输出态的机制事件是「补齐恢复」，属于补齐后的正常恢复，不算撕裂。
        band_conflict = False
        if last is not None and s["output_mode"] != MODE_OF.get(last["level"]):
            last_mode_ev = conn.execute(
                "SELECT kind FROM grade_events WHERE source=? AND kind IN ('分级落库','补齐恢复')"
                " ORDER BY id DESC LIMIT 1",
                (s["name"],),
            ).fetchone()
            band_conflict = not (last_mode_ev is not None and last_mode_ev["kind"] == "补齐恢复")
        rows.append({
            "name": s["name"],
            "sla_minutes": s["sla_minutes"],
            "lag_minutes": last["lag_minutes"] if last else 0,
            "level": last["level"] if last else "—",
            "output_mode": s["output_mode"],
            "mode_prev": s["mode_prev"],
            "band_conflict": band_conflict,
            "last_event": ev["kind"] if ev else "",
            "events": kinds,
            "arrival_count": conn.execute(
                "SELECT COUNT(*) c FROM arrivals WHERE source=?", (s["name"],)
            ).fetchone()["c"],
        })
    return rows


def effective_mechanism(row):
    """迟到详情：标出「令牌拒写」与「档位回退」何者生效。"""
    if row["last_event"] == "越权":
        return "令牌拒写"
    if row["last_event"] == "迟到分级争用":
        return "争用排队"
    if row["last_event"] == "分级落库":
        return "分级落库"
    return "无"


@app.on_event("startup")
def _s():
    init_db()


@app.get("/api/health")
def health():
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    finally:
        conn.close()
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT username,role FROM users WHERE username=? AND password_sha=?",
            (body.username, sha(body.password)),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


class DutyIn(BaseModel):
    operator: str


@app.post("/api/duty/token")
def duty_token(body: DutyIn, authorization: str | None = Header(default=None)):
    user = require_user(authorization)
    tok = secrets.token_hex(12)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO duty_tokens(token,operator,username,created_at) VALUES (?,?,?,?)",
            (tok, body.operator, user, datetime.now().isoformat(timespec="seconds")),
        )
    finally:
        conn.close()
    return {"duty_token": tok, "operator": body.operator}


@app.get("/api/grade/policy")
def grade_policy(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        p = conn.execute("SELECT * FROM grade_policy WHERE id=1").fetchone()
    finally:
        conn.close()
    return {
        **{k: p[k] for k in p.keys()},
        "label": POLICY_LABEL,
        "alternative": "乙路径（未启用）",
        "doc_sha": p["doc_sha"],
        "doc_sha_current": policy_doc_sha(),
    }


class PolicyIn(BaseModel):
    path: str


@app.put("/api/grade/policy")
def change_policy(body: PolicyIn, authorization: str | None = Header(default=None)):
    """锁定册当天不许改口：一律 423，并记「分级偏了」。"""
    require_user(authorization)
    conn = connect()
    try:
        log_event(conn, "*", "分级偏了", "untrusted", f"尝试改口迟到分级册为 {body.path!r}")
    finally:
        conn.close()
    raise HTTPException(423, "迟到分级册已锁定，当天不许改口")


@app.get("/api/grade/events")
def grade_events(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM grade_events ORDER BY id DESC").fetchall()]
    finally:
        conn.close()
    return {"items": items}


class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str
    policy_path: str | None = None


@app.post("/api/arrivals")
def add_arrival(
    body: ArrivalIn,
    authorization: str | None = Header(default=None),
    x_duty_token: str | None = Header(default=None, alias="X-Duty-Token"),
):
    user = require_user(authorization)
    conn = connect()
    try:
        src = conn.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
    finally:
        conn.close()
    if not src:
        raise HTTPException(404, "未知源")

    # 偏离锁定册：请求自带别的口径，直接拒绝并记「分级偏了」。
    if body.policy_path is not None and body.policy_path != POLICY_PATH:
        conn = connect()
        try:
            log_event(conn, body.source, "分级偏了", user, f"请求口径 {body.policy_path!r} ≠ 锁定册 {POLICY_PATH!r}")
        finally:
            conn.close()
        raise HTTPException(423, "偏离迟到分级说明册，已拒绝并记录")

    try:
        lag = int((datetime.fromisoformat(body.ingest_clock) - datetime.fromisoformat(body.source_clock)).total_seconds() // 60)
    except ValueError:
        raise HTTPException(422, "时钟格式不合法")
    if lag < 0:
        lag = 0
    lv = level_of(lag, src["sla_minutes"])
    mode = mode_of(lv)

    conn = connect()
    try:
        # 甲路径：分级写请求必须携带有效值班令牌；失败则整笔拒写、记越权、不切档。
        duty = None
        if x_duty_token:
            duty = conn.execute("SELECT * FROM duty_tokens WHERE token=?", (x_duty_token,)).fetchone()
        if not duty:
            conn.execute("BEGIN IMMEDIATE")
            log_event(conn, body.source, "越权", "anonymous", "缺值班令牌，拒写到达行（甲路径），正式侧保持未切档")
            conn.execute("COMMIT")
            raise HTTPException(403, "缺值班令牌：拒写到达行（甲路径）")

        conn.execute("BEGIN IMMEDIATE")
        try:
            old_mode = conn.execute(
                "SELECT output_mode FROM sources WHERE name=?", (body.source,)
            ).fetchone()["output_mode"]
            conn.execute(
                "INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                (body.source, body.source_clock, body.ingest_clock, lag, lv),
            )
            # 到达行与源输出态同事务落库：禁止分级写成功却源输出态仍旧。
            conn.execute(
                "UPDATE sources SET output_mode=?, mode_prev=? WHERE name=?",
                (mode, old_mode, body.source),
            )
            log_event(conn, body.source, "分级落库", duty["operator"],
                      f"迟到 {lag} 分 → {lv}；输出态 {old_mode} → {mode}")
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            conn.execute("BEGIN IMMEDIATE")
            log_event(conn, body.source, "迟到分级争用", duty["operator"],
                      "同一源到达已被先到值班落库，后到只记争用")
            conn.execute("COMMIT")
            raise HTTPException(409, "迟到分级争用：先到值班已落库，后到只记争用")
        except HTTPException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()

    return {"lag_minutes": lag, "level": lv, "output_mode": mode, "mode_prev": old_mode,
            "operator": duty["operator"], "policy_path": POLICY_PATH}


@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        return {"items": [dict(r) for r in conn.execute(
            "SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}
    finally:
        conn.close()


@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        return {"items": snapshot(conn)}
    finally:
        conn.close()


@app.get("/api/delay/{source}")
def delay_detail(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        rows = snapshot(conn)
    finally:
        conn.close()
    row = next((r for r in rows if r["name"] == source), None)
    if not row:
        raise HTTPException(404, "未知源")
    return {
        **row,
        "effective_mechanism": effective_mechanism(row),
        "token_reject_status": "生效（甲路径：缺值班令牌拒写且不切档）",
        "rollback_status": "未启用（乙路径：档位回退不适用）",
        "policy_path": POLICY_PATH,
    }


@app.get("/api/snapshot")
def snapshot_api(authorization: str | None = Header(default=None)):
    """五处读数共用快照，逐源对齐。"""
    require_user(authorization)
    conn = connect()
    try:
        rows = snapshot(conn)
        catchups = [dict(r) for r in conn.execute(
            "SELECT * FROM catchups ORDER BY id DESC").fetchall()]
    finally:
        conn.close()
    cards = {
        "sources": len(rows),
        "frozen": sum(1 for r in rows if r["output_mode"] == "冻结"),
        "degraded": sum(1 for r in rows if r["output_mode"] == "降级只读"),
        "writable": sum(1 for r in rows if r["output_mode"] == "可写"),
        "max_lag": max((r["lag_minutes"] for r in rows), default=0),
        "contention": sum(r["events"].get("迟到分级争用", 0) for r in rows),
    }
    return {"items": rows, "cards": cards, "catchups": catchups, "policy_path": POLICY_PATH}


@app.get("/api/watermark")
def watermark(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        rows = snapshot(conn)
    finally:
        conn.close()
    return {"cards": {
        "sources": len(rows),
        "frozen": sum(1 for r in rows if r["output_mode"] == "冻结"),
        "degraded": sum(1 for r in rows if r["output_mode"] == "降级只读"),
        "writable": sum(1 for r in rows if r["output_mode"] == "可写"),
        "max_lag": max((r["lag_minutes"] for r in rows), default=0),
    }, "items": rows}


@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        return {"items": [dict(r) for r in conn.execute(
            "SELECT * FROM catchups ORDER BY id DESC").fetchall()]}
    finally:
        conn.close()


@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    conn = connect()
    try:
        src = conn.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
        if not src:
            raise HTTPException(404, "未知源")
        old = src["output_mode"]
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE sources SET output_mode='可写', mode_prev=? WHERE name=?", (old, source))
        conn.execute(
            "INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
            (source, "成功", "可写", f"补齐后恢复可写（原 {old}）", now),
        )
        log_event(conn, source, "补齐恢复", "补齐操作", f"输出态 {old} → 可写")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {"ok": True, "output_mode": "可写", "mode_prev": old}


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
