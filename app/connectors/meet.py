"""Google Meet 文字起こし。唯一の実外部接続。

GOOGLE_CREDENTIALS_PATH が未設定なら fixtures/meet/*.txt を読む（フォールバック）。
文字起こしファイルの形式:
    # meeting: 定例会議
    # date: 2026-09-15 10:30
    [10:30] 鈴木: 発言...
"""
import hashlib
import logging
import re
from datetime import datetime
from pathlib import Path

from app import config
from app.models import Event

log = logging.getLogger("meet")

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

    def __init__(self, fixtures_dir: Path | None = None):
        self.fixtures_dir = fixtures_dir or (config.FIXTURES_DIR / "meet")

    def fetch(self, since: datetime | None) -> list[Event]:
        if config.GOOGLE_CREDENTIALS_PATH:
            try:
                return self._fetch_drive(since)
            except Exception as e:   # 実接続で詰まったらフォールバックだけで進める
                log.warning("Drive API failed (%s) → fixtures にフォールバック", e)
        return self._fetch_fixtures(since)

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
