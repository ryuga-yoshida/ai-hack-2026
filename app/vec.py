"""埋め込みの生成と検索。numpy の内積だけで賄う（件数が数百件規模のため）。"""
import sqlite3
from datetime import datetime

import numpy as np

from app import db
from app.llm import router
from app.models import Event


def embed_missing(conn: sqlite3.Connection, kinds: tuple[str, ...] = ("decision", "artifact_change", "utterance", "task_hint")) -> int:
    """埋め込み未生成の Event をまとめて生成する。戻り値は生成件数"""
    rows = conn.execute(
        f"SELECT e.id, e.text FROM events e LEFT JOIN embeddings m ON m.event_id = e.id "
        f"WHERE m.event_id IS NULL AND e.kind IN ({','.join('?' * len(kinds))}) ORDER BY e.occurred_at",
        kinds).fetchall()
    if not rows:
        return 0
    vecs = router.embed([r["text"] for r in rows])
    n = 0
    for r, v in zip(rows, vecs):
        if v.shape[0] > 1:          # replay でキャッシュにない場合はゼロ次元ダミーが返る
            db.save_embedding(conn, r["id"], v)
            n += 1
    conn.commit()
    return n


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n else v


def search(conn: sqlite3.Connection, query: str, kind: str, top_k: int,
           since: datetime | None = None, until: datetime | None = None,
           exclude_ids: set[str] | None = None) -> list[tuple[Event, float]]:
    """query に近い kind の Event を (Event, cos類似度) の降順で top_k 件返す"""
    sql = ("SELECT e.*, m.vector FROM events e JOIN embeddings m ON m.event_id = e.id "
           "WHERE e.kind = ?")
    params: list = [kind]
    if since:
        sql += " AND e.occurred_at >= ?"; params.append(db.to_iso(since))
    if until:
        sql += " AND e.occurred_at <= ?"; params.append(db.to_iso(until))
    rows = conn.execute(sql, params).fetchall()
    rows = [r for r in rows if not exclude_ids or r["id"] not in exclude_ids]
    if not rows:
        return []
    q = router.embed([query])[0]
    if q.shape[0] <= 1:
        return []
    q = _unit(q)
    mat = np.stack([_unit(np.frombuffer(r["vector"], dtype=np.float32)) for r in rows])
    sims = mat @ q
    order = np.argsort(-sims)[:top_k]
    return [(db.row_to_event(rows[i]), float(sims[i])) for i in order]


def similarity(conn: sqlite3.Connection, id_a: str, id_b: str) -> float | None:
    a, b = db.load_embedding(conn, id_a), db.load_embedding(conn, id_b)
    if a is None or b is None:
        return None
    return float(_unit(a) @ _unit(b))
