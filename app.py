"""
My AI Studio — Flask single-file app
แชท + สร้างรูปผ่าน OpenRouter, เก็บข้อมูลทั้งหมด (ผู้ใช้, API Key, ตั้งค่า, แชท, รูป) ลง SQLite

ติดตั้ง:  pip install flask
รัน:      python app.py        แล้วเปิด http://127.0.0.1:5050
ไฟล์ฐานข้อมูล: ai_studio.db (ข้างไฟล์นี้)  หรือกำหนดเองด้วย env AISTUDIO_DB

ระบบผู้ใช้: ไม่มีหน้าสมัครสมาชิก — แอดมินสร้างบัญชีและส่งรหัสผ่านให้ผู้ใช้เอง
  เปิดครั้งแรก (ยังไม่มีผู้ใช้) หน้าเว็บจะให้สร้างบัญชีแอดมินก่อน
  ลืมรหัส:  python app.py --reset-password <username>

Developed by Uncle Engineer — https://uncle-engineer.com/
"""
import datetime
import functools
import getpass
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time

from flask import Flask, Response, abort, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("AISTUDIO_DB", os.path.join(BASE_DIR, "ai_studio.db"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # รูปใหญ่สุด 64 MB ต่อครั้ง

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chats (
    id      TEXT PRIMARY KEY,
    title   TEXT,
    model   TEXT,
    updated INTEGER,
    user_id INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id   TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    pos       INTEGER NOT NULL,
    role      TEXT NOT NULL,
    content   TEXT,
    model     TEXT,
    reasoning TEXT,
    usage     TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, pos);
CREATE TABLE IF NOT EXISTS images (
    id      TEXT PRIMARY KEY,
    prompt  TEXT,
    model   TEXT,
    params  TEXT,
    mime    TEXT,
    data    BLOB,
    url     TEXT,
    cost    REAL,
    secs    INTEGER,
    created INTEGER,
    user_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_images_created ON images(created);
-- ข้อมูลลับฝั่งเซิร์ฟเวอร์ (secret key) — ไม่ถูกส่งไปหน้าเว็บ
CREATE TABLE IF NOT EXISTS auth (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- บัญชีผู้ใช้ (แอดมินเป็นคนสร้าง ไม่มีหน้าสมัครเอง)
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
    pw_hash    TEXT NOT NULL,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    created    INTEGER,
    last_login INTEGER
);
-- ค่าส่วนตัวของแต่ละคน เช่น โมเดลที่เลือก, แชทล่าสุด, ตั้งค่าสร้างรูป
CREATE TABLE IF NOT EXISTS user_kv (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key     TEXT NOT NULL,
    value   TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""

# ค่าที่ใช้ร่วมกันทุกคน (ตาราง kv) — แก้ได้เฉพาะแอดมิน ที่เหลือเก็บแยกรายคนใน user_kv
GLOBAL_KV = ("aichat.apiKey", "aichat.settings")


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- Database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def adopt_orphans(con, uid):
    """แชท/รูป/ค่าที่สร้างก่อนมีระบบผู้ใช้ ยกให้เป็นของแอดมินคนแรก"""
    con.execute("UPDATE chats SET user_id = ? WHERE user_id IS NULL", (uid,))
    con.execute("UPDATE images SET user_id = ? WHERE user_id IS NULL", (uid,))
    ph = ",".join("?" * len(GLOBAL_KV))
    con.execute(
        f"INSERT OR IGNORE INTO user_kv(user_id, key, value) "
        f"SELECT ?, key, value FROM kv WHERE key NOT IN ({ph})",
        (uid, *GLOBAL_KV),
    )
    con.execute(f"DELETE FROM kv WHERE key NOT IN ({ph})", GLOBAL_KV)


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    # ฐานข้อมูลเวอร์ชันก่อนยังไม่มีเจ้าของแชท/รูป
    for table in ("chats", "images"):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if "user_id" not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
    con.execute("CREATE INDEX IF NOT EXISTS idx_chats_user ON chats(user_id, updated)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_images_user ON images(user_id, created)")
    # รหัสผ่านเมนูตั้งค่าแบบเดิม → กลายเป็นบัญชีแอดมินชื่อ admin (รหัสเดิม)
    old = con.execute("SELECT value FROM auth WHERE key = 'password_hash'").fetchone()
    if old:
        if not con.execute("SELECT 1 FROM users").fetchone():
            cur = con.execute(
                "INSERT INTO users(username, pw_hash, is_admin, created) VALUES('admin', ?, 1, ?)",
                (old[0], now_ms()),
            )
            adopt_orphans(con, cur.lastrowid)
        con.execute("DELETE FROM auth WHERE key = 'password_hash'")
    con.commit()
    con.close()


init_db()


def load_secret_key():
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT value FROM auth WHERE key = 'secret_key'").fetchone()
    if row:
        key = row[0]
    else:
        key = secrets.token_hex(32)
        con.execute("INSERT INTO auth(key, value) VALUES('secret_key', ?)", (key,))
        con.commit()
    con.close()
    return key


app.secret_key = load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=14),
)

# ---------------------------------------------------------------- Users & login
MIN_PW = 6
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,32}$")
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))  # ให้เวลาตอบเท่ากันแม้ไม่มีชื่อผู้ใช้นี้


def _pw_ver(pw_hash):
    return pw_hash[-24:]


def current_user():
    if "user" not in g:
        u = None
        uid = session.get("uid")
        if uid:
            r = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
            # ผูก session กับ hash ปัจจุบัน — เปลี่ยน/รีเซ็ตรหัส หรือระงับบัญชี แล้วทุกเครื่องหลุดทันที
            if r and r["active"] and session.get("ver") == _pw_ver(r["pw_hash"]):
                u = r
        g.user = u
    return g.user


def me_id():
    return current_user()["id"]


def login_user(u):
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    session["ver"] = _pw_ver(u["pw_hash"])
    g.pop("user", None)


def me_json(u):
    return {"id": u["id"], "username": u["username"], "is_admin": bool(u["is_admin"])}


def user_json(r):
    return {
        "id": r["id"], "username": r["username"], "is_admin": bool(r["is_admin"]),
        "active": bool(r["active"]), "created": r["created"], "last_login": r["last_login"],
    }


def has_users():
    return get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def pw_problem(pw):
    if not isinstance(pw, str) or len(pw) < MIN_PW:
        return f"รหัสผ่านต้องยาวอย่างน้อย {MIN_PW} ตัวอักษร"
    return None


def username_problem(name):
    if not isinstance(name, str) or not USERNAME_RE.match(name):
        return "ชื่อผู้ใช้ต้องยาว 3–32 ตัว ใช้ได้เฉพาะ a-z, 0-9, จุด, ขีดกลาง, ขีดล่าง"
    return None


PUBLIC_ENDPOINTS = {"index", "auth_login", "auth_setup", "static"}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    if not current_user():
        return jsonify(error="กรุณาเข้าสู่ระบบก่อน"), 401
    return None


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*a, **kw):
        if not current_user()["is_admin"]:
            return jsonify(error="เฉพาะผู้ดูแลระบบเท่านั้น"), 403
        return f(*a, **kw)
    return wrapper


# กันเดารหัส: ผิด 5 ครั้งติดกัน ล็อก 30 วินาที (ต่อ IP)
_fails = {}
_fails_lock = threading.Lock()


def _blocked_for(ip):
    with _fails_lock:
        _n, until = _fails.get(ip, (0, 0))
        return max(0, int(until - time.time()))


def _record_attempt(ip, ok):
    with _fails_lock:
        if ok:
            _fails.pop(ip, None)
            return
        n, _until = _fails.get(ip, (0, 0))
        n += 1
        _fails[ip] = (0, time.time() + 30) if n >= 5 else (n, 0)


def load_kv(uid):
    out = {}
    ph = ",".join("?" * len(GLOBAL_KV))
    rows = list(get_db().execute(f"SELECT key, value FROM kv WHERE key IN ({ph})", GLOBAL_KV))
    rows += list(get_db().execute("SELECT key, value FROM user_kv WHERE user_id = ?", (uid,)))
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except ValueError:
            pass
    return out


def load_chats(uid):
    db = get_db()
    chats, by_id = [], {}
    for r in db.execute(
        "SELECT id, title, model, updated FROM chats WHERE user_id = ? ORDER BY updated", (uid,)
    ):
        c = {"id": r["id"], "title": r["title"] or "", "model": r["model"],
             "updated": r["updated"] or 0, "messages": []}
        chats.append(c)
        by_id[c["id"]] = c
    for r in db.execute(
        "SELECT m.* FROM messages m JOIN chats c ON c.id = m.chat_id "
        "WHERE c.user_id = ? ORDER BY m.chat_id, m.pos", (uid,)
    ):
        c = by_id.get(r["chat_id"])
        if not c:
            continue
        m = {"role": r["role"], "content": r["content"] or ""}
        if r["model"]:
            m["model"] = r["model"]
        if r["reasoning"]:
            m["reasoning"] = r["reasoning"]
        if r["usage"]:
            try:
                m["usage"] = json.loads(r["usage"])
            except ValueError:
                pass
        c["messages"].append(m)
    return chats


# ---------------------------------------------------------------- Pages
def _with_boot(page, boot):
    # กัน </script> ใน JSON ปิดแท็กก่อนเวลา
    boot_js = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    resp = Response(page.replace("/*__BOOT__*/null", boot_js, 1), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/")
def index():
    u = current_user()
    if not u:
        return _with_boot(LOGIN_PAGE, {"setup": not has_users()})
    return _with_boot(PAGE, {"kv": load_kv(u["id"]), "chats": load_chats(u["id"]), "me": me_json(u)})


@app.get("/api/state")
def state():
    u = current_user()
    return jsonify(kv=load_kv(u["id"]), chats=load_chats(u["id"]), me=me_json(u))


# ---------------------------------------------------------------- Key-value (settings, api key, caches)
@app.put("/api/kv/<path:key>")
def kv_put(key):
    if key in GLOBAL_KV and not current_user()["is_admin"]:
        return jsonify(error="เฉพาะผู้ดูแลระบบเท่านั้น"), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or "value" not in body:
        abort(400, "ต้องส่ง JSON รูปแบบ {\"value\": ...}")
    value = json.dumps(body["value"], ensure_ascii=False)
    db = get_db()
    with db:
        if key in GLOBAL_KV:
            db.execute(
                "INSERT INTO kv(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        else:
            db.execute(
                "INSERT INTO user_kv(user_id, key, value) VALUES(?, ?, ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                (me_id(), key, value),
            )
    return "", 204


@app.delete("/api/kv/<path:key>")
def kv_delete(key):
    if key in GLOBAL_KV and not current_user()["is_admin"]:
        return jsonify(error="เฉพาะผู้ดูแลระบบเท่านั้น"), 403
    db = get_db()
    with db:
        if key in GLOBAL_KV:
            db.execute("DELETE FROM kv WHERE key = ?", (key,))
        else:
            db.execute("DELETE FROM user_kv WHERE user_id = ? AND key = ?", (me_id(), key))
    return "", 204


# ---------------------------------------------------------------- Chats
def _owned_by_other(table, row_id):
    r = get_db().execute(f"SELECT user_id FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return r is not None and r["user_id"] != me_id()


@app.get("/api/chats")
def chats_list():
    return jsonify(load_chats(me_id()))


@app.put("/api/chats/<cid>")
def chat_put(cid):
    c = request.get_json(silent=True)
    if not isinstance(c, dict):
        abort(400)
    if _owned_by_other("chats", cid):
        abort(404)
    db = get_db()
    with db:
        db.execute(
            "INSERT INTO chats(id, title, model, updated, user_id) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET title = excluded.title, "
            "model = excluded.model, updated = excluded.updated",
            (cid, c.get("title", ""), c.get("model"), int(c.get("updated") or 0), me_id()),
        )
        db.execute("DELETE FROM messages WHERE chat_id = ?", (cid,))
        rows = []
        for i, m in enumerate(c.get("messages") or []):
            usage = m.get("usage")
            rows.append((
                cid, i, m.get("role", "user"), m.get("content", ""), m.get("model"),
                m.get("reasoning"),
                json.dumps(usage, ensure_ascii=False) if usage is not None else None,
            ))
        db.executemany(
            "INSERT INTO messages(chat_id, pos, role, content, model, reasoning, usage) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return "", 204


@app.delete("/api/chats/<cid>")
def chat_delete(cid):
    db = get_db()
    with db:
        db.execute("DELETE FROM chats WHERE id = ? AND user_id = ?", (cid, me_id()))
    return "", 204


@app.delete("/api/chats")
def chats_clear():
    db = get_db()
    with db:
        db.execute("DELETE FROM chats WHERE user_id = ?", (me_id(),))  # messages ลบตามด้วย CASCADE
    return "", 204


# ---------------------------------------------------------------- Images
@app.get("/api/images")
def images_list():
    rows = get_db().execute(
        "SELECT id, prompt, model, params, mime, url, cost, secs, created, "
        "data IS NOT NULL AS has_file FROM images WHERE user_id = ? ORDER BY created DESC",
        (me_id(),),
    )
    out = []
    for r in rows:
        try:
            params = json.loads(r["params"]) if r["params"] else {}
        except ValueError:
            params = {}
        out.append({
            "id": r["id"], "prompt": r["prompt"] or "", "model": r["model"],
            "params": params, "type": r["mime"], "url": r["url"], "cost": r["cost"],
            "secs": r["secs"], "created": r["created"] or 0, "has_file": bool(r["has_file"]),
        })
    return jsonify(out)


@app.post("/api/images")
def image_add():
    try:
        meta = json.loads(request.form.get("meta", "{}"))
    except ValueError:
        abort(400, "meta ไม่ใช่ JSON")
    if not meta.get("id"):
        abort(400, "ไม่มี id")
    if _owned_by_other("images", meta["id"]):
        abort(404)
    f = request.files.get("file")
    data = f.read() if f else None
    mime = (f.mimetype if f else None) or meta.get("type") or "image/png"
    if not data and not meta.get("url"):
        abort(400, "ไม่มีไฟล์รูปหรือ url")
    db = get_db()
    with db:
        db.execute(
            "INSERT OR REPLACE INTO images(id, prompt, model, params, mime, data, url, cost, secs, created, user_id) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meta["id"], meta.get("prompt", ""), meta.get("model"),
                json.dumps(meta.get("params") or {}, ensure_ascii=False), mime,
                sqlite3.Binary(data) if data else None, meta.get("url"),
                meta.get("cost"), meta.get("secs"), int(meta.get("created") or 0), me_id(),
            ),
        )
    return jsonify(ok=True, id=meta["id"], has_file=bool(data))


@app.get("/api/images/<iid>/file")
def image_file(iid):
    r = get_db().execute(
        "SELECT mime, data FROM images WHERE id = ? AND user_id = ?", (iid, me_id())
    ).fetchone()
    if not r or r["data"] is None:
        abort(404)
    resp = Response(bytes(r["data"]), mimetype=r["mime"] or "image/png")
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return resp


@app.delete("/api/images/<iid>")
def image_delete(iid):
    db = get_db()
    with db:
        db.execute("DELETE FROM images WHERE id = ? AND user_id = ?", (iid, me_id()))
    return "", 204


@app.delete("/api/images")
def images_clear():
    db = get_db()
    with db:
        db.execute("DELETE FROM images WHERE user_id = ?", (me_id(),))
    return "", 204


# ---------------------------------------------------------------- Auth API
def _body():
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@app.post("/api/auth/setup")
def auth_setup():
    """สร้างบัญชีแอดมินคนแรก — ใช้ได้ครั้งเดียวตอนยังไม่มีผู้ใช้เลย"""
    if has_users():
        return jsonify(error="มีบัญชีผู้ดูแลระบบแล้ว"), 409
    body = _body()
    name, pw = (body.get("username") or "").strip(), body.get("password")
    err = username_problem(name) or pw_problem(pw)
    if err:
        return jsonify(error=err), 400
    db = get_db()
    with db:
        cur = db.execute(
            "INSERT INTO users(username, pw_hash, is_admin, created, last_login) VALUES(?, ?, 1, ?, ?)",
            (name, generate_password_hash(pw), now_ms(), now_ms()),
        )
        adopt_orphans(db, cur.lastrowid)
    u = db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    login_user(u)
    return jsonify(me_json(u))


@app.post("/api/auth/login")
def auth_login():
    ip = request.remote_addr or "?"
    wait = _blocked_for(ip)
    if wait:
        return jsonify(error=f"ใส่รหัสผิดหลายครั้ง รออีก {wait} วินาทีแล้วลองใหม่"), 429
    body = _body()
    name, pw = (body.get("username") or "").strip(), body.get("password")
    pw = pw if isinstance(pw, str) else ""
    db = get_db()
    u = db.execute("SELECT * FROM users WHERE username = ?", (name,)).fetchone()
    ok = check_password_hash(u["pw_hash"] if u else _DUMMY_HASH, pw) and u is not None
    _record_attempt(ip, ok)
    if not ok:
        return jsonify(error="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง"), 401
    if not u["active"]:
        return jsonify(error="บัญชีนี้ถูกระงับ กรุณาติดต่อผู้ดูแลระบบ"), 403
    with db:
        db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now_ms(), u["id"]))
    login_user(u)
    return jsonify(me_json(u))


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return "", 204


@app.post("/api/auth/change")
def auth_change():
    body = _body()
    old, new = body.get("old") or "", body.get("new")
    u = current_user()
    if not check_password_hash(u["pw_hash"], old):
        return jsonify(error="รหัสผ่านปัจจุบันไม่ถูกต้อง"), 400
    err = pw_problem(new)
    if err:
        return jsonify(error=err), 400
    db = get_db()
    with db:
        db.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (generate_password_hash(new), u["id"]))
    login_user(db.execute("SELECT * FROM users WHERE id = ?", (u["id"],)).fetchone())
    return "", 204


# ---------------------------------------------------------------- User management (แอดมิน)
@app.get("/api/users")
@admin_required
def users_list():
    rows = get_db().execute("SELECT * FROM users ORDER BY is_admin DESC, username COLLATE NOCASE")
    return jsonify([user_json(r) for r in rows])


@app.post("/api/users")
@admin_required
def user_create():
    body = _body()
    name, pw = (body.get("username") or "").strip(), body.get("password")
    err = username_problem(name) or pw_problem(pw)
    if err:
        return jsonify(error=err), 400
    db = get_db()
    try:
        with db:
            cur = db.execute(
                "INSERT INTO users(username, pw_hash, is_admin, created) VALUES(?, ?, ?, ?)",
                (name, generate_password_hash(pw), 1 if body.get("is_admin") else 0, now_ms()),
            )
    except sqlite3.IntegrityError:
        return jsonify(error=f"มีชื่อผู้ใช้ {name} อยู่แล้ว"), 409
    return jsonify(user_json(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()))


def _target_user(uid):
    # กันแอดมินล็อกตัวเองออก — บัญชีตัวเองแก้ที่เมนูตั้งค่า
    if uid == me_id():
        abort(Response(json.dumps({"error": "แก้บัญชีของตัวเองที่เมนูตั้งค่าแทน"}, ensure_ascii=False),
                       400, mimetype="application/json"))
    r = get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    if not r:
        abort(404)
    return r


@app.patch("/api/users/<int:uid>")
@admin_required
def user_update(uid):
    _target_user(uid)
    body = _body()
    sets, args = [], []
    if "password" in body:
        err = pw_problem(body["password"])
        if err:
            return jsonify(error=err), 400
        sets.append("pw_hash = ?")
        args.append(generate_password_hash(body["password"]))
    for field in ("is_admin", "active"):
        if field in body:
            sets.append(f"{field} = ?")
            args.append(1 if body[field] else 0)
    if not sets:
        abort(400)
    db = get_db()
    with db:
        db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", (*args, uid))
    return jsonify(user_json(db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()))


@app.delete("/api/users/<int:uid>")
@admin_required
def user_delete(uid):
    _target_user(uid)
    db = get_db()
    with db:
        db.execute("DELETE FROM chats WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM images WHERE user_id = ?", (uid,))
        db.execute("DELETE FROM users WHERE id = ?", (uid,))  # user_kv ลบตามด้วย CASCADE
    return "", 204


# ---------------------------------------------------------------- Shared settings (แอดมิน)
@app.post("/api/settings")
@admin_required
def settings_save():
    body = request.get_json(silent=True) or {}
    key = (body.get("apiKey") or "").strip()
    settings = body.get("settings")
    if not isinstance(settings, dict):
        abort(400)
    db = get_db()
    with db:
        up = ("INSERT INTO kv(key, value) VALUES(?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value")
        if key:
            db.execute(up, ("aichat.apiKey", json.dumps(key)))
        else:
            db.execute("DELETE FROM kv WHERE key = 'aichat.apiKey'")
        db.execute(up, ("aichat.settings", json.dumps(settings, ensure_ascii=False)))
    return "", 204


# ---------------------------------------------------------------- Frontend
PAGE = r'''<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>My AI Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/marked/12.0.2/marked.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/dompurify/3.1.6/purify.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<style>
:root{
  --bg:#F3F5F8; --panel:#FFFFFF; --side:#E6EBF1; --ink:#18212C; --muted:#5C6878;
  --line:#D3DAE3; --accent:#2B59C3; --accent-ink:#FFFFFF; --amber:#C98A12;
  --user:#DCE6F7; --code:#EEF1F5; --danger:#C0392B; --ok:#2E8B57;
  --font:"IBM Plex Sans Thai",system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,Consolas,monospace;
  box-sizing:border-box;
  padding-top:env(safe-area-inset-top,0px); padding-bottom:env(safe-area-inset-bottom,0px);
}
@media (prefers-color-scheme: dark){
  :root{
    --bg:#141A22; --panel:#1B232D; --side:#10151C; --ink:#E4E9F0; --muted:#94A1B2;
    --line:#2C3744; --accent:#6C95F0; --accent-ink:#0E1420; --amber:#E7B04A;
    --user:#243453; --code:#10161E; --danger:#E26D5F; --ok:#4CC38A;
  }
}
*,*::before,*::after{box-sizing:inherit}
[hidden]{display:none!important}
.credit{font-size:12px;color:var(--muted);text-align:center;padding:6px 0 0}
.credit a{color:var(--accent);text-decoration:none}
.credit a:hover{text-decoration:underline}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.65 var(--font);overflow:hidden}
button,input,textarea,select{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}

.app{display:grid;grid-template-columns:272px 1fr;height:100%}
/* Sidebar */
.side{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.brand{padding:18px 18px 10px;font-weight:600;font-size:17px;display:flex;align-items:center;gap:10px}
.brand .dot{width:12px;height:12px;border-radius:3px;background:var(--accent);box-shadow:5px 5px 0 var(--amber)}
.newbtn{margin:6px 14px 10px;padding:10px 12px;border:1px solid var(--line);background:var(--panel);border-radius:10px;text-align:left;font-weight:500}
.newbtn:hover{border-color:var(--accent)}
.chats{flex:1;overflow-y:auto;padding:4px 8px}
.chat-item{display:flex;align-items:center;gap:4px;border-radius:8px;padding:0 4px 0 10px}
.chat-item:hover,.chat-item.active{background:var(--panel)}
.chat-item button.open{flex:1;min-width:0;background:none;border:0;text-align:left;padding:8px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px}
.chat-item button.del{background:none;border:0;color:var(--muted);padding:4px 6px;border-radius:6px;opacity:0}
.chat-item:hover button.del,.chat-item.active button.del{opacity:1}
.chat-item button.del:hover{color:var(--danger)}
.side-foot{border-top:1px solid var(--line);padding:10px 14px;display:flex;flex-direction:column;gap:6px}
.side-foot button{background:none;border:0;text-align:left;padding:8px 6px;border-radius:8px;display:flex;justify-content:space-between;align-items:center}
.side-foot button:hover{background:var(--panel)}
.keystate{font-size:12px;color:var(--muted)}
.keystate.ok{color:var(--ok)}

/* Main */
.main{display:flex;flex-direction:column;min-width:0;min-height:0}
.top{display:flex;align-items:center;gap:10px;padding:10px 16px;border-bottom:1px solid var(--line)}
.menu-btn{display:none;background:none;border:1px solid var(--line);border-radius:8px;padding:6px 10px}
.model-btn{display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:7px 12px;max-width:100%;min-width:0}
.model-btn:hover{border-color:var(--accent)}
.model-btn .mname{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:500}
.model-btn .caret{color:var(--muted);font-size:12px}
.msgs{flex:1;overflow-y:auto;padding:24px 16px 8px}
.col{max-width:780px;margin:0 auto}
.empty{text-align:center;padding:12vh 12px 0;color:var(--muted)}
.empty h1{color:var(--ink);font-size:30px;font-weight:600;margin:0 0 8px;letter-spacing:-.01em}
.empty .hints{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:22px}
.empty .hints button{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:7px 14px;font-size:14px}
.empty .hints button:hover{border-color:var(--accent)}

.msg{margin:0 0 22px}
.msg.user{display:flex;justify-content:flex-end}
.msg.user .bubble{background:var(--user);border-radius:16px 16px 4px 16px;padding:10px 14px;max-width:85%;white-space:pre-wrap;word-wrap:break-word}
.msg.ai .who{font-size:12px;color:var(--muted);margin-bottom:4px;font-family:var(--mono)}
.msg.ai .body{word-wrap:break-word}
.body p{margin:0 0 .8em}
.body p:last-child{margin-bottom:0}
.body h1,.body h2,.body h3{line-height:1.3;margin:1.1em 0 .5em}
.body h1{font-size:1.4em}.body h2{font-size:1.2em}.body h3{font-size:1.05em}
.body ul,.body ol{padding-left:1.4em;margin:0 0 .8em}
.body a{color:var(--accent)}
.body blockquote{margin:0 0 .8em;padding:2px 14px;border-left:3px solid var(--line);color:var(--muted)}
.body table{border-collapse:collapse;display:block;overflow-x:auto;margin:0 0 .8em}
.body th,.body td{border:1px solid var(--line);padding:6px 10px;text-align:left}
.body code{font-family:var(--mono);font-size:.88em;background:var(--code);padding:1px 5px;border-radius:4px}
.codewrap{position:relative;margin:0 0 .9em;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--code)}
.codehead{display:flex;justify-content:space-between;align-items:center;padding:4px 10px;font-size:12px;color:var(--muted);border-bottom:1px solid var(--line);font-family:var(--mono)}
.codehead button{background:none;border:0;color:var(--muted);font-size:12px;padding:2px 6px}
.codehead button:hover{color:var(--ink)}
.codewrap pre{margin:0;padding:12px 14px;overflow-x:auto}
.codewrap pre code{background:none;padding:0;font-size:13px;line-height:1.55}
.think{margin:0 0 10px;border-left:3px solid var(--amber);padding:2px 12px;color:var(--muted);font-size:14px}
.think summary{cursor:pointer;color:var(--amber);font-weight:500}
.think div{white-space:pre-wrap;margin-top:6px}
.actions{display:flex;gap:4px;margin-top:6px}
.actions button{background:none;border:0;color:var(--muted);font-size:12.5px;padding:3px 8px;border-radius:6px}
.actions button:hover{background:var(--side);color:var(--ink)}
.meta{font-size:12px;color:var(--muted);margin-left:auto;align-self:center;font-family:var(--mono)}
.err{background:color-mix(in srgb,var(--danger) 12%,transparent);border:1px solid color-mix(in srgb,var(--danger) 45%,transparent);border-radius:10px;padding:10px 14px;color:var(--ink)}
.err button{margin-top:8px;background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:5px 12px}
.cursor::after{content:"▍";color:var(--accent);animation:blink 1s steps(2) infinite}
@keyframes blink{50%{opacity:0}}

/* Composer */
.composer{padding:8px 16px 16px}
.box{display:flex;align-items:flex-end;gap:8px;background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:8px 8px 8px 14px}
.box:focus-within{border-color:var(--accent)}
.box textarea{flex:1;border:0;outline:0;background:none;resize:none;max-height:220px;padding:6px 0;line-height:1.5}
.send{background:var(--accent);color:var(--accent-ink);border:0;border-radius:10px;padding:8px 16px;font-weight:600}
.send:disabled{opacity:.45;cursor:not-allowed}
.send.stop{background:var(--ink);color:var(--bg)}
.foot-note{text-align:center;font-size:12px;color:var(--muted);margin-top:6px}

/* Modal */
.overlay{position:fixed;inset:0;background:rgba(10,15,22,.5);display:none;align-items:center;justify-content:center;padding:16px;z-index:50}
.overlay.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;width:100%;max-width:560px;max-height:calc(100% - 32px);display:flex;flex-direction:column;overflow:hidden}
.modal header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}
.modal header h2{margin:0;font-size:17px}
.modal header button{background:none;border:0;font-size:20px;line-height:1;color:var(--muted)}
.modal .content{padding:16px 18px;overflow-y:auto}
.field{margin-bottom:16px}
.field label{display:block;font-weight:500;margin-bottom:5px}
.field .hint{font-size:13px;color:var(--muted);margin-top:4px}
.field input[type=text],.field input[type=password],.field input[type=number],.field textarea{width:100%;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;outline:0}
.field input:focus,.field textarea:focus{border-color:var(--accent)}
.row{display:flex;gap:8px;align-items:center}
.row > input{flex:1}
.btn{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 12px;white-space:nowrap}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.btn.danger{color:var(--danger)}
.modal footer{display:flex;gap:8px;justify-content:flex-end;padding:12px 18px;border-top:1px solid var(--line)}
.status{font-size:13px;margin-top:6px}
.status.ok{color:#2E8B57}.status.bad{color:var(--danger)}

/* Model list */
.mlist{list-style:none;margin:0;padding:0}
.mlist li button{width:100%;text-align:left;background:none;border:0;border-bottom:1px solid var(--line);padding:9px 6px;display:grid;grid-template-columns:1fr auto;gap:2px 10px}
.mlist li button:hover,.mlist li button.cur{background:var(--bg)}
.mlist .n{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mlist .id{font-family:var(--mono);font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mlist .p{font-size:12px;color:var(--muted);text-align:right;font-family:var(--mono)}
.mlist .free{color:#2E8B57;font-weight:600}
.mtools{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.mtools input[type=text]{flex:1;min-width:180px}
.chk{display:flex;align-items:center;gap:6px;font-size:14px;white-space:nowrap}

.scrim{display:none}
@media (max-width:820px){
  .app{grid-template-columns:1fr}
  .side{position:fixed;top:0;bottom:0;left:0;width:280px;z-index:40;transform:translateX(-100%);transition:transform .2s;padding-top:env(safe-area-inset-top,0px)}
  .side.open{transform:none}
  .scrim.show{display:block;position:fixed;inset:0;background:rgba(10,15,22,.4);z-index:30}
  .menu-btn{display:block}
  .msg.user .bubble{max-width:92%}
}
@media (prefers-reduced-motion:reduce){.side{transition:none}.cursor::after{animation:none}}

.iapp{grid-template-columns:360px 1fr}
.iapp a,.modal a{color:var(--accent)}
/* Control panel */
.panel{background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
/* View switch (shared by both views) */
.nav{display:flex;gap:4px;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:3px;margin:4px 14px 8px}
.nav button{flex:1;background:none;border:0;color:var(--muted);padding:5px 10px;border-radius:7px;font-size:14px}
.nav button:hover{color:var(--ink)}
.nav button[aria-current=page]{background:var(--panel);color:var(--ink);font-weight:500}
.iapp .brand .dot{background:var(--amber);box-shadow:5px 5px 0 var(--accent)}
.controls{flex:1;overflow-y:auto;padding:10px 18px 18px}
.group{margin-bottom:18px}
.group > label,.group > .lbl{display:block;font-weight:500;margin-bottom:6px}
.hint{font-size:13px;color:var(--muted);margin-top:4px}
textarea#prompt{width:100%;min-height:130px;resize:vertical;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px;outline:0;line-height:1.5}
textarea#prompt:focus{border-color:var(--accent)}
.i-model-btn{width:100%;display:flex;flex-direction:column;align-items:flex-start;gap:1px;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 12px;text-align:left}
.i-model-btn:hover{border-color:var(--accent)}
.i-model-btn .n{font-weight:500}
.i-model-btn .s{font-size:12px;color:var(--muted);font-family:var(--mono)}

/* Aspect ratio chips: the signature element */
.ratios{display:grid;grid-template-columns:repeat(auto-fill,minmax(58px,1fr));gap:6px}
.ratio{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:4px;height:62px;background:var(--panel);border:1px solid var(--line);border-radius:9px;padding:6px 4px;font-size:12px;font-family:var(--mono);color:var(--muted)}
.ratio i{display:block;border:1.5px solid currentColor;border-radius:2px}
.ratio:hover{border-color:var(--accent)}
.ratio[aria-pressed=true]{border-color:var(--accent);color:var(--accent);background:color-mix(in srgb,var(--accent) 9%,var(--panel))}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.chip{background:var(--panel);border:1px solid var(--line);border-radius:999px;padding:4px 12px;font-size:14px}
.chip:hover{border-color:var(--accent)}
.chip[aria-pressed=true]{border-color:var(--accent);color:var(--accent);font-weight:500}
.stepper{display:inline-flex;align-items:center;border:1px solid var(--line);border-radius:9px;background:var(--panel)}
.stepper button{background:none;border:0;width:34px;height:32px;font-size:17px}
.stepper span{min-width:28px;text-align:center;font-family:var(--mono)}
.inp{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:7px 10px;outline:0;width:100%}
.inp:focus{border-color:var(--accent)}

.refs{display:flex;flex-wrap:wrap;gap:8px}
.ref{position:relative;width:64px;height:64px;border-radius:8px;overflow:hidden;border:1px solid var(--line)}
.ref img{width:100%;height:100%;object-fit:cover}
.ref button{position:absolute;top:2px;right:2px;background:rgba(0,0,0,.6);color:#fff;border:0;border-radius:50%;width:20px;height:20px;font-size:12px;line-height:1;padding:0}
.addref{width:64px;height:64px;border:1.5px dashed var(--line);border-radius:8px;background:none;color:var(--muted);font-size:22px}
.addref:hover{border-color:var(--accent);color:var(--accent)}

.go{padding:12px 18px 16px;border-top:1px solid var(--line)}
.gen{width:100%;background:var(--accent);color:var(--accent-ink);border:0;border-radius:12px;padding:12px;font-weight:600;font-size:16px}
.gen:disabled{opacity:.45;cursor:not-allowed}
.go .row2{display:flex;justify-content:space-between;align-items:center;margin-top:8px;font-size:13px;color:var(--muted)}
.linkbtn{background:none;border:0;color:var(--muted);padding:2px 0;font-size:13px;text-decoration:underline;text-underline-offset:3px}
.linkbtn:hover{color:var(--ink)}

/* Gallery */
.gallery-wrap{overflow-y:auto;min-height:0;padding:20px}
.gtop{display:flex;justify-content:space-between;align-items:center;margin:0 0 14px;gap:10px}
.gtop h1{font-size:20px;margin:0;font-weight:600}
.gtop .cnt{color:var(--muted);font-size:14px}
.gallery{columns:3 260px;column-gap:14px}
.card{break-inside:avoid;margin:0 0 14px;background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.card .img{display:block;width:100%;background:var(--side);cursor:zoom-in;border:0;padding:0}
.card .img img{display:block;width:100%;height:auto}
.card .cap{padding:8px 10px 10px}
.card .p{font-size:13.5px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.card .m{font-size:11.5px;color:var(--muted);font-family:var(--mono);margin-top:3px;display:flex;justify-content:space-between;gap:8px}
.card .acts{display:flex;gap:2px;margin-top:6px;flex-wrap:wrap}
.card .acts button{background:none;border:0;color:var(--muted);font-size:12.5px;padding:3px 7px;border-radius:6px}
.card .acts button:hover{background:var(--side);color:var(--ink)}
.card .acts button.del:hover{color:var(--danger)}
.job .ph{position:relative;display:flex;align-items:center;justify-content:center;background:repeating-linear-gradient(45deg,var(--side) 0 12px,var(--bg) 12px 24px);background-size:34px 34px;animation:slide 1.2s linear infinite;color:var(--muted);font-family:var(--mono);font-size:13px}
.job .ph img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;opacity:.85}
@keyframes slide{to{background-position:34px 0}}
.job .i-err{color:var(--danger);font-size:13.5px}
.i-empty{max-width:520px;margin:12vh auto 0;text-align:center;color:var(--muted)}
.i-empty h2{color:var(--ink);font-size:26px;margin:0 0 6px;font-weight:600}
.i-empty .ideas{display:flex;flex-direction:column;gap:8px;margin-top:20px}
.i-empty .ideas button{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:9px 14px;text-align:left;font-size:14px}
.i-empty .ideas button:hover{border-color:var(--accent)}

/* Modals */
.overlay{position:fixed;inset:0;background:rgba(10,15,22,.55);display:none;align-items:center;justify-content:center;padding:16px;z-index:50}
.overlay.show{display:flex}
.modal{background:var(--panel);border:1px solid var(--line);border-radius:14px;width:100%;max-width:560px;max-height:calc(100% - 32px);display:flex;flex-direction:column;overflow:hidden}
.modal header{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line)}
.modal header h2{margin:0;font-size:17px}
.modal header button{background:none;border:0;font-size:22px;line-height:1;color:var(--muted)}
.modal .content{padding:16px 18px;overflow-y:auto}
.modal footer{display:flex;gap:8px;justify-content:flex-end;padding:12px 18px;border-top:1px solid var(--line)}
.row{display:flex;gap:8px;align-items:center}
.row > input{flex:1}
.btn{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 12px;white-space:nowrap}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);color:var(--accent-ink);border-color:var(--accent);font-weight:600}
.btn.danger{color:var(--danger)}
.status{font-size:13px;margin-top:6px}
.status.ok{color:var(--ok)}.status.bad{color:var(--danger)}
.mlist{list-style:none;margin:0;padding:0}
.mlist li button{width:100%;text-align:left;background:none;border:0;border-bottom:1px solid var(--line);padding:9px 6px;display:grid;grid-template-columns:1fr auto;gap:2px 10px}
.mlist li button:hover,.mlist li button.cur{background:var(--bg)}
.mlist .n{font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mlist .id{font-family:var(--mono);font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mlist .t{font-size:11.5px;color:var(--muted);text-align:right}
.lightbox .modal{max-width:min(1100px,100%);background:var(--panel)}
.lightbox .big{background:var(--side);display:flex;justify-content:center;overflow:auto;max-height:70vh}
.lightbox .big img{max-width:100%;max-height:70vh;object-fit:contain}
.lightbox .info{padding:12px 18px;display:flex;flex-direction:column;gap:6px}
.lightbox .info .p{white-space:pre-wrap}
.lightbox .info .m{font-size:12.5px;color:var(--muted);font-family:var(--mono)}

.drop-hint{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:color-mix(in srgb,var(--accent) 18%,transparent);border:3px dashed var(--accent);z-index:60;font-size:20px;font-weight:600;color:var(--accent);pointer-events:none}
.drop-hint.show{display:flex}
.pwgrid{display:grid;gap:8px}
.whoami{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:2px 6px;font-size:13px;color:var(--muted)}
.whoami b{color:var(--ink);font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.side-foot .whoami button{padding:4px 8px;font-size:13px;color:var(--muted)}
.side-foot .whoami button:hover{color:var(--danger)}
.note{background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:14px;color:var(--muted);margin-bottom:16px}
/* User management */
.ulist{list-style:none;margin:0;padding:0}
.ulist li{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding:10px 4px;border-bottom:1px solid var(--line)}
.ulist .who{flex:1;min-width:160px}
.ulist .who b{font-weight:500}
.ulist .who .t{display:block;font-size:12px;color:var(--muted)}
.ulist .acts{display:flex;flex-wrap:wrap;gap:4px}
.ulist .acts button{background:none;border:1px solid var(--line);border-radius:7px;padding:3px 9px;font-size:12.5px;color:var(--muted)}
.ulist .acts button:hover{border-color:var(--accent);color:var(--ink)}
.ulist .acts button.del:hover{border-color:var(--danger);color:var(--danger)}
.ulist li.off .who b{color:var(--muted);text-decoration:line-through}
.badge{display:inline-block;font-size:11px;border-radius:999px;padding:0 8px;margin-left:6px;border:1px solid currentColor;vertical-align:1px}
.badge.admin{color:var(--accent)}.badge.off{color:var(--danger)}.badge.me{color:var(--ok)}
.cred{background:color-mix(in srgb,var(--ok) 10%,var(--panel));border:1px solid color-mix(in srgb,var(--ok) 45%,transparent);border-radius:10px;padding:10px 12px;margin:10px 0 0;font-size:14px}
.cred pre{margin:6px 0 8px;font-family:var(--mono);font-size:13px;white-space:pre-wrap;word-break:break-all}

@media (max-width:860px){
  body.image-mode{overflow:auto}
  .iapp{grid-template-columns:1fr;height:auto;min-height:100%}
  .panel{border-right:0;border-bottom:1px solid var(--line)}
  .controls{overflow:visible}
  .gallery-wrap{overflow:visible}
  .gallery{columns:2 150px}
}
@media (prefers-reduced-motion:reduce){.job .ph{animation:none}}
</style>
<link id="hlLight" rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css" media="(prefers-color-scheme: light)">
<link id="hlDark" rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css" media="(prefers-color-scheme: dark)">
</head>
<body>
<div class="app" id="chatView">
  <aside class="side" id="side">
    <div class="brand"><span class="dot"></span><span id="brandName">My AI Chat</span></div>
    <nav class="nav" aria-label="สลับหน้า"><button type="button" data-view="chat">แชท</button><button type="button" data-view="image">สร้างรูป</button></nav>
    <button class="newbtn" id="newChat">＋ แชทใหม่</button>
    <nav class="chats" id="chatList" aria-label="ประวัติแชท"></nav>
    <div class="side-foot">
      <button id="openUsers" hidden><span>จัดการผู้ใช้</span><span class="keystate">แอดมิน</span></button>
      <button id="openSettings"><span>ตั้งค่า</span><span class="keystate" id="keyState">ยังไม่มี API Key</span></button>
      <div class="whoami"><span>ผู้ใช้: <b id="meName"></b></span><button id="logoutSide" type="button">ออกจากระบบ</button></div>
      <div class="credit">Developed by <a href="https://uncle-engineer.com/" target="_blank" rel="noopener">Uncle Engineer</a></div>
    </div>
  </aside>
  <div class="scrim" id="scrim"></div>

  <main class="main">
    <div class="top">
      <button class="menu-btn" id="menuBtn" aria-label="เปิดเมนู">☰</button>
      <button class="model-btn" id="modelBtn" title="เปลี่ยนโมเดล"><span class="mname" id="modelName">-</span><span class="caret">▼</span></button>
    </div>
    <div class="msgs" id="msgs"><div class="col" id="col"></div></div>
    <div class="composer">
      <div class="col">
        <div class="box">
          <textarea id="input" rows="1" placeholder="พิมพ์ข้อความ… (Enter ส่ง, Shift+Enter ขึ้นบรรทัดใหม่)"></textarea>
          <button class="send" id="sendBtn" disabled>ส่ง</button>
        </div>
        <div class="foot-note">ใช้ API Key ของคุณผ่าน OpenRouter · ข้อมูลทั้งหมดเก็บใน SQLite บนเครื่องที่รันเซิร์ฟเวอร์นี้</div>
      </div>
    </div>
  </main>
</div>

<!-- Settings -->
<div class="overlay" id="settingsModal" role="dialog" aria-modal="true" aria-labelledby="setTitle">
  <div class="modal">
    <header><h2 id="setTitle">ตั้งค่า</h2><button data-close aria-label="ปิด">×</button></header>
    <div class="content">
      <div class="note" id="userNote" hidden>API Key, System prompt และค่าการตอบของ AI ตั้งโดยผู้ดูแลระบบ ใช้ร่วมกันทุกคน</div>
      <div id="adminSettings">
      <div class="field">
        <label for="apiKey">OpenRouter API Key</label>
        <div class="row">
          <input type="password" id="apiKey" placeholder="sk-or-v1-..." autocomplete="off" spellcheck="false">
          <button class="btn" id="toggleKey" type="button">แสดง</button>
          <button class="btn" id="testKey" type="button">ทดสอบ</button>
        </div>
        <div class="status" id="keyStatus"></div>
        <div class="hint">สร้างคีย์ได้ที่ <a href="https://openrouter.ai/keys" target="_blank" rel="noopener">openrouter.ai/keys</a> · คีย์ถูกเก็บในฐานข้อมูล SQLite ของเซิร์ฟเวอร์นี้ ใช้ร่วมกันทั้งแชทและสร้างรูป</div>
      </div>
      <div class="field">
        <label for="appName">ชื่อแอป</label>
        <input type="text" id="appName" placeholder="My AI Chat">
      </div>
      <div class="field">
        <label for="sysPrompt">System prompt</label>
        <textarea id="sysPrompt" rows="4" placeholder="เช่น คุณคือผู้ช่วยที่ตอบเป็นภาษาไทย กระชับ และเป็นกันเอง"></textarea>
        <div class="hint">กำหนดบุคลิกและกติกาให้ AI ใช้กับทุกแชท</div>
      </div>
      <div class="row" style="gap:16px">
        <div class="field" style="flex:1">
          <label for="temp">Temperature</label>
          <input type="number" id="temp" min="0" max="2" step="0.1">
          <div class="hint">0 = ตรงไปตรงมา, 1+ = สร้างสรรค์</div>
        </div>
        <div class="field" style="flex:1">
          <label for="maxTok">Max tokens</label>
          <input type="number" id="maxTok" min="0" step="256" placeholder="ไม่จำกัด">
          <div class="hint">เว้นว่าง = ให้โมเดลกำหนดเอง</div>
        </div>
      </div>
      </div>
      <div class="field">
        <label for="pwOld">เปลี่ยนรหัสผ่านของฉัน</label>
        <div class="pwgrid">
          <input type="password" id="pwOld" placeholder="รหัสผ่านปัจจุบัน" autocomplete="current-password">
          <input type="password" id="pwNew" placeholder="รหัสผ่านใหม่ (อย่างน้อย 6 ตัวอักษร)" autocomplete="new-password">
          <input type="password" id="pwNew2" placeholder="ยืนยันรหัสผ่านใหม่" autocomplete="new-password">
        </div>
        <div class="row" style="margin-top:8px"><button class="btn" id="changePw" type="button">เปลี่ยนรหัสผ่าน</button></div>
        <div class="status" id="pwStatus"></div>
      </div>
      <div class="field">
        <label>ข้อมูลของฉัน</label>
        <div class="row" style="flex-wrap:wrap">
          <button class="btn" id="exportBtn" type="button">ส่งออกแชท (JSON)</button>
          <button class="btn danger" id="clearChats" type="button">ลบแชทของฉันทั้งหมด</button>
          <button class="btn danger" id="clearGallery" type="button">ลบรูปของฉันทั้งหมด</button>
          <button class="btn danger" id="clearKey" type="button">ลบ API Key</button>
        </div>
      </div>
    </div>
    <footer><button class="btn" id="logoutBtn" type="button" style="margin-right:auto">ออกจากระบบ</button><button class="btn" data-close>ปิด</button><button class="btn primary" id="saveSettings">บันทึก</button></footer>
  </div>
</div>

<!-- User management (แอดมิน) -->
<div class="overlay" id="usersModal" role="dialog" aria-modal="true" aria-labelledby="usersTitle">
  <div class="modal" style="max-width:640px">
    <header><h2 id="usersTitle">จัดการผู้ใช้</h2><button data-close aria-label="ปิด">×</button></header>
    <div class="content">
      <form class="field" id="addUserForm" autocomplete="off">
        <label for="nuName">เพิ่มผู้ใช้ใหม่</label>
        <div class="pwgrid">
          <input type="text" id="nuName" placeholder="ชื่อผู้ใช้ (a-z, 0-9, . _ -)" spellcheck="false" autocapitalize="off">
          <div class="row">
            <input type="text" id="nuPw" placeholder="รหัสผ่าน (อย่างน้อย 6 ตัวอักษร)" spellcheck="false" autocomplete="new-password" style="font-family:var(--mono)">
            <button class="btn" id="nuGen" type="button">สุ่มรหัส</button>
          </div>
        </div>
        <div class="row" style="margin-top:8px;justify-content:space-between">
          <label class="chk"><input type="checkbox" id="nuAdmin"> เป็นผู้ดูแลระบบ</label>
          <button class="btn primary" type="submit">เพิ่มผู้ใช้</button>
        </div>
        <div class="status" id="nuStatus"></div>
        <div class="hint">ไม่มีหน้าสมัครสมาชิก — ผู้ใช้จะเข้าระบบได้ต่อเมื่อคุณส่งชื่อผู้ใช้และรหัสผ่านให้เอง</div>
        <div class="cred" id="credBox" hidden>
          <span id="credTitle"></span>
          <pre id="credText"></pre>
          <button class="btn" id="credCopy" type="button">คัดลอกไปส่งให้ผู้ใช้</button>
        </div>
      </form>
      <div class="field" style="margin-bottom:0">
        <label>ผู้ใช้ทั้งหมด <span class="keystate" id="uCount"></span></label>
        <ul class="ulist" id="uList"></ul>
      </div>
    </div>
  </div>
</div>

<!-- Model picker -->
<div class="overlay" id="modelModal" role="dialog" aria-modal="true" aria-labelledby="modelTitle">
  <div class="modal" style="max-width:640px">
    <header><h2 id="modelTitle">เลือกโมเดล</h2><button data-close aria-label="ปิด">×</button></header>
    <div class="content">
      <div class="mtools">
        <input type="text" id="mSearch" class="field" placeholder="ค้นหา เช่น claude, gpt, gemini, llama" style="background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px">
        <label class="chk"><input type="checkbox" id="mFree"> เฉพาะฟรี</label>
        <button class="btn" id="mReload" type="button">รีเฟรช</button>
      </div>
      <div class="row" style="margin-bottom:12px">
        <input type="text" id="mCustom" placeholder="หรือพิมพ์ model id เอง เช่น anthropic/claude-sonnet-4" style="background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px 10px;font-family:var(--mono);font-size:13px">
        <button class="btn primary" id="mUseCustom" type="button">ใช้</button>
      </div>
      <div class="status" id="mStatus"></div>
      <ul class="mlist" id="mList"></ul>
    </div>
  </div>
</div>


<div class="app iapp" id="imageView" hidden>
  <aside class="panel">
    <div class="brand"><span class="dot"></span><span id="iBrandName">My AI Image</span></div>
    <nav class="nav" aria-label="สลับหน้า"><button type="button" data-view="chat">แชท</button><button type="button" data-view="image">สร้างรูป</button></nav>
    <div class="controls">
      <div class="group">
        <label for="prompt">อยากได้รูปแบบไหน</label>
        <textarea id="prompt" placeholder="บรรยายภาพให้ละเอียด เช่น หุ่นยนต์แขนกลสีส้มในโรงงานสะอาด แสงนุ่ม ถ่ายแบบภาพสินค้า มุมกล้องระดับสายตา"></textarea>
        <div class="hint">Ctrl/⌘ + Enter เพื่อสร้าง · ใส่ภาษาอังกฤษมักได้ผลแม่นกว่ากับบางโมเดล</div>
      </div>
      <div class="group">
        <span class="lbl">โมเดล</span>
        <button class="i-model-btn" id="iModelBtn" type="button"><span class="n" id="mName">กำลังโหลด…</span><span class="s" id="mSub"></span></button>
      </div>
      <div id="params"></div>
      <div class="group" id="refGroup" hidden>
        <span class="lbl">ภาพอ้างอิง (ไม่บังคับ)</span>
        <div class="refs" id="refs"><button class="addref" id="addRef" type="button" aria-label="เพิ่มภาพอ้างอิง">＋</button></div>
        <input type="file" id="refFile" accept="image/*" multiple hidden>
        <div class="hint">ใช้แก้ไขภาพหรือคุมสไตล์ ลากไฟล์มาวาง หรือกด Ctrl+V วางภาพได้</div>
      </div>
    </div>
    <div class="go">
      <button class="gen" id="genBtn" type="button" disabled>สร้างรูป</button>
      <div class="row2"><span id="costHint"></span><button class="linkbtn" id="iOpenSettings" type="button">ตั้งค่า · <span class="keystate" id="iKeyState">ยังไม่มี API Key</span></button></div>
      <div class="credit">Developed by <a href="https://uncle-engineer.com/" target="_blank" rel="noopener">Uncle Engineer</a></div>
    </div>
  </aside>

  <main class="gallery-wrap">
    <div class="gtop"><h1>รูปของคุณ</h1><span class="cnt" id="count"></span></div>
    <div class="gallery" id="gallery"></div>
    <div id="emptyBox"></div>
  </main>
</div>
<div class="drop-hint" id="dropHint">วางภาพเพื่อใช้เป็นภาพอ้างอิง</div>

<!-- Model picker -->
<div class="overlay" id="iModelModal" role="dialog" aria-modal="true" aria-labelledby="iModelTitle">
  <div class="modal" style="max-width:620px">
    <header><h2 id="iModelTitle">เลือกโมเดลสร้างรูป</h2><button data-close aria-label="ปิด">×</button></header>
    <div class="content">
      <div class="row" style="margin-bottom:10px">
        <input class="inp" type="text" id="iSearch" placeholder="ค้นหา เช่น flux, gemini, gpt-image, seedream">
        <button class="btn" id="iReload" type="button">รีเฟรช</button>
      </div>
      <div class="row" style="margin-bottom:12px">
        <input class="inp" type="text" id="iCustom" placeholder="หรือพิมพ์ model id เอง" style="font-family:var(--mono);font-size:13px">
        <button class="btn primary" id="iUseCustom" type="button">ใช้</button>
      </div>
      <div class="status" id="iStatus"></div>
      <ul class="mlist" id="iList"></ul>
    </div>
  </div>
</div>

<!-- Lightbox -->
<div class="overlay lightbox" id="lightbox" role="dialog" aria-modal="true" aria-label="ดูรูปขนาดเต็ม">
  <div class="modal">
    <header><h2>รูปขนาดเต็ม</h2><button data-close aria-label="ปิด">×</button></header>
    <div class="big"><img id="lbImg" alt=""></div>
    <div class="info"><div class="p" id="lbPrompt"></div><div class="m" id="lbMeta"></div></div>
    <footer id="lbActs"></footer>
  </div>
</div>

<script>
// ---------- Store: ข้อมูลจาก SQLite (ฝังมากับหน้าเว็บ) + บันทึกกลับผ่าน Flask API ----------
window.Store = (() => {
  const boot = /*__BOOT__*/null || { kv:{}, chats:[] };
  const kv = boot.kv || {};
  const queues = {};
  // session หมดอายุ / ถูกระงับ / รหัสถูกรีเซ็ต → โหลดหน้าใหม่ให้ไปหน้าเข้าสู่ระบบ
  const checkSession = r => { if(r.status === 401) location.reload(); return r; };
  // เรียง request ของ key เดียวกันให้ไปถึงเซิร์ฟเวอร์ตามลำดับ
  function enqueue(qk, url, opt){
    const run = () => fetch(url, opt).then(checkSession).then(r => { if(!r.ok) throw new Error(r.status + ' ' + r.statusText); }).catch(e => console.warn('บันทึกไม่สำเร็จ', url, e));
    queues[qk] = (queues[qk] || Promise.resolve()).then(run, run);
    return queues[qk];
  }
  function jsonOpt(method, data){
    const body = data === undefined ? undefined : JSON.stringify(data);
    return { method, headers:{ 'Content-Type':'application/json' }, body, keepalive: !body || body.length < 60000 };
  }
  // เรียก API แบบรอผล (ใช้กับงานที่ต้องรู้ว่าสำเร็จไหม เช่น รหัสผ่าน/ตั้งค่า)
  async function call(method, url, data){
    const r = checkSession(await fetch(url, { method, headers:{ 'Content-Type':'application/json' }, body: data === undefined ? undefined : JSON.stringify(data) }));
    let j = null; try{ j = await r.json(); }catch{}
    if(!r.ok) throw Object.assign(new Error((j && j.error) || (r.status + ' ' + r.statusText)), { status:r.status });
    return j;
  }
  return {
    me: boot.me || { username:'', is_admin:false },
    call,
    checkSession,
    async logout(){ try{ await fetch('/api/auth/logout', { method:'POST' }); }finally{ location.reload(); } },
    remember(k, v){ kv[k] = v; },
    forget(k){ delete kv[k]; },
    get(k, fb){ return Object.prototype.hasOwnProperty.call(kv, k) ? kv[k] : fb; },
    set(k, v){ kv[k] = v; return enqueue('kv:' + k, '/api/kv/' + encodeURIComponent(k), jsonOpt('PUT', { value:v })); },
    del(k){ delete kv[k]; return enqueue('kv:' + k, '/api/kv/' + encodeURIComponent(k), jsonOpt('DELETE')); },
    chats: boot.chats || [],
    saveChat(c){ return enqueue('chat:' + c.id, '/api/chats/' + encodeURIComponent(c.id), jsonOpt('PUT', c)); },
    deleteChat(id){ return enqueue('chat:' + id, '/api/chats/' + encodeURIComponent(id), jsonOpt('DELETE')); },
    clearChats(){ return enqueue('chats', '/api/chats', jsonOpt('DELETE')); }
  };
})();
</script>
<script>
// ---------- Shared: view switch + events between chat and image ----------
window.App = (() => {
  const ls = {};
  const app = {
    view:'chat', openSettings(){},
    on(e, f){ (ls[e] = ls[e] || []).push(f); },
    emit(e, d){ (ls[e] || []).forEach(f => f(d)); },
    setView(v, focus){
      app.view = v === 'image' ? 'image' : 'chat';
      document.getElementById('chatView').hidden = app.view !== 'chat';
      document.getElementById('imageView').hidden = app.view !== 'image';
      document.body.classList.toggle('image-mode', app.view === 'image');
      document.querySelectorAll('.nav button').forEach(b => b.setAttribute('aria-current', b.dataset.view === app.view ? 'page' : 'false'));
      const name = (Store.get('aichat.settings', {}) || {}).appName || 'My AI Chat';
      document.title = app.view === 'image' ? name + ' · สร้างรูป' : name;
      if(location.hash !== '#' + app.view) history.replaceState(null, '', '#' + app.view);
      if(focus) (app.view === 'image' ? document.getElementById('prompt') : document.getElementById('input'))?.focus();
    }
  };
  app.on('settings', () => app.setView(app.view));
  document.addEventListener('click', e => { const b = e.target.closest('.nav button[data-view]'); if(b) app.setView(b.dataset.view, true); });
  window.addEventListener('hashchange', () => app.setView(location.hash.slice(1)));
  return app;
})();
</script>
<script>
(() => {
  // ---------- Storage (SQLite ผ่าน Store) ----------
  const K = { key:'aichat.apiKey', settings:'aichat.settings', model:'aichat.model', current:'aichat.current', models:'aichat.modelsCache' };
  const store = Store;
  const DEFAULTS = { appName:'My AI Chat', model:'openai/gpt-4o-mini', system:'คุณคือผู้ช่วย AI ที่ตอบเป็นภาษาไทยอย่างชัดเจนและเป็นมิตร', temperature:0.7, maxTokens:'' };
  let settings = { ...DEFAULTS, ...store.get(K.settings, {}) };
  settings.model = store.get(K.model, settings.model); // โมเดลเปลี่ยนได้โดยไม่ต้องปลดล็อก
  let apiKey = store.get(K.key, '');
  let chats = Store.chats;
  let currentId = store.get(K.current, null);
  let models = [];
  let controller = null;
  const isAdmin = !!Store.me.is_admin;
  const NO_KEY = 'ผู้ดูแลระบบยังไม่ได้ตั้งค่า OpenRouter API Key — ติดต่อผู้ดูแลระบบก่อนเริ่มใช้งาน';

  const $ = s => document.querySelector(s);
  const el = (tag, cls, txt) => { const e = document.createElement(tag); if(cls) e.className = cls; if(txt != null) e.textContent = txt; return e; };
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2,7);
  const saveChat = c => Store.saveChat(c);
  const current = () => chats.find(c => c.id === currentId);

  // ---------- Markdown ----------
  const hasMd = typeof marked !== 'undefined' && typeof DOMPurify !== 'undefined';
  function renderMd(text){
    if(!hasMd){ const d = el('div'); d.textContent = text; return d.innerHTML.replace(/\n/g,'<br>'); }
    return DOMPurify.sanitize(marked.parse(text, { gfm:true, breaks:true }));
  }
  function enhanceCode(root){
    root.querySelectorAll('pre > code').forEach(code => {
      const pre = code.parentElement;
      if(pre.parentElement.classList.contains('codewrap')) return;
      const lang = (code.className.match(/language-([\w-]+)/) || [,''])[1];
      const wrap = el('div','codewrap');
      const head = el('div','codehead');
      head.append(el('span',null,lang || 'code'));
      const cp = el('button',null,'คัดลอก');
      cp.onclick = () => copy(code.innerText, cp);
      head.append(cp);
      pre.replaceWith(wrap); wrap.append(head, pre);
      if(typeof hljs !== 'undefined'){ try{ hljs.highlightElement(code); }catch{} }
    });
    root.querySelectorAll('a').forEach(a => { a.target = '_blank'; a.rel = 'noopener'; });
  }
  function copy(text, btn){
    navigator.clipboard.writeText(text).then(() => {
      const o = btn.textContent; btn.textContent = 'คัดลอกแล้ว'; setTimeout(() => btn.textContent = o, 1400);
    });
  }

  // ---------- Sidebar ----------
  function renderList(){
    const list = $('#chatList'); list.innerHTML = '';
    [...chats].sort((a,b) => b.updated - a.updated).forEach(c => {
      const row = el('div','chat-item' + (c.id === currentId ? ' active' : ''));
      const open = el('button','open', c.title || 'แชทใหม่');
      open.title = c.title || '';
      open.onclick = () => { selectChat(c.id); closeSide(); };
      const del = el('button','del','✕'); del.title = 'ลบแชทนี้'; del.setAttribute('aria-label','ลบแชท');
      del.onclick = e => { e.stopPropagation(); if(confirm('ลบแชท "' + (c.title||'แชทใหม่') + '" ?')) deleteChat(c.id); };
      row.append(open, del); list.append(row);
    });
    const ks = $('#keyState');
    ks.textContent = apiKey ? 'มี API Key แล้ว' : 'ยังไม่มี API Key';
    ks.classList.toggle('ok', !!apiKey);
    $('#brandName').textContent = settings.appName || DEFAULTS.appName;
    App.emit('settings', settings);
  }
  function selectChat(id){ currentId = id; store.set(K.current, id); renderList(); renderChat(); }
  function newChat(){ currentId = null; store.set(K.current, null); renderList(); renderChat(); $('#input').focus(); }
  function deleteChat(id){
    chats = chats.filter(c => c.id !== id); Store.deleteChat(id);
    if(currentId === id){ currentId = null; store.set(K.current, null); }
    renderList(); renderChat();
  }
  function activeModel(){ return current()?.model || settings.model; }
  function updateModelLabel(){
    const id = activeModel();
    const m = models.find(x => x.id === id);
    $('#modelName').textContent = m ? m.name : id;
  }

  // ---------- Chat rendering ----------
  const hints = ['อธิบาย API ให้เด็ก ม.ปลาย เข้าใจ','เขียนโค้ด Python อ่านไฟล์ CSV แล้วสรุปยอดขาย','ช่วยร่างอีเมลขอเลื่อนประชุมแบบสุภาพ','สรุปข้อดีข้อเสียของ Arduino กับ ESP32'];
  function renderChat(){
    const col = $('#col'); col.innerHTML = '';
    const c = current();
    updateModelLabel();
    if(!c || !c.messages.length){
      const e = el('div','empty');
      e.append(el('h1',null, settings.appName || DEFAULTS.appName), el('div',null, apiKey ? 'เริ่มคุยได้เลย หรือเลือกตัวอย่างด้านล่าง' : isAdmin ? 'ใส่ OpenRouter API Key ในหน้าตั้งค่าก่อนเริ่มคุย' : NO_KEY));
      const h = el('div','hints');
      if(!apiKey){ if(isAdmin){ const b = el('button',null,'ไปที่ตั้งค่า'); b.onclick = openSettings; h.append(b); } }
      else hints.forEach(t => { const b = el('button',null,t); b.onclick = () => { $('#input').value = t; autoGrow(); send(); }; h.append(b); });
      e.append(h); col.append(e); return;
    }
    c.messages.forEach((m, i) => col.append(msgNode(m, i, c)));
    scrollBottom(true);
  }
  function msgNode(m, i, c){
    if(m.role === 'user'){
      const w = el('div','msg user'); const b = el('div','bubble', m.content); w.append(b); return w;
    }
    const w = el('div','msg ai');
    w.append(el('div','who', m.model || ''));
    if(m.reasoning){
      const d = el('details','think'); d.append(el('summary',null,'กระบวนการคิดของโมเดล'), el('div',null,m.reasoning)); w.append(d);
    }
    const body = el('div','body'); body.innerHTML = renderMd(m.content || ''); enhanceCode(body); w.append(body);
    const act = el('div','actions');
    const cp = el('button',null,'คัดลอก'); cp.onclick = () => copy(m.content, cp); act.append(cp);
    if(i === c.messages.length - 1){ const rg = el('button',null,'ตอบใหม่'); rg.onclick = regenerate; act.append(rg); }
    if(m.usage) act.append(el('span','meta', `${m.usage.prompt_tokens ?? '?'} → ${m.usage.completion_tokens ?? '?'} tokens`));
    w.append(act);
    return w;
  }
  const msgsEl = $('#msgs');
  let stickBottom = true;
  msgsEl.addEventListener('scroll', () => { stickBottom = msgsEl.scrollHeight - msgsEl.scrollTop - msgsEl.clientHeight < 80; });
  function scrollBottom(force){ if(force || stickBottom) msgsEl.scrollTop = msgsEl.scrollHeight; }

  // ---------- Sending ----------
  const input = $('#input'), sendBtn = $('#sendBtn');
  function autoGrow(){ input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 220) + 'px'; sendBtn.disabled = !controller && !input.value.trim(); }
  input.addEventListener('input', autoGrow);
  input.addEventListener('keydown', e => {
    if(e.key === 'Enter' && !e.shiftKey && !e.isComposing){ e.preventDefault(); if(!controller) send(); }
  });
  sendBtn.onclick = () => controller ? controller.abort() : send();

  function setBusy(b){
    sendBtn.textContent = b ? 'หยุด' : 'ส่ง';
    sendBtn.classList.toggle('stop', b);
    if(!b) controller = null;
    autoGrow();
  }

  async function send(){
    const text = input.value.trim();
    if(!text) return;
    if(!apiKey){ isAdmin ? openSettings() : alert(NO_KEY); return; }
    let c = current();
    if(!c){
      c = { id: uid(), title: text.slice(0, 40), model: settings.model, messages: [], updated: Date.now() };
      chats.push(c); currentId = c.id; store.set(K.current, c.id);
    }
    c.messages.push({ role:'user', content:text });
    c.updated = Date.now(); saveChat(c);
    input.value = ''; autoGrow();
    renderList(); renderChat();
    await complete(c);
  }

  function regenerate(){
    const c = current(); if(!c || controller) return;
    if(c.messages.at(-1)?.role === 'assistant') c.messages.pop();
    saveChat(c); renderChat(); complete(c);
  }

  async function complete(c){
    const model = c.model || settings.model;
    const col = $('#col');
    const w = el('div','msg ai');
    w.append(el('div','who', model));
    const think = el('details','think'); think.hidden = true;
    const thinkText = el('div'); think.append(el('summary',null,'กำลังคิด…'), thinkText);
    const body = el('div','body cursor');
    w.append(think, body); col.append(w); scrollBottom(true);

    const messages = [];
    if(settings.system?.trim()) messages.push({ role:'system', content: settings.system.trim() });
    c.messages.forEach(m => messages.push({ role:m.role, content:m.content }));

    const payload = { model, messages, stream:true, temperature: Number(settings.temperature) };
    if(settings.maxTokens) payload.max_tokens = Number(settings.maxTokens);

    controller = new AbortController(); setBusy(true);
    let content = '', reasoning = '', usage = null, pending = false;
    const paint = () => {
      if(pending) return; pending = true;
      requestAnimationFrame(() => {
        pending = false;
        body.innerHTML = renderMd(content);
        if(reasoning){ think.hidden = false; thinkText.textContent = reasoning; }
        scrollBottom();
      });
    };

    try{
      const res = await fetch('https://openrouter.ai/api/v1/chat/completions', {
        method:'POST', signal: controller.signal,
        headers:{
          'Authorization': 'Bearer ' + apiKey,
          'Content-Type': 'application/json',
          'HTTP-Referer': location.origin && location.origin !== 'null' ? location.origin : 'http://localhost',
          'X-Title': settings.appName || DEFAULTS.appName
        },
        body: JSON.stringify(payload)
      });
      if(!res.ok){
        let msg = res.status + ' ' + res.statusText;
        try{ const j = await res.json(); msg = j.error?.message || msg; }catch{}
        throw Object.assign(new Error(msg), { status: res.status });
      }
      const reader = res.body.getReader(); const dec = new TextDecoder(); let buf = '';
      while(true){
        const { done, value } = await reader.read();
        if(done) break;
        buf += dec.decode(value, { stream:true });
        const lines = buf.split('\n'); buf = lines.pop();
        for(const line of lines){
          const l = line.trim();
          if(!l.startsWith('data:')) continue;           // ข้าม comment เช่น ": OPENROUTER PROCESSING"
          const data = l.slice(5).trim();
          if(data === '[DONE]') continue;
          let j; try{ j = JSON.parse(data); }catch{ continue; }
          if(j.error) throw new Error(j.error.message || 'เกิดข้อผิดพลาดระหว่างสตรีม');
          const d = j.choices?.[0]?.delta || {};
          if(d.content) content += d.content;
          if(d.reasoning) reasoning += d.reasoning;
          if(j.usage) usage = j.usage;
          paint();
        }
      }
    }catch(err){
      if(err.name !== 'AbortError'){
        body.classList.remove('cursor');
        if(content){ c.messages.push({ role:'assistant', content, model, reasoning: reasoning || undefined }); c.updated = Date.now(); saveChat(c); }
        showError(w, body, err);
        setBusy(false); return;
      }
    }
    body.classList.remove('cursor');
    if(!content && !reasoning) content = '_(หยุดก่อนได้รับคำตอบ)_';
    c.messages.push({ role:'assistant', content, model, reasoning: reasoning || undefined, usage: usage || undefined });
    c.updated = Date.now(); saveChat(c);
    setBusy(false);
    renderList(); renderChat();
  }

  function showError(w, body, err){
    const box = el('div','err');
    let hint = '';
    if(err.status === 401) hint = ' — API Key ไม่ถูกต้องหรือหมดอายุ ตรวจสอบในหน้าตั้งค่า';
    else if(err.status === 402) hint = ' — เครดิตใน OpenRouter ไม่พอ เติมเครดิตหรือเลือกโมเดลฟรี';
    else if(err.status === 429) hint = ' — ส่งถี่เกินไป รอสักครู่แล้วลองใหม่';
    else if(err.status === 404 || err.status === 400) hint = ' — ตรวจสอบว่า model id ถูกต้อง';
    else if(err instanceof TypeError) hint = ' — เชื่อมต่อ OpenRouter ไม่ได้ ตรวจสอบอินเทอร์เน็ต';
    box.append(el('div',null,'ส่งข้อความไม่สำเร็จ: ' + err.message + hint));
    const r = el('button',null,'ลองอีกครั้ง'); r.onclick = () => { w.remove(); complete(current()); };
    box.append(r);
    if(body.textContent.trim()) w.append(box); else body.replaceWith(box);
    scrollBottom(true);
  }

  // ---------- Modals ----------
  function openModal(id){ const m = $(id); m.classList.add('show'); }
  function closeModal(m){ m.classList.remove('show'); }
  document.querySelectorAll('.overlay').forEach(o => {
    o.addEventListener('click', e => { if(o.classList.contains('locked')) return; if(e.target === o || e.target.closest('[data-close]')) closeModal(o); });
  });
  document.addEventListener('keydown', e => { if(e.key === 'Escape') document.querySelectorAll('.overlay.show:not(.locked)').forEach(closeModal); });

  // ---------- ผู้ใช้ที่ล็อกอินอยู่ ----------
  // ค่าที่ใช้ร่วมกัน (API Key, System prompt ฯลฯ) แก้ได้เฉพาะแอดมิน — ผู้ใช้ทั่วไปเห็นแค่ส่วนของตัวเอง
  $('#meName').textContent = Store.me.username;
  $('#openUsers').hidden = !isAdmin;
  ['#adminSettings', '#saveSettings', '#clearKey'].forEach(s => $(s).hidden = !isAdmin);
  $('#userNote').hidden = isAdmin;
  $('#logoutBtn').onclick = $('#logoutSide').onclick = () => Store.logout();
  function protectedError(e, prefix){ alert(prefix + ': ' + e.message); }
  App.protectedError = protectedError;

  // Settings
  function openSettings(){
    $('#apiKey').value = apiKey; $('#appName').value = settings.appName; $('#sysPrompt').value = settings.system;
    $('#temp').value = settings.temperature; $('#maxTok').value = settings.maxTokens; $('#keyStatus').textContent = '';
    ['#pwOld','#pwNew','#pwNew2'].forEach(s => $(s).value = ''); $('#pwStatus').textContent = '';
    openModal('#settingsModal'); setTimeout(() => $(isAdmin ? '#apiKey' : '#pwOld').focus(), 50); closeSide();
  }
  $('#openSettings').onclick = openSettings;
  $('#toggleKey').onclick = () => { const i = $('#apiKey'); const show = i.type === 'password'; i.type = show ? 'text' : 'password'; $('#toggleKey').textContent = show ? 'ซ่อน' : 'แสดง'; };
  $('#testKey').onclick = async () => {
    const k = $('#apiKey').value.trim(), st = $('#keyStatus');
    if(!k){ st.className = 'status bad'; st.textContent = 'ใส่ API Key ก่อน'; return; }
    st.className = 'status'; st.textContent = 'กำลังตรวจสอบ…';
    try{
      const r = await fetch('https://openrouter.ai/api/v1/key', { headers:{ Authorization:'Bearer ' + k } });
      const j = await r.json();
      if(!r.ok) throw new Error(j.error?.message || r.status);
      const d = j.data || {};
      const lim = d.limit == null ? 'ไม่จำกัดวงเงิน' : `วงเงินคงเหลือ $${(d.limit_remaining ?? 0).toFixed(2)}`;
      st.className = 'status ok'; st.textContent = `ใช้งานได้ · ใช้ไปแล้ว $${Number(d.usage||0).toFixed(4)} · ${lim}`;
    }catch(e){ st.className = 'status bad'; st.textContent = 'คีย์ใช้ไม่ได้: ' + e.message; }
  };
  $('#saveSettings').onclick = async () => {
    const newKey = $('#apiKey').value.trim();
    const t = parseFloat($('#temp').value);
    const next = { ...settings,
      appName: $('#appName').value.trim() || DEFAULTS.appName,
      system: $('#sysPrompt').value,
      temperature: isNaN(t) ? DEFAULTS.temperature : Math.min(2, Math.max(0, t)),
      maxTokens: $('#maxTok').value.trim() };
    const btn = $('#saveSettings'); btn.disabled = true;
    try{ await Store.call('POST', '/api/settings', { apiKey:newKey, settings:next }); }
    catch(e){ protectedError(e, 'บันทึกไม่สำเร็จ'); return; }
    finally{ btn.disabled = false; }
    apiKey = newKey; settings = next;
    apiKey ? Store.remember(K.key, apiKey) : Store.forget(K.key);
    Store.remember(K.settings, settings);
    closeModal($('#settingsModal')); renderList(); renderChat(); App.emit('key', apiKey);
  };
  $('#clearKey').onclick = async () => {
    if(!confirm('ลบ API Key ออกจากฐานข้อมูล?')) return;
    try{ await Store.call('DELETE', '/api/kv/' + encodeURIComponent(K.key)); }
    catch(e){ protectedError(e, 'ลบ API Key ไม่สำเร็จ'); return; }
    apiKey = ''; Store.forget(K.key); $('#apiKey').value = ''; renderList(); renderChat(); App.emit('key', '');
  };
  $('#clearChats').onclick = async () => {
    if(!confirm('ลบประวัติแชทของคุณทั้งหมด? กู้คืนไม่ได้')) return;
    try{ await Store.call('DELETE', '/api/chats'); }
    catch(e){ protectedError(e, 'ลบแชทไม่สำเร็จ'); return; }
    chats = []; currentId = null; store.set(K.current, null); renderList(); renderChat();
  };
  $('#changePw').onclick = async () => {
    const st = $('#pwStatus'), oldPw = $('#pwOld').value, newPw = $('#pwNew').value;
    st.className = 'status bad';
    if(!oldPw){ st.textContent = 'ใส่รหัสผ่านปัจจุบัน'; return; }
    if(newPw.length < 6){ st.textContent = 'รหัสผ่านใหม่ต้องยาวอย่างน้อย 6 ตัวอักษร'; return; }
    if(newPw !== $('#pwNew2').value){ st.textContent = 'รหัสผ่านใหม่ทั้งสองช่องไม่ตรงกัน'; return; }
    try{ await Store.call('POST', '/api/auth/change', { old:oldPw, new:newPw }); }
    catch(e){ st.textContent = e.message; return; }
    ['#pwOld','#pwNew','#pwNew2'].forEach(s => $(s).value = '');
    st.className = 'status ok'; st.textContent = 'เปลี่ยนรหัสผ่านแล้ว — อุปกรณ์อื่นที่ล็อกอินไว้จะต้องเข้าสู่ระบบใหม่';
  };
  $('#exportBtn').onclick = () => {
    const blob = new Blob([JSON.stringify(chats, null, 2)], { type:'application/json' });
    const a = el('a'); a.href = URL.createObjectURL(blob); a.download = 'ai-chats-' + new Date().toISOString().slice(0,10) + '.json'; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };

  // Model picker
  function price(p){ const n = parseFloat(p); if(!n) return 0; return n * 1e6; }
  async function loadModels(force){
    const st = $('#mStatus');
    const cache = store.get(K.models, null);
    if(!force && cache && Date.now() - cache.t < 6 * 3600e3){ models = cache.list; return; }
    st.className = 'status'; st.textContent = 'กำลังโหลดรายชื่อโมเดล…';
    try{
      const r = await fetch('https://openrouter.ai/api/v1/models');
      const j = await r.json();
      models = (j.data || []).map(m => ({
        id: m.id, name: m.name || m.id, ctx: m.context_length,
        pin: price(m.pricing?.prompt), pout: price(m.pricing?.completion)
      })).sort((a,b) => a.name.localeCompare(b.name));
      store.set(K.models, { t: Date.now(), list: models });
      st.textContent = '';
    }catch(e){
      st.className = 'status bad'; st.textContent = 'โหลดรายชื่อโมเดลไม่ได้ — พิมพ์ model id เองด้านบนแทนได้';
      if(cache) models = cache.list;
    }
  }
  function renderModels(){
    const q = $('#mSearch').value.trim().toLowerCase(), free = $('#mFree').checked, cur = activeModel();
    const list = $('#mList'); list.innerHTML = '';
    const shown = models.filter(m => (!free || (m.pin === 0 && m.pout === 0)) && (!q || m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q)));
    shown.slice(0, 300).forEach(m => {
      const li = el('li'); const b = el('button', m.id === cur ? 'cur' : '');
      const isFree = m.pin === 0 && m.pout === 0;
      const p = el('span','p' + (isFree ? ' free' : ''), isFree ? 'ฟรี' : `$${m.pin.toFixed(2)} / $${m.pout.toFixed(2)}`);
      const ctx = el('span','p', m.ctx ? Math.round(m.ctx/1000) + 'K ctx' : '');
      b.append(el('span','n', m.name), p, el('span','id', m.id), ctx);
      b.onclick = () => pickModel(m.id);
      li.append(b); list.append(li);
    });
    if(!shown.length && models.length) list.append(el('li','status','ไม่พบโมเดลที่ตรงกับคำค้น'));
    else if(shown.length) list.append(el('li','status', `ราคาต่อ 1 ล้าน tokens (input / output) · แสดง ${Math.min(shown.length,300)} จาก ${shown.length}`));
  }
  function pickModel(id){
    settings.model = id; store.set(K.model, id);
    const c = current(); if(c){ c.model = id; saveChat(c); }
    updateModelLabel(); closeModal($('#modelModal'));
  }
  $('#modelBtn').onclick = async () => {
    $('#mCustom').value = activeModel(); openModal('#modelModal'); renderModels();
    if(!models.length){ await loadModels(false); renderModels(); updateModelLabel(); }
    $('#mSearch').focus();
  };
  $('#mSearch').oninput = renderModels;
  $('#mFree').onchange = renderModels;
  $('#mReload').onclick = async () => { await loadModels(true); renderModels(); };
  $('#mUseCustom').onclick = () => { const v = $('#mCustom').value.trim(); if(v) pickModel(v); };
  $('#mCustom').onkeydown = e => { if(e.key === 'Enter') $('#mUseCustom').click(); };

  // Mobile sidebar
  function closeSide(){ $('#side').classList.remove('open'); $('#scrim').classList.remove('show'); }
  $('#menuBtn').onclick = () => { $('#side').classList.add('open'); $('#scrim').classList.add('show'); };
  $('#scrim').onclick = closeSide;
  $('#newChat').onclick = () => { newChat(); closeSide(); };

  // ---------- Init ----------
  if(currentId && !current()) currentId = null;
  renderList(); renderChat(); autoGrow();
  loadModels(false).then(updateModelLabel);
  App.openSettings = openSettings;
  // แอดมินที่ยังไม่ได้ใส่ API Key: เปิดตั้งค่าให้ทันที
  if(!apiKey && isAdmin) setTimeout(openSettings, 300);
})();
</script>
<script>
(() => {
  const API = 'https://openrouter.ai/api/v1';
  const K = { key:'aichat.apiKey', chatSettings:'aichat.settings', settings:'aiimage.settings', models:'aiimage.modelsCache' };
  const store = Store;
  const $ = s => document.querySelector(s);
  const el = (tag, cls, txt) => { const e = document.createElement(tag); if(cls) e.className = cls; if(txt != null) e.textContent = txt; return e; };
  const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2,7);

  let apiKey = store.get(K.key, '');
  const isAdmin = !!Store.me.is_admin;
  const NO_KEY = 'ผู้ดูแลระบบยังไม่ได้ตั้งค่า OpenRouter API Key — ติดต่อผู้ดูแลระบบก่อนเริ่มใช้งาน';
  let appName = (store.get(K.chatSettings, {}).appName || 'My AI') + ' Image';
  let S = { model:'google/gemini-2.5-flash-image', aspect_ratio:'1:1', resolution:'', quality:'', n:1, seed:'', ...store.get(K.settings, {}) };
  const saveS = () => store.set(K.settings, S);
  let models = [];
  const priceCache = {};
  let refs = []; // data URLs

  // ---------- รูปเก็บในตาราง images ของ SQLite ผ่าน Flask ----------
  const fileUrl = id => '/api/images/' + encodeURIComponent(id) + '/file';
  async function dbAll(){
    const r = await fetch('/api/images'); if(!r.ok) throw new Error(r.status);
    return r.json();
  }
  async function dbPut(item){
    const { blob, ...meta } = item;
    const fd = new FormData();
    fd.append('meta', JSON.stringify(meta));
    if(blob) fd.append('file', blob, item.id);
    const r = await fetch('/api/images', { method:'POST', body:fd });
    if(!r.ok) throw new Error('บันทึกรูปลงฐานข้อมูลไม่สำเร็จ (' + r.status + ')');
    item.has_file = !!blob;
  }
  const dbDel = id => fetch('/api/images/' + encodeURIComponent(id), { method:'DELETE' });

  // ---------- Models ----------
  const COMMON_RATIOS = ['1:1','16:9','9:16','4:3','3:4','3:2','2:3'];
  async function loadModels(force){
    const cache = store.get(K.models, null);
    if(!force && cache && Date.now() - cache.t < 6 * 3600e3){ models = cache.list; return; }
    const st = $('#iStatus'); st.className = 'status'; st.textContent = 'กำลังโหลดรายชื่อโมเดล…';
    try{
      let list;
      try{
        const r = await fetch(API + '/images/models'); if(!r.ok) throw new Error(r.status);
        list = (await r.json()).data || [];
      }catch{
        // สำรอง: ดึงจาก Models API แบบกรองเฉพาะที่ output เป็นรูป
        const r = await fetch(API + '/models?output_modalities=image');
        list = ((await r.json()).data || []).map(m => ({ ...m, supported_parameters:{}, _viaChat:true }));
      }
      models = list.map(m => ({
        id:m.id, name:m.name || m.id,
        inputs:m.architecture?.input_modalities || ['text'],
        outputs:m.architecture?.output_modalities || ['image'],
        sp:(m.supported_parameters && !Array.isArray(m.supported_parameters)) ? m.supported_parameters : {},
        stream:!!m.supports_streaming, viaChat:!!m._viaChat
      })).sort((a,b) => a.name.localeCompare(b.name));
      store.set(K.models, { t:Date.now(), list:models });
      st.textContent = '';
    }catch(e){
      st.className = 'status bad'; st.textContent = 'โหลดรายชื่อโมเดลไม่ได้ ลองกดรีเฟรช หรือพิมพ์ model id เอง';
      if(cache) models = cache.list;
    }
  }
  const curModel = () => models.find(m => m.id === S.model) || { id:S.model, name:S.model, inputs:['text','image'], outputs:['image'], sp:{}, stream:false };

  async function loadPrice(id){
    if(priceCache[id] !== undefined) return priceCache[id];
    try{
      const r = await fetch(`${API}/images/models/${id}/endpoints`); if(!r.ok) throw 0;
      const eps = (await r.json()).endpoints || [];
      const lines = eps.flatMap(e => e.pricing || []).filter(p => p.billable === 'output_image');
      if(!lines.length) throw 0;
      const min = lines.reduce((a,b) => a.cost_usd <= b.cost_usd ? a : b);
      const unit = { image:'ต่อรูป', megapixel:'ต่อ MP', token:'ต่อ token' }[min.unit] || min.unit;
      priceCache[id] = min.unit === 'token' ? 'คิดตาม token' : `เริ่ม $${+min.cost_usd.toFixed(4)} ${unit}`;
    }catch{ priceCache[id] = ''; }
    return priceCache[id];
  }

  function renderModelBtn(){
    const m = curModel();
    $('#mName').textContent = m.name;
    $('#mSub').textContent = m.id;
    loadPrice(m.id).then(p => { if(curModel().id === m.id) $('#costHint').textContent = p; });
  }

  function renderModelList(){
    const q = $('#iSearch').value.trim().toLowerCase(), list = $('#iList'); list.innerHTML = '';
    const shown = models.filter(m => !q || m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q));
    shown.forEach(m => {
      const li = el('li'), b = el('button', m.id === S.model ? 'cur' : '');
      const tags = [m.inputs.includes('image') ? 'รับภาพอ้างอิง' : '', m.outputs.includes('text') ? 'ตอบข้อความได้' : ''].filter(Boolean).join(' · ');
      b.append(el('span','n',m.name), el('span','t',tags), el('span','id',m.id), el('span','t', m.stream ? 'พรีวิวระหว่างสร้าง' : ''));
      b.onclick = () => pickModel(m.id);
      li.append(b); list.append(li);
    });
    if(!shown.length && models.length) list.append(el('li','status','ไม่พบโมเดลที่ตรงกับคำค้น'));
  }
  function pickModel(id){ S.model = id; saveS(); renderModelBtn(); buildParams(); closeModal($('#iModelModal')); }

  // ---------- Parameters (สร้างตามความสามารถของแต่ละโมเดล) ----------
  function enumOf(d){ return d && d.type === 'enum' ? d.values : null; }
  function buildParams(){
    const m = curModel(), sp = m.sp, known = Object.keys(sp).length > 0, box = $('#params');
    box.innerHTML = '';

    // Aspect ratio
    if(!known || sp.aspect_ratio){
      let vals = enumOf(sp.aspect_ratio) || ['auto', ...COMMON_RATIOS];
      if(!vals.includes(S.aspect_ratio)) S.aspect_ratio = vals.includes('1:1') ? '1:1' : vals[0];
      const g = el('div','group'); g.append(el('span','lbl','สัดส่วนภาพ'));
      const wrap = el('div','ratios');
      vals.forEach(v => {
        const b = el('button','ratio'); b.type = 'button'; b.setAttribute('aria-pressed', v === S.aspect_ratio);
        const i = el('i');
        const [w,h] = v === 'auto' ? [1,1] : v.split(':').map(Number);
        const sc = 26 / Math.max(w,h); i.style.width = Math.max(4, w*sc) + 'px'; i.style.height = Math.max(4, h*sc) + 'px';
        if(v === 'auto') i.style.borderStyle = 'dashed';
        b.append(i, el('span',null, v === 'auto' ? 'อัตโนมัติ' : v));
        b.onclick = () => { S.aspect_ratio = v; saveS(); buildParams(); };
        wrap.append(b);
      });
      g.append(wrap); box.append(g);
    } else S.aspect_ratio = '';

    // Resolution
    const res = enumOf(sp.resolution);
    if(res){ chipGroup(box, 'ความละเอียด', ['', ...res], 'resolution', v => v || 'ค่าเริ่มต้น'); } else S.resolution = '';

    // Quality
    const qual = enumOf(sp.quality);
    if(qual){ const map = { auto:'อัตโนมัติ', low:'ต่ำ', medium:'กลาง', high:'สูง' }; chipGroup(box, 'คุณภาพ', ['', ...qual.filter(v => v !== 'auto')], 'quality', v => v ? (map[v] || v) : 'อัตโนมัติ'); } else S.quality = '';

    // Number of images
    const nMax = sp.n ? (sp.n.type === 'range' ? sp.n.max : 4) : 1;
    S.n = Math.min(Math.max(1, S.n), Math.min(nMax, 10));
    const g = el('div','group'); g.append(el('span','lbl','จำนวนรูปต่อครั้ง'));
    const stp = el('div','stepper'); const minus = el('button',null,'−'), val = el('span',null,S.n), plus = el('button',null,'+');
    minus.type = plus.type = 'button'; minus.setAttribute('aria-label','ลดจำนวน'); plus.setAttribute('aria-label','เพิ่มจำนวน');
    minus.onclick = () => { if(S.n > 1){ S.n--; saveS(); val.textContent = S.n; } };
    plus.onclick = () => { if(S.n < Math.min(nMax,10)){ S.n++; saveS(); val.textContent = S.n; } };
    stp.append(minus, val, plus); g.append(stp);
    if(nMax === 1) g.append(el('div','hint','โมเดลนี้สร้างได้ครั้งละ 1 รูป — กดสร้างซ้ำได้เลย งานจะรันพร้อมกัน'));
    box.append(g);

    // Seed
    if(sp.seed){
      const gs = el('div','group'); const lb = el('label',null,'Seed (ไม่บังคับ)'); lb.htmlFor = 'seed';
      const inp = el('input','inp'); inp.id = 'seed'; inp.type = 'number'; inp.placeholder = 'สุ่ม'; inp.value = S.seed;
      inp.oninput = () => { S.seed = inp.value; saveS(); };
      gs.append(lb, inp, el('div','hint','ใส่เลขเดิม + prompt เดิม = ได้รูปใกล้เคียงเดิม')); box.append(gs);
    } else S.seed = '';

    // Reference images
    $('#refGroup').hidden = !m.inputs.includes('image');
    saveS();
  }
  function chipGroup(box, label, vals, key, fmt){
    if(!vals.includes(S[key])) S[key] = '';
    const g = el('div','group'); g.append(el('span','lbl',label));
    const c = el('div','chips');
    vals.forEach(v => { const b = el('button','chip',fmt(v)); b.type = 'button'; b.setAttribute('aria-pressed', v === S[key]); b.onclick = () => { S[key] = v; saveS(); buildParams(); }; c.append(b); });
    g.append(c); box.append(g);
  }

  // ---------- Reference images ----------
  function fileToDataUrl(file, max = 1536){
    return new Promise((res, rej) => {
      const img = new Image(); const url = URL.createObjectURL(file);
      img.onload = () => {
        const sc = Math.min(1, max / Math.max(img.width, img.height));
        const c = document.createElement('canvas'); c.width = Math.round(img.width*sc); c.height = Math.round(img.height*sc);
        c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
        URL.revokeObjectURL(url);
        res(c.toDataURL(file.type === 'image/png' ? 'image/png' : 'image/jpeg', 0.92));
      };
      img.onerror = () => { URL.revokeObjectURL(url); rej(new Error('อ่านไฟล์ภาพไม่ได้')); };
      img.src = url;
    });
  }
  async function addRefs(files){
    if(!curModel().inputs.includes('image')){ toastErr('โมเดลนี้ไม่รับภาพอ้างอิง เลือกโมเดลที่มีป้าย "รับภาพอ้างอิง"'); return; }
    for(const f of files){ if(f.type.startsWith('image/') && refs.length < 8) refs.push(await fileToDataUrl(f)); }
    renderRefs();
  }
  function addRefUrl(u){ if(refs.length < 8){ refs.push(u); renderRefs(); } }
  function renderRefs(){
    const box = $('#refs'); box.querySelectorAll('.ref').forEach(n => n.remove());
    refs.forEach((u, i) => {
      const d = el('div','ref'); const im = el('img'); im.src = u; im.alt = 'ภาพอ้างอิง ' + (i+1);
      const x = el('button',null,'✕'); x.setAttribute('aria-label','เอาภาพนี้ออก'); x.onclick = () => { refs.splice(i,1); renderRefs(); };
      d.append(im, x); box.insertBefore(d, $('#addRef'));
    });
  }
  $('#addRef').onclick = () => $('#refFile').click();
  $('#refFile').onchange = e => { addRefs([...e.target.files]); e.target.value = ''; };
  const active = () => App.view === 'image';
  document.addEventListener('paste', e => {
    if(!active()) return;
    const files = [...(e.clipboardData?.files || [])].filter(f => f.type.startsWith('image/'));
    if(files.length){ e.preventDefault(); addRefs(files); }
  });
  let dragDepth = 0;
  document.addEventListener('dragenter', e => { if(active() && e.dataTransfer?.types?.includes('Files')){ dragDepth++; $('#dropHint').classList.add('show'); } });
  document.addEventListener('dragleave', () => { if(--dragDepth <= 0){ dragDepth = 0; $('#dropHint').classList.remove('show'); } });
  document.addEventListener('dragover', e => { if(active()) e.preventDefault(); });
  document.addEventListener('drop', e => { if(!active()) return; e.preventDefault(); dragDepth = 0; $('#dropHint').classList.remove('show'); addRefs([...(e.dataTransfer?.files || [])]); });

  // ---------- Generation ----------
  const promptEl = $('#prompt'), genBtn = $('#genBtn');
  const updateGen = () => { genBtn.disabled = !promptEl.value.trim(); };
  promptEl.addEventListener('input', updateGen);
  promptEl.addEventListener('keydown', e => { if(e.key === 'Enter' && (e.ctrlKey || e.metaKey)){ e.preventDefault(); generate(); } });
  genBtn.onclick = generate;

  function headers(){
    return {
      'Authorization':'Bearer ' + apiKey, 'Content-Type':'application/json',
      'HTTP-Referer': location.origin && location.origin !== 'null' ? location.origin : 'http://localhost',
      'X-Title': appName
    };
  }
  async function apiError(res){
    let msg = res.status + ' ' + res.statusText;
    try{ const j = await res.json(); msg = j.error?.message || msg; }catch{}
    const hint = { 401:' — API Key ไม่ถูกต้อง ตรวจสอบในตั้งค่า', 402:' — เครดิต OpenRouter ไม่พอ', 429:' — ส่งถี่เกินไป รอสักครู่', 502:' — ผู้ให้บริการสร้างรูปไม่สำเร็จ (ไม่ถูกคิดเงิน)' }[res.status] || '';
    return Object.assign(new Error(msg + hint), { status:res.status });
  }

  async function generate(){
    const prompt = promptEl.value.trim();
    if(!prompt) return;
    if(!apiKey){ isAdmin ? openSettings() : alert(NO_KEY); return; }
    const m = curModel();
    const params = { aspect_ratio:S.aspect_ratio, resolution:S.resolution, quality:S.quality, n:S.n, seed:S.seed };
    const job = makeJobCard(prompt, m.id);
    const refsNow = m.inputs.includes('image') ? [...refs] : [];
    const started = Date.now();
    const tick = setInterval(() => job.time.textContent = Math.round((Date.now()-started)/1000) + ' วิ', 1000);
    try{
      let out;
      if(m.viaChat) out = await genViaChat(m, prompt, params, refsNow, job);
      else{
        try{ out = await genViaImages(m, prompt, params, refsNow, job); }
        catch(e){ if(e.status === 404 || e.status === 405) out = await genViaChat(m, prompt, params, refsNow, job); else throw e; }
      }
      if(!out.images.length) throw new Error(out.text ? 'โมเดลตอบเป็นข้อความแทนรูป: ' + out.text.slice(0, 200) : 'ไม่ได้รับรูปจากโมเดล');
      const secs = Math.round((Date.now()-started)/1000);
      const perCost = out.cost != null ? out.cost / out.images.length : null;
      const saved = [];
      for(const img of out.images){
        const item = { id:uid(), prompt, model:m.id, params, blob:img.blob || null, url:img.url || null, type:img.type, cost:perCost, secs, created:Date.now() };
        await dbPut(item); saved.push(item);
      }
      job.card.remove();
      for(const item of saved){ gallery.prepend(cardFor(item)); items.unshift(item); }
      updateCount();
    }catch(e){
      if(e.name === 'AbortError'){ job.card.remove(); }
      else{ job.ph.remove(); job.cap.prepend(el('div','i-err','สร้างไม่สำเร็จ: ' + e.message)); job.cancel.textContent = 'ปิด'; job.cancel.onclick = () => job.card.remove(); }
    }finally{ clearInterval(tick); updateEmpty(); }
  }

  async function genViaImages(m, prompt, p, refsNow, job){
    const body = { model:m.id, prompt };
    if(p.aspect_ratio) body.aspect_ratio = p.aspect_ratio;
    if(p.resolution) body.resolution = p.resolution;
    if(p.quality) body.quality = p.quality;
    if(p.n > 1) body.n = p.n;
    if(p.seed !== '' && p.seed != null) body.seed = Number(p.seed);
    if(refsNow.length) body.input_references = refsNow.map(u => ({ type:'image_url', image_url:{ url:u } }));
    const useStream = m.stream;
    if(useStream) body.stream = true;
    const res = await fetch(API + '/images', { method:'POST', headers:headers(), body:JSON.stringify(body), signal:job.ctrl.signal });
    if(!res.ok) throw await apiError(res);
    const images = []; let cost = null;
    if(!useStream){
      const j = await res.json();
      for(const d of j.data || []) images.push(await b64ToImg(d.b64_json, d.media_type));
      cost = j.usage?.cost ?? null;
      return { images, cost };
    }
    const reader = res.body.getReader(), dec = new TextDecoder(); let buf = '';
    while(true){
      const { done, value } = await reader.read(); if(done) break;
      buf += dec.decode(value, { stream:true });
      const lines = buf.split('\n'); buf = lines.pop();
      for(const line of lines){
        const l = line.trim(); if(!l.startsWith('data:')) continue;
        const data = l.slice(5).trim(); if(data === '[DONE]') continue;
        let ev; try{ ev = JSON.parse(data); }catch{ continue; }
        if(ev.type === 'error' || ev.error) throw new Error(ev.error?.message || 'เกิดข้อผิดพลาดระหว่างสร้าง');
        if(ev.type === 'image_generation.partial_image' && ev.b64_json) job.preview('data:image/png;base64,' + ev.b64_json);
        if(ev.type === 'image_generation.completed'){ images.push(await b64ToImg(ev.b64_json, ev.media_type)); if(ev.usage?.cost != null) cost = (cost || 0) + ev.usage.cost; }
      }
    }
    return { images, cost };
  }

  // ทางสำรอง: สร้างรูปผ่าน Chat Completions (modalities)
  async function genViaChat(m, prompt, p, refsNow, job){
    const content = refsNow.length ? [{ type:'text', text:prompt }, ...refsNow.map(u => ({ type:'image_url', image_url:{ url:u } }))] : prompt;
    const body = { model:m.id, messages:[{ role:'user', content }], modalities: m.outputs.includes('text') ? ['image','text'] : ['image'] };
    const ic = {}; if(p.aspect_ratio && p.aspect_ratio !== 'auto') ic.aspect_ratio = p.aspect_ratio; if(p.resolution) ic.image_size = p.resolution;
    if(Object.keys(ic).length) body.image_config = ic;
    const res = await fetch(API + '/chat/completions', { method:'POST', headers:headers(), body:JSON.stringify(body), signal:job.ctrl.signal });
    if(!res.ok) throw await apiError(res);
    const j = await res.json();
    const msg = j.choices?.[0]?.message || {};
    const images = [];
    for(const im of msg.images || []){
      const u = im.image_url?.url || im.url; if(!u) continue;
      try{ const b = await (await fetch(u)).blob(); images.push({ blob:b, type:b.type }); }catch{ images.push({ url:u }); }
    }
    return { images, cost:j.usage?.cost ?? null, text: typeof msg.content === 'string' ? msg.content : '' };
  }
  async function b64ToImg(b64, type = 'image/png'){
    const blob = await (await fetch(`data:${type};base64,${b64}`)).blob();
    return { blob, type };
  }

  // ---------- Gallery ----------
  const gallery = $('#gallery');
  let items = [];
  const objUrls = new Map();
  function srcOf(item){
    if(item.blob){ if(!objUrls.has(item.id)) objUrls.set(item.id, URL.createObjectURL(item.blob)); return objUrls.get(item.id); }
    if(item.has_file) return fileUrl(item.id);
    return item.url;
  }
  function ratioOf(p){ if(!p?.aspect_ratio || p.aspect_ratio === 'auto') return '1 / 1'; const [w,h] = p.aspect_ratio.split(':'); return `${w} / ${h}`; }

  function makeJobCard(prompt, model){
    const card = el('div','card job'), ctrl = new AbortController();
    const ph = el('div','ph'); ph.style.aspectRatio = ratioOf(S);
    const time = el('span',null,'0 วิ'); ph.append(time);
    const cap = el('div','cap'); cap.append(el('div','p',prompt));
    const m = el('div','m'); m.append(el('span',null,model), el('span',null, S.n > 1 ? `${S.n} รูป` : '')); cap.append(m);
    const acts = el('div','acts'); const cancel = el('button',null,'ยกเลิก'); cancel.onclick = () => ctrl.abort(); acts.append(cancel); cap.append(acts);
    card.append(ph, cap); gallery.prepend(card); updateEmpty();
    return { card, ph, cap, time, cancel, ctrl, preview(u){ let im = ph.querySelector('img'); if(!im){ im = el('img'); im.alt = 'พรีวิวระหว่างสร้าง'; ph.prepend(im); } im.src = u; } };
  }

  function cardFor(item){
    const card = el('div','card'); card.dataset.id = item.id;
    const btn = el('button','img'); btn.type = 'button'; btn.setAttribute('aria-label','ดูรูปขนาดเต็ม');
    const im = el('img'); im.src = srcOf(item); im.alt = item.prompt; im.loading = 'lazy';
    btn.append(im); btn.onclick = () => openLightbox(item);
    const cap = el('div','cap'); cap.append(el('div','p',item.prompt));
    const m = el('div','m'); m.append(el('span',null,item.model), el('span',null, item.cost != null ? '$' + (+item.cost.toFixed(4)) : '')); cap.append(m);
    cap.append(actionsFor(item, () => card.remove()));
    card.append(btn, cap); return card;
  }
  function actionsFor(item, onDelete){
    const acts = el('div','acts');
    const dl = el('button',null,'ดาวน์โหลด'); dl.onclick = () => download(item);
    const reuse = el('button',null,'ใช้ prompt นี้'); reuse.onclick = () => { promptEl.value = item.prompt; updateGen(); if(models.some(x => x.id === item.model)) pickModel(item.model); closeModal($('#lightbox')); promptEl.focus(); };
    const asRef = el('button',null,'ใช้เป็นภาพอ้างอิง'); asRef.onclick = async () => {
      if(!curModel().inputs.includes('image')){ toastErr('โมเดลปัจจุบันไม่รับภาพอ้างอิง'); return; }
      if(item.blob || item.has_file){
        const b = item.blob || await (await fetch(fileUrl(item.id))).blob();
        addRefs([new File([b], 'ref.png', { type:b.type || 'image/png' })]);
      } else addRefUrl(item.url);
      closeModal($('#lightbox'));
    };
    const del = el('button','del','ลบ'); del.onclick = async () => {
      if(!confirm('ลบรูปนี้?')) return;
      await dbDel(item.id); items = items.filter(x => x.id !== item.id);
      if(objUrls.has(item.id)){ URL.revokeObjectURL(objUrls.get(item.id)); objUrls.delete(item.id); }
      gallery.querySelector(`[data-id="${item.id}"]`)?.remove(); onDelete?.(); closeModal($('#lightbox')); updateCount(); updateEmpty();
    };
    acts.append(dl, reuse, asRef, del); return acts;
  }
  function download(item){
    const ext = ({ 'image/jpeg':'jpg', 'image/webp':'webp', 'image/svg+xml':'svg' })[item.blob?.type || item.type] || 'png';
    const a = el('a'); a.href = srcOf(item);
    a.download = item.prompt.slice(0, 40).replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g,'') + '-' + item.id + '.' + ext;
    a.target = '_blank'; a.click();
  }
  function openLightbox(item){
    $('#lbImg').src = srcOf(item); $('#lbImg').alt = item.prompt;
    $('#lbPrompt').textContent = item.prompt;
    const p = item.params || {};
    $('#lbMeta').textContent = [item.model, p.aspect_ratio, p.resolution, p.quality && 'quality ' + p.quality, p.seed !== '' && p.seed != null && 'seed ' + p.seed, item.secs && item.secs + ' วิ', item.cost != null && '$' + (+item.cost.toFixed(4)), new Date(item.created).toLocaleString('th-TH')].filter(Boolean).join('  ·  ');
    const f = $('#lbActs'); f.innerHTML = ''; f.append(actionsFor(item));
    openModal('#lightbox');
  }
  function updateCount(){ $('#count').textContent = items.length ? items.length + ' รูป' : ''; }
  const ideas = ['โลโก้มินิมอลรูปฟันเฟืองผสมใบข้าว สีน้ำเงินกับเหลือง พื้นหลังขาว','ภาพสินค้าพวงกุญแจพิมพ์ 3 มิติ วางบนโต๊ะไม้ แสงธรรมชาติจากหน้าต่าง','ภาพประกอบสไตล์สีน้ำ ตลาดน้ำอยุธยายามเช้า มีหมอกบาง ๆ'];
  function updateEmpty(){
    const box = $('#emptyBox'); box.innerHTML = '';
    if(gallery.children.length) return;
    const e = el('div','i-empty');
    e.append(el('h2',null,'ยังไม่มีรูป'), el('div',null, apiKey ? 'พิมพ์คำบรรยายทางซ้ายแล้วกดสร้างรูป หรือลองตัวอย่างนี้' : isAdmin ? 'ใส่ OpenRouter API Key ในตั้งค่าก่อนเริ่มสร้างรูป' : NO_KEY));
    const list = el('div','ideas');
    if(!apiKey){ if(isAdmin){ const b = el('button',null,'ไปที่ตั้งค่า'); b.onclick = openSettings; list.append(b); } }
    else ideas.forEach(t => { const b = el('button',null,t); b.onclick = () => { promptEl.value = t; updateGen(); promptEl.focus(); }; list.append(b); });
    e.append(list); box.append(e);
  }
  function toastErr(msg){ alert(msg); }

  // ---------- Modals & settings ----------
  function openModal(id){ $(id).classList.add('show'); }
  function closeModal(m){ m.classList.remove('show'); }
  document.querySelectorAll('.overlay').forEach(o => o.addEventListener('click', e => { if(o.classList.contains('locked')) return; if(e.target === o || e.target.closest('[data-close]')) closeModal(o); }));
  document.addEventListener('keydown', e => { if(e.key === 'Escape') document.querySelectorAll('.overlay.show:not(.locked)').forEach(closeModal); });

  function renderKeyState(){ const k = $('#iKeyState'); k.textContent = apiKey ? 'มี API Key แล้ว' : 'ยังไม่มี API Key'; k.classList.toggle('ok', !!apiKey); }
  const openSettings = () => App.openSettings();
  $('#iOpenSettings').onclick = openSettings;
  App.on('key', k => { apiKey = k; renderKeyState(); updateEmpty(); });
  App.on('settings', s => { appName = (s.appName || 'My AI') + ' Image'; $('#iBrandName').textContent = appName; });
  $('#clearGallery').onclick = async () => {
    if(!confirm('ลบรูปของคุณทั้งหมด? กู้คืนไม่ได้')) return;
    try{ await Store.call('DELETE', '/api/images'); }
    catch(e){ App.protectedError(e, 'ลบรูปไม่สำเร็จ'); return; }
    items = []; objUrls.forEach(u => URL.revokeObjectURL(u)); objUrls.clear();
    gallery.querySelectorAll('.card:not(.job)').forEach(c => c.remove()); updateCount(); updateEmpty();
  };

  $('#iModelBtn').onclick = async () => {
    $('#iCustom').value = S.model; openModal('#iModelModal'); renderModelList();
    if(!models.length){ await loadModels(false); renderModelList(); }
    $('#iSearch').focus();
  };
  $('#iSearch').oninput = renderModelList;
  $('#iReload').onclick = async () => { await loadModels(true); renderModelList(); renderModelBtn(); buildParams(); };
  $('#iUseCustom').onclick = () => { const v = $('#iCustom').value.trim(); if(v) pickModel(v); };
  $('#iCustom').onkeydown = e => { if(e.key === 'Enter') $('#iUseCustom').click(); };

  // ---------- Init ----------
  $('#iBrandName').textContent = appName;
  renderKeyState(); updateGen();
  loadModels(false).then(() => {
    if(models.length && !models.some(m => m.id === S.model)){
      const pref = models.find(m => /gemini.*image/i.test(m.id)) || models[0];
      S.model = pref.id; saveS();
    }
    renderModelBtn(); buildParams();
  });
  renderModelBtn(); buildParams();
  dbAll().then(all => {
    items = all.sort((a,b) => b.created - a.created);
    items.forEach(it => gallery.append(cardFor(it)));
    updateCount(); updateEmpty();
  }).catch(() => updateEmpty());
})();
</script>
<script>
// ---------- จัดการผู้ใช้ (เฉพาะแอดมิน) ----------
(() => {
  if(!Store.me.is_admin) return;
  const $ = s => document.querySelector(s);
  const el = (tag, cls, txt) => { const e = document.createElement(tag); if(cls) e.className = cls; if(txt != null) e.textContent = txt; return e; };
  let users = [];

  // รหัสสุ่มที่อ่านง่าย (ตัด 0/O, 1/l/I ออก) เพื่อส่งให้ผู้ใช้
  function genPw(len = 10){
    const abc = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
    const r = crypto.getRandomValues(new Uint32Array(len));
    return Array.from(r, n => abc[n % abc.length]).join('');
  }
  function showCred(title, username, pw){
    $('#credTitle').textContent = title;
    $('#credText').textContent = `เว็บ: ${location.origin}\nชื่อผู้ใช้: ${username}\nรหัสผ่าน: ${pw}`;
    $('#credBox').hidden = false;
  }
  $('#credCopy').onclick = () => navigator.clipboard.writeText($('#credText').textContent).then(() => {
    const b = $('#credCopy'), o = b.textContent; b.textContent = 'คัดลอกแล้ว'; setTimeout(() => b.textContent = o, 1400);
  });
  const fmt = t => t ? new Date(t).toLocaleString('th-TH', { dateStyle:'medium', timeStyle:'short' }) : 'ยังไม่เคยเข้าใช้';

  async function load(){
    try{ users = await Store.call('GET', '/api/users'); }
    catch(e){ $('#uList').innerHTML = ''; $('#uList').append(el('li','status bad','โหลดรายชื่อไม่สำเร็จ: ' + e.message)); return; }
    render();
  }
  function render(){
    const list = $('#uList'); list.innerHTML = '';
    $('#uCount').textContent = users.length + ' บัญชี';
    users.forEach(u => {
      const self = u.id === Store.me.id;
      const li = el('li', u.active ? '' : 'off');
      const who = el('div','who'); const name = el('b',null,u.username); who.append(name);
      if(u.is_admin) who.append(el('span','badge admin','แอดมิน'));
      if(!u.active) who.append(el('span','badge off','ระงับ'));
      if(self) who.append(el('span','badge me','คุณ'));
      who.append(el('span','t','เข้าใช้ล่าสุด: ' + fmt(u.last_login)));
      li.append(who);
      if(!self){
        const acts = el('div','acts');
        const btn = (txt, fn, cls) => { const b = el('button', cls || '', txt); b.type = 'button'; b.onclick = fn; acts.append(b); };
        btn('ตั้งรหัสใหม่', () => resetPw(u));
        btn(u.active ? 'ระงับ' : 'เปิดใช้', () => patch(u, { active:!u.active }));
        btn(u.is_admin ? 'ยกเลิกแอดมิน' : 'ตั้งเป็นแอดมิน', () => {
          if(u.is_admin || confirm(`ให้ ${u.username} เป็นผู้ดูแลระบบ? จะเห็นและแก้ API Key และจัดการผู้ใช้ได้`)) patch(u, { is_admin:!u.is_admin });
        });
        btn('ลบ', () => removeUser(u), 'del');
        li.append(acts);
      }
      list.append(li);
    });
  }
  async function patch(u, data){
    try{ const nu = await Store.call('PATCH', '/api/users/' + u.id, data); Object.assign(u, nu); render(); return true; }
    catch(e){ alert('แก้ไขไม่สำเร็จ: ' + e.message); return false; }
  }
  async function resetPw(u){
    const pw = prompt(`รหัสผ่านใหม่ของ ${u.username} (อย่างน้อย 6 ตัวอักษร)\nผู้ใช้จะถูกออกจากระบบทุกเครื่อง`, genPw());
    if(pw == null) return;
    if(pw.length < 6){ alert('รหัสผ่านต้องยาวอย่างน้อย 6 ตัวอักษร'); return; }
    if(await patch(u, { password:pw })) showCred(`ตั้งรหัสใหม่ให้ ${u.username} แล้ว — ส่งข้อมูลนี้ให้ผู้ใช้`, u.username, pw);
  }
  async function removeUser(u){
    if(!confirm(`ลบผู้ใช้ ${u.username}? แชทและรูปทั้งหมดของผู้ใช้นี้จะถูกลบด้วย กู้คืนไม่ได้\n(ถ้าแค่ต้องการหยุดการใช้งาน ให้กด "ระงับ" แทน)`)) return;
    try{ await Store.call('DELETE', '/api/users/' + u.id); }
    catch(e){ alert('ลบไม่สำเร็จ: ' + e.message); return; }
    users = users.filter(x => x.id !== u.id); render();
  }

  $('#nuGen').onclick = () => { $('#nuPw').value = genPw(); };
  $('#addUserForm').onsubmit = async e => {
    e.preventDefault();
    const st = $('#nuStatus'), username = $('#nuName').value.trim(), password = $('#nuPw').value;
    st.className = 'status bad';
    if(!/^[A-Za-z0-9._-]{3,32}$/.test(username)){ st.textContent = 'ชื่อผู้ใช้ต้องยาว 3–32 ตัว ใช้ได้เฉพาะ a-z, 0-9, จุด, ขีดกลาง, ขีดล่าง'; return; }
    if(password.length < 6){ st.textContent = 'รหัสผ่านต้องยาวอย่างน้อย 6 ตัวอักษร'; return; }
    try{ users.push(await Store.call('POST', '/api/users', { username, password, is_admin:$('#nuAdmin').checked })); }
    catch(err){ st.textContent = err.message; return; }
    st.textContent = '';
    $('#nuName').value = ''; $('#nuPw').value = ''; $('#nuAdmin').checked = false;
    showCred(`สร้างบัญชี ${username} แล้ว — ส่งข้อมูลนี้ให้ผู้ใช้`, username, password);
    render();
  };

  $('#openUsers').onclick = () => {
    $('#credBox').hidden = true; $('#nuStatus').textContent = '';
    if(!$('#nuPw').value) $('#nuPw').value = genPw();
    document.getElementById('side').classList.remove('open'); document.getElementById('scrim').classList.remove('show');
    $('#usersModal').classList.add('show'); setTimeout(() => $('#nuName').focus(), 50);
    load();
  };
})();
</script>
<script>App.setView(location.hash.slice(1));</script>
</body>
</html>
'''


# ---------------------------------------------------------------- Login page (ไม่มีหน้าสมัครสมาชิก)
LOGIN_PAGE = r'''<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>เข้าสู่ระบบ · My AI Studio</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Thai:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{
  --bg:#F3F5F8; --panel:#FFFFFF; --ink:#18212C; --muted:#5C6878; --line:#D3DAE3;
  --accent:#2B59C3; --accent-ink:#FFFFFF; --amber:#C98A12; --danger:#C0392B;
  --font:"IBM Plex Sans Thai",system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif;
}
@media (prefers-color-scheme: dark){
  :root{ --bg:#141A22; --panel:#1B232D; --ink:#E4E9F0; --muted:#94A1B2; --line:#2C3744; --accent:#6C95F0; --accent-ink:#0E1420; --amber:#E7B04A; --danger:#E26D5F; }
}
*,*::before,*::after{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--ink);font:15px/1.6 var(--font);display:flex;align-items:center;justify-content:center;padding:16px}
button,input{font:inherit;color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.card{width:100%;max-width:380px;background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:26px 24px 20px}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:18px;margin-bottom:4px}
.brand .dot{width:12px;height:12px;border-radius:3px;background:var(--accent);box-shadow:5px 5px 0 var(--amber)}
.desc{color:var(--muted);font-size:14px;margin:0 0 18px}
label{display:block;font-weight:500;margin-bottom:5px}
.field{margin-bottom:14px}
input{width:100%;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:9px 11px;outline:0}
input:focus{border-color:var(--accent)}
.hint{font-size:12.5px;color:var(--muted);margin-top:4px}
.err{color:var(--danger);font-size:14px;min-height:1.4em;margin-bottom:6px}
.go{width:100%;background:var(--accent);color:var(--accent-ink);border:0;border-radius:10px;padding:11px;font-weight:600;cursor:pointer}
.go:disabled{opacity:.5;cursor:wait}
.foot{text-align:center;font-size:12px;color:var(--muted);margin-top:16px}
.foot a{color:var(--accent);text-decoration:none}
</style>
</head>
<body>
<form class="card" id="f" autocomplete="on">
  <div class="brand"><span class="dot"></span><span>My AI Studio</span></div>
  <p class="desc" id="desc">เข้าสู่ระบบด้วยบัญชีที่ได้รับจากผู้ดูแลระบบ</p>
  <div class="field"><label for="u">ชื่อผู้ใช้</label><input id="u" name="username" autocomplete="username" autocapitalize="off" spellcheck="false" required></div>
  <div class="field"><label for="p">รหัสผ่าน</label><input id="p" name="password" type="password" autocomplete="current-password" required></div>
  <div class="field" id="p2Wrap" hidden><label for="p2">ยืนยันรหัสผ่าน</label><input id="p2" type="password" autocomplete="new-password"></div>
  <div class="err" id="err" role="alert"></div>
  <button class="go" id="go" type="submit">เข้าสู่ระบบ</button>
  <div class="foot" id="noSignup">ยังไม่มีบัญชี? ติดต่อผู้ดูแลระบบเพื่อขอชื่อผู้ใช้และรหัสผ่าน</div>
  <div class="foot">Developed by <a href="https://uncle-engineer.com/" target="_blank" rel="noopener">Uncle Engineer</a></div>
</form>
<script>
(() => {
  const boot = /*__BOOT__*/null || { setup:false };
  const $ = s => document.getElementById(s);
  if(boot.setup){
    // ยังไม่มีผู้ใช้เลย: สร้างบัญชีแอดมินคนแรก
    $('desc').textContent = 'เปิดใช้งานครั้งแรก — สร้างบัญชีผู้ดูแลระบบ (แอดมิน) เพื่อตั้งค่า API Key และสร้างบัญชีให้ผู้ใช้คนอื่น';
    $('p2Wrap').hidden = false; $('noSignup').hidden = true;
    $('p').autocomplete = 'new-password';
    $('u').insertAdjacentHTML('afterend', '<div class="hint">3–32 ตัว ใช้ได้เฉพาะ a-z, 0-9, จุด, ขีดกลาง, ขีดล่าง</div>');
    $('go').textContent = 'สร้างบัญชีแอดมิน';
  }
  $('u').focus();
  $('f').onsubmit = async e => {
    e.preventDefault();
    const username = $('u').value.trim(), password = $('p').value, err = $('err');
    if(boot.setup){
      if(!/^[A-Za-z0-9._-]{3,32}$/.test(username)){ err.textContent = 'ชื่อผู้ใช้ต้องยาว 3–32 ตัว ใช้ได้เฉพาะ a-z, 0-9, จุด, ขีดกลาง, ขีดล่าง'; return; }
      if(password.length < 6){ err.textContent = 'รหัสผ่านต้องยาวอย่างน้อย 6 ตัวอักษร'; return; }
      if(password !== $('p2').value){ err.textContent = 'รหัสผ่านทั้งสองช่องไม่ตรงกัน'; return; }
    }
    $('go').disabled = true; err.textContent = '';
    try{
      const r = await fetch(boot.setup ? '/api/auth/setup' : '/api/auth/login', {
        method:'POST', headers:{ 'Content-Type':'application/json' }, body: JSON.stringify({ username, password })
      });
      if(r.ok){ location.reload(); return; }
      let j = null; try{ j = await r.json(); }catch{}
      err.textContent = (j && j.error) || ('เข้าสู่ระบบไม่สำเร็จ (' + r.status + ')');
      $('p').select();
    }catch{ err.textContent = 'เชื่อมต่อเซิร์ฟเวอร์ไม่ได้'; }
    finally{ $('go').disabled = false; }
  };
})();
</script>
</body>
</html>
'''


if __name__ == "__main__":
    if "--reset-password" in sys.argv:
        # ลืมรหัส (เช่น แอดมินลืมรหัสตัวเอง):  python app.py --reset-password <username>
        con = sqlite3.connect(DB_PATH)
        i = sys.argv.index("--reset-password")
        name = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        row = con.execute("SELECT id FROM users WHERE username = ?", (name,)).fetchone()
        if not row:
            names = [r[0] + (" (แอดมิน)" if r[1] else "") for r in
                     con.execute("SELECT username, is_admin FROM users ORDER BY is_admin DESC, username")]
            print("วิธีใช้: python app.py --reset-password <username>")
            print("ผู้ใช้ที่มี: " + (", ".join(names) or "(ยังไม่มี — เปิดหน้าเว็บเพื่อสร้างบัญชีแอดมิน)"))
            sys.exit(1)
        pw = getpass.getpass(f"รหัสผ่านใหม่ของ {name}: ")
        if pw_problem(pw) or pw != getpass.getpass("ยืนยันรหัสผ่านใหม่: "):
            print(pw_problem(pw) or "รหัสผ่านทั้งสองครั้งไม่ตรงกัน")
            sys.exit(1)
        # เปิดใช้งานบัญชีด้วย เผื่อถูกระงับไว้
        con.execute("UPDATE users SET pw_hash = ?, active = 1 WHERE id = ?", (generate_password_hash(pw), row[0]))
        con.commit()
        con.close()
        print(f"ตั้งรหัสผ่านใหม่ให้ {name} แล้ว")
        sys.exit(0)
    # ค่าเริ่มต้นใช้ในเครื่องตัวเอง (127.0.0.1) — ถ้าจะให้เครื่องอื่นในวงแลนเข้าได้ ตั้ง env HOST=0.0.0.0
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", 5050)), debug=False)