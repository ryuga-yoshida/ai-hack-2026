"""認証: メール＋パスワード、署名付きセッション cookie。
本番の SSO（Entra ID / Google）に置き換えるときは verify_session() の中身を差し替えるだけで、
呼び出し側（get_me）は変わらない。"""
import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from app import config, db
from app.models import new_id

DDL = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,          -- 画面で使う表示名（登場人物名）
    password_hash TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_login    TEXT
);
"""

SESSION_COOKIE = "session"
SESSION_DAYS = 30
_SECRET = os.getenv("SESSION_SECRET") or ""
DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"        # /login/as/{name} を許すか（公開環境では 0）
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "aoba2026")
DEMO_ACCOUNTS = {"鈴木": "suzuki", "山田": "yamada", "田中": "tanaka", "佐藤": "sato", "高橋": "takahashi"}


def _secret() -> bytes:
    global _SECRET
    if not _SECRET:
        # 未設定なら DB に保存した乱数を使う（再起動してもセッションが切れない）
        conn = db.connect()
        r = conn.execute("SELECT value FROM settings WHERE key='session_secret'").fetchone()
        if r:
            _SECRET = r["value"]
        else:
            _SECRET = secrets.token_hex(32)
            db.set_setting(conn, "session_secret", _SECRET)
        conn.close()
    return _SECRET.encode()


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def hash_password(pw: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"


def check_password(pw: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw, salt), stored)


def create_user(conn: sqlite3.Connection, email: str, name: str, password: str) -> str:
    uid = new_id()
    conn.execute("INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?,?,?,?,?)",
                 (uid, email.strip().lower(), name.strip(), hash_password(password), db.now_iso()))
    conn.commit()
    return uid


def seed_demo_users(conn: sqlite3.Connection) -> None:
    """架空社員5名のアカウント（無ければ作る）"""
    for name, romaji in DEMO_ACCOUNTS.items():
        email = f"{romaji}@aoba-beverage.example"
        if not conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            create_user(conn, email, name, DEMO_PASSWORD)


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> dict | None:
    r = conn.execute("SELECT * FROM users WHERE email=? AND active=1", (email.strip().lower(),)).fetchone()
    if r and check_password(password, r["password_hash"]):
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (db.now_iso(), r["id"]))
        conn.commit()
        return dict(r)
    return None


def user_by_name(conn: sqlite3.Connection, name: str) -> dict | None:
    r = conn.execute("SELECT * FROM users WHERE name=? AND active=1", (name,)).fetchone()
    return dict(r) if r else None


def list_users(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, email, name, active, created_at, last_login FROM users ORDER BY created_at")]


def set_password(conn: sqlite3.Connection, user_id: str, password: str) -> None:
    conn.execute("UPDATE users SET password_hash=? WHERE id=?", (hash_password(password), user_id)); conn.commit()


# ---------- セッション ----------

def make_session(user_id: str, name: str) -> str:
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = base64.urlsafe_b64encode(f"{user_id}|{name}|{exp}".encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_session(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32], sig):
        return None
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode()
        user_id, name, exp = raw.split("|")
        if int(exp) < time.time():
            return None
        return {"user_id": user_id, "name": name}
    except Exception:
        return None
