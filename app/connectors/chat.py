"""自作チャット。DB に直接テーブルを持つ最小構成（スレッド・リアクションなし）。"""
import sqlite3
from datetime import datetime

from app import db
from app.models import Event, new_id

DDL = """
CREATE TABLE IF NOT EXISTS chat_messages (
    id          TEXT PRIMARY KEY,
    channel     TEXT NOT NULL,
    actor       TEXT NOT NULL,
    text        TEXT NOT NULL,
    posted_at   TEXT NOT NULL
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def post_message(conn: sqlite3.Connection, channel: str, actor: str, text: str,
                 posted_at: datetime | None = None, msg_id: str | None = None) -> str:
    msg_id = msg_id or new_id()
    conn.execute(
        "INSERT INTO chat_messages (id, channel, actor, text, posted_at) VALUES (?,?,?,?,?)",
        (msg_id, channel, actor, text, db.to_iso(posted_at or datetime.now().replace(microsecond=0))),
    )
    conn.commit()
    return msg_id


def list_messages(conn: sqlite3.Connection, channel: str | None = None) -> list[sqlite3.Row]:
    if channel:
        return conn.execute(
            "SELECT * FROM chat_messages WHERE channel=? ORDER BY posted_at, rowid", (channel,)
        ).fetchall()
    return conn.execute("SELECT * FROM chat_messages ORDER BY posted_at, rowid").fetchall()


def list_channels(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT channel FROM chat_messages ORDER BY channel").fetchall()]


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
                "SELECT * FROM chat_messages WHERE posted_at > ? ORDER BY posted_at, rowid",
                (db.to_iso(since),)).fetchall()
        else:
            rows = list_messages(self.conn)
        return [
            Event(
                id=r["id"], source="chat", kind="utterance", text=r["text"],
                actor=r["actor"], occurred_at=db.from_iso(r["posted_at"]),
                ref=f"chat:{r['channel']}#{r['id']}",
                meta={"channel": r["channel"], "message_id": r["id"]},
            )
            for r in rows
        ]
