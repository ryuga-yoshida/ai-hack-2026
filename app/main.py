"""FastAPI エントリポイント。Jinja2 + Tailwind CDN + Alpine.js の UI。"""
import json
import re
import sqlite3
from datetime import date, datetime
from html import escape
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from app import config, db
from app.connectors import chat, meet
from app.models import Event, Link, Task, new_id

app = FastAPI(title="進行管理エージェント")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))

STATUSES = [("todo", "未着手"), ("in_progress", "進行中"), ("blocked", "ブロック"), ("done", "完了")]
STATUS_LABEL = dict(STATUSES)
KIND_LABEL = {"contradiction": "矛盾", "stalled": "停滞", "orphan_change": "根拠なし変更"}
EVENT_LABEL = {"decision": "決定", "task_hint": "タスク候補", "utterance": "発言", "artifact_change": "変更"}
FINDING_STATUS_LABEL = {"notified": "通知済", "pending": "確認待ち", "acknowledged": "確認済", "dismissed": "却下"}
PEOPLE = config.PERSONS


from urllib.parse import quote, unquote


def get_me(request: Request) -> str:
    return unquote(request.cookies.get("me", ""))


def set_me(resp, name: str):
    resp.set_cookie("me", quote(name.strip()), max_age=86400 * 30)
    return resp


def get_conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def linkify(text: str) -> Markup:
    out, pos = [], 0
    for m in re.finditer(r"https?://[^\s<>\"']+", text or ""):
        out.append(escape(text[pos:m.start()]))
        url = m.group(0)
        label = url if len(url) < 60 else url[:57] + "…"
        out.append(f'<a href="{escape(url)}" class="text-indigo-600 hover:underline break-all" target="_blank">{escape(label)}</a>')
        pos = m.end()
    out.append(escape((text or "")[pos:]))
    return Markup("".join(out))


def initials(name: str | None) -> str:
    return (name or "?")[:1]


def fmt_dt(v, with_date=True) -> str:
    if not v:
        return ""
    if isinstance(v, str):
        v = db.from_iso(v)
    return v.strftime("%m/%d %H:%M") if with_date else v.strftime("%H:%M")


templates.env.filters["linkify"] = linkify
templates.env.filters["initials"] = initials
templates.env.filters["dt"] = fmt_dt
templates.env.globals.update(STATUSES=STATUSES, STATUS_LABEL=STATUS_LABEL, KIND_LABEL=KIND_LABEL,
                             EVENT_LABEL=EVENT_LABEL, FINDING_STATUS_LABEL=FINDING_STATUS_LABEL, PEOPLE=PEOPLE)


def nav_context(conn: sqlite3.Connection) -> dict:
    from app import agent
    open_findings = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE status IN ('notified','pending')").fetchone()[0]
    return {"nav_open_findings": open_findings, "agent_state": dict(agent.state)}


def render(name: str, request: Request, conn: sqlite3.Connection | None = None, **ctx) -> HTMLResponse:
    if conn is not None:
        ctx.update(nav_context(conn))
    ctx.setdefault("active", name.split(".")[0])
    return templates.TemplateResponse(request, name, ctx)


# ---------- 共通: Finding カード ----------

def finding_cards(conn: sqlite3.Connection, where: str = "", params: tuple = (), limit: int | None = None) -> list[dict]:
    sql = (f"SELECT * FROM findings {where} ORDER BY "
           "CASE status WHEN 'notified' THEN 0 WHEN 'pending' THEN 1 WHEN 'acknowledged' THEN 2 ELSE 3 END, "
           "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
           "CASE kind WHEN 'contradiction' THEN 0 WHEN 'orphan_change' THEN 1 ELSE 2 END, "
           "confidence DESC, created_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    cards = []
    for r in conn.execute(sql, params).fetchall():
        f = db.row_to_finding(r)
        evidence = [e for e in (db.get_event(conn, i) for i in f.evidence) if e]
        evidence.sort(key=lambda e: e.occurred_at)
        cards.append({"finding": f, "evidence": evidence, "dismiss_note": r["dismiss_note"],
                      "created_at": r["created_at"],
                      "task": db.get_task(conn, f.task_id) if f.task_id else None})
    return cards


# ---------- ダッシュボード ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = get_conn()
    counts = {k: 0 for k in FINDING_STATUS_LABEL}
    for r in conn.execute("SELECT status, COUNT(*) n FROM findings GROUP BY status"):
        counts[r["status"]] = r["n"]
    kinds = {k: 0 for k in KIND_LABEL}
    for r in conn.execute("SELECT kind, COUNT(*) n FROM findings WHERE status IN ('notified','pending') GROUP BY kind"):
        kinds[r["kind"]] = r["n"]
    task_counts = {s: 0 for s, _ in STATUSES}
    for r in conn.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status"):
        task_counts[r["status"]] = r["n"]
    events_n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    sources = {r["source"]: r["n"] for r in conn.execute("SELECT source, COUNT(*) n FROM events GROUP BY source")}
    from app.llm import cost
    return render("index.html", request, conn,
                  cards=finding_cards(conn, "WHERE status IN ('notified','pending')", limit=6),
                  counts=counts, kinds=kinds, task_counts=task_counts, events_n=events_n, sources=sources,
                  cost=cost.summary(conn), logs=db.recent_logs(conn, limit=12))


# ---------- 検知結果 ----------

@app.get("/findings", response_class=HTMLResponse)
def findings_page(request: Request, status: str = "open", kind: str = ""):
    conn = get_conn()
    cond, params = [], []
    if status == "open":
        cond.append("status IN ('notified','pending')")
    elif status in FINDING_STATUS_LABEL:
        cond.append("status = ?"); params.append(status)
    if kind in KIND_LABEL:
        cond.append("kind = ?"); params.append(kind)
    where = ("WHERE " + " AND ".join(cond)) if cond else ""
    counts = {"all": conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0],
              "open": conn.execute("SELECT COUNT(*) FROM findings WHERE status IN ('notified','pending')").fetchone()[0]}
    for k in FINDING_STATUS_LABEL:
        counts[k] = conn.execute("SELECT COUNT(*) FROM findings WHERE status=?", (k,)).fetchone()[0]
    return render("findings.html", request, conn, cards=finding_cards(conn, where, tuple(params)),
                  status=status, kind=kind, counts=counts)


@app.post("/findings/{finding_id}/ack")
def ack_finding(finding_id: str, request: Request):
    conn = get_conn()
    conn.execute("UPDATE findings SET status='acknowledged' WHERE id=?", (finding_id,))
    conn.commit()
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.post("/findings/{finding_id}/dismiss")
def dismiss_finding(finding_id: str, request: Request, note: str = Form("")):
    conn = get_conn()
    conn.execute("UPDATE findings SET status='dismissed', dismiss_note=? WHERE id=?",
                 (note.strip() or None, finding_id))
    conn.commit()
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.post("/findings/{finding_id}/reopen")
def reopen_finding(finding_id: str, request: Request):
    conn = get_conn()
    conn.execute("UPDATE findings SET status='pending', dismiss_note=NULL WHERE id=?", (finding_id,))
    conn.commit()
    return RedirectResponse(request.headers.get("referer") or "/findings", status_code=303)


# ---------- タスク ----------

def task_meta(conn: sqlite3.Connection, t: Task) -> dict:
    n_msgs = conn.execute(
        "SELECT COUNT(*) FROM links WHERE to_type='task' AND to_id=? AND relation='discusses'", (t.id,)).fetchone()[0]
    n_findings = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE task_id=? AND status IN ('notified','pending')", (t.id,)).fetchone()[0]
    origin = db.get_event(conn, t.created_from) if t.created_from else None
    return {"task": t, "n_msgs": n_msgs, "n_findings": n_findings,
            "origin": origin, "overdue": bool(t.due_date and t.due_date < date.today() and t.status != "done")}


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, assignee: str = "", q: str = ""):
    conn = get_conn()
    tasks = db.list_tasks(conn)
    if assignee:
        tasks = [t for t in tasks if (t.assignee or "") == assignee]
    if q:
        tasks = [t for t in tasks if q in t.title or q in (t.description or "")]
    by_status = {s: [task_meta(conn, t) for t in tasks if t.status == s] for s, _ in STATUSES}
    assignees = sorted({t.assignee for t in db.list_tasks(conn) if t.assignee})
    return render("tasks.html", request, conn, by_status=by_status, assignees=assignees,
                  assignee=assignee, q=q)


@app.post("/tasks")
def create_task(title: str = Form(...), assignee: str = Form(""), due_date: str = Form(""),
                artifacts: str = Form(""), description: str = Form(""), status: str = Form("todo")):
    conn = get_conn()
    task = Task(
        id=new_id(), title=title.strip(), assignee=assignee.strip() or None,
        description=description.strip() or None, status=status if status in STATUS_LABEL else "todo",
        due_date=date.fromisoformat(due_date) if due_date else None,
        artifacts=[a.strip() for a in artifacts.split(",") if a.strip()],
    )
    db.save_task(conn, task)
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


LABELS = {"decision": ("決定", "bg-rose-500"), "task_hint": ("タスク候補", "bg-gray-400"),
          "utterance": ("発言", "bg-sky-500"), "artifact_change": ("変更", "bg-amber-500")}


def task_timeline(conn: sqlite3.Connection, task: Task) -> list[dict]:
    items, seen = [], set()

    def add(ev, label=None, color=None):
        if ev.id in seen:
            return
        seen.add(ev.id)
        lb, cl = LABELS.get(ev.kind, (ev.kind, "bg-gray-400"))
        items.append({"when": ev.occurred_at, "label": label or lb, "color": color or cl,
                      "text": ev.text, "actor": ev.actor, "ref": ev.ref, "quote": ev.quote,
                      "meta": ev.meta, "kind": ev.kind})

    rows = conn.execute(
        "SELECT e.*, l.relation FROM links l JOIN events e ON e.id = l.from_id "
        "WHERE l.to_type='task' AND l.to_id=? AND l.from_type='event'", (task.id,)).fetchall()
    for r in rows:
        add(db.row_to_event(r))
    created = conn.execute("SELECT created_at FROM tasks WHERE id=?", (task.id,)).fetchone()["created_at"]
    items.append({"when": db.from_iso(created), "label": "タスク生成", "color": "bg-emerald-500",
                  "text": task.title, "actor": task.assignee, "ref": None, "quote": None, "meta": {}, "kind": "task"})
    for r in conn.execute("SELECT * FROM findings WHERE task_id=?", (task.id,)).fetchall():
        f = db.row_to_finding(r)
        evs = [e for e in (db.get_event(conn, i) for i in f.evidence) if e]
        for ev in evs:
            add(ev)
        when = max((e.occurred_at for e in evs), default=db.from_iso(r["created_at"])) if f.kind != "stalled" else db.from_iso(r["created_at"])
        items.append({"when": when, "label": "検知", "color": "bg-red-600", "text": f.summary,
                      "actor": None, "ref": None, "quote": None, "meta": {"finding_id": f.id, "status": f.status}, "kind": "finding"})
    items.sort(key=lambda x: x["when"])
    return items


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: str):
    conn = get_conn()
    task = db.get_task(conn, task_id)
    if not task:
        raise HTTPException(404)
    return render("task_detail.html", request, conn, task=task, meta=task_meta(conn, task),
                  timeline=task_timeline(conn, task), me=get_me(request))


class TaskPatch(BaseModel):
    status: str | None = None
    assignee: str | None = None
    title: str | None = None
    description: str | None = None
    due_date: date | None = None
    artifacts: list[str] | None = None


@app.patch("/tasks/{task_id}")
def patch_task(task_id: str, patch: TaskPatch):
    conn = get_conn()
    task = db.get_task(conn, task_id)
    if not task:
        raise HTTPException(404)
    if patch.status is not None and patch.status not in STATUS_LABEL:
        raise HTTPException(422, "invalid status")
    for k, v in patch.model_dump(exclude_unset=True).items():
        if k in ("assignee", "description", "title") and isinstance(v, str):
            v = v.strip() or None
        setattr(task, k, v)
    db.save_task(conn, task)   # updated_at を更新（stalled 検知の起点）
    return {"ok": True, "task": task}


@app.post("/tasks/{task_id}/comment")
def task_comment(task_id: str, request: Request, actor: str = Form(...), text: str = Form(...)):
    """タスクへのコメント。チャットの #tasks チャンネルに投稿し、明示的にこのタスクへ紐付ける"""
    conn = get_conn()
    task = db.get_task(conn, task_id)
    if not task:
        raise HTTPException(404)
    mid = chat.post_message(conn, "tasks", actor.strip(), text.strip())
    _ingest_chat(conn)
    db.save_link(conn, Link(id=new_id(), from_type="event", from_id=mid, to_type="task", to_id=task_id,
                            relation="discusses", confidence=1.0, method="explicit"))
    db.mark_processed(conn, "link", mid, {"method": "explicit", "task_id": task_id})
    db.save_task(conn, task)   # 動きがあったので updated_at を更新
    resp = RedirectResponse(f"/tasks/{task_id}", status_code=303)
    set_me(resp, actor)
    return resp


# ---------- チャット ----------

_MENTION = re.compile(r"@(" + "|".join(re.escape(p) for p in PEOPLE) + r"|all|channel)")


def render_message(text: str) -> str:
    html = str(linkify(text))
    return _MENTION.sub(r'<span class="bg-indigo-100 text-indigo-800 rounded px-1 font-medium">@\1</span>', html)


def _message_dict(conn: sqlite3.Connection, r: sqlite3.Row) -> dict:
    link = conn.execute(
        "SELECT l.to_id, l.method, l.confidence, t.title FROM links l JOIN tasks t ON t.id = l.to_id "
        "WHERE l.from_type='event' AND l.from_id=? AND l.to_type='task' AND l.relation='discusses' LIMIT 1",
        (r["id"],)).fetchone()
    decided = conn.execute(
        "SELECT text FROM events WHERE kind='decision' AND source='chat' AND json_extract(meta,'$.message_id')=?",
        (r["id"],)).fetchone()
    reactions: dict[str, list[str]] = {}
    for x in conn.execute("SELECT emoji, actor FROM chat_reactions WHERE message_id=? ORDER BY rowid", (r["id"],)):
        reactions.setdefault(x["emoji"], []).append(x["actor"])
    replies = conn.execute("SELECT COUNT(*) FROM chat_messages WHERE reply_to=? AND deleted=0", (r["id"],)).fetchone()[0]
    last_reply = conn.execute("SELECT posted_at FROM chat_messages WHERE reply_to=? AND deleted=0 ORDER BY posted_at DESC LIMIT 1", (r["id"],)).fetchone()
    return {"id": r["id"], "channel": r["channel"], "actor": r["actor"], "text": r["text"], "posted_at": r["posted_at"],
            "html": render_message(r["text"]), "reply_to": r["reply_to"], "edited": bool(r["edited_at"]),
            "attachments": json.loads(r["attachments"]) if r["attachments"] else [],
            "reactions": reactions, "replies": replies, "last_reply": last_reply["posted_at"] if last_reply else None,
            "task": {"id": link["to_id"], "title": link["title"], "method": link["method"],
                     "confidence": link["confidence"]} if link else None,
            "decision": decided["text"] if decided else None}


def message_rows(conn: sqlite3.Connection, channel: str, after: str | None = None) -> list[dict]:
    if after:
        rows = conn.execute("SELECT * FROM chat_messages WHERE channel=? AND deleted=0 AND reply_to IS NULL AND posted_at > ? "
                            "ORDER BY posted_at, rowid", (channel, after)).fetchall()
    else:
        rows = chat.list_messages(conn, channel, include_replies=False)
    return [_message_dict(conn, r) for r in rows]


def thread_rows(conn: sqlite3.Connection, root_id: str) -> dict | None:
    root = conn.execute("SELECT * FROM chat_messages WHERE id=?", (root_id,)).fetchone()
    if not root:
        return None
    replies = conn.execute("SELECT * FROM chat_messages WHERE reply_to=? AND deleted=0 ORDER BY posted_at, rowid", (root_id,)).fetchall()
    return {"root": _message_dict(conn, root), "replies": [_message_dict(conn, r) for r in replies]}


def _ingest_chat(conn: sqlite3.Connection) -> None:
    """投稿を即座に Event 化する（巡回を待たない）。コストゼロの紐付け（明示・文脈・スレッド）もその場で行い、
    残り（埋め込み・LLM）は巡回に任せる"""
    from app import linker
    adapter = chat.ChatAdapter(conn)
    events = adapter.fetch(db.last_synced(conn, adapter.name))
    db.save_events(conn, events)
    db.update_sync_state(conn, adapter.name)
    for ev in events:
        r = linker.by_explicit(conn, ev) or linker.by_context(conn, ev)
        if r:
            method = "explicit" if linker.by_explicit(conn, ev) else "context"
            db.save_link(conn, Link(id=new_id(), from_type="event", from_id=ev.id, to_type="task", to_id=r[0],
                                    relation="discusses", confidence=r[1], method=method))
            linker.mark_linked(conn, ev, method, r[0])
            linker.register_artifacts(conn, r[0], ev)


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, channel: str = "general", thread: str = "", q: str = ""):
    conn = get_conn()
    channels = chat.list_channels(conn)
    if channel not in channels:
        channels.append(channel)
    counts = {r["channel"]: r["n"] for r in conn.execute("SELECT channel, COUNT(*) n FROM chat_messages WHERE deleted=0 GROUP BY channel")}
    descs = {r["name"]: r["description"] for r in conn.execute("SELECT name, description FROM chat_channels")}
    results = [_message_dict(conn, r) for r in chat.search_messages(conn, q)] if q else []
    return render("chat.html", request, conn, channel=channel, channels=channels, counts=counts, descs=descs,
                  messages=message_rows(conn, channel), thread=thread_rows(conn, thread) if thread else None,
                  q=q, results=results, me=get_me(request))


@app.get("/api/chat/messages")
def chat_messages_api(channel: str = "general", after: str = ""):
    conn = get_conn()
    return {"messages": message_rows(conn, channel, after or None)}


@app.get("/api/chat/thread/{root_id}")
def chat_thread_api(root_id: str):
    conn = get_conn()
    t = thread_rows(conn, root_id)
    if not t:
        raise HTTPException(404)
    return t


@app.post("/chat")
def post_chat(request: Request, channel: str = Form("general"), actor: str = Form(...), text: str = Form(...),
              reply_to: str = Form(""), attachment_url: str = Form(""), attachment_name: str = Form("")):
    conn = get_conn()
    attachments = None
    if attachment_url.strip():
        url = attachment_url.strip()
        name = attachment_name.strip() or url.rstrip("/").rsplit("/", 1)[-1]
        attachments = [{"url": url, "name": name}]
        if url not in text:
            text = f"{text.strip()} {url}".strip()   # URL を本文にも入れる（成果物の自動登録が効く）
    chat.post_message(conn, channel.strip() or "general", actor.strip(), text.strip(),
                      reply_to=reply_to.strip() or None, attachments=attachments)
    _ingest_chat(conn)
    url = f"/chat?channel={channel}" + (f"&thread={reply_to}" if reply_to else "")
    resp = RedirectResponse(url, status_code=303)
    set_me(resp, actor)
    return resp


@app.post("/chat/{msg_id}/edit")
def edit_chat(msg_id: str, request: Request, text: str = Form(...)):
    conn = get_conn()
    chat.edit_message(conn, msg_id, text.strip())
    return RedirectResponse(request.headers.get("referer") or "/chat", status_code=303)


@app.post("/chat/{msg_id}/delete")
def delete_chat(msg_id: str, request: Request):
    conn = get_conn()
    chat.delete_message(conn, msg_id)
    return RedirectResponse(request.headers.get("referer") or "/chat", status_code=303)


class ReactBody(BaseModel):
    emoji: str
    actor: str


@app.post("/api/chat/{msg_id}/react")
def react_chat(msg_id: str, body: ReactBody):
    conn = get_conn()
    added = chat.toggle_reaction(conn, msg_id, body.emoji[:8], body.actor.strip() or "匿名")
    reactions: dict[str, list[str]] = {}
    for x in conn.execute("SELECT emoji, actor FROM chat_reactions WHERE message_id=? ORDER BY rowid", (msg_id,)):
        reactions.setdefault(x["emoji"], []).append(x["actor"])
    return {"added": added, "reactions": reactions}


@app.post("/chat/channels")
def create_channel(name: str = Form(...), description: str = Form("")):
    conn = get_conn()
    name = re.sub(r"[^\w\-]", "", name.strip().lstrip("#"))[:32] or "general"
    chat.ensure_channel(conn, name, description.strip() or None)
    return RedirectResponse(f"/chat?channel={name}", status_code=303)


# ---------- 会議室 ----------

def meeting_rows(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM meetings ORDER BY started_at DESC").fetchall():
        ps = [p[0] for p in conn.execute(
            "SELECT name FROM meeting_participants WHERE meeting_id=? ORDER BY joined_at", (r["id"],))]
        n_dec = conn.execute("SELECT COUNT(*) FROM events WHERE kind='decision' AND json_extract(meta,'$.meeting_id')=?", (r["id"],)).fetchone()[0]
        out.append({**dict(r), "participants": ps, "n_decisions": n_dec,
                    "n_lines": (r["transcript"] or "").count("\n[") if r["transcript"] else 0})
    return out


def fixture_meetings(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT json_extract(meta,'$.meeting_id') mid, json_extract(meta,'$.meeting_title') title, "
        "MIN(occurred_at) at, COUNT(*) n FROM events WHERE source='meet' AND kind='utterance' "
        "GROUP BY mid ORDER BY at DESC").fetchall()
    out = []
    for r in rows:
        if conn.execute("SELECT 1 FROM meetings WHERE id=?", (r["mid"],)).fetchone():
            continue
        n_dec = conn.execute("SELECT COUNT(*) FROM events WHERE kind='decision' AND json_extract(meta,'$.meeting_id')=?", (r["mid"],)).fetchone()[0]
        out.append({"id": r["mid"], "title": r["title"], "started_at": r["at"], "n_lines": r["n"], "n_decisions": n_dec})
    return out


@app.get("/meet", response_class=HTMLResponse)
def meet_page(request: Request):
    conn = get_conn()
    return render("meet.html", request, conn, meetings=meeting_rows(conn), fixtures=fixture_meetings(conn),
                  me=get_me(request))


@app.post("/meet")
def create_meeting(title: str = Form("定例会議"), me: str = Form(...)):
    conn = get_conn()
    mid = meet.create_meeting(conn, title)
    meet.join(conn, mid, me.strip())
    resp = RedirectResponse(f"/meet/{mid}?me={me.strip()}", status_code=303)
    set_me(resp, me)
    return resp


def meeting_extracted(conn: sqlite3.Connection, meeting_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events WHERE kind IN ('decision','task_hint') AND json_extract(meta,'$.meeting_id')=? "
        "ORDER BY occurred_at", (meeting_id,)).fetchall()
    return [db.row_to_event(r) for r in rows]


@app.get("/meet/{meeting_id}", response_class=HTMLResponse)
def meet_room(request: Request, meeting_id: str, me: str = ""):
    conn = get_conn()
    m = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if not m:
        # fixtures / Drive 由来の会議は utterance から組み立てて表示する
        utts = conn.execute(
            "SELECT * FROM events WHERE source='meet' AND kind='utterance' AND json_extract(meta,'$.meeting_id')=? "
            "ORDER BY json_extract(meta,'$.line')", (meeting_id,)).fetchall()
        if not utts:
            raise HTTPException(404)
        first = db.row_to_event(utts[0])
        m = {"id": meeting_id, "title": first.meta.get("meeting_title", meeting_id),
             "started_at": first.meta.get("meeting_at") or first.occurred_at.isoformat(), "status": "done",
             "transcript": None, "error": None, "readonly": True}
        lines = [{"time": fmt_dt(db.from_iso(u["occurred_at"]), False), "actor": u["actor"], "text": u["text"]} for u in utts]
        participants = sorted({u["actor"] for u in utts})
    else:
        m = dict(m); m["readonly"] = False
        if me.strip():
            meet.join(conn, meeting_id, me.strip())
        participants = [p[0] for p in conn.execute(
            "SELECT name FROM meeting_participants WHERE meeting_id=? ORDER BY joined_at", (meeting_id,))]
        lines = []
        for raw in (m["transcript"] or "").splitlines():
            mm = re.match(r"^\[(\d{1,2}:\d{2})\]\s*([^:：]+)[:：]\s*(.*)$", raw)
            if mm:
                lines.append({"time": mm[1], "actor": mm[2].strip(), "text": mm[3]})
    return render("meet_room.html", request, conn, m=m, me=me.strip() or get_me(request),
                  participants=participants, lines=lines, extracted=meeting_extracted(conn, meeting_id))


@app.get("/meet/{meeting_id}/status")
def meet_status(meeting_id: str):
    conn = get_conn()
    m = conn.execute("SELECT status, error FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if not m:
        raise HTTPException(404)
    ps = [p[0] for p in conn.execute(
        "SELECT name FROM meeting_participants WHERE meeting_id=? ORDER BY joined_at", (meeting_id,))]
    tracks = conn.execute("SELECT COUNT(*) FROM meeting_tracks WHERE meeting_id=?", (meeting_id,)).fetchone()[0]
    return {"status": m["status"], "error": m["error"], "participants": ps, "tracks": tracks}


@app.post("/meet/{meeting_id}/audio")
async def meet_audio(meeting_id: str, speaker: str = Form(...), rec_started_at: str = Form(...),
                     audio: UploadFile = File(...)):
    conn = get_conn()
    data = await audio.read()
    started = datetime.fromisoformat(rec_started_at.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
    mime = (audio.content_type or "audio/webm").split(";")[0]
    tid = meet.add_track(conn, meeting_id, speaker.strip(), mime, data, started)
    return {"ok": True, "track": tid, "bytes": len(data)}


def _finalize_job(meeting_id: str) -> None:
    conn = get_conn()
    try:
        meet.finalize(conn, meeting_id)
    except Exception:
        pass   # 失敗は meetings.status='failed' に記録済み


@app.post("/meet/{meeting_id}/finalize")
def meet_finalize(meeting_id: str, background: BackgroundTasks):
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone():
        raise HTTPException(404)
    conn.execute("UPDATE meetings SET status='processing' WHERE id=?", (meeting_id,))
    conn.commit()
    background.add_task(_finalize_job, meeting_id)
    return {"ok": True}


# ---------- エージェント ----------

@app.get("/agent", response_class=HTMLResponse)
def agent_page(request: Request):
    conn = get_conn()
    from app import linker
    from app.llm import cost
    processed = {r["stage"]: r["n"] for r in conn.execute("SELECT stage, COUNT(*) n FROM processed GROUP BY stage")}
    sync = {r["source"]: r["last_synced"] for r in conn.execute("SELECT * FROM sync_state")}
    return render("agent.html", request, conn, logs=db.recent_logs(conn, limit=150), processed=processed,
                  sync=sync, link=linker.method_breakdown(conn), cost=cost.summary(conn))


@app.get("/api/agent/status")
def agent_status():
    from app import agent
    conn = get_conn()
    return {"state": dict(agent.state), "open_findings": nav_context(conn)["nav_open_findings"]}


class AgentToggle(BaseModel):
    enabled: bool
    interval: int | None = None


@app.post("/api/agent/toggle")
def agent_toggle(body: AgentToggle):
    from app import agent
    return {"state": agent.set_enabled(body.enabled, body.interval)}


@app.get("/api/agent/logs")
def agent_logs(after_id: int = 0):
    conn = get_conn()
    return {"logs": db.recent_logs(conn, after_id=after_id, limit=200)}


@app.post("/tick")
def manual_tick(request: Request, background: BackgroundTasks):
    from app import agent

    def run():
        conn = get_conn()
        try:
            agent.tick(conn)
        except Exception as e:
            agent.say(f"tick 失敗: {e}")

    background.add_task(run)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True}
    return RedirectResponse(request.headers.get("referer") or "/agent", status_code=303)


# ---------- コスト ----------

@app.get("/cost", response_class=HTMLResponse)
def cost_page(request: Request):
    from app import linker
    from app.llm import cost, router
    conn = get_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT task, tier, model, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) c "
        "FROM cost_logs GROUP BY task, tier, model ORDER BY c DESC")]
    models = {t: router.model_for(t) for t in ("high", "mid", "embed")}
    return render("cost.html", request, conn, s=cost.summary(conn), rows=rows, models=models,
                  link=linker.method_breakdown(conn), prices=cost.model_prices())
