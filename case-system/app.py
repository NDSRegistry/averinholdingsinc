import os
from __future__ import annotations
from flask import Flask, request, jsonify, render_template, abort
import sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

BRAND_TITLE = "Averin Holdings"
BRAND_SUBTITLE = "Powered by NDS Registry"

API_KEY = "CHANGE_ME_TO_A_LONG_SECRET"
HOST = "127.0.0.1"
PORT = 8000

DB_PATH = "registry.db"

app = Flask(__name__)


# ------------------------
# Helpers
# ------------------------

def now_utc() -> str:
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        identifier TEXT UNIQUE NOT NULL,
        platform TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS cases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        case_type TEXT NOT NULL,
        platform TEXT NOT NULL,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN / CLOSED / ARCHIVED
        thread_id TEXT,                      -- discord thread id (forum post)
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS case_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER NOT NULL,
        event_type TEXT NOT NULL,   -- CREATE / UPDATE / NOTE / STATUS / ARCHIVE / THREAD
        message TEXT NOT NULL,
        author TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(case_id) REFERENCES cases(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_intel (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        intel_type TEXT NOT NULL,   -- ALT / NOTE / FLAG
        value TEXT NOT NULL,
        author TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )
    """)

    # Indexes for speed
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cases_user_id ON cases(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cases_status ON cases(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cases_type ON cases(case_type)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_users_identifier ON users(identifier)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_case_id ON case_events(case_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_intel_user_id ON user_intel(user_id)")

    conn.commit()
    conn.close()


def api_auth_or_401():
    if request.headers.get("X-API-Key") != API_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


def row_to_dict(r):
    return dict(r) if r else None


# ------------------------
# Website routes
# ------------------------

@app.route("/")
def registry():
    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().upper()
    case_type = (request.args.get("type") or "").strip()
    platform = (request.args.get("platform") or "").strip()

    conn = db()
    sql = """
        SELECT
            cases.*,
            users.identifier AS user_identifier,
            users.platform   AS user_platform
        FROM cases
        JOIN users ON users.id = cases.user_id
        WHERE 1=1
    """
    params = []

    if q:
        sql += " AND users.identifier LIKE ?"
        params.append(f"%{q}%")

    if status in ("OPEN", "CLOSED", "ARCHIVED"):
        sql += " AND cases.status = ?"
        params.append(status)

    if case_type:
        sql += " AND cases.case_type = ?"
        params.append(case_type)

    if platform:
        sql += " AND cases.platform = ?"
        params.append(platform)

    sql += " ORDER BY cases.updated_at DESC LIMIT 500"

    cases = conn.execute(sql, params).fetchall()
    conn.close()

    return render_template(
        "index.html",
        BRAND_TITLE=BRAND_TITLE,
        BRAND_SUBTITLE=BRAND_SUBTITLE,
        cases=cases,
        filters={"q": q, "status": status, "type": case_type, "platform": platform},
        CASE_TYPES=_case_types(),
        PLATFORMS=_platforms(),
    )


@app.route("/dashboard")
def dashboard():
    conn = db()

    total = conn.execute("SELECT COUNT(*) AS c FROM cases").fetchone()["c"]
    open_count = conn.execute("SELECT COUNT(*) AS c FROM cases WHERE status='OPEN'").fetchone()["c"]
    closed_count = conn.execute("SELECT COUNT(*) AS c FROM cases WHERE status='CLOSED'").fetchone()["c"]
    archived_count = conn.execute("SELECT COUNT(*) AS c FROM cases WHERE status='ARCHIVED'").fetchone()["c"]

    by_type = conn.execute("""
        SELECT case_type, COUNT(*) AS c
        FROM cases
        GROUP BY case_type
        ORDER BY c DESC
    """).fetchall()

    by_platform = conn.execute("""
        SELECT platform, COUNT(*) AS c
        FROM cases
        GROUP BY platform
        ORDER BY c DESC
    """).fetchall()

    # Trend last 14 days
    start = (datetime.utcnow() - timedelta(days=13)).strftime("%Y-%m-%d")
    trend_rows = conn.execute("""
        SELECT substr(created_at, 1, 10) AS day, COUNT(*) AS c
        FROM cases
        WHERE substr(created_at, 1, 10) >= ?
        GROUP BY day
        ORDER BY day ASC
    """, (start,)).fetchall()

    conn.close()

    # Normalize trend to include missing days
    trend_map = {r["day"]: r["c"] for r in trend_rows}
    days = []
    counts = []
    for i in range(14):
        d = (datetime.utcnow() - timedelta(days=13 - i)).strftime("%Y-%m-%d")
        days.append(d)
        counts.append(trend_map.get(d, 0))

    return render_template(
        "dashboard.html",
        BRAND_TITLE=BRAND_TITLE,
        BRAND_SUBTITLE=BRAND_SUBTITLE,
        total=total,
        open_count=open_count,
        closed_count=closed_count,
        archived_count=archived_count,
        by_type=[{"label": r["case_type"], "value": r["c"]} for r in by_type],
        by_platform=[{"label": r["platform"], "value": r["c"]} for r in by_platform],
        trend_days=days,
        trend_counts=counts
    )


@app.route("/case/<int:case_id>")
def case_page(case_id: int):
    conn = db()
    case = conn.execute("""
        SELECT
            cases.*,
            users.id         AS user_id,
            users.identifier AS user_identifier,
            users.platform   AS user_platform
        FROM cases
        JOIN users ON users.id = cases.user_id
        WHERE cases.id = ?
    """, (case_id,)).fetchone()

    if not case:
        conn.close()
        abort(404)

    events = conn.execute("""
        SELECT * FROM case_events
        WHERE case_id = ?
        ORDER BY id DESC
        LIMIT 200
    """, (case_id,)).fetchall()

    intel = conn.execute("""
        SELECT * FROM user_intel
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 200
    """, (case["user_id"],)).fetchall()

    conn.close()

    return render_template(
        "case.html",
        BRAND_TITLE=BRAND_TITLE,
        BRAND_SUBTITLE=BRAND_SUBTITLE,
        case=case,
        events=events,
        intel=intel
    )


@app.route("/user/<int:user_id>")
def user_page(user_id: int):
    conn = db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        abort(404)

    cases = conn.execute("""
        SELECT * FROM cases
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 200
    """, (user_id,)).fetchall()

    intel = conn.execute("""
        SELECT * FROM user_intel
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 200
    """, (user_id,)).fetchall()

    conn.close()

    return render_template(
        "user.html",
        BRAND_TITLE=BRAND_TITLE,
        BRAND_SUBTITLE=BRAND_SUBTITLE,
        user=user,
        cases=cases,
        intel=intel
    )


# ------------------------
# API helpers: enums
# ------------------------

def _case_types():
    return ["R-Individual", "R-Discord", "R-Group", "D-Server", "ROBLOX", "Discord"]

def _platforms():
    return ["Discord", "ROBLOX", "External"]


# ------------------------
# API routes
# ------------------------

@app.route("/api/meta", methods=["GET"])
def api_meta():
    auth = api_auth_or_401()
    if auth:
        return auth
    return jsonify({"ok": True, "case_types": _case_types(), "platforms": _platforms()})


@app.route("/api/users/lookup", methods=["GET"])
def api_user_lookup():
    auth = api_auth_or_401()
    if auth:
        return auth

    identifier = (request.args.get("identifier") or "").strip()
    if not identifier:
        return jsonify({"ok": False, "error": "identifier required"}), 400

    conn = db()
    user = conn.execute("SELECT * FROM users WHERE identifier = ?", (identifier,)).fetchone()
    if not user:
        conn.close()
        return jsonify({"ok": False, "error": "user not found"}), 404

    cases = conn.execute("""
        SELECT * FROM cases
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 50
    """, (user["id"],)).fetchall()

    intel = conn.execute("""
        SELECT * FROM user_intel
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 50
    """, (user["id"],)).fetchall()

    conn.close()

    return jsonify({
        "ok": True,
        "user": row_to_dict(user),
        "cases": [row_to_dict(c) for c in cases],
        "intel": [row_to_dict(i) for i in intel],
    })


@app.route("/api/cases", methods=["POST"])
def api_create_case():
    auth = api_auth_or_401()
    if auth:
        return auth

    d = request.get_json(silent=True) or {}
    identifier = (d.get("identifier") or "").strip()
    platform = (d.get("platform") or "").strip()
    case_type = (d.get("case_type") or "").strip()
    reason = (d.get("reason") or "").strip()
    author = (d.get("author") or "staff").strip()

    if not identifier or not platform or not case_type or not reason:
        return jsonify({"ok": False, "error": "identifier, platform, case_type, reason required"}), 400

    if case_type not in _case_types():
        return jsonify({"ok": False, "error": "invalid case_type"}), 400

    if platform not in _platforms():
        return jsonify({"ok": False, "error": "invalid platform"}), 400

    conn = db()
    cur = conn.cursor()
    ts = now_utc()

    user = cur.execute("SELECT id FROM users WHERE identifier = ?", (identifier,)).fetchone()
    if not user:
        cur.execute("""
            INSERT INTO users (identifier, platform, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (identifier, platform, ts, ts))
        user_id = cur.lastrowid
    else:
        user_id = user["id"]
        cur.execute("UPDATE users SET updated_at = ? WHERE id = ?", (ts, user_id))

    cur.execute("""
        INSERT INTO cases (user_id, case_type, platform, reason, status, thread_id, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'OPEN', NULL, ?, ?)
    """, (user_id, case_type, platform, reason, ts, ts))

    case_id = cur.lastrowid

    cur.execute("""
        INSERT INTO case_events (case_id, event_type, message, author, created_at)
        VALUES (?, 'CREATE', ?, ?, ?)
    """, (case_id, f"Case created. Type={case_type}, Platform={platform}", author, ts))

    conn.commit()
    conn.close()

    return jsonify({"ok": True, "case_id": case_id, "user_id": user_id})


@app.route("/api/cases/<int:case_id>", methods=["GET"])
def api_get_case(case_id: int):
    auth = api_auth_or_401()
    if auth:
        return auth

    conn = db()
    case = conn.execute("""
        SELECT
            cases.*,
            users.identifier AS user_identifier
        FROM cases
        JOIN users ON users.id = cases.user_id
        WHERE cases.id = ?
    """, (case_id,)).fetchone()

    if not case:
        conn.close()
        return jsonify({"ok": False, "error": "case not found"}), 404

    conn.close()
    return jsonify({"ok": True, "case": row_to_dict(case)})


@app.route("/api/cases/<int:case_id>", methods=["PATCH"])
def api_patch_case(case_id: int):
    auth = api_auth_or_401()
    if auth:
        return auth

    d = request.get_json(silent=True) or {}
    author = (d.get("author") or "staff").strip()

    allowed_fields = {
        "reason": "reason",
        "status": "status",
        "thread_id": "thread_id",
        "case_type": "case_type",
        "platform": "platform",
    }

    updates = []
    params = []

    for k, col in allowed_fields.items():
        if k in d and d[k] is not None and str(d[k]).strip() != "":
            val = str(d[k]).strip()
            if k == "status":
                val = val.upper()
                if val not in ("OPEN", "CLOSED", "ARCHIVED"):
                    return jsonify({"ok": False, "error": "invalid status"}), 400
            if k == "case_type" and val not in _case_types():
                return jsonify({"ok": False, "error": "invalid case_type"}), 400
            if k == "platform" and val not in _platforms():
                return jsonify({"ok": False, "error": "invalid platform"}), 400
            updates.append(f"{col} = ?")
            params.append(val)

    if not updates:
        return jsonify({"ok": False, "error": "nothing to update"}), 400

    ts = now_utc()
    updates.append("updated_at = ?")
    params.append(ts)
    params.append(case_id)

    conn = db()
    cur = conn.cursor()
    existing = cur.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not existing:
        conn.close()
        return jsonify({"ok": False, "error": "case not found"}), 404

    cur.execute(f"UPDATE cases SET {', '.join(updates)} WHERE id = ?", params)

    # event log
    msg = (d.get("log_message") or "Case updated").strip()
    cur.execute("""
        INSERT INTO case_events (case_id, event_type, message, author, created_at)
        VALUES (?, 'UPDATE', ?, ?, ?)
    """, (case_id, msg, author, ts))

    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/api/cases/<int:case_id>/events", methods=["POST"])
def api_add_case_event(case_id: int):
    auth = api_auth_or_401()
    if auth:
        return auth

    d = request.get_json(silent=True) or {}
    event_type = (d.get("event_type") or "NOTE").strip().upper()
    message = (d.get("message") or "").strip()
    author = (d.get("author") or "staff").strip()

    if not message:
        return jsonify({"ok": False, "error": "message required"}), 400

    if event_type not in ("NOTE", "STATUS", "ARCHIVE", "THREAD", "UPDATE"):
        event_type = "NOTE"

    conn = db()
    cur = conn.cursor()

    exists = cur.execute("SELECT id FROM cases WHERE id = ?", (case_id,)).fetchone()
    if not exists:
        conn.close()
        return jsonify({"ok": False, "error": "case not found"}), 404

    ts = now_utc()
    cur.execute("""
        INSERT INTO case_events (case_id, event_type, message, author, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (case_id, event_type, message, author, ts))

    cur.execute("UPDATE cases SET updated_at = ? WHERE id = ?", (ts, case_id))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.route("/api/users/<int:user_id>/intel", methods=["POST"])
def api_add_user_intel(user_id: int):
    auth = api_auth_or_401()
    if auth:
        return auth

    d = request.get_json(silent=True) or {}
    intel_type = (d.get("intel_type") or "").strip().upper()
    value = (d.get("value") or "").strip()
    author = (d.get("author") or "staff").strip()

    if intel_type not in ("ALT", "NOTE", "FLAG"):
        return jsonify({"ok": False, "error": "intel_type must be ALT/NOTE/FLAG"}), 400
    if not value:
        return jsonify({"ok": False, "error": "value required"}), 400

    conn = db()
    cur = conn.cursor()

    u = cur.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
    if not u:
        conn.close()
        return jsonify({"ok": False, "error": "user not found"}), 404

    ts = now_utc()
    cur.execute("""
        INSERT INTO user_intel (user_id, intel_type, value, author, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (user_id, intel_type, value, author, ts))

    cur.execute("UPDATE users SET updated_at = ? WHERE id = ?", (ts, user_id))
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


if __name__ == "__main__":
    init_db()
    app.run(host=HOST, port=PORT, debug=True)
