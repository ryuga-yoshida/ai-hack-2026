"""自作チャット。DB に直接テーブルを持つ最小構成（スレッド・リアクションなし）。"""
import sqlite3
from datetime import datetime

from app import db
from app.models import Event, new_id

DDL = """
CREATE TABLE IF NOT EXISTS chat_channels (
    name        TEXT PRIMARY KEY,
    description TEXT,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'channel',   -- channel | dm | group
    members     TEXT,                              -- JSON 配列（dm / group）
    title       TEXT                               -- 表示名（group）
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    text        TEXT NOT NULL,
    posted_at   TEXT NOT NULL,
    reply_to    TEXT,             -- スレッド返信なら親メッセージ id
    edited_at   TEXT,
    deleted     INTEGER NOT NULL DEFAULT 0,
    attachments TEXT              -- JSON [{"url":..., "name":...}]
);
CREATE TABLE IF NOT EXISTS chat_reactions (
    message_id  TEXT NOT NULL,
    emoji       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    PRIMARY KEY (message_id, emoji, actor)
);
"""

_COLUMNS = {"reply_to": "TEXT", "edited_at": "TEXT", "deleted": "INTEGER NOT NULL DEFAULT 0", "attachments": "TEXT"}
_CH_COLUMNS = {"kind": "TEXT NOT NULL DEFAULT 'channel'", "members": "TEXT", "title": "TEXT"}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    # 既存 DB への列追加（冪等）
    have = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
    for col, typ in _COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE chat_messages ADD COLUMN {col} {typ}")
    have = {r[1] for r in conn.execute("PRAGMA table_info(chat_channels)")}
    for col, typ in _CH_COLUMNS.items():
        if col not in have:
            conn.execute(f"ALTER TABLE chat_channels ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_channel_time ON chat_messages(channel, posted_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_reply ON chat_messages(reply_to)")
    conn.commit()


def post_message(conn: sqlite3.Connection, channel: str, actor: str, text: str,
                 posted_at: datetime | None = None, msg_id: str | None = None,
                 reply_to: str | None = None, attachments: list[dict] | None = None) -> str:
    import json
    msg_id = msg_id or new_id()
    ensure_channel(conn, channel)
    conn.execute(
        "INSERT INTO chat_messages (id, channel, actor, text, posted_at, reply_to, attachments) VALUES (?,?,?,?,?,?,?)",
        (msg_id, channel, actor, text, db.to_iso(posted_at or datetime.now().replace(microsecond=0)),
         reply_to, json.dumps(attachments, ensure_ascii=False) if attachments else None),
    )
    conn.commit()
    return msg_id


def edit_message(conn: sqlite3.Connection, msg_id: str, text: str) -> None:
    conn.execute("UPDATE chat_messages SET text=?, edited_at=? WHERE id=?",
                 (text, datetime.now().replace(microsecond=0).isoformat(), msg_id))
    conn.execute("UPDATE events SET text=? WHERE id=?", (text, msg_id))   # Event 側も追従
    conn.commit()


def delete_message(conn: sqlite3.Connection, msg_id: str) -> None:
    conn.execute("UPDATE chat_messages SET deleted=1 WHERE id=?", (msg_id,))
    conn.commit()


def toggle_reaction(conn: sqlite3.Connection, msg_id: str, emoji: str, actor: str) -> bool:
    """付いていれば外す、なければ付ける。戻り値は付けたかどうか"""
    if conn.execute("SELECT 1 FROM chat_reactions WHERE message_id=? AND emoji=? AND actor=?",
                    (msg_id, emoji, actor)).fetchone():
        conn.execute("DELETE FROM chat_reactions WHERE message_id=? AND emoji=? AND actor=?", (msg_id, emoji, actor))
        conn.commit()
        return False
    conn.execute("INSERT INTO chat_reactions (message_id, emoji, actor) VALUES (?,?,?)", (msg_id, emoji, actor))
    conn.commit()
    return True


def list_messages(conn: sqlite3.Connection, channel: str | None = None,
                  include_replies: bool = True) -> list[sqlite3.Row]:
    sql, params = "SELECT * FROM chat_messages WHERE deleted=0", []
    if channel:
        sql += " AND channel=?"; params.append(channel)
    if not include_replies:
        sql += " AND reply_to IS NULL"
    sql += " ORDER BY posted_at, rowid"
    return conn.execute(sql, params).fetchall()


def search_messages(conn: sqlite3.Connection, q: str, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM chat_messages WHERE deleted=0 AND (text LIKE ? OR actor LIKE ?) ORDER BY posted_at DESC LIMIT ?",
        (f"%{q}%", f"%{q}%", limit)).fetchall()


def ensure_channel(conn: sqlite3.Connection, name: str, description: str | None = None) -> None:
    conn.execute("INSERT OR IGNORE INTO chat_channels (name, description, created_at) VALUES (?,?,?)",
                 (name, description, datetime.now().replace(microsecond=0).isoformat()))
    conn.commit()


def list_channels(conn: sqlite3.Connection) -> list[str]:
    """公開チャンネル名の一覧（dm / group は含まない）"""
    import json
    dm = {r[0] for r in conn.execute("SELECT name FROM chat_channels WHERE kind != 'channel'")}
    names = {r[0] for r in conn.execute("SELECT name FROM chat_channels WHERE kind = 'channel'")}
    names |= {r[0] for r in conn.execute("SELECT DISTINCT channel FROM chat_messages")} - dm
    names = {n for n in names if not n.startswith("task:") and n != "tasks"}
    order = {"general": 0, "sales": 1, "random": 2}
    return sorted(names, key=lambda n: (order.get(n, 9), n))


def conversations(conn: sqlite3.Connection, me: str | None = None) -> list[dict]:
    """dm / group の一覧。me を渡すとその人が参加しているものだけ"""
    import json
    out = []
    for r in conn.execute("SELECT * FROM chat_channels WHERE kind IN ('dm', 'group') ORDER BY created_at DESC"):
        members = json.loads(r["members"]) if r["members"] else []
        if me and me not in members:
            continue
        last = conn.execute("SELECT posted_at FROM chat_messages WHERE channel=? AND deleted=0 ORDER BY posted_at DESC LIMIT 1",
                            (r["name"],)).fetchone()
        out.append({"name": r["name"], "kind": r["kind"], "members": members,
                    "title": r["title"] or "・".join(m for m in members if m != me) or "・".join(members),
                    "last": last["posted_at"] if last else r["created_at"]})
    return sorted(out, key=lambda c: c["last"], reverse=True)


def get_channel(conn: sqlite3.Connection, name: str) -> dict:
    import json
    r = conn.execute("SELECT * FROM chat_channels WHERE name=?", (name,)).fetchone()
    if not r:
        return {"name": name, "kind": "channel", "members": [], "title": name, "description": None}
    d = dict(r)
    d["members"] = json.loads(d["members"]) if d["members"] else []
    d["title"] = d["title"] or ("・".join(d["members"]) if d["kind"] != "channel" else d["name"])
    return d


def ensure_conversation(conn: sqlite3.Connection, members: list[str], title: str | None = None,
                        name: str | None = None, kind: str | None = None) -> str:
    """メンバーの集合で dm（2人）/ group（3人以上）を作る。同じメンバーの dm は使い回す。
    name を指定すると固定名（チームのチャットなど）で作り、メンバーは最新に更新する"""
    import hashlib, json
    members = sorted({m.strip() for m in members if m.strip()})
    if len(members) < 2 and not name:
        raise ValueError("2人以上のメンバーが必要")
    kind = kind or ("dm" if len(members) == 2 else "group")
    if not name:
        name = ("dm-" + hashlib.sha1("|".join(members).encode()).hexdigest()[:8]) if kind == "dm" else "grp-" + new_id()[:8]
    if conn.execute("SELECT 1 FROM chat_channels WHERE name=?", (name,)).fetchone():
        conn.execute("UPDATE chat_channels SET members=?, title=COALESCE(?, title) WHERE name=?",
                     (json.dumps(members, ensure_ascii=False), title, name))
    else:
        conn.execute("INSERT INTO chat_channels (name, description, created_at, kind, members, title) VALUES (?,?,?,?,?,?)",
                     (name, None, datetime.now().replace(microsecond=0).isoformat(), kind,
                      json.dumps(members, ensure_ascii=False), title))
    conn.commit()
    return name


class ChatAdapter:
    """chat_messages を Event(kind=utterance, source=chat) に変換する。

    Event.id はメッセージ id をそのまま使う（再取得しても重複しない）。
    """
    name = "chat"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def fetch(self, since: datetime | None) -> list[Event]:
        if since:
            rows = self.conn.execute(
                "SELECT * FROM chat_messages WHERE deleted=0 AND posted_at > ? ORDER BY posted_at, rowid",
                (db.to_iso(since),)).fetchall()
        else:
            rows = list_messages(self.conn)
        return [
            Event(
                id=r["id"], source="chat", kind="utterance", text=r["text"],
                actor=r["actor"], occurred_at=db.from_iso(r["posted_at"]),
                ref=f"chat:{r['channel']}#{r['id']}",
                meta={"channel": r["channel"], "message_id": r["id"], "reply_to": r["reply_to"]},
            )
            for r in rows
        ]
