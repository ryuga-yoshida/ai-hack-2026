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

_VER = re.compile(r"^(?P<base>.+)_v(?P<ver>\d+)(?P<ext>\.[A-Za-z0-9]+)$")
_XLSX_VER = re.compile(r"^(?P<base>.+)_v(?P<ver>\d+)\.xlsx$")


def version_series(dir_: Path, ext: str | None = ".xlsx") -> dict[str, list[tuple[int, Path]]]:
    """{base: [(1, path_v1), (2, path_v2), ...]} をバージョン順で返す。
    base はライブラリ直下からの相対パス（拡張子なし）。サブフォルダも再帰的に見る。
    ext=None なら全種類（base に拡張子を含める）"""
    out: dict[str, list[tuple[int, Path]]] = {}
    if not dir_.exists():
        return out
    for p in sorted(dir_.rglob("*")):
        if not p.is_file() or p.name.startswith((".", "~$")) or ".versions" in p.parts:
            continue
        m = _VER.match(p.name)
        if not m:
            continue
        if ext is not None and m["ext"].lower() != ext:
            continue
        rel = p.parent.relative_to(dir_)
        base = str(rel / m["base"]) if str(rel) != "." else m["base"]
        if ext is None:
            base += m["ext"]
        out.setdefault(base, []).append((int(m["ver"]), p))
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
                info = self.versions.get(str(new.relative_to(self.dir))) or self.versions.get(new.name, {})
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
                            "file": new.name, "base": base, "path": str(new.relative_to(self.dir)),
                            "url": info.get("url"), "sheet": rec.sheet,
                            "row_key": rec.row_key, "column_label": rec.column_label,
                            "old": rec.old, "new": rec.new, "diff_kind": rec.kind,
                            "formula_cell": rec.formula_cell,
                        },
                    ))
        return events


# ---------- 監視フォルダからの取り込み（OneDrive / デスクトップの Excel） ----------

def register_version(data: bytes, rel_path: str, actor: str, dest_dir: Path | None = None,
                     url: str | None = None) -> Path | None:
    """任意のファイルを <フォルダ>/<名前>_vN<拡張子> として版フォルダに保存し versions.json に記録する。
    rel_path はライブラリ直下からの相対パス（例: 商品企画/売上見込.xlsx）。
    直前の版と内容が同じなら何もしない"""
    import hashlib
    dest_dir = dest_dir or (config.FIXTURES_DIR / "excel")
    rel = Path(rel_path)
    stem, ext = re.sub(r"_v\d+$", "", rel.stem), rel.suffix.lower()
    key = str(rel.parent / stem) if str(rel.parent) != "." else stem
    series = version_series(dest_dir, ext=None).get(key + ext, [])
    if series and hashlib.sha1(series[-1][1].read_bytes()).hexdigest() == hashlib.sha1(data).hexdigest():
        return None
    n = (series[-1][0] + 1) if series else 1
    dest = dest_dir / rel.parent / f"{stem}_v{n}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    vp = dest_dir / "versions.json"
    versions = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else {}
    prev = next((versions.get(str(s[1].relative_to(dest_dir))) or versions.get(s[1].name) for s in reversed(series)), None) or {}
    versions[str(dest.relative_to(dest_dir))] = {
        "actor": actor, "at": datetime.now().replace(microsecond=0).isoformat(),
        "url": url or prev.get("url") or "https://aoba-beverage-example.sharepoint.com/sites/planning/Shared%20Documents/" + str(rel.parent / (stem + ext)).replace(" ", "%20")}
    vp.write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding="utf-8")
    return dest


def snapshot_new_version(src: Path, actor: str, dest_dir: Path | None = None,
                         url: str | None = None, rel_dir: str = "") -> Path | None:
    """監視フォルダのファイルを新しい版として取り込む（内容が同じなら何もしない）"""
    rel = str(Path(rel_dir) / src.name) if rel_dir else src.name
    return register_version(src.read_bytes(), rel, actor, dest_dir, url)


def watch_signature(dir_: Path) -> dict[str, float]:
    """監視フォルダ内のファイル（サブフォルダ含む・版番号なし・一時ファイル除く）の更新時刻。キーは相対パス"""
    if not dir_ or not dir_.exists():
        return {}
    out = {}
    for p in dir_.rglob("*"):
        if not p.is_file() or p.name.startswith(("~$", ".")) or any(part.startswith(".") for part in p.relative_to(dir_).parts):
            continue
        if re.search(r"_v\d+\.[A-Za-z0-9]+$", p.name) or p.name == "versions.json":
            continue
        out[str(p.relative_to(dir_))] = p.stat().st_mtime
    return out
