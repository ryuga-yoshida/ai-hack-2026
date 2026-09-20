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
from app.connectors.docs import DocsAdapter
from app.connectors.excel import ExcelAdapter
from app.connectors.mail import MailAdapter
from app.connectors.meet import MeetAdapter
from app.llm import router
from app.models import Event, Finding

log = logging.getLogger("agent")


_log_conn: sqlite3.Connection | None = None


def say(msg: str, at: datetime | None = None) -> None:
    """1行ログ。標準出力と agent_logs（UI のリアルタイム表示）の両方に出す"""
    print(f"[{(at or datetime.now()):%H:%M:%S}] {msg}", flush=True)
    if _log_conn is not None:
        level = ("finding" if msg.startswith("FINDING") else "action" if msg.startswith(("action", "review", "record"))
                 else "warn" if "失敗" in msg or "スキップ" in msg else "info")
        try:
            db.add_log(_log_conn, msg, level)
        except Exception:
            pass


# ---------- バックグラウンド巡回（UI から ON/OFF） ----------

import threading

state = {"enabled": False, "interval": 60, "running": False, "last_tick": None, "ticks": 0, "last_result": None,
         "last_trigger": None}
_thread: threading.Thread | None = None
_stop = threading.Event()
_wake = threading.Event()        # イベント駆動の起床要求
_pending_reasons: list[str] = []
_file_sig: dict[str, float] = {}


def request_tick(reason: str) -> bool:
    """感度の高いイベント（会議終了・チャット投稿・成果物の新版）から即時巡回を要求する。
    自動巡回が OFF のときは何もしない（ポーリングもトリガーも止まっている状態）"""
    if not state["enabled"]:
        return False
    _pending_reasons.append(reason)
    _wake.set()
    return True


def _excel_signature() -> dict[str, float]:
    d = config.FIXTURES_DIR / "excel"
    return {str(p.relative_to(d)): p.stat().st_mtime for p in d.rglob("*")
            if p.is_file() and p.name != "versions.json" and not p.name.startswith(".")} if d.exists() else {}


_watch_sig: dict[str, float] = {}
_watch_seen_at: dict[str, float] = {}


def _poll_watch_dir() -> list[str]:
    """監視フォルダ（OneDrive / デスクトップ）で保存されたファイルを新しい版として取り込む。
    Excel は保存中に何度か書き込むため、更新から 2 秒静止してから取り込む"""
    global _watch_sig
    from pathlib import Path
    from app.connectors.excel import snapshot_new_version, watch_signature
    if not config.ARTIFACT_WATCH_DIR:
        return []
    d = Path(config.ARTIFACT_WATCH_DIR).expanduser()
    sig = watch_signature(d)
    out = []
    now = time.time()
    for name, mtime in sig.items():
        if _watch_sig.get(name) == mtime:
            continue
        if now - mtime < 2:
            continue   # まだ書き込み中かもしれない
        _watch_sig[name] = mtime
        try:
            rel_dir = str(Path(name).parent) if str(Path(name).parent) != "." else ""
            dest = snapshot_new_version(d / name, config.ARTIFACT_WATCH_ACTOR, rel_dir=rel_dir)
        except Exception as e:
            log.warning("watch snapshot failed %s: %s", name, e)
            continue
        if dest:
            out.append(f"{name} を保存（{config.ARTIFACT_WATCH_ACTOR}）→ {dest.name}")
    for name in list(_watch_sig):
        if name not in sig:
            _watch_sig.pop(name)
    return out


def _loop():
    global _file_sig, _watch_sig
    _file_sig = _excel_signature()
    # 起動時は監視フォルダの現状を「既知」として記録するだけ（古いコピーを新版として取り込まない）
    if config.ARTIFACT_WATCH_DIR:
        from pathlib import Path
        from app.connectors.excel import watch_signature
        _watch_sig = watch_signature(Path(config.ARTIFACT_WATCH_DIR).expanduser())
    while not _stop.is_set():
        if state["enabled"]:
            reasons = []
            # OneDrive / デスクトップの監視フォルダで Excel が保存されたら版としてスナップショット
            reasons += ["Excel を保存: " + r for r in _poll_watch_dir()]
            # 成果物の新版（SharePoint 代替のフォルダ）をファイル監視で検出
            sig = _excel_signature()
            if sig != _file_sig:
                added = sorted(set(sig) - set(_file_sig)) or sorted(k for k in sig if sig[k] != _file_sig.get(k))
                _file_sig = sig
                reasons.append("成果物の新版: " + "・".join(added[:3]))
            if _pending_reasons:
                reasons += _pending_reasons[:]
                _pending_reasons.clear()
            _wake.clear()
            conn = db.connect()
            db.init_db(conn)
            try:
                state["running"] = True
                if reasons:
                    state["last_trigger"] = reasons[-1]
                    _log_conn_set(conn)
                    say("trigger: " + " / ".join(dict.fromkeys(reasons)) + " → 即時巡回")
                r = tick(conn, trigger="event" if reasons else "auto")
                state["ticks"] += 1
                state["last_tick"] = datetime.now().replace(microsecond=0).isoformat()
                state["last_result"] = {"fetched": r.fetched, "extracted": r.extracted, "linked": r.linked,
                                        "findings": len(r.findings), "actions": r.actions}
            except Exception as e:
                log.exception("background tick failed")
                say(f"tick 失敗: {e}")
            finally:
                state["running"] = False
                conn.close()
        # 次の定期巡回まで待つ。ただしイベントが来たら即起きる。ファイル監視のため 3 秒刻みで見る
        waited = 0
        while waited < state["interval"] and not _stop.is_set():
            if _wake.wait(3):
                time.sleep(1.5)     # 連続イベントをまとめる（デバウンス）
                break
            waited += 3
            if state["enabled"] and (_excel_signature() != _file_sig or
                                     (config.ARTIFACT_WATCH_DIR and _watch_changed())):
                break


def _watch_changed() -> bool:
    from pathlib import Path
    from app.connectors.excel import watch_signature
    sig = watch_signature(Path(config.ARTIFACT_WATCH_DIR).expanduser())
    now = time.time()
    return any(_watch_sig.get(n) != m and now - m >= 2 for n, m in sig.items())


def _log_conn_set(conn):
    global _log_conn
    _log_conn = conn


def set_enabled(enabled: bool, interval: int | None = None) -> dict:
    global _thread
    state["enabled"] = enabled
    if interval:
        state["interval"] = max(10, int(interval))
    if enabled and (_thread is None or not _thread.is_alive()):
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="agent-loop")
        _thread.start()
    if enabled:
        say(f"自動巡回を開始（{state['interval']}秒間隔）")
    else:
        say("自動巡回を停止")
    return dict(state)


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
    return [MeetAdapter(conn=conn), ChatAdapter(conn), MailAdapter(conn), ExcelAdapter(), DocsAdapter()]


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

    # チャット・メール: 未処理発言をチャンネル（メールはスレッド）ごとにまとめて
    rows = conn.execute(
        "SELECT e.* FROM events e LEFT JOIN processed p ON p.stage='extract' AND p.key = e.id "
        "WHERE e.source IN ('chat','mail') AND e.kind='utterance' AND p.key IS NULL ORDER BY json_extract(e.meta,'$.channel'), e.occurred_at").fetchall()
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

    for f, payload in detector.suggest_status_updates(conn):
        db.save_finding(conn, f, payload=payload)
        stats.findings.append(f)
        stats.actions["review"] += 1
        say(f"suggest: {f.summary}（{f.reason[:40]}…）")


def stage_calendar(conn, stats: TickResult, now: datetime | None = None) -> None:
    """開始 15 分前までの予定に対して、会議室を用意してチャットにリマインドする（人の指示なし）"""
    from datetime import timedelta
    from app.connectors import calendar, chat, meet
    now = now or datetime.now()
    for ev in calendar.upcoming_unreminded(conn, now, timedelta(minutes=15)):
        mid = ev.get("meeting_id")
        if not mid or not conn.execute("SELECT 1 FROM meetings WHERE id=?", (mid,)).fetchone():
            mid = meet.create_meeting(conn, ev["title"], started_at=ev["start"], channel=ev.get("channel"))
            calendar.update(conn, ev["id"], meeting_id=mid)
        for a in ev["attendees"]:
            meet.join(conn, mid, a)
        mins = max(1, int((ev["start"] - now).total_seconds() // 60))
        url = f"{config.APP_BASE_URL}/meet/{mid}"
        text = (f"⏰ {mins}分後に「{ev['title']}」です（{ev['start']:%H:%M}〜、{ev.get('location') or 'オンライン'}）。"
                f"会議室を用意しました → {url}  参加: {'・'.join(ev['attendees'])}")
        chat.post_message(conn, ev.get("channel") or "general", "エージェント", text)
        calendar.update(conn, ev["id"], reminded_at=db.now_iso())
        say(f"calendar: 「{ev['title']}」の {mins} 分前。会議室 {mid} を用意して #{ev.get('channel') or 'general'} にリマインド")


# ---------- ループ本体 ----------

def tick(conn: sqlite3.Connection, now: datetime | None = None,
         events_override: list[Event] | None = None, trigger: str = "auto") -> TickResult:
    global _log_conn
    router.bind(conn)
    _log_conn = conn
    stats = TickResult()
    label = {"auto": "自動", "manual": "手動", "event": "トリガー", "replay": "再生"}.get(trigger, trigger)
    say(f"巡回開始（{label}）" if events_override is None else f"再生: {now:%Y-%m-%d} の出来事を投入")
    stage_fetch(conn, stats, events_override)     # 1. 取込
    stage_extract(conn, stats)                     # 2. 抽出（新規 utterance のみ）
    stage_embed(conn, stats)                       # 3. 埋め込み（未生成のみ）
    stage_link(conn, stats)                        # 4. 紐付け（新規 utterance のみ）
    stage_detect(conn, stats, now)                 # 5-6. 検知（新規 artifact_change＋定期）
    if events_override is None:
        stage_calendar(conn, stats, now)           # 7. 予定のリマインド（再生時は行わない）
    say(f"巡回終了: 取込{stats.fetched} 抽出{stats.extracted} 紐付け{stats.linked} Finding{len(stats.findings)}")
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


def mirror_to_watch_dir(version_path) -> None:
    """ブラウザ編集・アップロードで作った新版を監視フォルダ（OneDrive 代替）の <base>.xlsx にも書き戻す。
    書き戻した分は「既知」にして再取り込みしない"""
    import re as _re
    import shutil
    from pathlib import Path
    if not config.ARTIFACT_WATCH_DIR:
        return
    d = Path(config.ARTIFACT_WATCH_DIR).expanduser()
    if not d.exists():
        return
    vp = Path(version_path)
    root = config.FIXTURES_DIR / "excel"
    rel_dir = vp.parent.relative_to(root) if vp.is_relative_to(root) else Path(".")
    base = _re.sub(r"_v\d+(?=\.[A-Za-z0-9]+$)", "", vp.name)
    dest = d / rel_dir / base
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(version_path, dest)
    _watch_sig[str(dest.relative_to(d))] = dest.stat().st_mtime


def autonomy_stats(conn: sqlite3.Connection) -> dict:
    """自律性の数値化（デモ・ダッシュボード用）"""
    changes = conn.execute("SELECT COUNT(*) FROM processed WHERE stage='detect'").fetchone()[0]
    with_finding = conn.execute("SELECT COUNT(*) FROM processed WHERE stage='detect' AND json_extract(result,'$.finding') IS NOT NULL").fetchone()[0]
    notified = conn.execute("SELECT COUNT(*) FROM findings WHERE status IN ('notified','acknowledged') AND kind != 'status_suggestion'").fetchone()[0]
    review = conn.execute("SELECT COUNT(*) FROM findings WHERE status='pending' AND kind != 'status_suggestion'").fetchone()[0]
    dismissed = conn.execute("SELECT COUNT(*) FROM findings WHERE status='dismissed'").fetchone()[0]
    suggestions = conn.execute("SELECT COUNT(*) FROM findings WHERE kind='status_suggestion'").fetchone()[0]
    applied = conn.execute("SELECT COUNT(*) FROM findings WHERE kind='status_suggestion' AND status='acknowledged'").fetchone()[0]
    tasks_auto = conn.execute("SELECT COUNT(*) FROM tasks WHERE created_from IS NOT NULL").fetchone()[0]
    tasks_manual = conn.execute("SELECT COUNT(*) FROM tasks WHERE created_from IS NULL").fetchone()[0]
    links = conn.execute("SELECT result FROM processed WHERE stage='link'").fetchall()
    link_auto = sum(1 for r in links if r["result"] and '"task_id": "' in r["result"] and '"manual"' not in r["result"])
    link_manual = sum(1 for r in links if r["result"] and '"manual"' in r["result"])
    ticks_auto = conn.execute("SELECT COUNT(*) FROM agent_logs WHERE message IN ('巡回開始（自動）','巡回開始（トリガー）')").fetchone()[0]
    ticks_manual = conn.execute("SELECT COUNT(*) FROM agent_logs WHERE message='巡回開始（手動）'").fetchone()[0]
    reminders = conn.execute("SELECT COUNT(*) FROM cal_events WHERE reminded_at IS NOT NULL").fetchone()[0]
    return {"changes": changes, "ignored": changes - with_finding, "notified": notified, "review": review,
            "dismissed": dismissed, "suggestions": suggestions, "applied": applied,
            "tasks_auto": tasks_auto, "tasks_manual": tasks_manual, "link_auto": link_auto, "link_manual": link_manual,
            "ticks_auto": ticks_auto, "ticks_manual": ticks_manual, "reminders": reminders,
            "auto_rate": round(100 * (changes - with_finding + notified) / changes) if changes else 0}
