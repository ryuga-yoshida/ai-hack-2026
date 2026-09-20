"""文書ファイル（docx / pptx / md / txt など）の版差分。

xlsx 以外の成果物は本文をテキストとして取り出し、difflib で段落単位の差分を Event(kind=artifact_change) にする。
テキストが取り出せない種類（pdf / 画像 など）は「更新されました」の Event だけを出す。
LLM は使わない。
"""
import difflib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

from app import config
from app.connectors.excel import version_series
from app.models import Event, new_id

TEXT_EXT = {".md", ".txt", ".csv", ".json", ".html", ".htm"}
OFFICE_EXT = {".docx", ".pptx"}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _docx_paragraphs(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(f"{W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{W}t")).strip()
        if text:
            out.append(text)
    return out


def _pptx_paragraphs(path: Path) -> list[str]:
    out = []
    with zipfile.ZipFile(path) as z:
        names = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                       key=lambda n: int(re.search(r"(\d+)", n).group(1)))
        for i, n in enumerate(names, 1):
            root = ET.fromstring(z.read(n))
            for p in root.iter(f"{A}p"):
                text = "".join(t.text or "" for t in p.iter(f"{A}t")).strip()
                if text:
                    out.append(f"[スライド{i}] {text}")
    return out


def extract_paragraphs(path: Path) -> list[str] | None:
    ext = path.suffix.lower()
    try:
        if ext == ".docx":
            return _docx_paragraphs(path)
        if ext == ".pptx":
            return _pptx_paragraphs(path)
        if ext in TEXT_EXT:
            return [l.strip() for l in path.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    except Exception:
        return None
    return None


def diff_paragraphs(old: list[str], new: list[str]) -> list[dict]:
    """段落単位の差分 [{kind: changed|added|removed, old, new, index}]"""
    out = []
    sm = difflib.SequenceMatcher(a=old, b=new, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            for k in range(max(i2 - i1, j2 - j1)):
                o = old[i1 + k] if i1 + k < i2 else None
                n = new[j1 + k] if j1 + k < j2 else None
                out.append({"kind": "changed" if o and n else ("added" if n else "removed"), "old": o, "new": n, "index": j1 + k})
        elif tag == "insert":
            for k in range(j1, j2):
                out.append({"kind": "added", "old": None, "new": new[k], "index": k})
        elif tag == "delete":
            for k in range(i1, i2):
                out.append({"kind": "removed", "old": old[k], "new": None, "index": j1})
    return out


def _short(s: str | None, n: int = 60) -> str:
    return (s or "")[:n] + ("…" if s and len(s) > n else "")


class DocsAdapter:
    """xlsx 以外の版ファイルを差分 Event にする。ref は "<ファイル名>:段落<index>" """
    name = "docs"

    def __init__(self, dir_: Path | None = None):
        self.dir = dir_ or (config.FIXTURES_DIR / "excel")
        vp = self.dir / "versions.json"
        self.versions = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else {}

    def fetch(self, since: datetime | None) -> list[Event]:
        events: list[Event] = []
        for key, series in version_series(self.dir, ext=None).items():
            if key.lower().endswith(".xlsx"):
                continue
            display = Path(key).name
            for (_, old), (_, new) in zip(series, series[1:]):
                info = self.versions.get(str(new.relative_to(self.dir))) or self.versions.get(new.name, {})
                at = datetime.fromisoformat(info["at"]) if info.get("at") else datetime.fromtimestamp(new.stat().st_mtime)
                at = at.replace(microsecond=0)
                if since and at <= since:
                    continue
                actor = info.get("actor")
                base_meta = {"file": new.name, "base": key, "path": str(new.relative_to(self.dir)), "url": info.get("url")}
                old_p, new_p = extract_paragraphs(old), extract_paragraphs(new)
                if old_p is None or new_p is None:
                    events.append(Event(id=new_id(), source="docs", kind="artifact_change",
                                        text=f"{display} が更新されました（{new.stat().st_size // 1024} KB）",
                                        actor=actor, occurred_at=at, ref=f"{new.name}",
                                        meta={**base_meta, "diff_kind": "binary"}))
                    continue
                for d in diff_paragraphs(old_p, new_p):
                    if d["kind"] == "changed":
                        text = f"{display} の段落が「{_short(d['old'])}」から「{_short(d['new'])}」に変更されました"
                    elif d["kind"] == "added":
                        text = f"{display} に段落「{_short(d['new'])}」が追加されました"
                    else:
                        text = f"{display} から段落「{_short(d['old'])}」が削除されました"
                    events.append(Event(id=new_id(), source="docs", kind="artifact_change", text=text, actor=actor,
                                        occurred_at=at, ref=f"{new.name}:段落{d['index'] + 1}",
                                        meta={**base_meta, "diff_kind": f"text_{d['kind']}", "old": d["old"], "new": d["new"],
                                              "row_key": _short(d["old"] or d["new"], 20), "column_label": "本文"}))
        return events


# ---------- 書き出し（新規作成・簡易編集用） ----------

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def docx_bytes(paragraphs: list[str]) -> bytes:
    """段落のリストから最小の docx を作る（Word / LibreOffice で開ける）"""
    import io
    from xml.sax.saxutils import escape
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>' for p in paragraphs)
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
           f"{body}<w:sectPr/></w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


def xlsx_bytes(sheet_name: str = "Sheet1") -> bytes:
    import io
    from openpyxl import Workbook
    wb = Workbook()
    wb.active.title = sheet_name[:31]
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
