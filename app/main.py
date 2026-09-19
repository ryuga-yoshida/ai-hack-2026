"""FastAPI エントリポイント。最小 UI（Jinja2 + Tailwind CDN）。"""
import sqlite3
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import db
from app.connectors import chat
from app.models import Task, new_id

app = FastAPI(title="進行管理エージェント")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))

STATUSES = [("todo", "未着手"), ("in_progress", "進行中"), ("blocked", "ブロック"), ("done", "完了")]


def get_conn() -> sqlite3.Connection:
    conn = db.connect()
    db.init_db(conn)
    return conn


def render(name: str, request: Request, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx)


# ---------- ダッシュボード（M7 で矛盾カードを載せる） ----------

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    return render("placeholder.html", request, title="ダッシュボード",
                  note="検知結果（Finding）は M7 で表示します。")


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
                  timeline=[])  # タイムラインは M7 で links を辿って埋める


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


# ---------- 以降は後続マイルストーンで実装 ----------

@app.get("/cost", response_class=HTMLResponse)
def cost_page(request: Request):
    return render("placeholder.html", request, title="コスト", note="M6 で実装します。")
