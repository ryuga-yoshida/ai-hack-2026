"""成果物の中身を Wiki 向けに構造化する。

xlsx / docx / pptx の中身をテキストに起こし（LLM 不使用）、mid モデルで
「概要 → 見出しごとの表・箇条書き」の Markdown に整理する。
数値・固有名詞は改変しない指示を入れ、人名などはマスクして送る。
応答はプロンプトをキーにキャッシュされるので、同じ版に対して 2 回目以降は API を呼ばない。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from pathlib import Path

from app import db
from app.llm import router
from app.llm.mask import mask, unmask

log = logging.getLogger(__name__)

MAX_ROWS, MAX_COLS, MAX_CHARS = 60, 14, 12000

SYSTEM = """あなたは社内 Wiki の編集者です。渡された成果物ファイルの中身を、読む人が 1 分で把握できるように Markdown で整理してください。

規則:
- 数値・日付・固有名詞は一切変えない。無いものを補わない。評価や推測は書かない
- 冒頭に「## 概要」を置き、このファイルが何を表しているかを 2〜3 行で書く
- 続けて内容のまとまりごとに「## 見出し」を立てる。表形式のデータは Markdown の表にする。手順や条件は箇条書きにする
- 合計・上限・期限など、読む人が探しそうな数字は「## 主な数値」として表にまとめる
- 語り口調にせず、簡潔に書く
- 出力は {"markdown": "..."} の JSON のみ。前置きは書かない"""


# ---------- 中身のテキスト化（LLM 不使用） ----------

def _xlsx_text(path: Path) -> str:
    import openpyxl
    wb_v = openpyxl.load_workbook(path, data_only=True, read_only=True)
    wb_f = openpyxl.load_workbook(path, data_only=False, read_only=True)
    out: list[str] = []
    for name in wb_v.sheetnames:
        ws_v, ws_f = wb_v[name], wb_f[name]
        rows_v = list(ws_v.iter_rows(min_row=1, max_row=MAX_ROWS, max_col=MAX_COLS, values_only=True))
        rows_f = list(ws_f.iter_rows(min_row=1, max_row=MAX_ROWS, max_col=MAX_COLS, values_only=True))
        lines = [f"### シート: {name}"]
        for rv, rf in zip(rows_v, rows_f):
            cells = []
            for v, f in zip(rv, rf):
                if v is None and isinstance(f, str) and f.startswith("="):
                    cells.append(f"{f}")          # 計算値が無い数式はそのまま
                else:
                    cells.append("" if v is None else str(v))
            while cells and cells[-1] == "":       # 右側の空セルを落とす
                cells.pop()
            if cells:
                lines.append("| " + " | ".join(cells) + " |")
        if len(lines) > 1:
            out.append("\n".join(lines))
    return "\n\n".join(out)


def _docx_text(path: Path) -> str:
    from app.connectors.docs import _docx_paragraphs
    return "\n".join(_docx_paragraphs(path))


def _pptx_text(path: Path) -> str:
    from app.connectors.docs import _pptx_paragraphs
    return "\n".join(_pptx_paragraphs(path))


def extract_text(path: Path) -> str | None:
    ext = path.suffix.lower()
    try:
        if ext in (".xlsx", ".xlsm"):
            text = _xlsx_text(path)
        elif ext == ".docx":
            text = _docx_text(path)
        elif ext == ".pptx":
            text = _pptx_text(path)
        elif ext in (".md", ".txt", ".csv"):
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            return None
    except Exception as e:
        log.warning("extract failed %s: %s", path.name, e)
        return None
    text = text.strip()
    return text[:MAX_CHARS] if text else None


# ---------- 構造化（mid） ----------

def structure(name: str, text: str) -> str | None:
    user = f"ファイル名: {name}\n\n----- 中身 -----\n{text}"
    masked, table = mask(user)
    res = router.complete(SYSTEM, masked, tier="mid", task="wiki")
    if not isinstance(res, dict) or not res.get("markdown"):
        return None
    md = unmask({"markdown": res["markdown"]}, table)["markdown"]
    return md.strip()


def stored(conn: sqlite3.Connection, key: str) -> dict | None:
    raw = db.get_settings(conn).get(f"wiki_digest:{key}")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def is_stale(conn: sqlite3.Connection, key: str, latest: Path) -> bool:
    """前回の整理から版が変わっているか（= 読み直しが必要か）"""
    c = stored(conn, key)
    return not c or c.get("hash") != file_hash(latest)


def content_markdown(conn: sqlite3.Connection, key: str, latest: Path, refresh: bool = False) -> tuple[str | None, bool]:
    """成果物の最新版の中身を構造化した Markdown を (markdown, LLM を呼んだか) で返す。
    refresh=False なら保存済みのものだけ返し、ファイルは読まない（定期巡回はこちら）。
    refresh=True で、版が変わっているものだけ読み直す（週 1 回の整理・手動実行）"""
    c = stored(conn, key)
    digest = file_hash(latest)
    if c and c.get("hash") == digest:
        return c.get("markdown"), False
    if not refresh:
        return (c.get("markdown") if c else None), False
    text = extract_text(latest)
    if not text or digest is None:
        return (c.get("markdown") if c else None), False
    md = structure(latest.name, text)
    if md is None:
        return (c.get("markdown") if c else None), False
    db.set_setting(conn, f"wiki_digest:{key}", json.dumps({"hash": digest, "markdown": md, "at": db.now_iso()}, ensure_ascii=False))
    return md, True
