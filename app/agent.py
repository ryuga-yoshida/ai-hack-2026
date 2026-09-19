"""自律ループ。定期巡回して取込 → 抽出 → 埋め込み → 紐付け → 検知 → アクション決定を行う。

全て増分処理。sync_state と processed で新規分だけを回す。
"""
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime

from app import config, db, detector, extract, linker, vec
from app.connectors.chat import ChatAdapter
from app.connectors.excel import ExcelAdapter
from app.connectors.meet import MeetAdapter
from app.llm import router
from app.models import Event, Finding

log = logging.getLogger("agent")


def say(msg: str, at: datetime | None = None) -> None:
    """デモ用の1行ログ（標準出力）"""
    print(f"[{(at or datetime.now()):%H:%M:%S}] {msg}", flush=True)


@dataclass
class TickResult:
    fetched: int = 0
    extracted: int = 0
    tasks_created: int = 0
    embedded: int = 0
    linked: int = 0
    findings: list[Finding] = field(default_factory=list)
    actions: dict[str, int] = field(default_factory=lambda: {"save_only": 0, "review": 0, "notify": 0})


def adapters(conn: sqlite3.Connection):
    return [MeetAdapter(conn=conn), ChatAdapter(conn), ExcelAdapter()]


# ---------- アクション決定（エージェントが自分で決める部分） ----------

def _actor_of(conn: sqlite3.Connection, event_id: str) -> str | None:
    ev = db.get_event(conn, event_id)
    return ev.actor if ev else None


def decision_actor(conn, f: Finding) -> str | None:
    return _actor_of(conn, f.evidence[0]) if f.kind == "contradiction" else None


def change_actor(conn, f: Finding) -> str | None:
    return _actor_of(conn, f.evidence[1] if f.kind == "contradiction" else f.evidence[0])


def save_only(conn, f: Finding) -> str:
    f.status = "pending"
    db.save_finding(conn, f)
    say(f"record: {f.summary}（confidence {f.confidence:.2f}、通知なし）")
    return "save_only"


def enqueue_for_review(conn, f: Finding) -> str:
    f.status = "pending"
    db.save_finding(conn, f)
    say(f"review: 人間の確認へ → {f.summary}（confidence {f.confidence:.2f}）")
    return "review"


def notify(conn, f: Finding, to: list[str | None]) -> str:
    f.status = "notified"
    db.save_finding(conn, f)
    names = "・".join(n for n in to if n) or "担当者"
    say(f"FINDING: {f.summary}（confidence {f.confidence:.2f}, severity {f.severity}）")
    say(f"action: {names}に通知（画面表示のみ・外部送信なし）")
    return "notify"


def decide_action(conn: sqlite3.Connection, f: Finding) -> str:
    if f.confidence < config.DETECT_REVIEW_THRESHOLD:
        return save_only(conn, f)                       # 記録のみ
    if f.confidence < config.DETECT_NOTIFY_THRESHOLD:
        return enqueue_for_review(conn, f)              # 人間の確認へ
    d, c = decision_actor(conn, f), change_actor(conn, f)
    if f.severity == "high" and d and c and d != c:
        return notify(conn, f, to=[d, c])               # 決定者と変更者の両方へ
    return notify(conn, f, to=[c])


# ---------- 各ステージ ----------

def stage_fetch(conn, stats: TickResult, events_override: list[Event] | None = None) -> None:
    if events_override is not None:
        db.save_events(conn, events_override)
        stats.fetched += len(events_override)
        return
    for adapter in adapters(conn):
        try:
            events = adapter.fetch(db.last_synced(conn, adapter.name))
            db.save_events(conn, events)
            if events:
                db.update_sync_state(conn, adapter.name, max(e.occurred_at for e in events))
                say(f"{adapter.name}: {len(events)}件の出来事を取込")
            stats.fetched += len(events)
        except Exception as e:                           # 1つ落ちても他は続行
            log.warning("%s skipped: %s", adapter.name, e)
            say(f"{adapter.name}: 取込失敗のためスキップ（{e}）")


def stage_extract(conn, stats: TickResult) -> None:
    # 会議: meeting_id 単位
    rows = conn.execute(
        "SELECT DISTINCT json_extract(meta, '$.meeting_id') mid FROM events "
        "WHERE source='meet' AND kind='utterance'").fetchall()
    for r in rows:
        mid = r["mid"]
        if not mid or db.is_processed(conn, "extract", f"meet:{mid}"):
            continue
        first = conn.execute(
            "SELECT * FROM events WHERE source='meet' AND kind='utterance' AND json_extract(meta, '$.meeting_id')=? "
            "ORDER BY occurred_at, json_extract(meta, '$.line') LIMIT 1", (mid,)).fetchone()
        ev = db.row_to_event(first)
        transcript = ev.meta.get("transcript")
        if not transcript:
            utts = conn.execute(
                "SELECT * FROM events WHERE source='meet' AND kind='utterance' AND json_extract(meta, '$.meeting_id')=? "
                "ORDER BY json_extract(meta, '$.line')", (mid,)).fetchall()
            transcript = "\n".join(f"[{db.from_iso(u['occurred_at']):%H:%M}] {u['actor']}: {u['text']}" for u in utts)
        base = db.from_iso(ev.meta.get("meeting_at")) or ev.occurred_at
        before = dict(extract.stats)
        events = extract.extract_from_transcript(transcript, mid, base, source="meet",
                                                 meta={"meeting_title": ev.meta.get("meeting_title")})
        n, created = extract.save_extracted(conn, events)
        rejected = extract.stats["rejected_quote"] - before["rejected_quote"]
        say(f"extract: {ev.meta.get('meeting_title', mid)}（{base:%m/%d}）から "
            f"決定{sum(1 for e in events if e.kind == 'decision')}件・タスク候補{sum(1 for e in events if e.kind == 'task_hint')}件"
            f"、Task自動生成{created}件、幻覚として破棄{rejected}件")
        stats.extracted += n
        stats.tasks_created += created
        db.mark_processed(conn, "extract", f"meet:{mid}", {"events": n, "tasks": created})

    # チャット: 未処理発言をチャンネルごとにまとめて
    rows = conn.execute(
        "SELECT e.* FROM events e LEFT JOIN processed p ON p.stage='extract' AND p.key = e.id "
        "WHERE e.source='chat' AND e.kind='utterance' AND p.key IS NULL ORDER BY json_extract(e.meta,'$.channel'), e.occurred_at").fetchall()
    msgs = [db.row_to_event(r) for r in rows]
    if msgs:
        by_ch: dict[str, list[Event]] = {}
        for m in msgs:
            by_ch.setdefault(m.meta.get("channel", "general"), []).append(m)
        for ch, batch in by_ch.items():
            events = extract.extract_from_chat(batch)
            n, created = extract.save_extracted(conn, events)
            if n:
                say(f"extract: #{ch} の発言{len(batch)}件から決定{n}件（Task自動生成{created}件）")
            stats.extracted += n
            stats.tasks_created += created
            for m in batch:
                db.mark_processed(conn, "extract", m.id)


def stage_embed(conn, stats: TickResult) -> None:
    n = vec.embed_missing(conn)
    stats.embedded += n
    if n:
        say(f"embed: {n}件の埋め込みを生成")


def stage_link(conn, stats: TickResult) -> None:
    counts: dict[str, int] = {}
    for msg in linker.unlinked_chat_messages(conn):
        task_id, conf, method = linker.link_and_save(conn, msg)
        linker.mark_linked(conn, msg, method, task_id)
        key = method if task_id else "none"
        counts[key] = counts.get(key, 0) + 1
        if task_id:
            stats.linked += 1
    if counts:
        detail = " / ".join(f"{k} {v}" for k, v in counts.items())
        say(f"link: 発言{sum(counts.values())}件を判定（{detail}）")


def stage_detect(conn, stats: TickResult, now: datetime | None = None) -> None:
    rows = conn.execute(
        "SELECT e.* FROM events e LEFT JOIN processed p ON p.stage='detect' AND p.key = e.id "
        "WHERE e.kind='artifact_change' AND p.key IS NULL ORDER BY e.occurred_at").fetchall()
    for r in rows:
        ch = db.row_to_event(r)
        if not db.is_processed(conn, "link", ch.id):
            tid = linker.link_change_and_save(conn, ch)
            db.mark_processed(conn, "link", ch.id, {"task_id": tid, "kind": "artifact_change"})
            if tid:
                t = db.get_task(conn, tid)
                say(f"link: 変更 {ch.ref} → タスク「{t.title if t else tid}」")
        say(f"detect: {ch.ref} の変更を判定中...")
        try:
            f = detector.detect_for_change(conn, ch)
        except Exception as e:                       # 次回の巡回で再試行する（処理済みにしない）
            log.warning("detect failed for %s: %s", ch.id, e)
            say(f"detect: 判定失敗のためスキップ（{e}）")
            continue
        if f:
            stats.findings.append(f)
            stats.actions[decide_action(conn, f)] += 1
        db.mark_processed(conn, "detect", ch.id, {"finding": f.id if f else None})

    for f in detector.detect_stalled(conn, now=now):
        stats.findings.append(f)
        stats.actions[decide_action(conn, f)] += 1


# ---------- ループ本体 ----------

def tick(conn: sqlite3.Connection, now: datetime | None = None,
         events_override: list[Event] | None = None) -> TickResult:
    router.bind(conn)
    stats = TickResult()
    stage_fetch(conn, stats, events_override)     # 1. 取込
    stage_extract(conn, stats)                     # 2. 抽出（新規 utterance のみ）
    stage_embed(conn, stats)                       # 3. 埋め込み（未生成のみ）
    stage_link(conn, stats)                        # 4. 紐付け（新規 utterance のみ）
    stage_detect(conn, stats, now)                 # 5-6. 検知（新規 artifact_change＋定期）
    return stats


def watch(conn: sqlite3.Connection, interval_sec: int = 300) -> None:
    say(f"watch: {interval_sec}秒間隔で巡回します（Ctrl+C で停止）")
    while True:
        tick(conn)
        time.sleep(interval_sec)


def replay(conn: sqlite3.Connection, speed: float = 1.0, record: bool = False) -> list[TickResult]:
    """fixtures の Event を occurred_at 順（日単位）に投入し、検知までを時系列に沿って再現する。
    LLM 呼び出しは記録済みレスポンス（fixtures/llm_cache.json）だけを使う。
    record=True のときは API を呼んでキャッシュを作る（提出前に1回実行しておく）。"""
    router.CACHE_ONLY = not record
    router.bind(conn)
    all_events: list[Event] = []
    for adapter in adapters(conn):
        try:
            all_events += adapter.fetch(None)
        except Exception as e:
            log.warning("%s skipped: %s", adapter.name, e)
    all_events.sort(key=lambda e: e.occurred_at)
    days = sorted({e.occurred_at.date() for e in all_events})
    results = []
    for d in days:
        batch = [e for e in all_events if e.occurred_at.date() == d]
        say(f"=== {d:%Y-%m-%d}: 出来事{len(batch)}件 ===")
        now = max(e.occurred_at for e in batch)
        results.append(tick(conn, now=now, events_override=batch))
        if speed > 0:
            time.sleep(min(1.0 / speed, 3.0))
    # 最終日の翌日の定期巡回（stalled 検知）も再現する
    from datetime import timedelta
    final = max(e.occurred_at for e in all_events) + timedelta(days=1)
    say(f"=== {final:%Y-%m-%d}: 定期巡回 ===")
    results.append(tick(conn, now=final, events_override=[]))
    return results
