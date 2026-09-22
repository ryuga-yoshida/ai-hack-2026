"""会議の文字起こしコネクタ。

取得元は3つ（上から順に試し、全て同じ Event を生成する）:
1. 自作 Meet（DB の meetings テーブル。会議室で録音 → Gemini で文字起こし）
2. Google Drive 上の文字起こしドキュメント（GOOGLE_CREDENTIALS_PATH が設定されている場合）
3. fixtures/meet/*.txt（フォールバック。API キーなしで動かすため）

文字起こしの形式:
    # meeting: 定例会議
    # date: 2026-09-15 10:30
    [10:30] 鈴木: 発言...
"""
import hashlib
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app import config
from app.models import Event, new_id

log = logging.getLogger("meet")

# ---------- 自作 Meet の保存先 ----------

DDL = """
CREATE TABLE IF NOT EXISTS meetings (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT NOT NULL DEFAULT 'recording',  -- recording|processing|done|failed
    transcript    TEXT,
    error         TEXT,
    channel       TEXT             -- チャットから開始した場合の会話名
);
CREATE TABLE IF NOT EXISTS meeting_tracks (
    id            TEXT PRIMARY KEY,
    meeting_id    TEXT NOT NULL,
    speaker       TEXT NOT NULL,
    mime          TEXT NOT NULL,
    audio         BLOB NOT NULL,
    rec_started_at TEXT NOT NULL,   -- 録音開始時刻（ISO8601）。t 秒を足して絶対時刻にする
    uploaded_at   TEXT NOT NULL,
    utterances    TEXT              -- JSON [{t, text}]（文字起こし済みなら）
);
CREATE TABLE IF NOT EXISTS meeting_participants (
    meeting_id    TEXT NOT NULL,
    name          TEXT NOT NULL,
    joined_at     TEXT NOT NULL,
    PRIMARY KEY (meeting_id, name)
);
CREATE TABLE IF NOT EXISTS meeting_notes (
    meeting_id    TEXT PRIMARY KEY,
    body          TEXT NOT NULL DEFAULT '',
    updated_by    TEXT,
    updated_at    TEXT
);
CREATE TABLE IF NOT EXISTS meeting_captions (
    id            TEXT PRIMARY KEY,
    meeting_id    TEXT NOT NULL,
    speaker       TEXT NOT NULL,
    text          TEXT NOT NULL,
    at            TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meeting_videos (
    id            TEXT PRIMARY KEY,
    meeting_id    TEXT NOT NULL,
    speaker       TEXT NOT NULL,
    path          TEXT NOT NULL,
    mime          TEXT NOT NULL,
    size          INTEGER NOT NULL,
    rec_started_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meeting_chat (
    id            TEXT PRIMARY KEY,
    meeting_id    TEXT NOT NULL,
    name          TEXT NOT NULL,
    text          TEXT NOT NULL,
    at            TEXT NOT NULL
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    if "channel" not in {r[1] for r in conn.execute("PRAGMA table_info(meetings)")}:
        conn.execute("ALTER TABLE meetings ADD COLUMN channel TEXT")
    conn.commit()


def create_meeting(conn: sqlite3.Connection, title: str, started_at: datetime | None = None,
                   channel: str | None = None) -> str:
    mid = new_id()
    conn.execute("INSERT INTO meetings (id, title, started_at, channel) VALUES (?,?,?,?)",
                 (mid, title.strip() or "会議", (started_at or datetime.now()).replace(microsecond=0).isoformat(), channel))
    conn.commit()
    return mid


def get_notes(conn: sqlite3.Connection, meeting_id: str) -> dict:
    r = conn.execute("SELECT * FROM meeting_notes WHERE meeting_id=?", (meeting_id,)).fetchone()
    return dict(r) if r else {"meeting_id": meeting_id, "body": "", "updated_by": None, "updated_at": None}


def save_notes(conn: sqlite3.Connection, meeting_id: str, body: str, by: str) -> None:
    conn.execute("INSERT INTO meeting_notes (meeting_id, body, updated_by, updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(meeting_id) DO UPDATE SET body=excluded.body, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                 (meeting_id, body, by, datetime.now().replace(microsecond=0).isoformat()))
    conn.commit()


def add_chat(conn: sqlite3.Connection, meeting_id: str, name: str, text: str) -> None:
    conn.execute("INSERT INTO meeting_chat (id, meeting_id, name, text, at) VALUES (?,?,?,?,?)",
                 (new_id(), meeting_id, name, text, datetime.now().replace(microsecond=0).isoformat()))
    conn.commit()


def add_caption(conn: sqlite3.Connection, meeting_id: str, speaker: str, text: str) -> None:
    conn.execute("INSERT INTO meeting_captions (id, meeting_id, speaker, text, at) VALUES (?,?,?,?,?)",
                 (new_id(), meeting_id, speaker, text, datetime.now().replace(microsecond=0).isoformat()))
    conn.commit()


def captions(conn: sqlite3.Connection, meeting_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM meeting_captions WHERE meeting_id=? ORDER BY at, rowid", (meeting_id,))]


def add_video(conn: sqlite3.Connection, meeting_id: str, speaker: str, data: bytes, mime: str, rec_started_at: datetime) -> str:
    from app import config
    vid = new_id()
    d = config.DB_PATH.parent / "recordings" / meeting_id
    d.mkdir(parents=True, exist_ok=True)
    ext = ".mp4" if "mp4" in mime else ".webm"
    path = d / f"{speaker}_{vid}{ext}"
    path.write_bytes(data)
    conn.execute("INSERT INTO meeting_videos (id, meeting_id, speaker, path, mime, size, rec_started_at) VALUES (?,?,?,?,?,?,?)",
                 (vid, meeting_id, speaker, str(path), mime, len(data), rec_started_at.replace(microsecond=0).isoformat()))
    conn.commit()
    return vid


def videos(conn: sqlite3.Connection, meeting_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM meeting_videos WHERE meeting_id=? ORDER BY rec_started_at", (meeting_id,))]


def chat_log(conn: sqlite3.Connection, meeting_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM meeting_chat WHERE meeting_id=? ORDER BY at, rowid", (meeting_id,))]


def tracks(conn: sqlite3.Connection, meeting_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT id, speaker, mime, rec_started_at, uploaded_at, length(audio) size FROM meeting_tracks WHERE meeting_id=? ORDER BY rec_started_at", (meeting_id,))]


def join(conn: sqlite3.Connection, meeting_id: str, name: str) -> None:
    conn.execute("INSERT OR IGNORE INTO meeting_participants (meeting_id, name, joined_at) VALUES (?,?,?)",
                 (meeting_id, name, datetime.now().replace(microsecond=0).isoformat()))
    conn.commit()


def add_track(conn: sqlite3.Connection, meeting_id: str, speaker: str, mime: str,
              audio: bytes, rec_started_at: datetime) -> str:
    tid = new_id()
    conn.execute(
        "INSERT INTO meeting_tracks (id, meeting_id, speaker, mime, audio, rec_started_at, uploaded_at) VALUES (?,?,?,?,?,?,?)",
        (tid, meeting_id, speaker, mime, audio, rec_started_at.replace(microsecond=0).isoformat(),
         datetime.now().replace(microsecond=0).isoformat()))
    conn.commit()
    return tid


def finalize(conn: sqlite3.Connection, meeting_id: str) -> str:
    """全トラックを文字起こしして時刻順に統合し、transcript を確定する。戻り値は文字起こし本文。"""
    import json
    from app import stt

    m = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if not m:
        raise KeyError(meeting_id)
    conn.execute("UPDATE meetings SET status='processing', error=NULL WHERE id=?", (meeting_id,))
    conn.commit()
    try:
        segs: list[tuple[datetime, str, str]] = []
        for t in conn.execute("SELECT * FROM meeting_tracks WHERE meeting_id=? ORDER BY uploaded_at", (meeting_id,)):
            if t["utterances"]:
                utts = json.loads(t["utterances"])
            else:
                utts = stt.transcribe_track(bytes(t["audio"]), t["mime"], t["speaker"], conn)
                conn.execute("UPDATE meeting_tracks SET utterances=? WHERE id=?",
                             (json.dumps(utts, ensure_ascii=False), t["id"]))
                conn.commit()
            base = datetime.fromisoformat(t["rec_started_at"])
            for u in utts:
                segs.append((base + timedelta(seconds=float(u.get("t", 0) or 0)), t["speaker"], u["text"].strip()))
        segs.sort(key=lambda x: x[0])
        started = datetime.fromisoformat(m["started_at"])
        lines = [f"# meeting: {m['title']}", f"# date: {started:%Y-%m-%d %H:%M}"]
        lines += [f"[{at:%H:%M}] {sp}: {text}" for at, sp, text in segs if text]
        transcript = "\n".join(lines)
        conn.execute("UPDATE meetings SET status='done', transcript=?, ended_at=? WHERE id=?",
                     (transcript, datetime.now().replace(microsecond=0).isoformat(), meeting_id))
        conn.commit()
        log.info("meeting %s finalized: %d utterances", meeting_id, len(segs))
        return transcript
    except Exception as e:
        conn.execute("UPDATE meetings SET status='failed', error=? WHERE id=?", (str(e)[:500], meeting_id))
        conn.commit()
        raise

_HEADER = re.compile(r"^#\s*(\w+):\s*(.+)$")
_LINE = re.compile(r"^\[(\d{1,2}):(\d{2})\]\s*([^:：]+)[:：]\s*(.*)$")


def parse_transcript(text: str) -> tuple[dict, list[tuple[int, str, str, str]]]:
    """(ヘッダ, [(行index, 時刻'HH:MM', 発言者, 発言)])"""
    header, lines = {}, []
    for i, raw in enumerate(text.splitlines()):
        m = _HEADER.match(raw)
        if m:
            header[m[1]] = m[2].strip()
            continue
        m = _LINE.match(raw)
        if m:
            lines.append((i, f"{int(m[1]):02d}:{m[2]}", m[3].strip(), m[4].strip()))
    return header, lines


def _stable_id(file_id: str, idx: int) -> str:
    return hashlib.sha1(f"meet:{file_id}#{idx}".encode()).hexdigest()[:12]


def events_from_transcript(file_id: str, text: str) -> list[Event]:
    header, lines = parse_transcript(text)
    base = datetime.fromisoformat(header.get("date", "1970-01-01 00:00"))
    title = header.get("meeting", file_id)
    out = []
    for n, (idx, hm, actor, body) in enumerate(lines):
        h, m = map(int, hm.split(":"))
        meta = {"meeting_id": file_id, "meeting_title": title, "line": idx,
                "meeting_at": base.isoformat()}
        if n == 0:
            meta["transcript"] = text   # 抽出パイプラインが使う（先頭発言にだけ持たせる）
        out.append(Event(
            id=_stable_id(file_id, idx), source="meet", kind="utterance", text=body,
            actor=actor, occurred_at=base.replace(hour=h, minute=m, second=0),
            ref=f"meet:{file_id}#{idx}", meta=meta,
        ))
    return out


class MeetAdapter:
    name = "meet"

    def __init__(self, fixtures_dir: Path | None = None, conn: sqlite3.Connection | None = None):
        self.fixtures_dir = fixtures_dir or (config.FIXTURES_DIR / "meet")
        self.conn = conn

    def fetch(self, since: datetime | None) -> list[Event]:
        events: list[Event] = []
        if self.conn is not None:
            events += self._fetch_own(since)          # 自作 Meet（本物の入力）
        if config.GOOGLE_CREDENTIALS_PATH:
            try:
                events += self._fetch_drive(since)
            except Exception as e:   # 実接続で詰まったらフォールバックだけで進める
                log.warning("Drive API failed (%s) → fixtures にフォールバック", e)
        if not config.BLANK_START:
            events += self._fetch_fixtures(since)     # 架空データ（デモ・評価用）
        return events

    def _fetch_own(self, since: datetime | None) -> list[Event]:
        rows = self.conn.execute(
            "SELECT id, transcript, ended_at FROM meetings WHERE status='done' AND transcript IS NOT NULL").fetchall()
        out: list[Event] = []
        for r in rows:
            if since and datetime.fromisoformat(r["ended_at"]) <= since:
                continue
            out += events_from_transcript(r["id"], r["transcript"])
        return out

    def _fetch_fixtures(self, since: datetime | None) -> list[Event]:
        events: list[Event] = []
        for p in sorted(self.fixtures_dir.glob("*.txt")):
            evs = events_from_transcript(p.stem, p.read_text(encoding="utf-8"))
            if evs and since and evs[0].occurred_at <= since:
                continue
            events.extend(evs)
        return events

    def _fetch_drive(self, since: datetime | None) -> list[Event]:
        """Drive API（読み取り専用スコープ）で Meet の文字起こし Google ドキュメントを取得する。

        - files.list: mimeType='application/vnd.google-apps.document' and name contains 'Transcript'
        - files.export: text/plain
        サービスアカウント鍵（GOOGLE_CREDENTIALS_PATH）で認証する。
        """
        import httpx
        from google.oauth2 import service_account          # 任意依存（未導入なら例外→フォールバック）
        from google.auth.transport.requests import Request

        creds = service_account.Credentials.from_service_account_file(
            config.GOOGLE_CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/drive.readonly"])
        creds.refresh(Request())
        headers = {"Authorization": f"Bearer {creds.token}"}
        q = "mimeType='application/vnd.google-apps.document' and (name contains 'Transcript' or name contains '文字起こし')"
        if since:
            q += f" and modifiedTime > '{since.isoformat()}Z'"
        r = httpx.get("https://www.googleapis.com/drive/v3/files",
                      params={"q": q, "fields": "files(id,name,createdTime)"}, headers=headers, timeout=30)
        r.raise_for_status()
        events: list[Event] = []
        for f in r.json().get("files", []):
            e = httpx.get(f"https://www.googleapis.com/drive/v3/files/{f['id']}/export",
                          params={"mimeType": "text/plain"}, headers=headers, timeout=60)
            e.raise_for_status()
            text = e.text
            if not text.lstrip().startswith("#"):
                created = f["createdTime"][:16].replace("T", " ")
                text = f"# meeting: {f['name']}\n# date: {created}\n{text}"
            events.extend(events_from_transcript(f["id"], text))
        return events
