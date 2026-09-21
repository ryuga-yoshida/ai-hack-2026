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
CREATE TABLE IF NOT EXISTS mail_state (
    thread_id   TEXT NOT NULL,
    user        TEXT NOT NULL,
    flagged     INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    trashed     INTEGER NOT NULL DEFAULT 0,
    unread      INTEGER NOT NULL DEFAULT 0,   -- 1 = 手動で未読に戻した
    PRIMARY KEY (thread_id, user)
);
CREATE TABLE IF NOT EXISTS mail_drafts (
    id          TEXT PRIMARY KEY,
    user        TEXT NOT NULL,
    recipients  TEXT NOT NULL,
    cc          TEXT,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    thread_id   TEXT,
    updated_at  TEXT NOT NULL
);
"""

FOLDERS = [("inbox", "受信トレイ", "download"), ("flagged", "フラグ付き", "flag"), ("sent", "送信済み", "upload"),
           ("drafts", "下書き", "edit"), ("archive", "アーカイブ", "archive"), ("trash", "ごみ箱", "trash")]


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


def states(conn: sqlite3.Connection, me: str) -> dict[str, dict]:
    return {r["thread_id"]: dict(r) for r in conn.execute("SELECT * FROM mail_state WHERE user=?", (me,))}


def set_state(conn: sqlite3.Connection, thread_id: str, me: str, **kw) -> None:
    conn.execute("INSERT OR IGNORE INTO mail_state (thread_id, user) VALUES (?,?)", (thread_id, me))
    for k, v in kw.items():
        if k in ("flagged", "archived", "trashed", "unread"):
            conn.execute(f"UPDATE mail_state SET {k}=? WHERE thread_id=? AND user=?", (1 if v else 0, thread_id, me))
    conn.commit()


def toggle_state(conn: sqlite3.Connection, thread_id: str, me: str, key: str) -> bool:
    cur = states(conn, me).get(thread_id, {}).get(key, 0)
    set_state(conn, thread_id, me, **{key: not cur})
    return not cur


def inbox(conn: sqlite3.Connection, me: str | None, folder: str = "inbox", q: str = "") -> list[dict]:
    """スレッド単位（最新メール順）。me が空なら全員分。folder: inbox/flagged/sent/archive/trash/all"""
    rows = [_row(r) for r in conn.execute("SELECT * FROM mails ORDER BY sent_at DESC, rowid DESC")]
    st = states(conn, me) if me else {}
    if me:
        if folder == "sent":
            rows = [m for m in rows if m["sender"] == me]
        elif folder == "all":
            rows = [m for m in rows if me in (m["sender"], *m["recipients"], *m["cc"])]
        else:
            rows = [m for m in rows if me in m["recipients"] or me in m["cc"] or m["sender"] == me]
    if q:
        ql = q.lower()
        rows = [m for m in rows if ql in m["subject"].lower() or ql in m["body"].lower() or ql in m["sender"].lower()]
    threads: dict[str, dict] = {}
    for m in rows:
        t = threads.setdefault(m["thread_id"], {"thread_id": m["thread_id"], "subject": m["subject"], "latest": m,
                                                "count": 0, "unread": 0, "participants": [], "has_attachment": False,
                                                **{k: st.get(m["thread_id"], {}).get(k, 0) for k in ("flagged", "archived", "trashed")}})
        t["count"] += 1
        t["unread"] += 0 if m["read"] else 1
        t["has_attachment"] = t["has_attachment"] or bool(m["attachments"])
        for p in [m["sender"], *m["recipients"]]:
            if p not in t["participants"]:
                t["participants"].append(p)
    out = []
    for t in threads.values():
        if st.get(t["thread_id"], {}).get("unread"):
            t["unread"] = max(t["unread"], 1)
        f = folder
        if f == "trash":
            keep = t["trashed"]
        elif f == "archive":
            keep = t["archived"] and not t["trashed"]
        elif f == "flagged":
            keep = t["flagged"] and not t["trashed"]
        elif f in ("sent", "all"):
            keep = not t["trashed"]
        else:  # inbox: 自分宛（自分だけの送信スレッドは送信済みへ）
            keep = not t["trashed"] and not t["archived"] and any(m for m in rows if m["thread_id"] == t["thread_id"] and m["sender"] != me)
        if keep:
            out.append(t)
    return out


def folder_counts(conn: sqlite3.Connection, me: str | None) -> dict[str, int]:
    return {f: sum(1 for t in inbox(conn, me, f) if t["unread"]) if f in ("inbox", "flagged") else len(inbox(conn, me, f))
            for f in ("inbox", "flagged", "sent", "archive", "trash")} | {"drafts": len(drafts(conn, me or ""))}


def drafts(conn: sqlite3.Connection, me: str) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM mail_drafts WHERE user=? ORDER BY updated_at DESC", (me,)):
        d = dict(r); d["recipients"] = json.loads(d["recipients"]); d["cc"] = json.loads(d["cc"] or "[]"); out.append(d)
    return out


def save_draft(conn: sqlite3.Connection, me: str, recipients: list[str], cc: list[str], subject: str, body: str,
               thread_id: str = "", draft_id: str = "") -> str:
    draft_id = draft_id or new_id()
    conn.execute("INSERT OR REPLACE INTO mail_drafts (id, user, recipients, cc, subject, body, thread_id, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                 (draft_id, me, json.dumps(recipients, ensure_ascii=False), json.dumps(cc, ensure_ascii=False), subject, body,
                  thread_id or None, db.to_iso(datetime.now().replace(microsecond=0))))
    conn.commit()
    return draft_id


def delete_draft(conn: sqlite3.Connection, draft_id: str) -> None:
    conn.execute("DELETE FROM mail_drafts WHERE id=?", (draft_id,)); conn.commit()


def thread(conn: sqlite3.Connection, thread_id: str) -> list[dict]:
    return [_row(r) for r in conn.execute("SELECT * FROM mails WHERE thread_id=? ORDER BY sent_at, rowid", (thread_id,))]


def mark_read(conn: sqlite3.Connection, thread_id: str, me: str = "") -> None:
    conn.execute("UPDATE mails SET read=1 WHERE thread_id=?", (thread_id,))
    if me:
        conn.execute("UPDATE mail_state SET unread=0 WHERE thread_id=? AND user=?", (thread_id, me))
    conn.commit()


class MailAdapter:
    """mails を Event(kind=utterance, source=mail) にする。Event.id はメール id"""
    name = "mail"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def fetch(self, since: datetime | None) -> list[Event]:
        sql, params = "SELECT * FROM mails", []
        if since:
            sql += " WHERE sent_at >= ? AND id NOT IN (SELECT id FROM events)"; params.append(db.to_iso(since))
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
