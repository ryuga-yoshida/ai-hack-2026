"""自作の予定表（Outlook 予定表の代わり）。

予定は会議室（meetings）・チャット・タスクの期限と繋がる。
エージェントは開始前に会議室を用意してリマインドし、終了後は議事録を予定に紐付ける。
Outlook（Graph /me/events）への差し替えは OutlookCalendarAdapter（空実装）を参照。
"""
import json
import sqlite3
from datetime import datetime, timedelta

from app import db
from app.models import new_id

DDL = """
CREATE TABLE IF NOT EXISTS cal_events (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    start_at    TEXT NOT NULL,
    end_at      TEXT NOT NULL,
    attendees   TEXT,              -- JSON 配列
    location    TEXT,
    description TEXT,
    channel     TEXT,              -- 関連するチャット
    meeting_id  TEXT,              -- 会議室（開催時に紐付く）
    organizer   TEXT,
    reminded_at TEXT,              -- エージェントがリマインドした時刻
    kind        TEXT NOT NULL DEFAULT 'meeting',   -- meeting（会議室を用意）| appointment（予定のみ）
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cal_start ON cal_events(start_at);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    if "kind" not in {r[1] for r in conn.execute("PRAGMA table_info(cal_events)")}:
        conn.execute("ALTER TABLE cal_events ADD COLUMN kind TEXT NOT NULL DEFAULT 'meeting'")
    conn.commit()


def create(conn: sqlite3.Connection, title: str, start_at: datetime, end_at: datetime,
           attendees: list[str] | None = None, location: str | None = None, description: str | None = None,
           channel: str | None = None, organizer: str | None = None, event_id: str | None = None,
           meeting_id: str | None = None, kind: str = "meeting") -> str:
    eid = event_id or new_id()
    conn.execute(
        "INSERT OR IGNORE INTO cal_events (id, title, start_at, end_at, attendees, location, description, channel, meeting_id, organizer, kind, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, title, db.to_iso(start_at.replace(microsecond=0)), db.to_iso(end_at.replace(microsecond=0)),
         json.dumps(attendees or [], ensure_ascii=False), location, description, channel, meeting_id, organizer,
         kind if kind in ("meeting", "appointment") else "meeting", db.now_iso()))
    conn.commit()
    return eid


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["attendees"] = json.loads(d["attendees"]) if d["attendees"] else []
    d["start"] = db.from_iso(d["start_at"])
    d["end"] = db.from_iso(d["end_at"])
    return d


def get(conn: sqlite3.Connection, event_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM cal_events WHERE id=?", (event_id,)).fetchone()
    return _row(r) if r else None


def between(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[dict]:
    rows = conn.execute("SELECT * FROM cal_events WHERE start_at < ? AND end_at > ? ORDER BY start_at",
                        (db.to_iso(end), db.to_iso(start))).fetchall()
    return [_row(r) for r in rows]


def upcoming_unreminded(conn: sqlite3.Connection, now: datetime, within: timedelta) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM cal_events WHERE reminded_at IS NULL AND start_at > ? AND start_at <= ? ORDER BY start_at",
        (db.to_iso(now - timedelta(minutes=1)), db.to_iso(now + within))).fetchall()
    return [_row(r) for r in rows]


def update(conn: sqlite3.Connection, event_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, list) else v for v in fields.values()]
    conn.execute(f"UPDATE cal_events SET {cols} WHERE id=?", (*vals, event_id))
    conn.commit()


def delete(conn: sqlite3.Connection, event_id: str) -> None:
    conn.execute("DELETE FROM cal_events WHERE id=?", (event_id,))
    conn.commit()


class OutlookCalendarAdapter:
    """Microsoft Graph 互換（空実装）。

    GET /me/calendarView?startDateTime={s}&endDateTime={e}
    → {"value": [{"id": str, "subject": str, "start": {"dateTime": str}, "end": {"dateTime": str},
                  "attendees": [{"emailAddress": {"name": str}}], "location": {"displayName": str},
                  "onlineMeeting": {"joinUrl": str}}]}
    要求スコープは Calendars.Read（読み取りのみ）。
    """
    name = "outlook-calendar"

    def fetch(self, since):
        raise NotImplementedError("本ハッカソンでは自作の予定表で代替")
