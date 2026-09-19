"""FastAPI エントリポイント。最小 UI（Jinja2 + Tailwind CDN）。"""
import sqlite3
from datetime import date
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from app import db
from app.connectors import chat, meet
from app.models import Task, new_id

app = FastAPI(title="進行管理エージェント")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


def linkify(text: str) -> Markup:
    """発言中の URL（SPO のリンク共有など）をアンカーにする"""
    import re
    from html import escape
    out, pos = [], 0
    for m in re.finditer(r"https?://[^\s<>\"']+", text):
        out.append(escape(text[pos:m.start()]))
        out.append(f'<a href="{escape(m.group(0))}" class="text-blue-600 underline" target="_blank">{escape(m.group(0))}</a>')
        pos = m.end()
    out.append(escape(text[pos:]))
    return Markup("".join(out))


templates.env.filters["linkify"] = linkify

STATUSES = [("todo", "未着手"), ("in_progress", "進行中"), ("blocked", "ブロック"), ("done", "完了")]


def get_conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def render(name: str, request: Request, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


# ---------- ダッシュボード（M7 で矛盾カードを載せる） ----------

def finding_cards(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM findings {where} ORDER BY "
        "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, "
        "CASE kind WHEN 'contradiction' THEN 0 WHEN 'orphan_change' THEN 1 ELSE 2 END, "
        "confidence DESC, created_at DESC", params).fetchall()
    cards = []
    for r in rows:
        f = db.row_to_finding(r)
        evidence = [e for e in (db.get_event(conn, i) for i in f.evidence) if e]
        evidence.sort(key=lambda e: e.occurred_at)
        cards.append({"finding": f, "evidence": evidence, "dismiss_note": r["dismiss_note"],
                      "task": db.get_task(conn, f.task_id) if f.task_id else None})
    return cards


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    conn = get_conn()
    return render("index.html", request,
                  cards=finding_cards(conn, "WHERE status IN ('notified', 'pending')"))


@app.get("/findings", response_class=HTMLResponse)
def findings_page(request: Request):
    conn = get_conn()
    return render("findings.html", request, cards=finding_cards(conn))


@app.post("/findings/{finding_id}/ack")
def ack_finding(finding_id: str):
    conn = get_conn()
    conn.execute("UPDATE findings SET status='acknowledged' WHERE id=?", (finding_id,))
    conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/findings/{finding_id}/dismiss")
def dismiss_finding(finding_id: str, note: str = Form("")):
    conn = get_conn()
    conn.execute("UPDATE findings SET status='dismissed', dismiss_note=? WHERE id=?",
                 (note.strip() or None, finding_id))
    conn.commit()
    return RedirectResponse("/", status_code=303)


@app.post("/tick")
def manual_tick():
    from app import agent
    conn = get_conn()
    agent.tick(conn)
    return RedirectResponse("/", status_code=303)


# ---------- タスクボード ----------

@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request):
    conn = get_conn()
    tasks = db.list_tasks(conn)
    by_status = {s: [t for t in tasks if t.status == s] for s, _ in STATUSES}
    return render("tasks.html", request, statuses=STATUSES, by_status=by_status)


@app.post("/tasks")
def create_task(title: str = Form(...), assignee: str = Form(""),
                due_date: str = Form(""), artifacts: str = Form(""),
                description: str = Form("")):
    conn = get_conn()
    task = Task(
        id=new_id(), title=title.strip(), assignee=assignee.strip() or None,
        description=description.strip() or None,
        due_date=date.fromisoformat(due_date) if due_date else None,
        artifacts=[a.strip() for a in artifacts.split(",") if a.strip()],
    )
    db.save_task(conn, task)
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
def task_detail(request: Request, task_id: str):
    conn = get_conn()
    task = db.get_task(conn, task_id)
    if not task:
        raise HTTPException(404)
    return render("task_detail.html", request, task=task, statuses=STATUSES,
                  timeline=task_timeline(conn, task))


LABELS = {"decision": ("決定", "border-red-400"), "task_hint": ("タスク候補", "border-gray-300"),
          "utterance": ("発言", "border-blue-300"), "artifact_change": ("変更", "border-orange-400")}


def task_timeline(conn: sqlite3.Connection, task: Task) -> list[dict]:
    """タスクに紐付く全 Event と Finding を時刻順に並べる。links を辿るだけ。"""
    items, seen = [], set()

    def add(ev, label=None, color=None):
        if ev.id in seen:
            return
        seen.add(ev.id)
        lb, cl = LABELS.get(ev.kind, (ev.kind, "border-gray-300"))
        items.append({"when": ev.occurred_at, "label": label or lb, "color": color or cl,
                      "text": ev.text, "actor": ev.actor, "ref": ev.ref})

    rows = conn.execute(
        "SELECT e.*, l.relation FROM links l JOIN events e ON e.id = l.from_id "
        "WHERE l.to_type='task' AND l.to_id=? AND l.from_type='event'", (task.id,)).fetchall()
    for r in rows:
        add(db.row_to_event(r))
    created = conn.execute("SELECT created_at FROM tasks WHERE id=?", (task.id,)).fetchone()["created_at"]
    items.append({"when": db.from_iso(created), "label": "タスク生成", "color": "border-green-400",
                  "text": task.title, "actor": task.assignee, "ref": None})
    for r in conn.execute("SELECT * FROM findings WHERE task_id=?", (task.id,)).fetchall():
        f = db.row_to_finding(r)
        for eid in f.evidence:
            if ev := db.get_event(conn, eid):
                add(ev)
        items.append({"when": db.from_iso(r["created_at"]) if f.kind == "stalled" else
                      max((db.get_event(conn, i).occurred_at for i in f.evidence if db.get_event(conn, i)), default=db.from_iso(r["created_at"])),
                      "label": "検知", "color": "border-red-600", "text": f.summary, "actor": None, "ref": None})
    items.sort(key=lambda x: x["when"])
    for it in items:
        it["when"] = it["when"].strftime("%m/%d %H:%M")
    return items


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
    if patch.status is not None and patch.status not in {s for s, _ in STATUSES}:
        raise HTTPException(422, "invalid status")
    for k, v in patch.model_dump(exclude_unset=True).items():
        setattr(task, k, v)
    db.save_task(conn, task)   # updated_at を更新（stalled 検知の起点）
    return {"ok": True, "task": task}


# ---------- チャット ----------

@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, channel: str = "general"):
    conn = get_conn()
    channels = chat.list_channels(conn)
    if channel not in channels:
        channels.append(channel)
    return render("chat.html", request, channel=channel, channels=channels,
                  messages=chat.list_messages(conn, channel))


@app.post("/chat")
def post_chat(channel: str = Form("general"), actor: str = Form(...), text: str = Form(...)):
    conn = get_conn()
    chat.post_message(conn, channel.strip() or "general", actor.strip(), text.strip())
    # 投稿を即座に Event 化する（巡回を待たない）
    adapter = chat.ChatAdapter(conn)
    db.save_events(conn, adapter.fetch(db.last_synced(conn, adapter.name)))
    db.update_sync_state(conn, adapter.name)
    return RedirectResponse(f"/chat?channel={channel}", status_code=303)


# ---------- 自作 Meet（会議室） ----------

@app.get("/meet", response_class=HTMLResponse)
def meet_page(request: Request):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM meetings ORDER BY started_at DESC").fetchall()
    meetings = []
    for r in rows:
        ps = [p[0] for p in conn.execute(
            "SELECT name FROM meeting_participants WHERE meeting_id=? ORDER BY joined_at", (r["id"],))]
        meetings.append({**dict(r), "participants": ps})
    return render("meet.html", request, meetings=meetings)


@app.post("/meet")
def create_meeting(title: str = Form("定例会議"), me: str = Form(...)):
    conn = get_conn()
    mid = meet.create_meeting(conn, title)
    meet.join(conn, mid, me.strip())
    return RedirectResponse(f"/meet/{mid}?me={me.strip()}", status_code=303)


@app.get("/meet/{meeting_id}", response_class=HTMLResponse)
def meet_room(request: Request, meeting_id: str, me: str = ""):
    conn = get_conn()
    m = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if not m:
        raise HTTPException(404)
    if me.strip():
        meet.join(conn, meeting_id, me.strip())
    ps = [p[0] for p in conn.execute(
        "SELECT name FROM meeting_participants WHERE meeting_id=? ORDER BY joined_at", (meeting_id,))]
    return render("meet_room.html", request, m=dict(m), me=me.strip(), participants=ps)


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
    from datetime import datetime, timezone
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


# ---------- コスト ----------

@app.get("/cost", response_class=HTMLResponse)
def cost_page(request: Request):
    from app import linker
    from app.llm import cost
    conn = get_conn()
    return render("cost.html", request, s=cost.summary(conn), link=linker.method_breakdown(conn))
