"""自作 Wiki（Confluence / Notion の代わり）。ページ階層・Markdown・改訂履歴。

改訂間の差分を段落単位で Event(kind=artifact_change, source=wiki) にする（LLM 不使用）。
Confluence への差し替えは connectors/confluence.py（空実装）を参照。
"""
import difflib
import re
import sqlite3
from datetime import datetime

from app import db
from app.connectors.docs import diff_paragraphs, _short
from app.models import Event, new_id

DDL = """
CREATE TABLE IF NOT EXISTS wiki_pages (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    parent_id   TEXT,
    space       TEXT NOT NULL DEFAULT 'general',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wiki_revisions (
    id          TEXT PRIMARY KEY,
    page_id     TEXT NOT NULL,
    body        TEXT NOT NULL,
    title       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_wiki_rev ON wiki_revisions(page_id, created_at);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def paragraphs(body: str) -> list[str]:
    out = []
    for block in re.split(r"\n\s*\n", body or ""):
        t = " ".join(l.strip() for l in block.splitlines() if l.strip())
        if t:
            out.append(t)
    return out


def create(conn: sqlite3.Connection, title: str, body: str, actor: str, parent_id: str | None = None,
           space: str = "general", at: datetime | None = None, page_id: str | None = None, note: str | None = None) -> str:
    pid = page_id or new_id()
    ts = db.to_iso((at or datetime.now()).replace(microsecond=0))
    conn.execute("INSERT OR IGNORE INTO wiki_pages (id, title, body, actor, updated_at, parent_id, space, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (pid, title.strip(), body, actor, ts, parent_id, space, ts))
    conn.execute("INSERT INTO wiki_revisions (id, page_id, body, title, actor, created_at, note) VALUES (?,?,?,?,?,?,?)",
                 (new_id(), pid, body, title.strip(), actor, ts, note or "作成"))
    conn.commit()
    return pid


def update(conn: sqlite3.Connection, page_id: str, title: str, body: str, actor: str, note: str | None = None,
           at: datetime | None = None) -> bool:
    """本文またはタイトルが変わっていれば改訂を積む。変わっていなければ False"""
    cur = conn.execute("SELECT title, body FROM wiki_pages WHERE id=?", (page_id,)).fetchone()
    if not cur:
        raise KeyError(page_id)
    if cur["title"] == title.strip() and cur["body"] == body:
        return False
    ts = db.to_iso((at or datetime.now()).replace(microsecond=0))
    conn.execute("UPDATE wiki_pages SET title=?, body=?, actor=?, updated_at=? WHERE id=?", (title.strip(), body, actor, ts, page_id))
    conn.execute("INSERT INTO wiki_revisions (id, page_id, body, title, actor, created_at, note) VALUES (?,?,?,?,?,?,?)",
                 (new_id(), page_id, body, title.strip(), actor, ts, note))
    conn.commit()
    return True


def get(conn: sqlite3.Connection, page_id: str) -> dict | None:
    r = conn.execute("SELECT * FROM wiki_pages WHERE id=?", (page_id,)).fetchone()
    return dict(r) if r else None


def tree(conn: sqlite3.Connection, space: str | None = None) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT id, title, parent_id, space, actor, updated_at FROM wiki_pages ORDER BY title")]
    if space:
        rows = [r for r in rows if r["space"] == space]
    by_parent: dict[str | None, list[dict]] = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)

    def build(pid, depth):
        out = []
        for r in by_parent.get(pid, []):
            out.append({**r, "depth": depth})
            out += build(r["id"], depth + 1)
        return out
    roots = build(None, 0)
    seen = {r["id"] for r in roots}
    roots += [{**r, "depth": 0} for r in rows if r["id"] not in seen]   # 親が消えたページ
    return roots


def revisions(conn: sqlite3.Connection, page_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM wiki_revisions WHERE page_id=? ORDER BY created_at, rowid", (page_id,))]


def render_markdown(body: str) -> str:
    """最小の Markdown（見出し・箇条書き・太字・リンク・段落）。外部ライブラリ不要"""
    from html import escape
    lines = (body or "").splitlines()
    html, in_list, in_code = [], False, False
    def inline(t):
        t = escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
        t = re.sub(r"`(.+?)`", r"<code class='bg-gray-100 rounded px-1'>\1</code>", t)
        t = re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r"<a href='\2' class='text-indigo-600 underline' target='_blank'>\1</a>", t)
        t = re.sub(r"(?<![\"'>])(https?://[^\s<]+)", r"<a href='\1' class='text-indigo-600 underline' target='_blank'>\1</a>", t)
        return t
    for l in lines:
        if l.strip().startswith("```"):
            in_code = not in_code
            html.append("<pre class='bg-gray-100 rounded p-3 text-xs overflow-x-auto'>" if in_code else "</pre>")
            continue
        if in_code:
            html.append(escape(l)); continue
        m = re.match(r"^(#{1,3})\s+(.*)", l)
        if in_list and not re.match(r"^\s*[-*]\s+", l):
            html.append("</ul>"); in_list = False
        if m:
            lvl = len(m[1]); cls = {1: "text-2xl font-bold mt-6 mb-2", 2: "text-xl font-semibold mt-5 mb-2", 3: "text-lg font-semibold mt-4 mb-1"}[lvl]
            html.append(f"<h{lvl} class='{cls}'>{inline(m[2])}</h{lvl}>")
        elif re.match(r"^\s*[-*]\s+", l):
            if not in_list:
                html.append("<ul class='list-disc pl-6 space-y-0.5'>"); in_list = True
            html.append(f"<li>{inline(re.sub(r'^\s*[-*]\s+', '', l))}</li>")
        elif re.match(r"^\s*\|", l):
            cells = [c.strip() for c in l.strip().strip("|").split("|")]
            if all(re.match(r"^:?-+:?$", c) for c in cells):
                continue
            html.append("<div class='grid gap-px text-sm' style='grid-template-columns: repeat(%d, minmax(0,1fr))'>%s</div>" % (len(cells), "".join(f"<div class='border border-gray-200 px-2 py-1'>{inline(c)}</div>" for c in cells)))
        elif l.strip() == "":
            html.append("")
        else:
            html.append(f"<p class='my-1 leading-relaxed'>{inline(l)}</p>")
    if in_list:
        html.append("</ul>")
    return "\n".join(html)


class WikiAdapter:
    """改訂間の段落差分を Event にする。ref は "wiki:<page_id>#<rev_id>:段落N" """
    name = "wiki"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def fetch(self, since: datetime | None) -> list[Event]:
        out: list[Event] = []
        for page in self.conn.execute("SELECT id, title FROM wiki_pages").fetchall():
            revs = revisions(self.conn, page["id"])
            for prev, cur in zip(revs, revs[1:]):
                at = db.from_iso(cur["created_at"])
                if since and at <= since:
                    continue
                meta = {"file": f"wiki:{page['id']}", "base": f"wiki:{page['title']}", "page_id": page["id"], "revision_id": cur["id"],
                        "url": f"/wiki/{page['id']}"}
                for d in diff_paragraphs(paragraphs(prev["body"]), paragraphs(cur["body"])):
                    if d["kind"] == "changed":
                        text = f"Wiki「{cur['title']}」の段落が「{_short(d['old'])}」から「{_short(d['new'])}」に変更されました"
                    elif d["kind"] == "added":
                        text = f"Wiki「{cur['title']}」に段落「{_short(d['new'])}」が追加されました"
                    else:
                        text = f"Wiki「{cur['title']}」から段落「{_short(d['old'])}」が削除されました"
                    out.append(Event(id=new_id(), source="wiki", kind="artifact_change", text=text, actor=cur["actor"],
                                     occurred_at=at, ref=f"wiki:{page['id']}#{cur['id']}:段落{d['index'] + 1}",
                                     meta={**meta, "diff_kind": f"text_{d['kind']}", "old": d["old"], "new": d["new"],
                                           "row_key": _short(d["old"] or d["new"], 20), "column_label": "本文"}))
        return out
