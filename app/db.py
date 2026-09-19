"""SQLite 接続と DDL 適用。dataclass と行の相互変換もここに置く。"""
import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, date, timezone
from pathlib import Path

import numpy as np

from app import config
from app.models import Event, Task, Link, Finding, CostLog

DDL = """
-- 全ソース共通の出来事
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    source        TEXT NOT NULL,   -- meet | chat | excel | wiki
    kind          TEXT NOT NULL,   -- decision | task_hint | utterance | artifact_change
    text          TEXT NOT NULL,   -- 自然言語化された内容
    actor         TEXT,
    occurred_at   TEXT NOT NULL,   -- ISO8601
    ref           TEXT,            -- 出典 "Sheet1!D5" / "meet:tx#12"
    quote         TEXT,            -- 根拠となる原文（抽出時のみ）
    confidence    REAL DEFAULT 1.0,
    meta          TEXT,            -- JSON
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_kind_time ON events(kind, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source);

-- 状態を持つ実体
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    description   TEXT,
    assignee      TEXT,
    status        TEXT NOT NULL DEFAULT 'todo',  -- todo|in_progress|blocked|done
    due_date      TEXT,
    created_from  TEXT,            -- 生成元の decision の event id
    artifacts     TEXT,            -- JSON 配列: 紐付く成果物の識別子
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    FOREIGN KEY (created_from) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);

-- 関連（Event↔Event, Event↔Task を同一表で扱う）
CREATE TABLE IF NOT EXISTS links (
    id            TEXT PRIMARY KEY,
    from_type     TEXT NOT NULL,   -- event | task
    from_id       TEXT NOT NULL,
    to_type       TEXT NOT NULL,
    to_id         TEXT NOT NULL,
    relation      TEXT NOT NULL,   -- implements|discusses|contradicts|follows
    confidence    REAL NOT NULL,
    method        TEXT,            -- explicit|context|assignee|embedding|llm
    reason        TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_links_from ON links(from_type, from_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_type, to_id);

-- 検知結果
CREATE TABLE IF NOT EXISTS findings (
    id            TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,   -- contradiction|stalled|orphan_change
    severity      TEXT NOT NULL,   -- high|medium|low
    task_id       TEXT,
    evidence      TEXT NOT NULL,   -- JSON 配列: event id を最低2件
    summary       TEXT NOT NULL,
    reason        TEXT,
    confidence    REAL NOT NULL,
    model_used    TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending|notified|acknowledged|dismissed
    dismiss_note  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_findings_status ON findings(status);

-- 埋め込み（numpy で内積を取るため BLOB で保持）
CREATE TABLE IF NOT EXISTS embeddings (
    event_id      TEXT PRIMARY KEY,
    vector        BLOB NOT NULL,
    dim           INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(id)
);

-- コスト記録
CREATE TABLE IF NOT EXISTS cost_logs (
    id            TEXT PRIMARY KEY,
    task          TEXT NOT NULL,   -- extract|judge|link|embed|notify
    model         TEXT NOT NULL,
    tier          TEXT NOT NULL,   -- high|mid|embed
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd      REAL NOT NULL,
    occurred_at   TEXT NOT NULL
);

-- 巡回の進捗（増分処理用）
CREATE TABLE IF NOT EXISTS sync_state (
    source        TEXT PRIMARY KEY,
    last_synced   TEXT NOT NULL
);

-- 処理済みフラグ（増分処理用）。stage = extract | link | detect
CREATE TABLE IF NOT EXISTS processed (
    stage         TEXT NOT NULL,
    key           TEXT NOT NULL,   -- meeting_id / event id など
    result        TEXT,            -- JSON
    done_at       TEXT NOT NULL,
    PRIMARY KEY (stage, key)
);
"""


# ---------- 接続 ----------

def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """DDL を冪等適用する。マイグレーション機構は持たない。"""
    conn.executescript(DDL)
    conn.commit()
    from app.connectors import chat  # 自作チャットのテーブル（循環importを避けて遅延）
    chat.init(conn)


def table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


# ---------- 日時 ----------

def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def to_iso(dt: datetime | date | None) -> str | None:
    if dt is None:
        return None
    return dt.isoformat()


def from_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def date_from_iso(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


# ---------- Event ----------

def save_event(conn: sqlite3.Connection, ev: Event) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO events
           (id, source, kind, text, actor, occurred_at, ref, quote, confidence, meta, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (ev.id, ev.source, ev.kind, ev.text, ev.actor, to_iso(ev.occurred_at),
         ev.ref, ev.quote, ev.confidence,
         json.dumps(ev.meta, ensure_ascii=False), now_iso()),
    )


def save_events(conn: sqlite3.Connection, events: list[Event]) -> None:
    for ev in events:
        save_event(conn, ev)
    conn.commit()


def row_to_event(r: sqlite3.Row) -> Event:
    return Event(
        id=r["id"], source=r["source"], kind=r["kind"], text=r["text"],
        occurred_at=from_iso(r["occurred_at"]), actor=r["actor"], ref=r["ref"],
        quote=r["quote"], confidence=r["confidence"],
        meta=json.loads(r["meta"]) if r["meta"] else {},
    )


def get_event(conn: sqlite3.Connection, event_id: str) -> Event | None:
    r = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return row_to_event(r) if r else None


def list_events(conn: sqlite3.Connection, kind: str | None = None,
                source: str | None = None) -> list[Event]:
    sql, params = "SELECT * FROM events", []
    cond = []
    if kind:
        cond.append("kind=?"); params.append(kind)
    if source:
        cond.append("source=?"); params.append(source)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY occurred_at"
    return [row_to_event(r) for r in conn.execute(sql, params).fetchall()]


# ---------- Task ----------

def save_task(conn: sqlite3.Connection, t: Task, at: datetime | None = None) -> None:
    """at を渡すと created_at / updated_at をその時刻にする（抽出由来のタスクは発言時刻を使う）"""
    ts = to_iso(at) if at else now_iso()
    conn.execute(
        """INSERT INTO tasks
           (id, title, description, assignee, status, due_date, created_from, artifacts, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             title=excluded.title, description=excluded.description,
             assignee=excluded.assignee, status=excluded.status,
             due_date=excluded.due_date, created_from=excluded.created_from,
             artifacts=excluded.artifacts, updated_at=excluded.updated_at""",
        (t.id, t.title, t.description, t.assignee, t.status, to_iso(t.due_date),
         t.created_from, json.dumps(t.artifacts, ensure_ascii=False), ts, ts),
    )
    conn.commit()


def row_to_task(r: sqlite3.Row) -> Task:
    return Task(
        id=r["id"], title=r["title"], status=r["status"],
        description=r["description"], assignee=r["assignee"],
        due_date=date_from_iso(r["due_date"]), created_from=r["created_from"],
        artifacts=json.loads(r["artifacts"]) if r["artifacts"] else [],
    )


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    r = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row_to_task(r) if r else None


def list_tasks(conn: sqlite3.Connection, status: str | None = None) -> list[Task]:
    if status:
        rows = conn.execute("SELECT * FROM tasks WHERE status=? ORDER BY created_at", (status,))
    else:
        rows = conn.execute("SELECT * FROM tasks ORDER BY created_at")
    return [row_to_task(r) for r in rows.fetchall()]


# ---------- Link ----------

def save_link(conn: sqlite3.Connection, l: Link) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO links
           (id, from_type, from_id, to_type, to_id, relation, confidence, method, reason, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (l.id, l.from_type, l.from_id, l.to_type, l.to_id, l.relation,
         l.confidence, l.method, l.reason, now_iso()),
    )
    conn.commit()


def row_to_link(r: sqlite3.Row) -> Link:
    return Link(
        id=r["id"], from_type=r["from_type"], from_id=r["from_id"],
        to_type=r["to_type"], to_id=r["to_id"], relation=r["relation"],
        confidence=r["confidence"], method=r["method"], reason=r["reason"],
    )


# ---------- Finding ----------

def save_finding(conn: sqlite3.Connection, f: Finding) -> None:
    # dataclass の __post_init__ に加えて DB 直前でも再確認する（安全装置）
    if len(f.evidence) < 2:
        raise ValueError("Finding には最低2件の根拠が必要")
    conn.execute(
        """INSERT OR IGNORE INTO findings
           (id, kind, severity, task_id, evidence, summary, reason, confidence, model_used, status, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (f.id, f.kind, f.severity, f.task_id, json.dumps(f.evidence),
         f.summary, f.reason, f.confidence, f.model_used, f.status, now_iso()),
    )
    conn.commit()


def row_to_finding(r: sqlite3.Row) -> Finding:
    return Finding(
        id=r["id"], kind=r["kind"], severity=r["severity"],
        evidence=json.loads(r["evidence"]), summary=r["summary"],
        confidence=r["confidence"], task_id=r["task_id"], reason=r["reason"],
        model_used=r["model_used"], status=r["status"],
    )


# ---------- Embedding ----------

def save_embedding(conn: sqlite3.Connection, event_id: str, vec: np.ndarray) -> None:
    v = np.asarray(vec, dtype=np.float32)
    conn.execute(
        "INSERT OR REPLACE INTO embeddings (event_id, vector, dim, created_at) VALUES (?,?,?,?)",
        (event_id, v.tobytes(), int(v.shape[0]), now_iso()),
    )


def load_embedding(conn: sqlite3.Connection, event_id: str) -> np.ndarray | None:
    r = conn.execute("SELECT vector FROM embeddings WHERE event_id=?", (event_id,)).fetchone()
    return np.frombuffer(r["vector"], dtype=np.float32) if r else None


# ---------- CostLog ----------

def save_cost_log(conn: sqlite3.Connection, c: CostLog) -> None:
    conn.execute(
        """INSERT INTO cost_logs
           (id, task, model, tier, input_tokens, output_tokens, cost_usd, occurred_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (c.id, c.task, c.model, c.tier, c.input_tokens, c.output_tokens,
         c.cost_usd, to_iso(c.occurred_at)),
    )
    conn.commit()


# ---------- sync_state ----------

def last_synced(conn: sqlite3.Connection, source: str) -> datetime | None:
    r = conn.execute("SELECT last_synced FROM sync_state WHERE source=?", (source,)).fetchone()
    return from_iso(r["last_synced"]) if r else None


def update_sync_state(conn: sqlite3.Connection, source: str,
                      at: datetime | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sync_state (source, last_synced) VALUES (?,?)",
        (source, to_iso(at) if at else now_iso()),
    )
    conn.commit()


# ---------- processed ----------

def is_processed(conn: sqlite3.Connection, stage: str, key: str) -> bool:
    return conn.execute("SELECT 1 FROM processed WHERE stage=? AND key=?", (stage, key)).fetchone() is not None


def mark_processed(conn: sqlite3.Connection, stage: str, key: str, result: dict | None = None) -> None:
    conn.execute("INSERT OR REPLACE INTO processed (stage, key, result, done_at) VALUES (?,?,?,?)",
                 (stage, key, json.dumps(result, ensure_ascii=False) if result else None, now_iso()))
    conn.commit()
