"""タグとチーム。発言・タスク・成果物・会議に横断で付けられる共通ラベル。

- kind='topic' … #商品D のような話題タグ（発言中の #xxx から自動作成）
- kind='team'  … 営業チームのようなメンバーの集合。@チーム名 でメンション（メンバー全員に届く）
tag_links で任意の対象（message / task / artifact / meeting / channel）に紐付ける。
"""
import json
import re
import sqlite3
from datetime import datetime

from app.models import new_id

DDL = """
CREATE TABLE IF NOT EXISTS tags (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL DEFAULT 'topic',   -- topic | team
    color       TEXT NOT NULL DEFAULT 'gray',
    members     TEXT,                            -- JSON 配列（team のみ）
    description TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tag_links (
    tag_id      TEXT NOT NULL,
    target_type TEXT NOT NULL,   -- message | task | artifact | meeting | channel
    target_id   TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (tag_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_tag_links_target ON tag_links(target_type, target_id);
"""

COLORS = ["indigo", "emerald", "amber", "rose", "sky", "violet", "teal", "orange"]
_HASHTAG = re.compile(r"(?<![\w/])#([^\s#@,、。！？!?()（）「」]{1,24})")


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.commit()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def all_tags(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM tags ORDER BY kind DESC, name"):
        d = dict(r)
        d["members"] = json.loads(d["members"]) if d["members"] else []
        d["count"] = conn.execute("SELECT COUNT(*) FROM tag_links WHERE tag_id=?", (d["id"],)).fetchone()[0]
        out.append(d)
    return out


def get_tag(conn: sqlite3.Connection, name: str) -> dict | None:
    r = conn.execute("SELECT * FROM tags WHERE name=?", (name,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["members"] = json.loads(d["members"]) if d["members"] else []
    return d


def ensure_tag(conn: sqlite3.Connection, name: str, kind: str = "topic",
               members: list[str] | None = None, description: str | None = None) -> dict:
    name = name.strip().lstrip("#@")
    t = get_tag(conn, name)
    if t:
        if members is not None or description is not None:
            conn.execute("UPDATE tags SET members=COALESCE(?, members), description=COALESCE(?, description), kind=? WHERE id=?",
                         (json.dumps(members, ensure_ascii=False) if members is not None else None, description,
                          kind if members else t["kind"], t["id"]))
            conn.commit()
        return get_tag(conn, name)
    n = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    conn.execute("INSERT INTO tags (id, name, kind, color, members, description, created_at) VALUES (?,?,?,?,?,?,?)",
                 (new_id(), name, kind, COLORS[n % len(COLORS)],
                  json.dumps(members, ensure_ascii=False) if members else None, description, _now()))
    conn.commit()
    return get_tag(conn, name)


def link(conn: sqlite3.Connection, tag_name: str, target_type: str, target_id: str) -> None:
    t = ensure_tag(conn, tag_name)
    conn.execute("INSERT OR IGNORE INTO tag_links (tag_id, target_type, target_id, created_at) VALUES (?,?,?,?)",
                 (t["id"], target_type, target_id, _now()))
    conn.commit()


def unlink(conn: sqlite3.Connection, tag_name: str, target_type: str, target_id: str) -> None:
    t = get_tag(conn, tag_name)
    if t:
        conn.execute("DELETE FROM tag_links WHERE tag_id=? AND target_type=? AND target_id=?", (t["id"], target_type, target_id))
        conn.commit()


def tags_for(conn: sqlite3.Connection, target_type: str, target_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT t.* FROM tag_links l JOIN tags t ON t.id = l.tag_id WHERE l.target_type=? AND l.target_id=? ORDER BY t.name",
        (target_type, target_id)).fetchall()
    return [dict(r) | {"members": json.loads(r["members"]) if r["members"] else []} for r in rows]


def targets_for(conn: sqlite3.Connection, tag_name: str) -> dict[str, list[str]]:
    t = get_tag(conn, tag_name)
    out: dict[str, list[str]] = {"message": [], "task": [], "artifact": [], "meeting": [], "channel": []}
    if not t:
        return out
    for r in conn.execute("SELECT target_type, target_id FROM tag_links WHERE tag_id=? ORDER BY created_at DESC", (t["id"],)):
        out.setdefault(r["target_type"], []).append(r["target_id"])
    return out


def hashtags(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in _HASHTAG.finditer(text or "")))


def apply_hashtags(conn: sqlite3.Connection, text: str, target_type: str, target_id: str) -> list[str]:
    """本文中の #タグ を作成して対象に紐付ける。戻り値は付けたタグ名"""
    names = hashtags(text)
    for n in names:
        link(conn, n, target_type, target_id)
    return names


def teams(conn: sqlite3.Connection) -> list[dict]:
    return [t for t in all_tags(conn) if t["kind"] == "team"]


def expand_mentions(conn: sqlite3.Connection, text: str, people: list[str]) -> set[str]:
    """@個人 と @チーム を展開して、メンションされた人の集合を返す"""
    out: set[str] = set()
    for m in re.finditer(r"@([^\s@,、。]+)", text or ""):
        name = m.group(1)
        if name in people:
            out.add(name)
        elif name in ("all", "channel", "全員"):
            out |= set(people)
        else:
            t = get_tag(conn, name)
            if t and t["kind"] == "team":
                out |= set(t["members"])
    return out
