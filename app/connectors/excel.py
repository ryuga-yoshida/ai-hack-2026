"""Excel 差分コネクタ。fixtures/excel/ 内の同名ファイルのバージョン列を比較する。

ファイル取得元（Box / SharePoint / Drive）は実装しない。ローカルの fixtures を読むだけ。
"""
import json
import re
from datetime import datetime
from pathlib import Path

from app import config
from app.excel_diff import diff_workbooks
from app.models import Event, new_id

_VER = re.compile(r"^(?P<base>.+)_v(?P<ver>\d+)\.xlsx$")


def version_series(dir_: Path) -> dict[str, list[tuple[int, Path]]]:
    """{base: [(1, path_v1), (2, path_v2), ...]} をバージョン順で返す"""
    out: dict[str, list[tuple[int, Path]]] = {}
    for p in sorted(dir_.glob("*.xlsx")):
        m = _VER.match(p.name)
        if m:
            out.setdefault(m["base"], []).append((int(m["ver"]), p))
    for v in out.values():
        v.sort()
    return out


class ExcelAdapter:
    name = "excel"

    def __init__(self, dir_: Path | None = None, actor: str | None = None,
                 changed_at: dict[str, datetime] | None = None):
        self.dir = dir_ or (config.FIXTURES_DIR / "excel")
        self.actor = actor
        # {新版ファイル名: 変更日時}。未指定なら versions.json → ファイルの更新時刻の順
        self.changed_at = changed_at or {}
        # versions.json = ストレージ側が持つ「誰がいつ更新したか」の代わり（fixtures 用）
        self.versions: dict = {}
        vp = self.dir / "versions.json"
        if vp.exists():
            self.versions = json.loads(vp.read_text(encoding="utf-8"))

    def fetch(self, since: datetime | None) -> list[Event]:
        events: list[Event] = []
        for base, series in version_series(self.dir).items():
            for (_, old), (_, new) in zip(series, series[1:]):
                info = self.versions.get(new.name, {})
                at = self.changed_at.get(new.name) or (
                    datetime.fromisoformat(info["at"]) if info.get("at") else
                    datetime.fromtimestamp(new.stat().st_mtime))
                at = at.replace(microsecond=0)
                actor = info.get("actor", self.actor)
                if since and at <= since:
                    continue
                for rec in diff_workbooks(str(old), str(new)):
                    events.append(Event(
                        id=new_id(), source="excel", kind="artifact_change",
                        text=rec.to_sentence(), actor=actor, occurred_at=at,
                        ref=f"{new.name}:{rec.sheet}!{rec.cell}" if rec.cell else f"{new.name}:{rec.sheet}",
                        meta={
                            "file": new.name, "base": base, "url": info.get("url"), "sheet": rec.sheet,
                            "row_key": rec.row_key, "column_label": rec.column_label,
                            "old": rec.old, "new": rec.new, "diff_kind": rec.kind,
                        },
                    ))
        return events
