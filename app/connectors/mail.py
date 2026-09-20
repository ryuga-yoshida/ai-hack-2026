"""自作メール（Outlook の代わり）。受信箱・送信済み・スレッド。

メールは第5の情報源。MailAdapter が各メールを Event(kind=utterance, source=mail) にし、
議事録・チャットと同じ抽出・紐付けパイプラインに乗せる。
Outlook（Microsoft Graph /me/messages）への差し替えは OutlookAdapter（空実装）を参照。
"""
import json
import sqlite3
from datetime import datetime

from app import db
from app.models import Event, new_id

DDL = """
CREATE TABLE IF NOT EXISTS mails (
    id          TEXT PRIMARY KEY,
    thread_id   TEXT NOT NULL,
    sender      TEXT NOT NULL,
    recipients  TEXT NOT NULL,     -- JSON 配列
    cc          TEXT,              -- JSON 配列
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    attachments TEXT,              -- JSON [{"url","name"}]
    read        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mails_thread ON mails(thread_id, sent_at);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def send(conn: sqlite3.Connection, sender: str, recipients: list[str], subject: str, body: str,
         cc: list[str] | None = None, sent_at: datetime | None = None, thread_id: str | None = None,
         attachments: list[dict] | None = None, mail_id: str | None = None) -> str:
    mail_id = mail_id or new_id()
    conn.execute(
        "INSERT INTO mails (id, thread_id, sender, recipients, cc, subject, body, sent_at, attachments) VALUES (?,?,?,?,?,?,?,?,?)",
        (mail_id, thread_id or mail_id, sender, json.dumps(recipients, ensure_ascii=False),
         json.dumps(cc or [], ensure_ascii=False), subject, body,
         db.to_iso((sent_at or datetime.now()).replace(microsecond=0)),
         json.dumps(attachments, ensure_ascii=False) if attachments else None))
    conn.commit()
    return mail_id


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["recipients"] = json.loads(d["recipients"])
    d["cc"] = json.loads(d["cc"]) if d["cc"] else []
    d["attachments"] = json.loads(d["attachments"]) if d["attachments"] else []
    return d


def inbox(conn: sqlite3.Connection, me: str | None, folder: str = "inbox") -> list[dict]:
    """スレッド単位（最新メール順）。me が空なら全員分"""
    rows = [_row(r) for r in conn.execute("SELECT * FROM mails ORDER BY sent_at DESC, rowid DESC")]
    if me:
        if folder == "sent":
            rows = [m for m in rows if m["sender"] == me]
        else:
            rows = [m for m in rows if me in m["recipients"] or me in m["cc"]]
    threads: dict[str, dict] = {}
    for m in rows:
        t = threads.setdefault(m["thread_id"], {"thread_id": m["thread_id"], "subject": m["subject"], "latest": m,
                                                "count": 0, "unread": 0, "participants": []})
        t["count"] += 1
        t["unread"] += 0 if m["read"] else 1
        for p in [m["sender"], *m["recipients"]]:
            if p not in t["participants"]:
                t["participants"].append(p)
    return list(threads.values())


def thread(conn: sqlite3.Connection, thread_id: str) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM mails WHERE thread_id=? ORDER BY sent_at, rowid", (thread_id,))]


def mark_read(conn: sqlite3.Connection, thread_id: str) -> None:
    conn.execute("UPDATE mails SET read=1 WHERE thread_id=?", (thread_id,))
    conn.commit()


class MailAdapter:
    """mails を Event(kind=utterance, source=mail) にする。Event.id はメール id"""
    name = "mail"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def fetch(self, since: datetime | None) -> list[Event]:
        sql, params = "SELECT * FROM mails", []
        if since:
            sql += " WHERE sent_at > ?"; params.append(db.to_iso(since))
        sql += " ORDER BY sent_at, rowid"
        out = []
        for r in self.conn.execute(sql, params):
            m = _row(r)
            out.append(Event(
                id=m["id"], source="mail", kind="utterance",
                text=f"件名: {m['subject']}\n{m['body']}", actor=m["sender"],
                occurred_at=db.from_iso(m["sent_at"]), ref=f"mail:{m['thread_id']}#{m['id']}",
                meta={"channel": f"mail:{m['thread_id']}", "thread_id": m["thread_id"], "subject": m["subject"],
                      "recipients": m["recipients"], "message_id": m["id"]},
            ))
        return out


class OutlookAdapter:
    """Microsoft Graph 互換のアダプタ（空実装）。

    GET /me/mailFolders/inbox/messages?$filter=receivedDateTime ge {since}&$select=id,conversationId,from,toRecipients,subject,bodyPreview,receivedDateTime
    → {"value": [{"id": str, "conversationId": str, "from": {"emailAddress": {"name": str}},
                  "toRecipients": [...], "subject": str, "bodyPreview": str, "receivedDateTime": str}]}

    自作メールと同一の Event を生成する。要求スコープは Mail.Read（読み取りのみ）。
    """
    name = "outlook"

    def fetch(self, since: datetime | None) -> list[Event]:
        raise NotImplementedError("本ハッカソンでは自作メールで代替")
