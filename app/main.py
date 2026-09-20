"""FastAPI エントリポイント。Jinja2 + Tailwind CDN + Alpine.js の UI。"""
import json
import re
import sqlite3
from datetime import date, datetime
from html import escape
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from pydantic import BaseModel

from app import config, db, tags as tagmod
from app.connectors import calendar as cal, chat, mail as mailmod, meet, wiki as wikimod
from app.models import Event, Link, Task, new_id

app = FastAPI(title="進行管理エージェント")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


@app.middleware("http")
async def require_signin(request: Request, call_next):
    """未サインイン（プロフィール未設定）なら /login へ。API・WebSocket・静的ファイルは対象外。
    本番では Microsoft Entra ID などの SSO に置き換える"""
    path = request.url.path
    if request.method == "GET" and not request.cookies.get("me") and not (
        path.startswith(("/login", "/api/", "/ws/", "/static", "/artifacts/file/", "/meet/")) or path == "/tick"
    ) and "application/json" not in request.headers.get("accept", ""):
        return RedirectResponse(f"/login?next={quote(str(request.url.path) + ('?' + request.url.query if request.url.query else ''))}", status_code=303)
    return await call_next(request)

STATUSES = [("todo", "未着手"), ("in_progress", "進行中"), ("blocked", "ブロック"), ("done", "完了")]
STATUS_LABEL = dict(STATUSES)
PRIORITIES = [("urgent", "緊急"), ("high", "高"), ("normal", "中"), ("low", "低")]
PRIORITY_LABEL = dict(PRIORITIES)
PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}
ACTIVITY_LABEL = {"created": "作成", "status": "状態", "assignee": "担当者", "due_date": "期限", "title": "タイトル",
                  "priority": "優先度", "artifacts": "成果物", "description": "説明", "checklist": "チェックリスト",
                  "delete": "削除", "due": "期限", "tag": "タグ", "team": "チーム"}

KIND_LABEL = {"contradiction": "矛盾", "stalled": "停滞", "orphan_change": "根拠なし変更", "status_suggestion": "状態更新の提案"}
EVENT_LABEL = {"decision": "決定", "task_hint": "タスク候補", "utterance": "発言", "artifact_change": "変更"}
FINDING_STATUS_LABEL = {"notified": "通知済", "pending": "確認待ち", "acknowledged": "確認済", "dismissed": "却下"}
PEOPLE = config.PERSONS


from urllib.parse import quote, unquote


def get_me(request: Request) -> str:
    return unquote(request.cookies.get("me", ""))


def set_me(resp, name: str):
    if name and name.strip():
        resp.set_cookie("me", quote(name.strip()), max_age=86400 * 365)
    return resp


def who(request: Request, actor: str = "") -> str:
    """操作している人。フォームで明示されていなければプロフィール（cookie）を使う"""
    name = (actor or "").strip() or get_me(request)
    if not name:
        raise HTTPException(400, "プロフィールで名前を設定してください（左下の「あなた」から）")
    return name


_overrides_loaded = False


def get_conn() -> sqlite3.Connection:
    global _overrides_loaded
    conn = db.connect()
    db.init_db(conn)
    if not _overrides_loaded:
        config.apply_overrides(db.get_settings(conn))
        _overrides_loaded = True
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
def fmt_rel(v) -> str:
    """相対時刻（3分前・2時間前・昨日 …）。古いものは日付"""
    if not v:
        return ""
    if isinstance(v, str):
        v = db.from_iso(v)
    d = datetime.now() - v
    sec = d.total_seconds()
    if sec < 60:
        return "たった今"
    if sec < 3600:
        return f"{int(sec // 60)}分前"
    if sec < 86400 and v.date() == date.today():
        return f"{int(sec // 3600)}時間前"
    if v.date() == date.today() - __import__("datetime").timedelta(days=1):
        return f"昨日 {v:%H:%M}"
    if d.days < 7:
        return f"{d.days}日前"
    return v.strftime("%m/%d %H:%M")


templates.env.filters["dt"] = fmt_dt
templates.env.filters["rel"] = fmt_rel
templates.env.filters["tag_colors"] = lambda tags: {t["name"]: t["color"] for t in tags}
templates.env.globals.update(STATUSES=STATUSES, STATUS_LABEL=STATUS_LABEL, KIND_LABEL=KIND_LABEL, PRIORITIES=PRIORITIES, PRIORITY_LABEL=PRIORITY_LABEL, ACTIVITY_LABEL=ACTIVITY_LABEL,
                             EVENT_LABEL=EVENT_LABEL, FINDING_STATUS_LABEL=FINDING_STATUS_LABEL, PEOPLE=PEOPLE)


def people_list(conn: sqlite3.Connection) -> list[str]:
    """メンバー一覧。設定の登場人物に加えて、チャット・メール・タスク・会議に登場した人を集める"""
    names = list(PEOPLE)
    seen = set(names)
    sql = ("SELECT actor n FROM chat_messages UNION SELECT sender FROM mails UNION SELECT assignee FROM tasks "
           "UNION SELECT name FROM meeting_participants UNION SELECT organizer FROM cal_events")
    for r in conn.execute(sql):
        n = (r[0] or "").strip()
        if n and n not in seen and n != "エージェント" and len(n) <= 20:
            names.append(n); seen.add(n)
    return names


def nav_context(conn: sqlite3.Connection) -> dict:
    from app import agent
    open_findings = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE status IN ('notified','pending')").fetchone()[0]
    unread_mail = 0
    return {"nav_open_findings": open_findings, "agent_state": dict(agent.state), "people": people_list(conn),
            "teams_all": [t["name"] for t in tagmod.teams(conn)]}


def render(name: str, request: Request, conn: sqlite3.Connection | None = None, **ctx) -> HTMLResponse:
    if conn is not None:
        ctx.update(nav_context(conn))
    ctx.setdefault("me", get_me(request))
    if conn is not None and ctx["me"]:
        ctx.setdefault("my_presence", presence_of(conn, ctx["me"]))
    ctx.setdefault("active", name.split(".")[0].replace("tag_detail", "tags").replace("task_detail", "tasks").replace("meet_room", "meet").replace("calendar_detail", "calendar").replace("wiki_edit", "wiki").replace("wiki_diff", "wiki"))
    return templates.TemplateResponse(request, name, ctx)


# ---------- 横断検索 ----------

@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = ""):
    conn = get_conn()
    q = q.strip()
    res = {"tasks": [], "messages": [], "mails": [], "files": [], "wiki": [], "meetings": [], "findings": []}
    if q:
        like = f"%{q}%"
        res["tasks"] = [t for t in db.list_tasks(conn) if q in t.title or q in (t.description or "")][:20]
        res["messages"] = [_message_dict(conn, r) for r in chat.search_messages(conn, q, 20)]
        res["mails"] = [dict(r) for r in conn.execute("SELECT id, thread_id, sender, subject, sent_at FROM mails WHERE subject LIKE ? OR body LIKE ? ORDER BY sent_at DESC LIMIT 20", (like, like))]
        from app.connectors.excel import version_series
        res["files"] = [k for k in version_series(LIB(), ext=None) if q.lower() in k.lower()][:20]
        res["wiki"] = [dict(r) for r in conn.execute("SELECT id, title, updated_at FROM wiki_pages WHERE title LIKE ? OR body LIKE ? ORDER BY updated_at DESC LIMIT 20", (like, like))]
        res["meetings"] = [dict(r) for r in conn.execute(
            "SELECT DISTINCT json_extract(meta,'$.meeting_id') id, json_extract(meta,'$.meeting_title') title, MIN(occurred_at) at "
            "FROM events WHERE source='meet' AND kind='utterance' AND text LIKE ? GROUP BY id ORDER BY at DESC LIMIT 20", (like,))]
        res["findings"] = [db.row_to_finding(r) for r in conn.execute("SELECT * FROM findings WHERE summary LIKE ? OR reason LIKE ? ORDER BY created_at DESC LIMIT 20", (like, like))]
    total = sum(len(v) for v in res.values())
    return render("search.html", request, conn, q=q, res=res, total=total)


# ---------- 通知（自分宛） ----------

def notifications_for(conn: sqlite3.Connection, me: str, limit: int = 100) -> list[dict]:
    """自分宛のもの: 通知先になった検知（決定者・変更者・担当者）、状態更新の提案（担当）、@メンション、会議リマインド"""
    from app import agent
    out: list[dict] = []
    if not me:
        return out
    for r in conn.execute("SELECT * FROM findings WHERE status IN ('notified','pending') ORDER BY created_at DESC LIMIT 200"):
        f = db.row_to_finding(r)
        task = db.get_task(conn, f.task_id) if f.task_id else None
        actors = set()
        if f.kind == "contradiction":
            actors |= {agent.decision_actor(conn, f), agent.change_actor(conn, f)}
        elif f.kind == "orphan_change":
            actors.add(agent.change_actor(conn, f))
        if task and task.assignee:
            actors.add(task.assignee)
        if me in actors:
            out.append({"kind": "finding", "at": r["created_at"], "title": KIND_LABEL.get(f.kind, f.kind), "text": f.summary,
                        "href": f"/findings?status=all#finding-{f.id}", "tone": "red" if f.kind == "contradiction" else ("emerald" if f.kind == "status_suggestion" else "amber"),
                        "id": f.id})
    for r in conn.execute("SELECT * FROM chat_messages WHERE deleted=0 AND text LIKE '%@%' ORDER BY posted_at DESC LIMIT 200"):
        if me in tagmod.expand_mentions(conn, r["text"], PEOPLE) and r["actor"] != me:
            ch = r["channel"]
            href = f"/tasks/{ch[5:]}" if ch.startswith("task:") else f"/chat?channel={ch}" + (f"&thread={r['reply_to']}" if r["reply_to"] else "")
            out.append({"kind": "mention", "at": r["posted_at"], "title": f"@メンション · {r['actor']}", "text": r["text"], "href": href, "tone": "indigo", "id": r["id"]})
    for r in conn.execute("SELECT * FROM cal_events WHERE reminded_at IS NOT NULL ORDER BY reminded_at DESC LIMIT 50"):
        e = cal._row(r)
        if me in e["attendees"]:
            out.append({"kind": "reminder", "at": e["reminded_at"], "title": "会議のリマインド", "text": f"{e['title']}（{e['start']:%m/%d %H:%M}〜）", "href": f"/calendar/{e['id']}", "tone": "sky", "id": e["id"]})
    out.sort(key=lambda x: x["at"] or "", reverse=True)
    return out[:limit]


def _notif_read_ids(conn: sqlite3.Connection, me: str) -> set[str]:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (f"notif_read:{me}",)).fetchone()
    return set(json.loads(r["value"])) if r else set()


@app.get("/notifications", response_class=HTMLResponse)
def notifications_page(request: Request, kind: str = "", unread: str = ""):
    conn = get_conn()
    me = get_me(request)
    items = notifications_for(conn, me)
    seen = conn.execute("SELECT value FROM settings WHERE key=?", (f"notif_seen:{me}",)).fetchone()
    seen_at = seen["value"] if seen else ""
    read_ids = _notif_read_ids(conn, me)
    for n in items:
        n["unread"] = (n["at"] or "") > seen_at and n["id"] not in read_ids
    counts = {k: sum(1 for n in items if n["kind"] == k) for k in ("finding", "mention", "reminder")}
    if kind:
        items = [n for n in items if n["kind"] == kind]
    if unread:
        items = [n for n in items if n["unread"]]
    return render("notifications.html", request, conn, items=items, seen_at=seen_at, kind=kind, unread=unread, counts=counts,
                  n_unread=sum(1 for n in notifications_for(conn, me) if (n["at"] or "") > seen_at and n["id"] not in read_ids))


@app.post("/notifications/read")
def notifications_read(request: Request, id: str = Form(""), all: str = Form("")):
    conn = get_conn()
    me = get_me(request)
    if all:
        db.set_setting(conn, f"notif_seen:{me}", db.now_iso())
        db.set_setting(conn, f"notif_read:{me}", "[]")
    elif id:
        ids = _notif_read_ids(conn, me) | {id}
        db.set_setting(conn, f"notif_read:{me}", json.dumps(sorted(ids)))
    return RedirectResponse(request.headers.get("referer") or "/notifications", status_code=303)


@app.get("/api/notifications/count")
def notifications_count(request: Request):
    conn = get_conn()
    me = get_me(request)
    read_ids = _notif_read_ids(conn, me) if me else set()
    seen = conn.execute("SELECT value FROM settings WHERE key=?", (f"notif_seen:{me}",)).fetchone()
    seen_at = seen["value"] if seen else ""
    return {"count": sum(1 for n in notifications_for(conn, me) if (n["at"] or "") > seen_at and n["id"] not in read_ids)}


# ---------- サインイン / プロフィール ----------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    conn = get_conn()
    accounts = [{"name": p, **config.PERSON_INFO.get(p, {"role": "", "dept": ""}), "email": f"{p}@aoba-beverage.example"}
                for p in people_list(conn)]
    return templates.TemplateResponse(request, "login.html", {"accounts": accounts, "next": next or "/"})


@app.post("/login")
def login_post(name: str = Form(...), next: str = Form("/")):
    resp = RedirectResponse(next if next.startswith("/") else "/", status_code=303)
    return set_me(resp, name)


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("me")
    return resp


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    conn = get_conn()
    me = get_me(request)
    stats = None
    if me:
        stats = {
            "tasks": conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee=? AND status != 'done'", (me,)).fetchone()[0],
            "notifs": len(notifications_for(conn, me)),
            "teams": [t["name"] for t in tagmod.teams(conn) if me in t["members"]],
        }
    return render("profile.html", request, conn, me=me, stats=stats, info=config.PERSON_INFO.get(me, {}),
                  presence=presence_of(conn, me) if me else None, PRESENCE=PRESENCE)


@app.get("/api/people/{name}")
def person_card(name: str):
    """ホバーカード用のプロフィール"""
    conn = get_conn()
    open_tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE assignee=? AND status != 'done'", (name,)).fetchone()[0]
    overdue = sum(1 for t in db.list_tasks(conn) if t.assignee == name and t.due_date and t.due_date < date.today() and t.status != "done")
    last = conn.execute("SELECT MAX(occurred_at) FROM events WHERE actor=?", (name,)).fetchone()[0]
    findings = conn.execute(
        "SELECT COUNT(*) FROM findings f JOIN tasks t ON t.id = f.task_id WHERE t.assignee=? AND f.status IN ('notified','pending')", (name,)).fetchone()[0]
    return {"name": name, "teams": [t["name"] for t in tagmod.teams(conn) if name in t["members"]],
            "open_tasks": open_tasks, "overdue": overdue, "last_active": last, "open_findings": findings,
            "email": f"{name}@aoba-beverage.example", "presence": presence_of(conn, name), **config.PERSON_INFO.get(name, {})}


@app.post("/profile")
def profile_set(request: Request, name: str = Form(...), next: str = Form("")):
    resp = RedirectResponse(next or "/profile", status_code=303)
    return set_me(resp, name)


PRESENCE = [("available", "在席", "bg-emerald-500"), ("busy", "取り込み中", "bg-red-500"), ("meeting", "会議中", "bg-red-500"),
            ("away", "離席中", "bg-amber-400"), ("offline", "退勤", "bg-gray-400")]


def presence_of(conn: sqlite3.Connection, name: str) -> dict:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (f"presence:{name}",)).fetchone()
    d = json.loads(r["value"]) if r else {}
    # 会議中は自動
    if conn.execute("SELECT 1 FROM meeting_participants p JOIN meetings m ON m.id = p.meeting_id WHERE p.name=? AND m.status='recording' LIMIT 1", (name,)).fetchone():
        d = {**d, "state": "meeting"}
    st = d.get("state", "available")
    label, color = next(((l, c) for k, l, c in PRESENCE if k == st), ("在席", "bg-emerald-500"))
    return {"state": st, "label": label, "color": color, "message": d.get("message", "")}


@app.post("/profile/presence")
def profile_presence(request: Request, state: str = Form("available"), message: str = Form("")):
    conn = get_conn()
    me = get_me(request)
    db.set_setting(conn, f"presence:{me}", json.dumps({"state": state if state in {k for k, _, _ in PRESENCE} else "available",
                                                        "message": message.strip()[:80]}, ensure_ascii=False))
    return RedirectResponse(request.headers.get("referer") or "/profile", status_code=303)


@app.get("/api/presence")
def presence_api():
    conn = get_conn()
    return {p: presence_of(conn, p) for p in people_list(conn)}


@app.get("/api/palette")
def palette_api(q: str = ""):
    """コマンドパレット用: ページ・タスク・人・チャンネル・ファイル・Wiki を横断検索（軽量）"""
    conn = get_conn()
    q = q.strip().lower()
    out = []
    pages = [("ダッシュボード", "/"), ("検知結果", "/findings"), ("タスク", "/tasks"), ("チャット", "/chat"), ("メール", "/mail"),
             ("予定表", "/calendar"), ("会議室", "/meet"), ("Wiki", "/wiki"), ("成果物", "/artifacts"), ("タグ・チーム", "/tags"),
             ("エージェント", "/agent"), ("コスト", "/cost"), ("設定", "/settings"), ("通知", "/notifications"), ("プロフィール", "/profile")]
    for label, href in pages:
        if not q or q in label.lower():
            out.append({"kind": "ページ", "label": label, "href": href})
    if q:
        for t in db.list_tasks(conn):
            if q in t.title.lower():
                out.append({"kind": "タスク", "label": t.title, "href": f"/tasks/{t.id}", "sub": STATUS_LABEL.get(t.status, "")})
        for p in people_list(conn):
            if q in p.lower():
                out.append({"kind": "人", "label": p, "href": f"/chat/dm/{p}", "sub": "DM を開く"})
        for c in chat.list_channels(conn):
            if q in c.lower():
                out.append({"kind": "チャンネル", "label": f"#{c}", "href": f"/chat?channel={c}"})
        from app.connectors.excel import version_series
        for k in version_series(LIB(), ext=None):
            if q in k.lower():
                out.append({"kind": "ファイル", "label": k, "href": f"/artifacts?q={k.rsplit('/', 1)[-1]}"})
        for r in conn.execute("SELECT id, title FROM wiki_pages WHERE lower(title) LIKE ? LIMIT 10", (f"%{q}%",)):
            out.append({"kind": "Wiki", "label": r["title"], "href": f"/wiki/{r['id']}"})
        for r in conn.execute("SELECT id, title FROM cal_events WHERE lower(title) LIKE ? ORDER BY start_at DESC LIMIT 5", (f"%{q}%",)):
            out.append({"kind": "予定", "label": r["title"], "href": f"/calendar/{r['id']}"})
        out.append({"kind": "検索", "label": f"「{q}」を横断検索", "href": f"/search?q={q}"})
    return {"items": out[:30]}


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
                      "created_at": r["created_at"], "payload": json.loads(r["payload"]) if r["payload"] else {},
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
    from app import agent
    from app.llm import cost
    cards_all = finding_cards(conn, "WHERE status IN ('notified','pending')")
    stalled = [c for c in cards_all if c["finding"].kind == "stalled"]
    cards = [c for c in cards_all if c["finding"].kind != "stalled"][:6]
    me = get_me(request)
    from datetime import timedelta
    my_tasks = sorted([task_meta(conn, t) for t in db.list_tasks(conn) if t.assignee == me and t.status != "done"],
                      key=lambda m: (m["task"].due_date is None, str(m["task"].due_date or ""), m["updated_at"]))[:8]
    today_evs = [e for e in cal.between(conn, datetime.combine(date.today(), datetime.min.time()),
                                        datetime.combine(date.today() + timedelta(days=1), datetime.min.time())) if not me or me in e["attendees"] or e.get("organizer") == me]
    my_notifs = notifications_for(conn, me, limit=5) if me else []
    return render("index.html", request, conn, autonomy=agent.autonomy_stats(conn), stalled=stalled,
                  config_stalled_days=config.STALLED_DAYS, my_tasks=my_tasks, today_evs=today_evs, my_notifs=my_notifs,
                  today=date.today(),
                  cards=cards,
                  counts=counts, kinds=kinds, task_counts=task_counts, events_n=events_n, sources=sources,
                  cost=cost.summary(conn), logs=db.recent_logs(conn, limit=12))


# ---------- 検知結果 ----------

@app.get("/findings", response_class=HTMLResponse)
def findings_page(request: Request, status: str = "open", kind: str = "", file: str = "", q: str = "", assignee: str = "",
                  severity: str = "", sort: str = "", mine: str = "", view: str = ""):
    conn = get_conn()
    me = get_me(request)
    cond, params = [], []
    if q.strip():
        cond.append("(summary LIKE ? OR reason LIKE ?)"); params += [f"%{q.strip()}%"] * 2
    if severity in ("high", "medium", "low"):
        cond.append("severity = ?"); params.append(severity)
    if assignee:
        cond.append("task_id IN (SELECT id FROM tasks WHERE assignee = ?)"); params.append(assignee)
    if file:
        # その版（ファイル名）の変更を根拠に含む Finding だけ
        cond.append("EXISTS (SELECT 1 FROM events e WHERE e.kind='artifact_change' AND json_extract(e.meta,'$.file')=? "
                    "AND findings.evidence LIKE '%' || e.id || '%')")
        params.append(file)
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
    cards = finding_cards(conn, where, tuple(params))
    if mine and me:
        from app import agent
        mine_ids = {n["id"] for n in notifications_for(conn, me, limit=1000) if n["kind"] == "finding"}
        cards = [c for c in cards if c["finding"].id in mine_ids or (c["task"] and c["task"].assignee == me)]
    if sort == "newest":
        cards.sort(key=lambda c: c["created_at"], reverse=True)
    elif sort == "confidence":
        cards.sort(key=lambda c: -c["finding"].confidence)
    elif sort == "task":
        cards.sort(key=lambda c: (c["task"].title if c["task"] else "～"))
    assignees = sorted({t.assignee for t in db.list_tasks(conn) if t.assignee})
    return render("findings.html", request, conn, cards=cards, status=status, kind=kind, counts=counts, file=file, q=q,
                  assignee=assignee, severity=severity, sort=sort, mine=mine, assignees=assignees, view=view or request.cookies.get("findings_view", "cards"))


@app.post("/findings/bulk")
def findings_bulk(request: Request, ids: list[str] = Form([]), action: str = Form(...), note: str = Form("")):
    conn = get_conn()
    for fid in ids:
        if action == "ack":
            conn.execute("UPDATE findings SET status='acknowledged' WHERE id=? AND status IN ('notified','pending')", (fid,))
        elif action == "dismiss":
            conn.execute("UPDATE findings SET status='dismissed', dismiss_note=? WHERE id=?", (note.strip() or "一括却下", fid))
        elif action == "reopen":
            conn.execute("UPDATE findings SET status='pending' WHERE id=?", (fid,))
    conn.commit()
    return RedirectResponse(request.headers.get("referer") or "/findings", status_code=303)


@app.get("/findings/export.csv")
def findings_export(status: str = "all"):
    import csv, io
    from fastapi.responses import Response
    conn = get_conn()
    where = "" if status == "all" else ("WHERE status IN ('notified','pending')" if status == "open" else "WHERE status = ?")
    params = () if status in ("all", "open") else (status,)
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["id", "種別", "重要度", "confidence", "状態", "タスク", "担当", "要約", "理由", "検知日時"])
    for c in finding_cards(conn, where, params):
        f = c["finding"]
        w.writerow([f.id, KIND_LABEL.get(f.kind, f.kind), f.severity, f"{f.confidence:.2f}", FINDING_STATUS_LABEL.get(f.status, f.status),
                    c["task"].title if c["task"] else "", c["task"].assignee if c["task"] else "", f.summary, f.reason or "", c["created_at"]])
    return Response(content="\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=findings.csv"})


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


@app.post("/findings/{finding_id}/notify_chat")
def finding_notify_chat(request: Request, finding_id: str, channel: str = Form("general")):
    """検知を担当者にチャットで知らせる（人が押したときだけ投稿する＝承認ゲート）"""
    from app import agent
    conn = get_conn()
    r = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    f = db.row_to_finding(r)
    to = [a for a in {agent.decision_actor(conn, f), agent.change_actor(conn, f)} if a]
    task = db.get_task(conn, f.task_id) if f.task_id else None
    if task and task.assignee:
        to.append(task.assignee)
    mention = " ".join(f"@{a}" for a in dict.fromkeys(to)) or ""
    chat.post_message(conn, channel, who(request), f"{mention} ⚠ {f.summary} {config.APP_BASE_URL}/findings?status=all#finding-{f.id}")
    _ingest_chat(conn)
    conn.execute("UPDATE findings SET status='notified' WHERE id=? AND status='pending'", (finding_id,))
    conn.commit()
    return RedirectResponse(request.headers.get("referer") or "/", status_code=303)


@app.post("/findings/{finding_id}/to_task")
def finding_to_task(request: Request, finding_id: str):
    """検知から対応タスクを作る"""
    from app import agent
    conn = get_conn()
    r = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    f = db.row_to_finding(r)
    assignee = agent.change_actor(conn, f) if f.kind in ("contradiction", "orphan_change") else None
    src = db.get_task(conn, f.task_id) if f.task_id else None
    title = {"contradiction": "決定との食い違いを確認する", "orphan_change": "根拠のない変更を確認する", "stalled": "停滞しているタスクをフォローする"}.get(f.kind, "検知を確認する")
    if src:
        title += f": {src.title[:40]}"
    task = Task(id=new_id(), title=title, assignee=assignee or (src.assignee if src else None), status="todo",
                description=f.summary, artifacts=list(src.artifacts) if src else [])
    db.save_task(conn, task)
    for eid in f.evidence[:2]:
        db.save_link(conn, Link(id=new_id(), from_type="event", from_id=eid, to_type="task", to_id=task.id,
                                relation="discusses", confidence=1.0, method="manual"))
    conn.execute("UPDATE findings SET status='acknowledged' WHERE id=?", (finding_id,))
    conn.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@app.post("/findings/{finding_id}/apply")
def apply_finding(finding_id: str, request: Request):
    """状態更新の提案を適用する（人の承認）"""
    conn = get_conn()
    r = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
    if not r or r["kind"] != "status_suggestion":
        raise HTTPException(404)
    payload = json.loads(r["payload"] or "{}")
    task = db.get_task(conn, r["task_id"]) if r["task_id"] else None
    if task and payload.get("to") in STATUS_LABEL:
        task.status = payload["to"]
        db.save_task(conn, task)
    conn.execute("UPDATE findings SET status='acknowledged' WHERE id=?", (finding_id,))
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
    tags_all = tagmod.tags_for(conn, "task", t.id)
    updated = conn.execute("SELECT updated_at FROM tasks WHERE id=?", (t.id,)).fetchone()["updated_at"]
    ck = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(done),0) d FROM task_checklist WHERE task_id=?", (t.id,)).fetchone()
    return {"task": t, "n_msgs": n_msgs, "n_findings": n_findings, "check_total": ck["n"], "check_done": ck["d"],
            "watchers": [r["user"] for r in conn.execute("SELECT user FROM task_watchers WHERE task_id=?", (t.id,))],
            "tags": [x for x in tags_all if x["kind"] != "team"], "teams": [x for x in tags_all if x["kind"] == "team"],
            "updated_at": updated,
            "origin": origin, "overdue": bool(t.due_date and t.due_date < date.today() and t.status != "done")}


@app.get("/tasks", response_class=HTMLResponse)
def tasks_page(request: Request, assignee: str = "", q: str = "", tag: str = "", team: str = "",
               view: str = "", sort: str = "updated", quick: str = "", priority: str = ""):
    from datetime import timedelta
    conn = get_conn()
    me = get_me(request)
    view = view or request.cookies.get("tasks_view", "board")
    tasks = db.list_tasks(conn)
    today = date.today()
    if quick == "mine":
        watching = {r["task_id"] for r in conn.execute("SELECT task_id FROM task_watchers WHERE user=?", (me,))}
        tasks = [t for t in tasks if t.assignee == me or t.id in watching]
    elif quick == "overdue":
        tasks = [t for t in tasks if t.due_date and t.due_date < today and t.status != "done"]
    elif quick == "week":
        tasks = [t for t in tasks if t.due_date and today <= t.due_date <= today + timedelta(days=7) and t.status != "done"]
    elif quick == "unassigned":
        tasks = [t for t in tasks if not t.assignee and t.status != "done"]
    elif quick == "findings":
        ids = {r["task_id"] for r in conn.execute("SELECT task_id FROM findings WHERE status IN ('notified','pending')")}
        tasks = [t for t in tasks if t.id in ids]
    elif quick == "nodue":
        tasks = [t for t in tasks if not t.due_date and t.status != "done"]
    if priority:
        tasks = [t for t in tasks if t.priority == priority]
    if assignee:
        tasks = [t for t in tasks if (t.assignee or "") == assignee]
    if tag:
        ids = set(tagmod.targets_for(conn, tag)["task"])
        tasks = [t for t in tasks if t.id in ids]
    if team:
        tm = tagmod.get_tag(conn, team)
        ids = set(tagmod.targets_for(conn, team)["task"])
        members = set(tm["members"]) if tm else set()
        # チームに明示的に紐付いたタスク ＋ 担当者がチームのメンバーであるタスク
        tasks = [t for t in tasks if t.id in ids or (t.assignee in members)]
    if q:
        tasks = [t for t in tasks if q in t.title or q in (t.description or "")]
    metas = [task_meta(conn, t) for t in tasks]
    by_status = {s: [m for m in metas if m["task"].status == s] for s, _ in STATUSES}
    order = {"todo": 0, "in_progress": 1, "blocked": 2, "done": 3}
    keyf = {"updated": lambda m: m["updated_at"], "due": lambda m: (m["task"].due_date is None, str(m["task"].due_date or "")),
            "status": lambda m: order[m["task"].status], "assignee": lambda m: m["task"].assignee or "～",
            "title": lambda m: m["task"].title, "findings": lambda m: -m["n_findings"],
            "priority": lambda m: PRIORITY_ORDER.get(m["task"].priority, 1)}.get(sort, lambda m: m["updated_at"])
    rows = sorted(metas, key=keyf, reverse=(sort == "updated"))
    assignees = sorted({t.assignee for t in db.list_tasks(conn) if t.assignee})
    all_tags = tagmod.all_tags(conn)
    counts = {"all": len(db.list_tasks(conn)), "overdue": sum(1 for t in db.list_tasks(conn) if t.due_date and t.due_date < today and t.status != "done"),
              "mine": sum(1 for t in db.list_tasks(conn) if t.assignee == me and t.status != "done")}
    resp = render("tasks.html", request, conn, by_status=by_status, rows=rows, assignees=assignees, quick=quick, priority=priority,
                  counts=counts, me=me, PRIORITIES=PRIORITIES,
                  assignee=assignee, q=q, tag=tag, team=team, view=view, sort=sort, today=date.today().isoformat(),
                  all_tags=[t for t in all_tags if t["kind"] != "team"], teams=[t for t in all_tags if t["kind"] == "team"])
    resp.set_cookie("tasks_view", view, max_age=86400 * 365)
    return resp


@app.post("/tasks/{task_id}/teams")
def set_task_teams(request: Request, task_id: str, teams: list[str] = Form([]), back: str = Form("")):
    """タスクをチームに紐付ける（複数可）。チームはタグ(kind=team)として保持"""
    conn = get_conn()
    if not db.get_task(conn, task_id):
        raise HTTPException(404)
    for t in tagmod.teams(conn):
        if t["name"] in teams:
            tagmod.link(conn, t["name"], "task", task_id)
        else:
            tagmod.unlink(conn, t["name"], "task", task_id)
    if back and request.headers.get("referer"):
        return RedirectResponse(request.headers["referer"], status_code=303)
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


@app.post("/tasks")
def create_task(request: Request, title: str = Form(...), assignee: str = Form(""), due_date: str = Form(""),
                artifacts: str = Form(""), description: str = Form(""), status: str = Form("todo"), priority: str = Form("normal"),
                back: str = Form("")):
    conn = get_conn()
    me = get_me(request)
    task = Task(
        id=new_id(), title=title.strip(), assignee=assignee.strip() or None,
        description=description.strip() or None, status=status if status in STATUS_LABEL else "todo",
        due_date=date.fromisoformat(due_date) if due_date else None,
        artifacts=[a.strip() for a in artifacts.split(",") if a.strip()],
        priority=priority if priority in PRIORITY_ORDER else "normal",
    )
    db.save_task(conn, task)
    db.task_activity(conn, task.id, me, "created", "手動で作成")
    if me:
        conn.execute("INSERT OR IGNORE INTO task_watchers (task_id, user) VALUES (?,?)", (task.id, me)); conn.commit()
    tagmod.apply_hashtags(conn, f"{title} {description}", "task", task.id)
    if back:
        return RedirectResponse(back, status_code=303)
    return RedirectResponse(f"/tasks/{task.id}", status_code=303)


@app.post("/tasks/bulk")
def tasks_bulk(request: Request, ids: list[str] = Form([]), action: str = Form(...), value: str = Form("")):
    """一覧の一括操作: status / assignee / due / priority / delete / team / tag"""
    conn = get_conn()
    me = get_me(request)
    for tid in ids:
        t = db.get_task(conn, tid)
        if not t:
            continue
        if action == "delete":
            _delete_task(conn, tid)
            continue
        if action == "status" and value in STATUS_LABEL:
            t.status = value
        elif action == "assignee":
            t.assignee = value or None
        elif action == "due":
            t.due_date = date.fromisoformat(value) if value else None
        elif action == "priority" and value in PRIORITY_ORDER:
            t.priority = value
        elif action == "tag" and value:
            tagmod.link(conn, value, "task", tid); continue
        elif action == "team" and value:
            tagmod.link(conn, value, "task", tid); continue
        else:
            continue
        db.save_task(conn, t)
        db.task_activity(conn, tid, me, action, f"一括操作: {value}")
    ref = request.headers.get("referer") or "/tasks?view=list"
    return RedirectResponse(ref, status_code=303)


def _delete_task(conn: sqlite3.Connection, task_id: str) -> None:
    for tbl, col in (("links", "to_id"), ("findings", "task_id"), ("task_checklist", "task_id"),
                     ("task_watchers", "task_id"), ("task_activity", "task_id"), ("tag_links", "target_id")):
        conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (task_id,))
    conn.execute("DELETE FROM chat_messages WHERE channel=?", (f"task:{task_id}",))
    conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    conn.commit()


@app.post("/tasks/{task_id}/delete")
def delete_task(task_id: str):
    conn = get_conn()
    if not db.get_task(conn, task_id):
        raise HTTPException(404)
    _delete_task(conn, task_id)
    return RedirectResponse("/tasks", status_code=303)


@app.post("/tasks/{task_id}/duplicate")
def duplicate_task(request: Request, task_id: str):
    conn = get_conn()
    t = db.get_task(conn, task_id)
    if not t:
        raise HTTPException(404)
    n = Task(id=new_id(), title=f"{t.title}（コピー）", description=t.description, assignee=t.assignee, status="todo",
             due_date=t.due_date, artifacts=list(t.artifacts), priority=t.priority)
    db.save_task(conn, n)
    for r in conn.execute("SELECT text, pos FROM task_checklist WHERE task_id=? ORDER BY pos", (task_id,)).fetchall():
        conn.execute("INSERT INTO task_checklist (id, task_id, text, done, pos) VALUES (?,?,?,0,?)", (new_id(), n.id, r["text"], r["pos"]))
    for tg in tagmod.tags_for(conn, "task", task_id):
        tagmod.link(conn, tg["name"], "task", n.id)
    conn.commit()
    db.task_activity(conn, n.id, get_me(request), "created", f"「{t.title}」を複製")
    return RedirectResponse(f"/tasks/{n.id}", status_code=303)


@app.post("/tasks/{task_id}/checklist")
def checklist_add(request: Request, task_id: str, text: str = Form(...)):
    conn = get_conn()
    if not db.get_task(conn, task_id):
        raise HTTPException(404)
    pos = conn.execute("SELECT COALESCE(MAX(pos),0)+1 FROM task_checklist WHERE task_id=?", (task_id,)).fetchone()[0]
    for line in [x.strip() for x in text.splitlines() if x.strip()]:
        conn.execute("INSERT INTO task_checklist (id, task_id, text, done, pos) VALUES (?,?,?,0,?)", (new_id(), task_id, line, pos)); pos += 1
    conn.commit()
    db.task_activity(conn, task_id, get_me(request), "checklist", f"項目を追加: {text.strip()[:40]}")
    return RedirectResponse(f"/tasks/{task_id}#checklist", status_code=303)


@app.post("/tasks/{task_id}/checklist/{item_id}/{action}")
def checklist_action(request: Request, task_id: str, item_id: str, action: str, text: str = Form("")):
    conn = get_conn()
    if action == "toggle":
        conn.execute("UPDATE task_checklist SET done = 1 - done WHERE id=? AND task_id=?", (item_id, task_id))
    elif action == "delete":
        conn.execute("DELETE FROM task_checklist WHERE id=? AND task_id=?", (item_id, task_id))
    elif action == "edit" and text.strip():
        conn.execute("UPDATE task_checklist SET text=? WHERE id=? AND task_id=?", (text.strip(), item_id, task_id))
    elif action == "to_task":
        r = conn.execute("SELECT text FROM task_checklist WHERE id=?", (item_id,)).fetchone()
        if r:
            parent = db.get_task(conn, task_id)
            n = Task(id=new_id(), title=r["text"], assignee=parent.assignee if parent else None, status="todo",
                     description=f"「{parent.title if parent else task_id}」のチェック項目から作成")
            db.save_task(conn, n)
            db.task_activity(conn, n.id, get_me(request), "created", "チェック項目から作成")
            conn.execute("DELETE FROM task_checklist WHERE id=?", (item_id,)); conn.commit()
            return RedirectResponse(f"/tasks/{n.id}", status_code=303)
    conn.commit()
    if action == "toggle":
        t = db.get_task(conn, task_id)
        if t:
            db.save_task(conn, t)   # 動きがあった
    return RedirectResponse(f"/tasks/{task_id}#checklist", status_code=303)


@app.post("/tasks/{task_id}/watch")
def task_watch(request: Request, task_id: str, user: str = Form("")):
    conn = get_conn()
    user = user.strip() or get_me(request)
    if conn.execute("SELECT 1 FROM task_watchers WHERE task_id=? AND user=?", (task_id, user)).fetchone():
        conn.execute("DELETE FROM task_watchers WHERE task_id=? AND user=?", (task_id, user))
    else:
        conn.execute("INSERT INTO task_watchers (task_id, user) VALUES (?,?)", (task_id, user))
    conn.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=303)


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
        ev = db.row_to_event(r)
        if str(ev.meta.get("channel", "")).startswith("task:"):
            continue   # コメントは詳細ページのコメント欄に出す（二重表示しない）
        elif ev.source == "mail":
            add(ev, label="メール", color="bg-sky-600")
        else:
            add(ev)
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
    all_tags = tagmod.all_tags(conn)
    checklist = [dict(r) for r in conn.execute("SELECT * FROM task_checklist WHERE task_id=? ORDER BY pos", (task_id,))]
    activity = [dict(r) for r in conn.execute("SELECT * FROM task_activity WHERE task_id=? ORDER BY at DESC LIMIT 50", (task_id,))]
    return render("task_detail.html", request, conn, task=task, meta=task_meta(conn, task), checklist=checklist, activity=activity,
                  PRIORITIES=PRIORITIES, open_tasks=[t for t in db.list_tasks(conn) if t.id != task_id],
                  timeline=task_timeline(conn, task), me=get_me(request), comments=task_comments(conn, task_id),
                  all_tags=[t for t in all_tags if t["kind"] != "team"], teams=[t for t in all_tags if t["kind"] == "team"])


class TaskPatch(BaseModel):
    status: str | None = None
    assignee: str | None = None
    title: str | None = None
    description: str | None = None
    due_date: date | None = None
    artifacts: list[str] | None = None
    priority: str | None = None


@app.patch("/tasks/{task_id}")
def patch_task(request: Request, task_id: str, patch: TaskPatch):
    conn = get_conn()
    task = db.get_task(conn, task_id)
    if not task:
        raise HTTPException(404)
    if patch.status is not None and patch.status not in STATUS_LABEL:
        raise HTTPException(422, "invalid status")
    if patch.priority is not None and patch.priority not in PRIORITY_ORDER:
        raise HTTPException(422, "invalid priority")
    me = get_me(request)
    for k, v in patch.model_dump(exclude_unset=True).items():
        if k in ("assignee", "description", "title") and isinstance(v, str):
            v = v.strip() or None
        old = getattr(task, k)
        setattr(task, k, v)
        if old != v and k != "description":
            fmt = lambda x: (STATUS_LABEL.get(x, x) if k == "status" else (PRIORITY_LABEL.get(x, x) if k == "priority" else x))
            db.task_activity(conn, task_id, me, k, f"{fmt(old) or '（なし）'} → {fmt(v) or '（なし）'}")
        elif old != v:
            db.task_activity(conn, task_id, me, k, "説明を更新")
    db.save_task(conn, task)   # updated_at を更新（stalled 検知の起点）
    return {"ok": True, "task": task}


def task_comments(conn: sqlite3.Connection, task_id: str) -> list[dict]:
    """タスク上のコメント（チャンネルには流さない。task:<id> という非表示の会話に置く）"""
    rows = conn.execute("SELECT * FROM chat_messages WHERE channel=? AND deleted=0 ORDER BY posted_at, rowid",
                        (f"task:{task_id}",)).fetchall()
    return [_message_dict(conn, r) for r in rows]


@app.post("/tasks/{task_id}/comment")
def task_comment(task_id: str, request: Request, actor: str = Form(""), text: str = Form(...)):
    """タスクへのコメント。タスク上に残り、Event としてこのタスクに紐付く（チャットには流さない）"""
    conn = get_conn()
    task = db.get_task(conn, task_id)
    if not task:
        raise HTTPException(404)
    actor = who(request, actor)
    mid = chat.post_message(conn, f"task:{task_id}", actor, text.strip())
    for tname in tagmod.apply_hashtags(conn, text, "message", mid):
        tagmod.link(conn, tname, "task", task_id)
    _ingest_chat(conn)
    db.save_link(conn, Link(id=new_id(), from_type="event", from_id=mid, to_type="task", to_id=task_id,
                            relation="discusses", confidence=1.0, method="explicit"))
    db.mark_processed(conn, "link", mid, {"method": "explicit", "task_id": task_id})
    db.save_task(conn, task)   # 動きがあったので updated_at を更新
    resp = RedirectResponse(f"/tasks/{task_id}", status_code=303)
    set_me(resp, actor)
    return resp


# ---------- チャット ----------

_MENTION = re.compile(r"@([^\s@,、。<]+)")
_HASH = re.compile(r"(?<![\w/])#([^\s#@,、。！？!?()（）「」<]{1,24})")


def render_message(text: str, conn: sqlite3.Connection | None = None) -> str:
    html = str(linkify(text))
    team_names = {t["name"] for t in tagmod.teams(conn)} if conn is not None else set()

    def mention(m):
        name = m.group(1)
        if name in PEOPLE or name in ("all", "channel", "全員"):
            return f'<span class="bg-indigo-100 text-indigo-800 rounded px-1 font-medium">@{name}</span>'
        if name in team_names:
            return f'<a href="/tags/{escape(name)}" class="bg-violet-100 text-violet-800 rounded px-1 font-medium">@{name}</a>'
        return m.group(0)
    html = _MENTION.sub(mention, html)
    html = _HASH.sub(lambda m: f'<a href="/tags/{escape(m.group(1))}" class="text-indigo-600 hover:underline">#{m.group(1)}</a>', html)
    return _markdown_lite(html)


def _markdown_lite(html: str) -> str:
    """エスケープ済み HTML に軽量の書式を当てる: ```code```, `code`, **太字**, ~~取消~~, > 引用, - 箇条書き, 改行"""
    html = re.sub(r"```\n?(.*?)```", lambda m: f'<pre class="bg-gray-900 text-gray-100 rounded-lg px-3 py-2 my-1 text-xs overflow-x-auto">{m.group(1).strip()}</pre>', html, flags=re.S)
    html = re.sub(r"`([^`\n]+)`", r'<code class="bg-gray-100 rounded px-1 text-[13px]">\1</code>', html)
    html = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", html)
    html = re.sub(r"~~([^~\n]+)~~", r"<s>\1</s>", html)
    lines, out, in_list = html.split("\n"), [], False
    for ln in lines:
        if ln.startswith("<pre"):
            out.append(ln); continue
        if re.match(r"^[-・] ", ln):
            if not in_list:
                out.append('<ul class="list-disc pl-5 my-0.5">'); in_list = True
            out.append(f"<li>{ln[2:]}</li>"); continue
        if in_list:
            out.append("</ul>"); in_list = False
        if ln.startswith("&gt; "):
            out.append(f'<blockquote class="border-l-2 border-gray-300 pl-2 text-gray-600 my-0.5">{ln[5:]}</blockquote>'); continue
        out.append(ln)
    if in_list:
        out.append("</ul>")
    res = []
    for i, ln in enumerate(out):
        res.append(ln)
        if i < len(out) - 1 and not (ln.startswith(("<ul", "<li", "</ul", "<blockquote", "<pre")) or out[i + 1].startswith(("<ul", "</ul", "<li", "<blockquote"))):
            res.append("<br>")
    return "".join(res)


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
            "html": render_message(r["text"], conn), "reply_to": r["reply_to"], "edited": bool(r["edited_at"]),
            "tags": tagmod.tags_for(conn, "message", r["id"]),
            "mentions": sorted(tagmod.expand_mentions(conn, r["text"], PEOPLE)),
            "attachments": json.loads(r["attachments"]) if r["attachments"] else [],
            "reactions": reactions, "replies": replies, "last_reply": last_reply["posted_at"] if last_reply else None,
            "task": {"id": link["to_id"], "title": link["title"], "method": link["method"],
                     "confidence": link["confidence"]} if link else None,
            "decision": decided["text"] if decided else None,
            "pinned": bool(conn.execute("SELECT 1 FROM chat_pins WHERE message_id=?", (r["id"],)).fetchone())}


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
    from app import linker, rtc
    adapter = chat.ChatAdapter(conn)
    events = adapter.fetch(db.last_synced(conn, adapter.name))
    for ev in events:
        try:
            rtc.notify_chat(str(ev.meta.get("channel", "")), ev.id)
        except Exception:
            pass
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


def _sidebar(conn: sqlite3.Connection, me: str) -> dict:
    counts = {r["channel"]: r["n"] for r in conn.execute("SELECT channel, COUNT(*) n FROM chat_messages WHERE deleted=0 GROUP BY channel")}
    return {"channels": chat.list_channels(conn), "conversations": chat.conversations(conn, me or None),
            "counts": counts, "teams": tagmod.teams(conn)}


def _active_meeting(conn: sqlite3.Connection, channel: str) -> dict | None:
    r = conn.execute("SELECT id, title, status, started_at FROM meetings WHERE channel=? AND status IN ('recording','processing') "
                     "ORDER BY started_at DESC LIMIT 1", (channel,)).fetchone()
    return dict(r) if r else None


def _unread_counts(conn: sqlite3.Connection, me: str) -> dict[str, int]:
    seen = {r["key"][len(f"chat_seen:{me}:"):]: r["value"] for r in conn.execute("SELECT key, value FROM settings WHERE key LIKE ?", (f"chat_seen:{me}:%",))}
    out = {}
    for r in conn.execute("SELECT channel, COUNT(*) n FROM chat_messages WHERE deleted=0 AND actor != ? AND posted_at > COALESCE((SELECT value FROM settings WHERE key = 'chat_seen:' || ? || ':' || channel), '') GROUP BY channel", (me, me)):
        out[r["channel"]] = r["n"]
    return out


@app.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request, channel: str = "general", thread: str = "", q: str = "", view: str = "", info: str = ""):
    conn = get_conn()
    me = get_me(request)
    ch = chat.get_channel(conn, channel)
    unread = _unread_counts(conn, me) if me else {}
    seen_at = conn.execute("SELECT value FROM settings WHERE key=?", (f"chat_seen:{me}:{channel}",)).fetchone() if me else None
    first_unread = None
    if seen_at and unread.get(channel):
        r = conn.execute("SELECT id FROM chat_messages WHERE channel=? AND deleted=0 AND reply_to IS NULL AND actor != ? AND posted_at > ? ORDER BY posted_at LIMIT 1",
                         (channel, me, seen_at["value"])).fetchone()
        first_unread = r["id"] if r else None
    if me:
        db.set_setting(conn, f"chat_seen:{me}:{channel}", db.now_iso())
    results = [_message_dict(conn, r) for r in chat.search_messages(conn, q)] if q else []
    mentions = []
    if view == "mentions" and me:
        for r in conn.execute("SELECT * FROM chat_messages WHERE deleted=0 AND text LIKE '%@%' ORDER BY posted_at DESC LIMIT 100"):
            if me in tagmod.expand_mentions(conn, r["text"], PEOPLE):
                mentions.append(_message_dict(conn, r))
    saved_msgs = [_message_dict(conn, r) for r in chat.saved(conn, me)] if view == "saved" and me else []
    pins = [_message_dict(conn, r) for r in chat.pins(conn, channel)]
    return render("chat.html", request, conn, channel=channel, ch=ch, **_sidebar(conn, me), unread=unread,
                  messages=message_rows(conn, channel), thread=thread_rows(conn, thread) if thread else None,
                  q=q, results=results, me=me, view=view, mentions=mentions, saved_msgs=saved_msgs, pins=pins,
                  saved_ids=chat.saved_ids(conn, me) if me else set(), first_unread=first_unread, info=bool(info),
                  muted=chat.muted(conn, me) if me else set(), files=chat.channel_files(conn, channel) if info or view == "files" else [],
                  stats=chat.channel_stats(conn, channel),
                  meeting=_active_meeting(conn, channel), channel_tags=tagmod.tags_for(conn, "channel", channel))


@app.post("/chat/{msg_id}/pin")
def pin_chat(msg_id: str, request: Request):
    conn = get_conn()
    on = chat.toggle_pin(conn, msg_id, get_me(request))
    return RedirectResponse(request.headers.get("referer") or "/chat", status_code=303)


@app.post("/chat/{msg_id}/save")
def save_chat(msg_id: str, request: Request):
    conn = get_conn()
    chat.toggle_saved(conn, get_me(request), msg_id)
    return RedirectResponse(request.headers.get("referer") or "/chat", status_code=303)


@app.post("/chat/channels/{name}/mute")
def mute_channel(name: str, request: Request):
    conn = get_conn()
    chat.toggle_mute(conn, get_me(request), name)
    return RedirectResponse(f"/chat?channel={name}&info=1", status_code=303)


@app.post("/chat/channels/{name}/update")
def update_channel(name: str, request: Request, description: str = Form(None), title: str = Form(None), members: list[str] = Form(None)):
    conn = get_conn()
    ch = chat.get_channel(conn, name)
    chat.ensure_channel(conn, name) if ch["kind"] == "channel" else None
    chat.update_channel(conn, name, description=description, title=title if ch["kind"] != "channel" else None,
                        members=list(members) if members is not None and ch["kind"] in ("group", "team") else None)
    return RedirectResponse(f"/chat?channel={name}&info=1", status_code=303)


@app.post("/chat/channels/{name}/leave")
def leave_channel(name: str, request: Request):
    conn = get_conn()
    me = get_me(request)
    ch = chat.get_channel(conn, name)
    if ch["kind"] in ("group", "dm") and me in ch["members"]:
        rest = [m for m in ch["members"] if m != me]
        chat.update_channel(conn, name, members=rest)
        chat.post_message(conn, name, "システム", f"{me} が退出しました")
    return RedirectResponse("/chat", status_code=303)


@app.post("/chat/upload")
async def chat_upload(request: Request, channel: str = Form("general"), file: UploadFile = File(...), text: str = Form(""),
                      reply_to: str = Form("")):
    """ファイルをそのまま投稿。成果物ライブラリの「チャット添付/<チャンネル>」に版として保存し、リンク付きで発言する"""
    actor = who(request)
    name = Path(file.filename or "upload.bin").name
    conn = get_conn()
    ch = chat.get_channel(conn, channel)
    folder = "チャット添付/" + re.sub(r"[^\w\-ぁ-んァ-ン一-龥ー@]", "_", ch["title"] if ch["kind"] != "channel" else channel)
    rel_path = f"{folder}/{name}"
    root = LIB()
    try:
        dest = _register_version(conn, rel_path, await file.read(), actor, "", "")
        versions = json.loads((root / "versions.json").read_text(encoding="utf-8"))
        url = versions[str(dest.relative_to(root))]["url"]
    except HTTPException:   # 同じ内容 → 既存の最新版のリンクを使う
        versions = json.loads((root / "versions.json").read_text(encoding="utf-8"))
        stem = Path(name).stem
        cands = sorted(k for k in versions if k.startswith(folder + "/") and Path(k).stem.rsplit("_v", 1)[0] == stem)
        url = versions[cands[-1]]["url"] if cands else f"{config.APP_BASE_URL}/artifacts?path={folder}"
    mid = chat.post_message(conn, channel, actor, f"{text.strip()} {url}".strip(), reply_to=reply_to.strip() or None,
                            attachments=[{"url": url, "name": name}])
    _ingest_chat(conn)
    from app import agent, rtc
    rtc.notify_chat(channel, mid)
    agent.request_tick(f"チャットにファイル投稿（#{channel} {actor}）")
    return RedirectResponse(f"/chat?channel={channel}" + (f"&thread={reply_to}" if reply_to else ""), status_code=303)


@app.get("/chat/team/{name}")
def team_chat(request: Request, name: str):
    """チームのチャット（メンバー全員のグループ）を開く。無ければ作る"""
    conn = get_conn()
    t = tagmod.get_tag(conn, name)
    if not t or t["kind"] != "team":
        raise HTTPException(404)
    ch = chat.ensure_conversation(conn, t["members"], title=f"@{t['name']}", name=f"team-{t['id']}", kind="team")
    tagmod.link(conn, t["name"], "channel", ch)
    return RedirectResponse(f"/chat?channel={ch}", status_code=303)


@app.get("/chat/dm/{name}")
def open_dm(request: Request, name: str):
    conn = get_conn()
    me = who(request)
    if name == me:
        return RedirectResponse("/chat", status_code=303)
    ch = chat.ensure_conversation(conn, [me, name])
    return RedirectResponse(f"/chat?channel={ch}", status_code=303)


@app.post("/chat/conversations")
def create_conversation(request: Request, members: list[str] = Form(...), title: str = Form(""), me: str = Form("")):
    conn = get_conn()
    me = who(request, me)
    people = list(members) + [me]
    try:
        name = chat.ensure_conversation(conn, people, title.strip() or None)
    except ValueError as e:
        raise HTTPException(422, str(e))
    resp = RedirectResponse(f"/chat?channel={name}", status_code=303)
    return set_me(resp, me) if me.strip() else resp


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
def post_chat(request: Request, channel: str = Form("general"), actor: str = Form(""), text: str = Form(...),
              reply_to: str = Form(""), attachment_url: str = Form(""), attachment_name: str = Form("")):
    conn = get_conn()
    actor = who(request, actor)
    attachments = None
    if attachment_url.strip():
        url = attachment_url.strip()
        name = attachment_name.strip() or url.rstrip("/").rsplit("/", 1)[-1]
        attachments = [{"url": url, "name": name}]
        if url not in text:
            text = f"{text.strip()} {url}".strip()   # URL を本文にも入れる（成果物の自動登録が効く）
    mid = chat.post_message(conn, channel.strip() or "general", actor, text.strip(),
                            reply_to=reply_to.strip() or None, attachments=attachments)
    for tname in tagmod.apply_hashtags(conn, text, "message", mid):
        for a in (attachments or []):
            tagmod.link(conn, tname, "artifact", a["name"])
    _ingest_chat(conn)
    from app import agent
    agent.request_tick(f"チャット投稿（#{channel.strip() or 'general'} {actor}）")
    linked = conn.execute("SELECT to_id FROM links WHERE from_type='event' AND from_id=? AND to_type='task' LIMIT 1", (mid,)).fetchone()
    if linked:
        for tname in tagmod.hashtags(text):
            tagmod.link(conn, tname, "task", linked["to_id"])
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


@app.post("/api/chat/{msg_id}/to_task")
def message_to_task(request: Request, msg_id: str):
    """発言からタスクを作る（本文をタイトル、発言者を担当、発言を紐付け）"""
    from app import linker
    conn = get_conn()
    m = conn.execute("SELECT * FROM chat_messages WHERE id=?", (msg_id,)).fetchone()
    if not m:
        raise HTTPException(404)
    if not db.get_event(conn, msg_id):
        _ingest_chat(conn)
    title = re.sub(r"https?://\S+", "", m["text"]).strip()[:80] or "無題のタスク"
    task = Task(id=new_id(), title=title, assignee=m["actor"], status="todo")
    db.save_task(conn, task)
    conn.execute("DELETE FROM links WHERE from_type='event' AND from_id=? AND to_type='task' AND relation='discusses'", (msg_id,))
    db.save_link(conn, Link(id=new_id(), from_type="event", from_id=msg_id, to_type="task", to_id=task.id,
                            relation="implements", confidence=1.0, method="manual"))
    db.mark_processed(conn, "link", msg_id, {"method": "manual", "task_id": task.id})
    ev = db.get_event(conn, msg_id)
    if ev:
        linker.register_artifacts(conn, task.id, ev)
        for tname in tagmod.hashtags(ev.text):
            tagmod.link(conn, tname, "task", task.id)
    return {"ok": True, "task_id": task.id}


class LinkBody(BaseModel):
    task_id: str | None = None   # None なら紐付けを外す


@app.post("/api/chat/{msg_id}/link")
def link_chat_message(msg_id: str, body: LinkBody):
    """発言をタスクに人力で紐付ける／外す。エージェントの判定より人の指定を優先し、以後は再判定しない"""
    from app import linker
    conn = get_conn()
    m = conn.execute("SELECT * FROM chat_messages WHERE id=?", (msg_id,)).fetchone()
    if not m:
        raise HTTPException(404)
    if not db.get_event(conn, msg_id):
        _ingest_chat(conn)
    conn.execute("DELETE FROM links WHERE from_type='event' AND from_id=? AND to_type='task' AND relation='discusses'", (msg_id,))
    conn.commit()
    if body.task_id:
        task = db.get_task(conn, body.task_id)
        if not task:
            raise HTTPException(404, "task not found")
        db.save_link(conn, Link(id=new_id(), from_type="event", from_id=msg_id, to_type="task", to_id=task.id,
                                relation="discusses", confidence=1.0, method="manual"))
        ev = db.get_event(conn, msg_id)
        if ev:
            linker.register_artifacts(conn, task.id, ev)
            for tname in tagmod.hashtags(ev.text):
                tagmod.link(conn, tname, "task", task.id)
        db.save_task(conn, task)   # 動きがあったので updated_at を更新
    db.mark_processed(conn, "link", msg_id, {"method": "manual", "task_id": body.task_id})
    return {"ok": True, "task_id": body.task_id}


@app.get("/api/tasks/open")
def open_tasks_api(q: str = ""):
    conn = get_conn()
    out = [{"id": t.id, "title": t.title, "assignee": t.assignee, "status": t.status}
           for t in db.list_tasks(conn) if t.status != "done" and (not q or q in t.title)]
    return {"tasks": out[:50]}


@app.post("/chat/channels")
def create_channel(name: str = Form(...), description: str = Form("")):
    conn = get_conn()
    name = re.sub(r"[^\w\-]", "", name.strip().lstrip("#"))[:32] or "general"
    chat.ensure_channel(conn, name, description.strip() or None)
    return RedirectResponse(f"/chat?channel={name}", status_code=303)


# ---------- メール（Outlook の代わり） ----------

def _ingest_mail(conn: sqlite3.Connection) -> None:
    from app import linker
    adapter = mailmod.MailAdapter(conn)
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


def _mail_meta(conn: sqlite3.Connection, m: dict) -> dict:
    link = conn.execute(
        "SELECT l.to_id, l.method, t.title FROM links l JOIN tasks t ON t.id = l.to_id "
        "WHERE l.from_type='event' AND l.from_id=? AND l.to_type='task' AND l.relation='discusses' LIMIT 1", (m["id"],)).fetchone()
    dec = conn.execute("SELECT text FROM events WHERE kind IN ('decision','task_hint') AND source='mail' AND json_extract(meta,'$.message_id')=?",
                       (m["id"],)).fetchall()
    return {**m, "html": render_message(m["body"], conn).replace("\n", "<br>"),
            "task": {"id": link["to_id"], "title": link["title"], "method": link["method"]} if link else None,
            "extracted": [d["text"] for d in dec]}


@app.get("/mail", response_class=HTMLResponse)
def mail_page(request: Request, folder: str = "inbox", thread: str = "", q: str = "", draft: str = ""):
    conn = get_conn()
    me = get_me(request)
    threads = [] if folder == "drafts" else mailmod.inbox(conn, me or None, folder, q=q.strip())
    msgs, state, thread_tasks = [], {}, []
    if thread:
        mailmod.mark_read(conn, thread, me)
        msgs = [_mail_meta(conn, m) for m in mailmod.thread(conn, thread)]
        state = mailmod.states(conn, me).get(thread, {})
        thread_tasks = [dict(r) for r in conn.execute(
            "SELECT DISTINCT t.id, t.title, t.status FROM links l JOIN tasks t ON t.id = l.to_id "
            "WHERE l.from_type='event' AND l.to_type='task' AND l.from_id IN (SELECT id FROM mails WHERE thread_id=?)", (thread,))]
    draft_obj = next((d for d in mailmod.drafts(conn, me) if d["id"] == draft), None) if draft else None
    return render("mail.html", request, conn, folder=folder, threads=threads, thread=thread, msgs=msgs, me=me, q=q,
                  state=state, thread_tasks=thread_tasks, folders=mailmod.FOLDERS, counts=mailmod.folder_counts(conn, me or None),
                  drafts=mailmod.drafts(conn, me) if folder == "drafts" else [], draft_obj=draft_obj,
                  open_tasks=[dict(r) for r in conn.execute("SELECT id, title FROM tasks WHERE status != 'done' ORDER BY created_at DESC")],
                  unread=sum(t["unread"] for t in mailmod.inbox(conn, me or None, "inbox")))


@app.post("/mail/thread/{thread_id}/link_task")
def mail_link_task(request: Request, thread_id: str, task_id: str = Form(""), title: str = Form("")):
    """スレッドをタスクに紐付け（既存タスク or 新規作成）。手動リンク=manual"""
    from app import linker
    conn = get_conn()
    me = get_me(request)
    msgs = mailmod.thread(conn, thread_id)
    if not msgs:
        return RedirectResponse("/mail", status_code=303)
    if not task_id and title.strip():
        t = Task(id=new_id(), title=title.strip(), assignee=me or None, status="todo", created_from=msgs[0]["id"],
                 description=f"メール「{msgs[0]['subject']}」から作成")
        db.save_task(conn, t)
        task_id = t.id
    if task_id:
        for m in msgs:
            ev = db.get_event(conn, m["id"])
            if ev and not conn.execute("SELECT 1 FROM links WHERE from_id=? AND to_id=?", (m["id"], task_id)).fetchone():
                db.save_link(conn, Link(id=new_id(), from_type="event", from_id=m["id"], to_type="task", to_id=task_id,
                                        relation="discusses", confidence=1.0, method="manual"))
                linker.register_artifacts(conn, task_id, ev)
    return RedirectResponse(f"/mail?thread={thread_id}", status_code=303)


@app.post("/mail/thread/{thread_id}/{action}")
def mail_thread_action(request: Request, thread_id: str, action: str, folder: str = Form("inbox")):
    conn = get_conn()
    me = get_me(request)
    if action in ("flag", "archive", "trash"):
        key = {"flag": "flagged", "archive": "archived", "trash": "trashed"}[action]
        mailmod.toggle_state(conn, thread_id, me, key)
    elif action == "unread":
        mailmod.set_state(conn, thread_id, me, unread=True)
        return RedirectResponse(f"/mail?folder={folder}", status_code=303)
    elif action == "restore":
        mailmod.set_state(conn, thread_id, me, trashed=False, archived=False)
        return RedirectResponse(f"/mail?folder=inbox&thread={thread_id}", status_code=303)
    return RedirectResponse(f"/mail?folder={folder}&thread={thread_id}", status_code=303)


@app.post("/mail/draft")
def mail_draft(request: Request, recipients: list[str] = Form([]), cc: list[str] = Form([]), subject: str = Form(""),
               body: str = Form(""), thread_id: str = Form(""), draft_id: str = Form("")):
    conn = get_conn()
    me = get_me(request)
    did = mailmod.save_draft(conn, me, list(recipients), list(cc), subject.strip(), body, thread_id.strip(), draft_id.strip())
    return RedirectResponse(f"/mail?folder=drafts&draft={did}", status_code=303)


@app.post("/mail/draft/{draft_id}/delete")
def mail_draft_delete(draft_id: str):
    mailmod.delete_draft(get_conn(), draft_id)
    return RedirectResponse("/mail?folder=drafts", status_code=303)


@app.post("/mail/send")
def mail_send(request: Request, sender: str = Form(""), recipients: list[str] = Form(...), cc: list[str] = Form([]),
              subject: str = Form(...), body: str = Form(...), thread_id: str = Form(""),
              attachment_url: str = Form(""), attachment_name: str = Form(""), draft_id: str = Form("")):
    conn = get_conn()
    sender = who(request, sender)
    if draft_id.strip():
        mailmod.delete_draft(conn, draft_id.strip())
    att = None
    if attachment_url.strip():
        att = [{"url": attachment_url.strip(), "name": attachment_name.strip() or attachment_url.strip().rsplit("/", 1)[-1]}]
        if attachment_url.strip() not in body:
            body = f"{body.rstrip()}\n{attachment_url.strip()}"
    tid = mailmod.send(conn, sender, list(recipients), subject.strip(), body.strip(), cc=list(cc),
                       thread_id=thread_id.strip() or None, attachments=att)
    _ingest_mail(conn)
    from app import agent
    agent.request_tick(f"メール送信（{sender}: {subject.strip()[:20]}）")
    resp = RedirectResponse(f"/mail?folder=sent&thread={thread_id.strip() or tid}", status_code=303)
    return set_me(resp, sender)


# ---------- 予定表（Outlook 予定表の代わり） ----------

@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, view: str = "week", d: str = "", person: str = ""):
    from datetime import timedelta
    conn = get_conn()
    base = date.fromisoformat(d) if d else date.today()
    if view == "month":
        first = base.replace(day=1)
        start = first - timedelta(days=first.weekday())
        end = start + timedelta(days=42)
    elif view == "day":
        start, end = base, base + timedelta(days=1)
    elif view == "agenda":
        start, end = base, base + timedelta(days=30)
    else:
        start = base - timedelta(days=base.weekday())
        end = start + timedelta(days=7)
    evs = cal.between(conn, datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.min.time()))
    if person:
        evs = [e for e in evs if person in e["attendees"] or e.get("organizer") == person]
    for e in evs:
        m = conn.execute("SELECT status FROM meetings WHERE id=?", (e["meeting_id"],)).fetchone() if e.get("meeting_id") else None
        fx = conn.execute("SELECT 1 FROM events WHERE source='meet' AND json_extract(meta,'$.meeting_id')=? LIMIT 1", (e["meeting_id"],)).fetchone() if e.get("meeting_id") else None
        e["meeting_status"] = m["status"] if m else ("done" if fx else None)
        e["n_decisions"] = conn.execute("SELECT COUNT(*) FROM events WHERE kind='decision' AND json_extract(meta,'$.meeting_id')=?", (e["meeting_id"],)).fetchone()[0] if e.get("meeting_id") else 0
    days = []
    for i in range((end - start).days):
        day = start + timedelta(days=i)
        day_evs = [e for e in evs if e["start"].date() <= day <= e["end"].date()]
        due = [t for t in db.list_tasks(conn) if t.due_date == day and (not person or t.assignee == person)]
        days.append({"date": day, "events": day_evs, "due": due, "today": day == date.today(), "in_month": day.month == base.month})
    step = {"week": timedelta(days=7), "day": timedelta(days=1), "agenda": timedelta(days=30)}.get(view)
    prev = (base - step) if step else (base.replace(day=1) - timedelta(days=1))
    nxt = (base + step) if step else (base.replace(day=28) + timedelta(days=4))
    now = datetime.now()
    return render("calendar.html", request, conn, view=view, base=base, days=days, prev=prev.isoformat(), nxt=nxt.isoformat(),
                  me=get_me(request), channels=chat.list_channels(conn), today=date.today().isoformat(), person=person,
                  hours=list(range(8, 20)), now_min=(now.hour - 8) * 60 + now.minute)


@app.post("/calendar")
def calendar_create(request: Request, title: str = Form(...), date_: str = Form(..., alias="date"), start: str = Form(...),
                    end: str = Form(...), attendees: list[str] = Form([]), location: str = Form(""),
                    description: str = Form(""), channel: str = Form(""), organizer: str = Form(""),
                    with_meeting: str = Form(""), repeat: str = Form("none"), count: int = Form(4), all_day: str = Form("")):
    conn = get_conn()
    if all_day:
        start, end = "00:00", "23:59"
    st = datetime.fromisoformat(f"{date_}T{start}")
    en = datetime.fromisoformat(f"{date_}T{end}")
    if en <= st:
        en = st + __import__("datetime").timedelta(minutes=30)
    who_ = who(request, organizer)
    kw = dict(title=title.strip(), attendees=list(attendees), location=location.strip() or None,
              description=description.strip() or None, channel=channel.strip() or None, organizer=who_,
              kind="meeting" if with_meeting and not all_day else "appointment", all_day=bool(all_day))
    if repeat in ("daily", "weekly", "biweekly", "monthly"):
        ids = cal.create_series(conn, repeat, count, start_at=st, end_at=en, **kw)
    else:
        ids = [cal.create(conn, start_at=st, end_at=en, **kw)]
    if channel.strip():
        rep = {"daily": "毎日", "weekly": "毎週", "biweekly": "隔週", "monthly": "毎月"}.get(repeat)
        chat.post_message(conn, channel.strip(), who_,
                          f"📅 予定を追加しました: {title.strip()} {st:%m/%d %H:%M}〜{en:%H:%M}{'（' + rep + '×' + str(len(ids)) + '回）' if rep else ''} 参加: {'・'.join(attendees)}")
        _ingest_chat(conn)
    return RedirectResponse(f"/calendar?view=week&d={date_}", status_code=303)


@app.post("/calendar/{event_id}/update")
def calendar_update(request: Request, event_id: str, title: str = Form(...), date_: str = Form(..., alias="date"), start: str = Form(...),
                    end: str = Form(...), attendees: list[str] = Form([]), location: str = Form(""), description: str = Form(""),
                    channel: str = Form(""), with_meeting: str = Form(""), scope: str = Form("one")):
    conn = get_conn()
    e = cal.get(conn, event_id)
    if not e:
        raise HTTPException(404)
    st = datetime.fromisoformat(f"{date_}T{start}")
    en = datetime.fromisoformat(f"{date_}T{end}")
    if en <= st:
        en = st + __import__("datetime").timedelta(minutes=30)
    targets = [e]
    if scope == "series" and e.get("series_id"):
        targets = [cal._row(r) for r in conn.execute("SELECT * FROM cal_events WHERE series_id=? AND start_at >= ?", (e["series_id"], e["start_at"]))]
    delta = st - e["start"]
    for t in targets:
        cal.update(conn, t["id"], title=title.strip(), start_at=db.to_iso(t["start"] + delta), end_at=db.to_iso(t["start"] + delta + (en - st)),
                   attendees=list(attendees), location=location.strip() or None, description=description.strip() or None,
                   channel=channel.strip() or None, kind="meeting" if with_meeting else "appointment")
    return RedirectResponse(f"/calendar/{event_id}", status_code=303)


class CalMove(BaseModel):
    start_at: str
    end_at: str


@app.patch("/api/calendar/{event_id}")
def calendar_move(event_id: str, body: CalMove):
    """ドラッグで時間を動かす"""
    conn = get_conn()
    if not cal.get(conn, event_id):
        raise HTTPException(404)
    cal.update(conn, event_id, start_at=body.start_at, end_at=body.end_at)
    return {"ok": True}


@app.get("/api/calendar/busy")
def calendar_busy(date_: str = Query("", alias="date"), start: str = "", end: str = "", people: str = "", exclude: str = ""):
    conn = get_conn()
    if not (date_ and start and end):
        return {"busy": []}
    st, en = datetime.fromisoformat(f"{date_}T{start}"), datetime.fromisoformat(f"{date_}T{end}")
    rows = cal.busy(conn, st, en, [p for p in people.split(",") if p], exclude)
    return {"busy": [{"title": r["title"], "people": r["people"], "start": r["start"].strftime("%H:%M"), "end": r["end"].strftime("%H:%M")} for r in rows]}


@app.post("/calendar/{event_id}/rsvp")
def calendar_rsvp(request: Request, event_id: str, answer: str = Form(...)):
    conn = get_conn()
    if answer in ("accepted", "declined", "tentative"):
        cal.set_rsvp(conn, event_id, get_me(request), answer)
    return RedirectResponse(f"/calendar/{event_id}", status_code=303)


@app.get("/calendar/{event_id}", response_class=HTMLResponse)
def calendar_detail(request: Request, event_id: str):
    conn = get_conn()
    e = cal.get(conn, event_id)
    if not e:
        raise HTTPException(404)
    meeting = None
    if e.get("meeting_id"):
        m = conn.execute("SELECT * FROM meetings WHERE id=?", (e["meeting_id"],)).fetchone()
        meeting = dict(m) if m else {"id": e["meeting_id"], "status": "done", "title": e["title"], "fixture": True}
    extracted = meeting_extracted(conn, e["meeting_id"]) if e.get("meeting_id") else []
    series = [cal._row(r) for r in conn.execute("SELECT * FROM cal_events WHERE series_id=? ORDER BY start_at", (e["series_id"],))] if e.get("series_id") else []
    return render("calendar_detail.html", request, conn, e=e, meeting=meeting, extracted=extracted, me=get_me(request),
                  channels=chat.list_channels(conn), series=series)


@app.post("/calendar/{event_id}/start")
def calendar_start_meeting(request: Request, event_id: str, me: str = Form("")):
    """予定から会議室を開く（既にあればそれに参加）"""
    conn = get_conn()
    e = cal.get(conn, event_id)
    if not e:
        raise HTTPException(404)
    who_ = who(request, me)
    mid = e.get("meeting_id")
    if not mid or not conn.execute("SELECT 1 FROM meetings WHERE id=?", (mid,)).fetchone():
        mid = meet.create_meeting(conn, e["title"], channel=e.get("channel"))
        cal.update(conn, event_id, meeting_id=mid)
        if e.get("channel"):
            chat.post_message(conn, e["channel"], who_, f"📹 「{e['title']}」を始めました。参加: {config.APP_BASE_URL}/meet/{mid}")
            _ingest_chat(conn)
    meet.join(conn, mid, who_)
    return RedirectResponse(f"/meet/{mid}", status_code=303)


@app.post("/calendar/{event_id}/delete")
def calendar_delete(event_id: str, scope: str = Form("one")):
    conn = get_conn()
    e = cal.get(conn, event_id)
    if e and scope == "series" and e.get("series_id"):
        conn.execute("DELETE FROM cal_events WHERE series_id=? AND start_at >= ?", (e["series_id"], e["start_at"])); conn.commit()
    else:
        cal.delete(conn, event_id)
    return RedirectResponse("/calendar", status_code=303)


# ---------- 会議室 ----------

def meeting_rows(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM meetings ORDER BY started_at DESC").fetchall():
        ps = [p[0] for p in conn.execute(
            "SELECT name FROM meeting_participants WHERE meeting_id=? ORDER BY joined_at", (r["id"],))]
        n_dec = conn.execute("SELECT COUNT(*) FROM events WHERE kind='decision' AND json_extract(meta,'$.meeting_id')=?", (r["id"],)).fetchone()[0]
        st, en = db.from_iso(r["started_at"]), (db.from_iso(r["ended_at"]) if r["ended_at"] else None)
        out.append({**dict(r), "participants": ps, "n_decisions": n_dec,
                    "duration": int((en - st).total_seconds() // 60) if en and en > st else None,
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
def meet_page(request: Request, q: str = "", status: str = ""):
    from datetime import timedelta
    conn = get_conn()
    meetings, fixtures = meeting_rows(conn), fixture_meetings(conn)
    if q:
        ql = q.lower()
        meetings = [m for m in meetings if ql in m["title"].lower() or any(ql in p.lower() for p in m["participants"])]
        fixtures = [m for m in fixtures if ql in (m["title"] or "").lower()]
    if status:
        meetings = [m for m in meetings if m["status"] == status]
        if status != "done":
            fixtures = []
    now = datetime.now()
    upcoming = [e for e in cal.between(conn, now, now + timedelta(days=14)) if e["kind"] != "appointment" and not e.get("meeting_id")]
    return render("meet.html", request, conn, meetings=meetings, fixtures=fixtures, q=q, status=status, upcoming=upcoming[:8],
                  live=[m for m in meeting_rows(conn) if m["status"] == "recording"], me=get_me(request))


@app.post("/meet")
def create_meeting(request: Request, title: str = Form("定例会議"), me: str = Form(""), channel: str = Form("")):
    conn = get_conn()
    me = who(request, me)
    mid = meet.create_meeting(conn, title, channel=channel.strip() or None)
    meet.join(conn, mid, me)
    if channel.strip():
        chat.post_message(conn, channel.strip(), me,
                          f"📹 会議「{title}」を始めました。参加: {config.APP_BASE_URL}/meet/{mid}")
        _ingest_chat(conn)
    resp = RedirectResponse(f"/meet/{mid}", status_code=303)
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
    me = me.strip() or get_me(request)
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
    return render("meet_room.html", request, conn, m=m, me=me, notes=meet.get_notes(conn, meeting_id),
                  chat_log=meet.chat_log(conn, meeting_id) if not m.get("readonly") else [],
                  tracks=meet.tracks(conn, meeting_id) if not m.get("readonly") else [],
                  participants=participants, lines=lines, extracted=meeting_extracted(conn, meeting_id))


@app.post("/meet/{meeting_id}/notes")
async def meet_notes(request: Request, meeting_id: str):
    """会議中の共有メモ（自動保存）"""
    conn = get_conn()
    body = await request.json()
    meet.save_notes(conn, meeting_id, str(body.get("body", ""))[:20000], who(request))
    return {"ok": True}


@app.get("/meet/{meeting_id}/notes")
def meet_notes_get(meeting_id: str):
    return meet.get_notes(get_conn(), meeting_id)


@app.get("/meet/{meeting_id}/track/{track_id}")
def meet_track(meeting_id: str, track_id: str):
    """話者ごとの録音を再生用に返す"""
    from fastapi.responses import Response
    conn = get_conn()
    r = conn.execute("SELECT mime, audio FROM meeting_tracks WHERE id=? AND meeting_id=?", (track_id, meeting_id)).fetchone()
    if not r:
        raise HTTPException(404)
    return Response(content=r["audio"], media_type=r["mime"] or "audio/webm")


@app.post("/meet/{meeting_id}/delete")
def meet_delete(meeting_id: str):
    conn = get_conn()
    if not conn.execute("SELECT 1 FROM meetings WHERE id=?", (meeting_id,)).fetchone():
        raise HTTPException(404)
    for tbl in ("meeting_tracks", "meeting_participants", "meeting_notes", "meeting_chat"):
        conn.execute(f"DELETE FROM {tbl} WHERE meeting_id=?", (meeting_id,))
    conn.execute("DELETE FROM meeting_summaries WHERE meeting_id=?", (meeting_id,))
    conn.execute("UPDATE cal_events SET meeting_id=NULL WHERE meeting_id=?", (meeting_id,))
    conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
    conn.commit()
    return RedirectResponse("/meet", status_code=303)


def _meeting_source(conn: sqlite3.Connection, meeting_id: str) -> tuple[dict, list[dict], str] | None:
    """会議のヘッダ・発言行・文字起こし全文（自作 Meet でも fixtures でも同じ形に）"""
    m = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if m and m["transcript"]:
        lines = []
        for raw in m["transcript"].splitlines():
            mm = re.match(r"^\[(\d{1,2}:\d{2})\]\s*([^:：]+)[:：]\s*(.*)$", raw)
            if mm:
                lines.append({"time": mm[1], "actor": mm[2].strip(), "text": mm[3]})
        return {"id": meeting_id, "title": m["title"], "started_at": m["started_at"], "ended_at": m["ended_at"]}, lines, m["transcript"]
    utts = conn.execute(
        "SELECT * FROM events WHERE source='meet' AND kind='utterance' AND json_extract(meta,'$.meeting_id')=? "
        "ORDER BY json_extract(meta,'$.line')", (meeting_id,)).fetchall()
    if not utts:
        return None
    first = db.row_to_event(utts[0])
    lines = [{"time": fmt_dt(db.from_iso(u["occurred_at"]), False), "actor": u["actor"], "text": u["text"]} for u in utts]
    transcript = first.meta.get("transcript") or "\n".join(f"[{l['time']}] {l['actor']}: {l['text']}" for l in lines)
    return {"id": meeting_id, "title": first.meta.get("meeting_title", meeting_id),
            "started_at": first.meta.get("meeting_at") or first.occurred_at.isoformat(),
            "ended_at": utts[-1]["occurred_at"]}, lines, transcript


@app.get("/meet/{meeting_id}/minutes", response_class=HTMLResponse)
def meeting_minutes(request: Request, meeting_id: str, regen: str = ""):
    """1枚の議事録サマリー"""
    from app import minutes as minutes_mod
    conn = get_conn()
    src = _meeting_source(conn, meeting_id)
    if not src:
        raise HTTPException(404)
    head, lines, transcript = src
    summary = minutes_mod.summarize(conn, meeting_id, transcript, force=bool(regen))
    extracted = meeting_extracted(conn, meeting_id)
    decisions = [e for e in extracted if e.kind == "decision"]
    hints = [e for e in extracted if e.kind == "task_hint"]
    todos = []
    for h in hints:
        t = conn.execute("SELECT t.* FROM tasks t WHERE t.created_from=?", (h.id,)).fetchone()
        if not t:
            l = conn.execute("SELECT to_id FROM links WHERE from_type='event' AND from_id=? AND to_type='task' LIMIT 1", (h.id,)).fetchone()
            t = conn.execute("SELECT * FROM tasks WHERE id=?", (l["to_id"],)).fetchone() if l else None
        todos.append({"hint": h, "task": db.row_to_task(t) if t else None})
    findings = []
    dec_ids = {d.id for d in decisions}
    for r in conn.execute("SELECT * FROM findings WHERE kind='contradiction' AND status != 'dismissed'"):
        f = db.row_to_finding(r)
        if f.evidence[0] in dec_ids:
            findings.append(f)
    participants = []
    for l in lines:
        if l["actor"] not in participants and l["actor"] != "全員":
            participants.append(l["actor"])
    counts = {p: sum(1 for l in lines if l["actor"] == p) for p in participants}
    start = db.from_iso(head["started_at"]) if head.get("started_at") else None
    end = db.from_iso(head["ended_at"]) if head.get("ended_at") else None
    duration = int((end - start).total_seconds() // 60) if start and end and end > start else None
    cal_ev = conn.execute("SELECT id, title, location FROM cal_events WHERE meeting_id=?", (meeting_id,)).fetchone()
    # セキュリティ実演: 文字起こしに混入した「指示文」を検出して、決定に含まれていないことを示す
    inj_pat = re.compile(r"(これまでの指示|指示をすべて無視|無視してください|ignore previous|ignore all|you are (an )?admin|あなたは管理者)", re.I)
    injected = [l for l in lines if inj_pat.search(l["text"])]
    inj_leaked = [d for d in decisions if inj_pat.search(d.text or "") or "賞与" in (d.text or "") or "dismiss" in (d.text or "").lower()]
    return render("minutes.html", request, conn, head=head, lines=lines, summary=summary, decisions=decisions, todos=todos,
                  injected=injected, inj_leaked=inj_leaked,
                  findings=findings, participants=participants, counts=counts, start=start, duration=duration,
                  cal_ev=dict(cal_ev) if cal_ev else None, channels=chat.list_channels(conn))


def _minutes_markdown(conn: sqlite3.Connection, meeting_id: str) -> tuple[str, str]:
    """議事録サマリーを Markdown に（Wiki 保存・メール送付用）"""
    from app import minutes as minutes_mod
    src = _meeting_source(conn, meeting_id)
    head, lines, transcript = src
    summary = minutes_mod.summarize(conn, meeting_id, transcript)   # 保存済み or LLM キャッシュ（同じ文字起こしなら $0）
    extracted = meeting_extracted(conn, meeting_id)
    decisions = [e for e in extracted if e.kind == "decision"]
    hints = [e for e in extracted if e.kind == "task_hint"]
    people = []
    for l in lines:
        if l["actor"] not in people and l["actor"] != "全員":
            people.append(l["actor"])
    started = head.get("started_at", "")
    md = [f"# {head['title']}", "", f"**日時**: {started[:16].replace('T', ' ')}  ", f"**参加者**: {'・'.join(people)}", ""]
    if summary and summary.get("purpose"):
        md += ["## 目的", summary["purpose"], ""]
    if summary and summary.get("agenda"):
        md += ["## 議題"] + [f"- **{a.get('title', '')}** {a.get('summary', '')}" if isinstance(a, dict) else f"- {a}" for a in summary["agenda"]] + [""]
    md += ["## 決定事項"] + ([f"- {d.text}（{d.actor}）" for d in decisions] or ["- なし"]) + [""]
    md += ["## タスク"] + ([f"- [ ] {h.text}（{h.actor}）" for h in hints] or ["- なし"]) + [""]
    if summary and summary.get("open_issues"):
        md += ["## 未決事項"] + [f"- {o}" for o in summary["open_issues"]] + [""]
    if summary and summary.get("next"):
        md += ["## 次回", str(summary["next"]), ""]
    notes = meet.get_notes(conn, meeting_id)["body"].strip()
    if notes:
        md += ["## 会議中の共有メモ", notes, ""]
    md += ["---", f"全文: {config.APP_BASE_URL}/meet/{meeting_id}"]
    return head["title"], "\n".join(md)


@app.post("/meet/{meeting_id}/minutes/to_wiki")
def minutes_to_wiki(request: Request, meeting_id: str, space: str = Form("議事録")):
    conn = get_conn()
    if not _meeting_source(conn, meeting_id):
        raise HTTPException(404)
    title, md = _minutes_markdown(conn, meeting_id)
    me = who(request)
    head = _meeting_source(conn, meeting_id)[0]
    page_title = f"{(head.get('started_at') or '')[:10]} {title}".strip()
    existing = conn.execute("SELECT id FROM wiki_pages WHERE title=?", (page_title,)).fetchone()
    if existing:
        wikimod.update(conn, existing["id"], page_title, md, me, note="議事録から再生成")
        pid = existing["id"]
    else:
        pid = wikimod.create(conn, page_title, md, me, space=space.strip() or "議事録")
    return RedirectResponse(f"/wiki/{pid}", status_code=303)


@app.post("/meet/{meeting_id}/minutes/mail")
def minutes_mail(request: Request, meeting_id: str, recipients: list[str] = Form([])):
    conn = get_conn()
    if not _meeting_source(conn, meeting_id):
        raise HTTPException(404)
    title, md = _minutes_markdown(conn, meeting_id)
    me = who(request)
    head, lines, _ = _meeting_source(conn, meeting_id)
    to = list(recipients) or [l["actor"] for l in lines if l["actor"] not in ("全員", me)]
    to = list(dict.fromkeys(p for p in to if p != me))
    tid = mailmod.send(conn, me, to, f"議事録: {title}", md.replace("# ", "").replace("**", ""),
                       attachments=[{"url": f"{config.APP_BASE_URL}/meet/{meeting_id}/minutes", "name": "議事録サマリー"}])
    _ingest_mail(conn)
    return RedirectResponse(f"/mail?folder=sent&thread={tid}", status_code=303)


@app.post("/meet/{meeting_id}/minutes/share")
def share_minutes(request: Request, meeting_id: str, channel: str = Form("general")):
    conn = get_conn()
    src = _meeting_source(conn, meeting_id)
    if not src:
        raise HTTPException(404)
    me = who(request)
    n_dec = conn.execute("SELECT COUNT(*) FROM events WHERE kind='decision' AND json_extract(meta,'$.meeting_id')=?", (meeting_id,)).fetchone()[0]
    chat.post_message(conn, channel, me, f"📝 「{src[0]['title']}」の議事録サマリーです（決定 {n_dec} 件） {config.APP_BASE_URL}/meet/{meeting_id}/minutes")
    _ingest_chat(conn)
    return RedirectResponse(f"/meet/{meeting_id}/minutes", status_code=303)


@app.on_event("startup")
async def _bind_loop():
    import asyncio
    from app import rtc
    rtc.bind_loop(asyncio.get_running_loop())


@app.websocket("/ws/chat")
async def chat_ws(websocket: WebSocket):
    from app import rtc
    await rtc.chat_ws(websocket)


@app.websocket("/ws/meet/{meeting_id}")
async def meet_ws(websocket: WebSocket, meeting_id: str, name: str = "参加者"):
    from app import rtc
    await rtc.handle(websocket, meeting_id, name.strip() or "参加者")


@app.get("/meet/{meeting_id}/status")
def meet_status(meeting_id: str):
    conn = get_conn()
    m = conn.execute("SELECT status, error FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if not m:
        raise HTTPException(404)
    ps = [p[0] for p in conn.execute(
        "SELECT name FROM meeting_participants WHERE meeting_id=? ORDER BY joined_at", (meeting_id,))]
    tracks = conn.execute("SELECT COUNT(*) FROM meeting_tracks WHERE meeting_id=?", (meeting_id,)).fetchone()[0]
    from app import rtc
    return {"status": m["status"], "error": m["error"], "participants": ps, "tracks": tracks,
            "online": rtc.room_members(meeting_id)}


@app.post("/meet/{meeting_id}/audio")
async def meet_audio(meeting_id: str, speaker: str = Form(""), rec_started_at: str = Form(...),
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
        transcript = meet.finalize(conn, meeting_id)
    except Exception:
        return   # 失敗は meetings.status='failed' に記録済み
    m = conn.execute("SELECT title, channel FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if m and m["channel"]:
        n = transcript.count("\n[")
        chat.post_message(conn, m["channel"], "エージェント",
                          f"📝 会議「{m['title']}」の議事録ができました（{n} 発言）。次の巡回で決定・タスクに分解します。 {config.APP_BASE_URL}/meet/{meeting_id}")
        _ingest_chat(conn)
        for t in tagmod.tags_for(conn, "channel", m["channel"]):
            tagmod.link(conn, t["name"], "meeting", meeting_id)
    from app import agent
    agent.request_tick(f"会議終了（{m['title'] if m else meeting_id}）")


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


@app.get("/api/agent/llm_calls")
def agent_llm_calls():
    """LLM に実際に送ったテキスト（マスク後）。「外部に出るのはマスク後だけ」の実演用"""
    from app.llm import router
    return {"calls": list(reversed(router.recent_calls))}


@app.get("/api/agent/status")
def agent_status():
    from app import agent
    conn = get_conn()
    return {"state": dict(agent.state), "open_findings": nav_context(conn)["nav_open_findings"]}


class TriggerBody(BaseModel):
    reason: str = "手動トリガー"


@app.post("/api/agent/trigger")
def agent_trigger(body: TriggerBody):
    from app import agent
    ok = agent.request_tick(body.reason)
    return {"queued": ok, "note": None if ok else "自動巡回が OFF のためトリガーは無効です"}


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
            agent.tick(conn, trigger="manual")
        except Exception as e:
            agent.say(f"tick 失敗: {e}")

    background.add_task(run)
    if "application/json" in request.headers.get("accept", ""):
        return {"ok": True}
    return RedirectResponse(request.headers.get("referer") or "/agent", status_code=303)


# ---------- 成果物（SharePoint のドキュメントライブラリ代替） ----------

LIB = lambda: config.FIXTURES_DIR / "excel"
FILE_ICON = {".xlsx": ("X", "bg-emerald-100 text-emerald-700"), ".docx": ("W", "bg-sky-100 text-sky-700"),
             ".pptx": ("P", "bg-orange-100 text-orange-700"), ".pdf": ("PDF", "bg-red-100 text-red-700"),
             ".md": ("MD", "bg-gray-200 text-gray-700"), ".txt": ("TXT", "bg-gray-200 text-gray-700"),
             ".png": ("IMG", "bg-violet-100 text-violet-700"), ".jpg": ("IMG", "bg-violet-100 text-violet-700"),
             ".jpeg": ("IMG", "bg-violet-100 text-violet-700"), ".csv": ("CSV", "bg-emerald-50 text-emerald-700")}


def _safe_rel(path: str) -> Path:
    rel = Path(path.strip("/")) if path else Path(".")
    if any(part in ("..", "") for part in rel.parts if part != "."):
        raise HTTPException(400, "invalid path")
    return rel


def library_rows(conn: sqlite3.Connection, folder: str = "", tag: str = "", q: str = "", sort: str = "updated",
                 mine: str = "") -> dict:
    """folder 直下のサブフォルダとファイル（版をまとめたもの）。q があればフォルダをまたいで名前検索"""
    from app.connectors.excel import version_series
    root = LIB()
    rel = _safe_rel(folder)
    cur = (root / rel) if str(rel) != "." else root
    versions = json.loads((root / "versions.json").read_text(encoding="utf-8")) if (root / "versions.json").exists() else {}
    tagged = set(tagmod.targets_for(conn, tag)["artifact"]) if tag else None
    if q or mine:
        tagged = tagged if tagged is not None else set()
        cross = True
    else:
        cross = False
    folders = []
    if cur.exists():
        for d in sorted(x for x in cur.iterdir() if x.is_dir() and not x.name.startswith(".")):
            n_files = len({k for k in version_series(root, ext=None) if k.startswith(str(d.relative_to(root)) + "/")})
            folders.append({"name": d.name, "path": str(d.relative_to(root)), "n_files": n_files})
    files = []
    for key, series in version_series(root, ext=None).items():
        k = Path(key)
        # タグで絞り込むときはフォルダをまたいで全ファイルから探す
        if not cross and tagged is None and (str(k.parent) if str(k.parent) != "." else "") != (str(rel) if str(rel) != "." else ""):
            continue
        display = key                      # タグの対象 id はライブラリ相対パス（例: 商品企画/売上見込.xlsx）
        legacy = k.name                    # 旧来の id（ファイル名のみ）
        tags = tagmod.tags_for(conn, "artifact", display) or tagmod.tags_for(conn, "artifact", legacy)
        if tag and display not in tagged and legacy not in tagged:
            continue
        if q and q.lower() not in key.lower():
            continue
        vs = []
        for n, path in series:
            info = versions.get(str(path.relative_to(root))) or versions.get(path.name, {})
            changes = conn.execute("SELECT COUNT(*) FROM events WHERE kind='artifact_change' AND json_extract(meta,'$.file')=?", (path.name,)).fetchone()[0]
            findings = conn.execute(
                "SELECT COUNT(*) FROM findings f WHERE f.status != 'dismissed' AND EXISTS ("
                "SELECT 1 FROM events e WHERE e.kind='artifact_change' AND json_extract(e.meta,'$.file')=? AND f.evidence LIKE '%' || e.id || '%')",
                (path.name,)).fetchone()[0]
            vs.append({"n": n, "file": path.name, "rel": str(path.relative_to(root)), "actor": info.get("actor"),
                       "at": info.get("at") or datetime.fromtimestamp(path.stat().st_mtime).isoformat(), "note": info.get("note", ""),
                       "url": info.get("url"), "size": path.stat().st_size, "changes": changes, "findings": findings})
        if mine and not any(v["actor"] == mine for v in vs):
            continue
        ext = k.suffix.lower()
        stem = k.stem
        tasks = [t for t in db.list_tasks(conn) if any(a in (k.name, display) or a.rsplit(".", 1)[0] == stem for a in t.artifacts)]
        icon, color = FILE_ICON.get(ext, ("FILE", "bg-gray-200 text-gray-700"))
        files.append({"key": key, "name": k.name, "stem": stem, "ext": ext, "icon": icon, "color": color, "versions": vs,
                      "folder": str(k.parent) if str(k.parent) != "." else "",
                      "latest": vs[-1], "tasks": tasks, "tags": tags, "editable": ext == ".xlsx",
                      "textual": ext in (".docx", ".pptx", ".md", ".txt", ".csv"), "image": ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"),
                      "pdf": ext == ".pdf", "n_findings": sum(v["findings"] for v in vs), "n_changes": sum(v["changes"] for v in vs),
                      "share_url": f"{config.APP_BASE_URL}/artifacts/file/{vs[-1]['rel']}"})
    keyf = {"name": lambda a: a["key"].lower(), "size": lambda a: -a["latest"]["size"], "versions": lambda a: -len(a["versions"]),
            "findings": lambda a: -a["n_findings"]}.get(sort)
    if keyf:
        files.sort(key=keyf)
    else:
        files.sort(key=lambda a: a["latest"]["at"], reverse=True)
    crumbs = []
    acc = Path(".")
    for part in ([] if str(rel) == "." else rel.parts):
        acc = acc / part
        crumbs.append({"name": part, "path": str(acc)})
    return {"folders": folders, "files": files, "crumbs": crumbs, "folder": "" if str(rel) == "." else str(rel)}


@app.get("/artifacts", response_class=HTMLResponse)
def artifacts_page(request: Request, path: str = "", tag: str = "", q: str = "", sort: str = "updated", view: str = "", mine: str = ""):
    conn = get_conn()
    view = view or request.cookies.get("lib_view", "list")
    resp = render("artifacts.html", request, conn, **library_rows(conn, path, tag, q.strip(), sort, mine), tag=tag, q=q, sort=sort, view=view, mine=mine,
                  all_tags=tagmod.all_tags(conn), me=get_me(request), channels=chat.list_channels(conn), all_folders=_all_folders())
    resp.set_cookie("lib_view", view, max_age=86400 * 365)
    return resp


def _all_folders() -> list[str]:
    root = LIB()
    return sorted(str(d.relative_to(root)) for d in root.rglob("*") if d.is_dir() and not d.name.startswith(".") and ".versions" not in d.parts)


def _series_paths(key: str) -> list[Path]:
    from app.connectors.excel import version_series
    return [p_ for _, p_ in version_series(LIB(), ext=None).get(key, [])]


def _rewrite_versions(fn) -> None:
    root = LIB()
    vp = root / "versions.json"
    versions = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else {}
    versions = fn(versions)
    vp.write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding="utf-8")


@app.post("/artifacts/rename")
def artifact_rename(request: Request, key: str = Form(...), new_name: str = Form(""), new_folder: str = Form(None)):
    """ファイル名の変更 / フォルダ移動（全版をまとめて）。タグ・タスクの参照も付け替える"""
    import shutil
    root = LIB()
    k = Path(key)
    paths = _series_paths(key)
    if not paths:
        raise HTTPException(404)
    new_stem = re.sub(r"[/\\]", "", new_name.strip()) or k.stem
    new_stem = new_stem[:-len(k.suffix)] if new_stem.lower().endswith(k.suffix.lower()) else new_stem
    folder = _safe_rel(new_folder) if new_folder is not None else k.parent
    new_key = str(folder / (new_stem + k.suffix)) if str(folder) != "." else new_stem + k.suffix
    if new_key == key:
        return RedirectResponse(f"/artifacts?path={k.parent if str(k.parent) != '.' else ''}", status_code=303)
    if _series_paths(new_key):
        raise HTTPException(409, "同じ名前のファイルがあります")
    (root / folder).mkdir(parents=True, exist_ok=True)
    mapping = {}
    for p_ in paths:
        m = re.match(r"^(.*)_v(\d+)$", p_.stem)
        dest = root / folder / f"{new_stem}_v{m.group(2)}{p_.suffix}"
        shutil.move(str(p_), str(dest))
        mapping[str(p_.relative_to(root))] = str(dest.relative_to(root))
    _rewrite_versions(lambda v: {mapping.get(kk, kk): vv for kk, vv in v.items()})
    conn = get_conn()
    conn.execute("UPDATE tag_links SET target_id=? WHERE target_type='artifact' AND target_id IN (?, ?)", (new_key, key, k.name))
    for t in db.list_tasks(conn):
        if any(a in (key, k.name) for a in t.artifacts):
            t.artifacts = [new_key if a in (key, k.name) else a for a in t.artifacts]
            db.save_task(conn, t)
    conn.commit()
    return RedirectResponse(f"/artifacts?path={folder if str(folder) != '.' else ''}", status_code=303)


@app.post("/artifacts/delete")
def artifact_delete(request: Request, key: str = Form(...)):
    """ファイル（全版）をごみ箱（.trash）へ。versions.json からも外す"""
    import shutil
    root = LIB()
    paths = _series_paths(key)
    if not paths:
        raise HTTPException(404)
    trash = root / ".trash" / datetime.now().strftime("%Y%m%d%H%M%S")
    trash.mkdir(parents=True, exist_ok=True)
    rels = []
    for p_ in paths:
        rels.append(str(p_.relative_to(root)))
        (trash / p_.parent.relative_to(root)).mkdir(parents=True, exist_ok=True)
        shutil.move(str(p_), str(trash / p_.relative_to(root)))
    _rewrite_versions(lambda v: {kk: vv for kk, vv in v.items() if kk not in rels})
    conn = get_conn()
    conn.execute("DELETE FROM tag_links WHERE target_type='artifact' AND target_id IN (?, ?)", (key, Path(key).name)); conn.commit()
    parent = Path(key).parent
    return RedirectResponse(f"/artifacts?path={parent if str(parent) != '.' else ''}", status_code=303)


@app.post("/artifacts/restore/{rel:path}")
def artifact_restore(request: Request, rel: str, channel: str = Form("")):
    """古い版の内容を新しい版として復元する"""
    root = LIB()
    src = root / _safe_rel(rel)
    if not src.exists():
        raise HTTPException(404)
    m = re.match(r"^(.*)_v(\d+)$", src.stem)
    rel_path = str(src.parent.relative_to(root) / (m.group(1) + src.suffix)) if src.parent != root else m.group(1) + src.suffix
    conn = get_conn()
    actor = who(request)
    try:
        _register_version(conn, rel_path, src.read_bytes(), actor, f"v{m.group(2)} の内容に戻しました", channel)
    except HTTPException as e:
        if e.status_code != 409:
            raise
    parent = src.parent.relative_to(root)
    return RedirectResponse(f"/artifacts?path={parent if str(parent) != '.' else ''}", status_code=303)


@app.post("/artifacts/share")
def artifact_share(request: Request, key: str = Form(...), channel: str = Form(...), note: str = Form("")):
    """ファイルのリンクをチャットに投稿"""
    conn = get_conn()
    paths = _series_paths(key)
    if not paths:
        raise HTTPException(404)
    actor = who(request)
    url = f"{config.APP_BASE_URL}/artifacts/file/{paths[-1].relative_to(LIB())}"
    mid = chat.post_message(conn, channel, actor, f"{note.strip() or Path(key).name + ' を共有します'} {url}",
                            attachments=[{"url": url, "name": Path(key).name}])
    _ingest_chat(conn)
    from app import rtc
    rtc.notify_chat(channel, mid)
    return RedirectResponse(f"/chat?channel={channel}", status_code=303)


@app.post("/artifacts/folder/rename")
def folder_rename(path: str = Form(...), new_name: str = Form(...)):
    import shutil
    root = LIB()
    rel = _safe_rel(path)
    src = root / rel
    if str(rel) == "." or not src.is_dir():
        raise HTTPException(404)
    new_name = re.sub(r"[/\\]", "", new_name.strip())
    dest = src.parent / new_name
    if dest.exists():
        raise HTTPException(409, "同じ名前のフォルダがあります")
    shutil.move(str(src), str(dest))
    old, new = str(rel), str(dest.relative_to(root))
    _rewrite_versions(lambda v: {(new + kk[len(old):] if kk.startswith(old + "/") else kk): vv for kk, vv in v.items()})
    conn = get_conn()
    for r in conn.execute("SELECT rowid, target_id FROM tag_links WHERE target_type='artifact' AND target_id LIKE ?", (old + "/%",)).fetchall():
        conn.execute("UPDATE tag_links SET target_id=? WHERE rowid=?", (new + r["target_id"][len(old):], r["rowid"]))
    conn.commit()
    parent = dest.parent.relative_to(root)
    return RedirectResponse(f"/artifacts?path={parent if str(parent) != '.' else ''}", status_code=303)


@app.post("/artifacts/folder/delete")
def folder_delete(path: str = Form(...)):
    import shutil
    root = LIB()
    rel = _safe_rel(path)
    src = root / rel
    if str(rel) == "." or not src.is_dir():
        raise HTTPException(404)
    trash = root / ".trash" / datetime.now().strftime("%Y%m%d%H%M%S")
    trash.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(trash / rel.name))
    prefix = str(rel) + "/"
    _rewrite_versions(lambda v: {kk: vv for kk, vv in v.items() if not kk.startswith(prefix)})
    parent = rel.parent
    return RedirectResponse(f"/artifacts?path={parent if str(parent) != '.' else ''}", status_code=303)


@app.post("/artifacts/new")
def artifacts_new(request: Request, path: str = Form(""), name: str = Form(...), kind: str = Form("xlsx"),
                  actor: str = Form("")):
    actor = who(request, actor)
    """空のファイルを v1 として作り、編集画面へ"""
    from app.connectors.docs import docx_bytes, xlsx_bytes
    rel = _safe_rel(path)
    stem = re.sub(r"[\\/:*?\"<>|]", "", name.strip())[:60] or "新規ファイル"
    stem = re.sub(r"\.(xlsx|docx|md|txt)$", "", stem, flags=re.I)
    ext = {"xlsx": ".xlsx", "docx": ".docx", "md": ".md", "txt": ".txt"}.get(kind, ".xlsx")
    if ext == ".xlsx":
        data = xlsx_bytes(stem)
    elif ext == ".docx":
        data = docx_bytes([stem])
    else:
        data = (f"# {stem}\n" if ext == ".md" else "").encode("utf-8")
    rel_path = str(rel / f"{stem}{ext}") if str(rel) != "." else f"{stem}{ext}"
    conn = get_conn()
    _register_version(conn, rel_path, data, actor, "", "")
    key = rel_path[:-len(ext)]
    return RedirectResponse(f"/artifacts/edit/{key}" if ext == ".xlsx" else f"/artifacts/write/{key}{ext}", status_code=303)


@app.get("/artifacts/write/{key:path}", response_class=HTMLResponse)
def artifact_write(request: Request, key: str):
    """docx / md / txt の簡易エディタ（段落＝行）。保存で新しい版"""
    from app.connectors.docs import extract_paragraphs
    from app.connectors.excel import version_series
    series = version_series(LIB(), ext=None).get(key)
    if not series:
        raise HTTPException(404)
    latest = series[-1][1]
    paragraphs = extract_paragraphs(latest) or []
    conn = get_conn()
    return render("artifact_write.html", request, conn, key=key, fname=Path(key).name, version=series[-1][0],
                  text="\n".join(paragraphs), me=get_me(request), channels=chat.list_channels(conn),
                  ext=Path(key).suffix.lower())


@app.post("/artifacts/write/{key:path}")
def artifact_write_save(request: Request, key: str, text: str = Form(""), actor: str = Form(""),
                        channel: str = Form(""), note: str = Form("")):
    from app.connectors.docs import docx_bytes
    actor = who(request, actor)
    ext = Path(key).suffix.lower()
    if ext == ".docx":
        data = docx_bytes([l for l in text.splitlines() if l.strip()])
    else:
        data = text.replace("\r\n", "\n").encode("utf-8")
    conn = get_conn()
    _register_version(conn, key, data, actor, note, channel)
    folder = str(Path(key).parent) if str(Path(key).parent) != "." else ""
    resp = RedirectResponse(f"/artifacts?path={folder}", status_code=303)
    return set_me(resp, actor)


@app.post("/artifacts/folder")
def artifacts_new_folder(path: str = Form(""), name: str = Form(...)):
    rel = _safe_rel(path)
    name = re.sub(r"[\\/:*?\"<>|]", "", name.strip())[:60]
    if not name:
        raise HTTPException(422, "フォルダ名が必要")
    (LIB() / rel / name).mkdir(parents=True, exist_ok=True)
    return RedirectResponse(f"/artifacts?path={(rel / name) if str(rel) != '.' else name}", status_code=303)


@app.get("/artifacts/file/{rel:path}")
def artifact_file(rel: str):
    root = LIB()
    path = (root / _safe_rel(rel)).resolve()
    if not path.is_file() or root.resolve() not in path.parents:
        raise HTTPException(404)
    return FileResponse(path, filename=path.name)


@app.get("/artifacts/edit/{key:path}", response_class=HTMLResponse)
def artifact_edit(request: Request, key: str):
    """ブラウザ上で編集（Excel Online の代わり）。保存すると新しい版として取り込まれる"""
    from app.connectors.excel import version_series
    series = version_series(LIB()).get(key)
    if not series:
        raise HTTPException(404)
    latest = series[-1][1]
    conn = get_conn()
    return render("artifact_edit.html", request, conn, base=key, fname=Path(key).name,
                  latest=str(latest.relative_to(LIB())), version=series[-1][0], me=get_me(request),
                  channels=chat.list_channels(conn))


@app.get("/artifacts/diff/{rel:path}", response_class=HTMLResponse)
def artifact_diff(request: Request, rel: str):
    """この版で何が変わったか（人間向け）。差分エンジンの出力と、それに対する検知を並べる"""
    from app.connectors.excel import version_series
    conn = get_conn()
    root = LIB()
    path = (root / _safe_rel(rel)).resolve()
    if not path.is_file() or root.resolve() not in path.parents:
        raise HTTPException(404)
    m = re.match(r"^(?P<base>.+)_v(?P<ver>\d+)(?P<ext>\.[A-Za-z0-9]+)$", path.name)
    key = str(path.parent.relative_to(root) / m["base"]) if str(path.parent.relative_to(root)) != "." else m["base"]
    series = version_series(root, ext=None).get(key + m["ext"].lower(), [])
    prev = next((p_ for n, p_ in reversed(series) if n < int(m["ver"])), None)
    changes = [db.row_to_event(r) for r in conn.execute(
        "SELECT * FROM events WHERE kind='artifact_change' AND json_extract(meta,'$.file')=? ORDER BY rowid", (path.name,))]
    rows = []
    for ch in changes:
        fs = []
        for r in conn.execute("SELECT * FROM findings WHERE evidence LIKE ? AND status != 'dismissed'", (f"%{ch.id}%",)):
            fs.append(db.row_to_finding(r))
        pr = conn.execute("SELECT result FROM processed WHERE stage='detect' AND key=?", (ch.id,)).fetchone()
        rows.append({"ev": ch, "findings": fs, "judged": bool(pr)})
    versions = json.loads((root / "versions.json").read_text(encoding="utf-8")) if (root / "versions.json").exists() else {}
    info = versions.get(str(path.relative_to(root)), {})
    return render("artifact_diff.html", request, conn, fname=m["base"] + m["ext"], ver=int(m["ver"]), prev=prev.name if prev else None,
                  rows=rows, info=info, rel=str(path.relative_to(root)), key=key)


@app.get("/artifacts/text/{rel:path}")
def artifact_text(rel: str):
    """docx / pptx / md などの本文（プレビュー用）"""
    from app.connectors.docs import extract_paragraphs
    root = LIB()
    path = (root / _safe_rel(rel)).resolve()
    if not path.is_file() or root.resolve() not in path.parents:
        raise HTTPException(404)
    return {"paragraphs": extract_paragraphs(path) or []}


def _register_version(conn: sqlite3.Connection, rel_path: str, data: bytes | None, actor: str, note: str,
                      channel: str, sheets: list[dict] | None = None) -> Path:
    """新しい版を保存し versions.json に記録 → 監視フォルダへ書き戻し → チャット共有 → 即時巡回"""
    from app.connectors.excel import register_version
    from app import sheet_export
    if sheets is not None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td) / Path(rel_path).name
            sheet_export.save_xlsx(sheets, tmp)
            data = tmp.read_bytes()
    dest = register_version(data or b"", rel_path, actor.strip(), note=note.strip())
    if dest is None:
        raise HTTPException(409, "内容が最新の版と同じです")
    root = LIB()
    versions = json.loads((root / "versions.json").read_text(encoding="utf-8"))
    url = versions[str(dest.relative_to(root))]["url"]
    if channel.strip():
        chat.post_message(conn, channel.strip(), actor.strip(), f"{note.strip() or Path(rel_path).name + ' を更新しました'} {url}")
        _ingest_chat(conn)
    from app import agent
    agent.mirror_to_watch_dir(dest)
    agent.request_tick(f"成果物の保存（{dest.relative_to(root)}）")
    return dest


class SheetSave(BaseModel):
    sheets: list[dict]
    actor: str = ""
    channel: str = ""
    note: str = ""


@app.post("/artifacts/save/{key:path}")
def artifact_save(request: Request, key: str, body: SheetSave):
    """ブラウザ編集（Luckysheet）の保存。JSON → xlsx → 新しい版"""
    conn = get_conn()
    actor = who(request, body.actor)
    dest = _register_version(conn, key + ".xlsx", None, actor, body.note, body.channel, sheets=body.sheets)
    return {"ok": True, "file": dest.name}


@app.post("/artifacts/upload")
async def upload_artifact(request: Request, file: UploadFile = File(...), actor: str = Form(""), note: str = Form(""),
                          channel: str = Form(""), path: str = Form("")):
    """任意の種類のファイルを新しい版としてアップロード（フォルダ指定可）"""
    actor = who(request, actor)
    name = Path(file.filename or "upload.bin").name
    rel = _safe_rel(path)
    rel_path = str(rel / name) if str(rel) != "." else name
    conn = get_conn()
    _register_version(conn, rel_path, await file.read(), actor, note, channel)
    resp = RedirectResponse(f"/artifacts?path={path}", status_code=303)
    return set_me(resp, actor)


# ---------- Wiki（Confluence / Notion の代わり） ----------

def _wiki_ctx(conn: sqlite3.Connection, page: dict | None = None, me: str = "") -> dict:
    pages = wikimod.tree(conn)
    spaces = sorted({p_["space"] for p_ in pages})
    favs = wikimod.favorites(conn, me) if me else []
    return {"pages": pages, "spaces": spaces, "page": page, "favs": favs, "fav_pages": [p_ for p_ in pages if p_["id"] in favs],
            "recent": wikimod.recent(conn), "templates": wikimod.TEMPLATES}


@app.get("/wiki", response_class=HTMLResponse)
def wiki_index(request: Request):
    conn = get_conn()
    pages = wikimod.tree(conn)
    if pages:
        latest = max(pages, key=lambda p_: p_["updated_at"])
        return RedirectResponse(f"/wiki/{latest['id']}", status_code=303)
    return render("wiki.html", request, conn, **_wiki_ctx(conn, me=get_me(request)), html="", revs=[], tags=[], linked_tasks=[], comments=[])


@app.post("/wiki")
def wiki_create(request: Request, title: str = Form(...), space: str = Form("general"), parent_id: str = Form(""),
                template: str = Form("blank")):
    conn = get_conn()
    body = wikimod.TEMPLATES.get(template, wikimod.TEMPLATES["blank"])[1].format(title=title.strip())
    pid = wikimod.create(conn, title, body, who(request), parent_id=parent_id or None, space=space.strip() or "general")
    return RedirectResponse(f"/wiki/{pid}/edit", status_code=303)


@app.post("/wiki/{page_id}/favorite")
def wiki_favorite(request: Request, page_id: str):
    conn = get_conn()
    wikimod.toggle_favorite(conn, who(request), page_id)
    return RedirectResponse(f"/wiki/{page_id}", status_code=303)


@app.post("/wiki/{page_id}/move")
def wiki_move(page_id: str, parent_id: str = Form(""), space: str = Form("")):
    conn = get_conn()
    wikimod.move(conn, page_id, parent_id or None, space.strip() or None)
    return RedirectResponse(f"/wiki/{page_id}", status_code=303)


@app.post("/wiki/{page_id}/duplicate")
def wiki_duplicate(request: Request, page_id: str):
    conn = get_conn()
    new = wikimod.duplicate(conn, page_id, who(request))
    return RedirectResponse(f"/wiki/{new}/edit", status_code=303)


@app.get("/wiki/{page_id}/export")
def wiki_export(page_id: str):
    from fastapi.responses import Response
    conn = get_conn()
    p_ = wikimod.get(conn, page_id)
    if not p_:
        raise HTTPException(404)
    return Response(p_["body"], media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(p_['title'])}.md"})


@app.post("/wiki/{page_id}/comment")
def wiki_comment(request: Request, page_id: str, text: str = Form(...)):
    conn = get_conn()
    if not wikimod.get(conn, page_id):
        raise HTTPException(404)
    mid = chat.post_message(conn, f"wiki:{page_id}", who(request), text.strip())
    for tname in tagmod.apply_hashtags(conn, text, "message", mid):
        tagmod.link(conn, tname, "wiki", page_id)
    _ingest_chat(conn)
    return RedirectResponse(f"/wiki/{page_id}#comments", status_code=303)


@app.get("/wiki/{page_id}", response_class=HTMLResponse)
def wiki_page(request: Request, page_id: str, reverted: str = "", undo_to: str = ""):
    conn = get_conn()
    page = wikimod.get(conn, page_id)
    if not page:
        raise HTTPException(404)
    revs = wikimod.revisions(conn, page_id)
    changes = conn.execute("SELECT COUNT(*) FROM events WHERE source='wiki' AND json_extract(meta,'$.page_id')=?", (page_id,)).fetchone()[0]
    findings = []
    for r in conn.execute("SELECT * FROM findings WHERE status != 'dismissed'"):
        f = db.row_to_finding(r)
        if any(conn.execute("SELECT 1 FROM events WHERE id=? AND source='wiki' AND json_extract(meta,'$.page_id')=?", (eid, page_id)).fetchone() for eid in f.evidence):
            findings.append(f)
    linked_tasks = [t for t in db.list_tasks(conn) if f"wiki:{page_id}" in t.artifacts or page["title"] in t.artifacts]
    children = [p_ for p_ in wikimod.tree(conn) if p_["parent_id"] == page_id]
    comments = [_message_dict(conn, r) for r in conn.execute("SELECT * FROM chat_messages WHERE channel=? AND deleted=0 ORDER BY posted_at, rowid", (f"wiki:{page_id}",))]
    breadcrumbs = []
    cur = page
    while cur and cur.get("parent_id"):
        cur = wikimod.get(conn, cur["parent_id"])
        if cur:
            breadcrumbs.insert(0, cur)
    return render("wiki.html", request, conn, **_wiki_ctx(conn, page, me=get_me(request)), html=wikimod.render_markdown(page["body"]),
                  revs=revs, tags=tagmod.tags_for(conn, "wiki", page_id), all_tags=tagmod.all_tags(conn),
                  linked_tasks=linked_tasks, findings=findings, changes=changes, children=children, comments=comments,
                  toc=wikimod.toc(page["body"]), breadcrumbs=breadcrumbs,
                  all_tasks=[t for t in db.list_tasks(conn) if t.status != "done"], reverted=reverted, undo_to=undo_to)


@app.get("/wiki/{page_id}/edit", response_class=HTMLResponse)
def wiki_edit(request: Request, page_id: str):
    conn = get_conn()
    page = wikimod.get(conn, page_id)
    if not page:
        raise HTTPException(404)
    return render("wiki_edit.html", request, conn, **_wiki_ctx(conn, page, me=get_me(request)), channels=chat.list_channels(conn))


@app.post("/wiki/{page_id}/edit")
def wiki_save(request: Request, page_id: str, title: str = Form(...), body: str = Form(""), note: str = Form(""),
              channel: str = Form("")):
    conn = get_conn()
    changed = wikimod.update(conn, page_id, title, body.replace("\r\n", "\n"), who(request), note=note.strip() or None)
    if changed:
        if channel.strip():
            chat.post_message(conn, channel.strip(), who(request), f"📖 Wiki「{title.strip()}」を更新しました{('（' + note.strip() + '）') if note.strip() else ''} {config.APP_BASE_URL}/wiki/{page_id}")
            _ingest_chat(conn)
        from app import agent
        agent.request_tick(f"Wiki 更新（{title.strip()}）")
    return RedirectResponse(f"/wiki/{page_id}", status_code=303)


@app.get("/wiki/{page_id}/diff/{rev_id}", response_class=HTMLResponse)
def wiki_diff(request: Request, page_id: str, rev_id: str):
    from app.connectors.docs import diff_paragraphs
    conn = get_conn()
    page = wikimod.get(conn, page_id)
    revs = wikimod.revisions(conn, page_id)
    idx = next((i for i, r in enumerate(revs) if r["id"] == rev_id), None)
    if not page or idx is None:
        raise HTTPException(404)
    cur = revs[idx]; prev = revs[idx - 1] if idx > 0 else None
    diffs = diff_paragraphs(wikimod.paragraphs(prev["body"]) if prev else [], wikimod.paragraphs(cur["body"]))
    evs = [db.row_to_event(r) for r in conn.execute("SELECT * FROM events WHERE source='wiki' AND json_extract(meta,'$.revision_id')=?", (rev_id,))]
    fmap = {}
    for ev in evs:
        for r in conn.execute("SELECT * FROM findings WHERE evidence LIKE ? AND status != 'dismissed'", (f"%{ev.id}%",)):
            fmap.setdefault(ev.ref, []).append(db.row_to_finding(r))
    return render("wiki_diff.html", request, conn, **_wiki_ctx(conn, page, me=get_me(request)), cur=cur, prev=prev, diffs=diffs, n=idx + 1,
                  findings_by_ref=fmap, evs=evs)


@app.post("/wiki/{page_id}/revert/{rev_id}")
def wiki_revert(request: Request, page_id: str, rev_id: str, undo: str = Form("")):
    """指定した改訂の内容に巻き戻す（新しい改訂として積む）。undo=1 なら「元に戻す」操作"""
    conn = get_conn()
    page = wikimod.get(conn, page_id)
    revs = wikimod.revisions(conn, page_id)
    idx = next((i for i, r in enumerate(revs) if r["id"] == rev_id), None)
    if not page or idx is None:
        raise HTTPException(404)
    target = revs[idx]
    before = revs[-1]   # 巻き戻す前に最新だった改訂（元に戻す先）
    me = who(request)
    note = "巻き戻しを取り消し" if undo else f"改訂 {idx + 1} に巻き戻し"
    changed = wikimod.update(conn, page_id, target["title"], target["body"], me, note=note)
    if changed:
        from app import agent
        agent.request_tick(f"Wiki 巻き戻し（{target['title']}）")
    if undo or not changed:
        return RedirectResponse(f"/wiki/{page_id}", status_code=303)
    return RedirectResponse(f"/wiki/{page_id}?reverted={idx + 1}&undo_to={before['id']}", status_code=303)


@app.post("/wiki/{page_id}/link_task")
def wiki_link_task(page_id: str, task_id: str = Form(...)):
    conn = get_conn()
    t = db.get_task(conn, task_id)
    page = wikimod.get(conn, page_id)
    if not t or not page:
        raise HTTPException(404)
    if f"wiki:{page_id}" not in t.artifacts:
        t.artifacts.append(f"wiki:{page_id}")
        db.save_task(conn, t)
    return RedirectResponse(f"/wiki/{page_id}", status_code=303)


@app.post("/wiki/{page_id}/delete")
def wiki_delete(page_id: str):
    conn = get_conn()
    conn.execute("UPDATE wiki_pages SET parent_id=NULL WHERE parent_id=?", (page_id,))
    conn.execute("DELETE FROM wiki_pages WHERE id=?", (page_id,))
    conn.commit()
    return RedirectResponse("/wiki", status_code=303)


# ---------- タグ・チーム ----------

@app.get("/tags", response_class=HTMLResponse)
def tags_page(request: Request):
    conn = get_conn()
    return render("tags.html", request, conn, tags=tagmod.all_tags(conn))


@app.post("/tags")
def create_tag(request: Request, name: str = Form(...), kind: str = Form("topic"), members: list[str] = Form([]),
               description: str = Form("")):
    conn = get_conn()
    tagmod.ensure_tag(conn, name, kind="team" if kind == "team" else "topic",
                      members=list(members) if kind == "team" else None, description=description.strip() or None)
    return RedirectResponse(f"/tags/{name.strip().lstrip('#@')}", status_code=303)


@app.get("/tags/{name}", response_class=HTMLResponse)
def tag_page(request: Request, name: str):
    conn = get_conn()
    t = tagmod.get_tag(conn, name)
    if not t:
        t = tagmod.ensure_tag(conn, name)
    targets = tagmod.targets_for(conn, name)
    msgs = [_message_dict(conn, r) for r in (conn.execute("SELECT * FROM chat_messages WHERE id=? AND deleted=0", (i,)).fetchone() for i in targets["message"]) if r]
    tasks = [task_meta(conn, t2) for t2 in (db.get_task(conn, i) for i in targets["task"]) if t2]
    meetings = [dict(r) for r in (conn.execute("SELECT id, title, started_at, status FROM meetings WHERE id=?", (i,)).fetchone() for i in targets["meeting"]) if r]
    artifacts = []
    for name_ in targets["artifact"]:
        changes = conn.execute("SELECT COUNT(*) FROM events WHERE kind='artifact_change' AND (json_extract(meta,'$.base') || '.xlsx' = ? OR json_extract(meta,'$.file') = ?)",
                               (name_, name_)).fetchone()[0]
        artifacts.append({"name": name_, "changes": changes})
    return render("tag_detail.html", request, conn, tag=t, msgs=msgs, tasks=tasks, meetings=meetings,
                  artifacts=artifacts, channels=targets["channel"], all_tasks=db.list_tasks(conn), all_tags_list=tagmod.all_tags(conn))


@app.post("/tags/{name}/link")
def tag_link(request: Request, name: str, target_type: str = Form(...), target_id: str = Form(...)):
    conn = get_conn()
    tagmod.link(conn, name, target_type, target_id.strip())
    return RedirectResponse(request.headers.get("referer") or f"/tags/{name}", status_code=303)


@app.post("/tags/{name}/unlink")
def tag_unlink(request: Request, name: str, target_type: str = Form(...), target_id: str = Form(...)):
    conn = get_conn()
    tagmod.unlink(conn, name, target_type, target_id.strip())
    return RedirectResponse(request.headers.get("referer") or f"/tags/{name}", status_code=303)


@app.post("/tags/{name}/update")
def tag_update(name: str, new_name: str = Form(""), color: str = Form(""), description: str = Form("")):
    conn = get_conn()
    try:
        nn = tagmod.update_tag(conn, name, new_name or None, color or None, description)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return RedirectResponse(f"/tags/{nn}", status_code=303)


@app.post("/tags/{name}/delete")
def tag_delete(name: str):
    tagmod.delete_tag(get_conn(), name)
    return RedirectResponse("/tags", status_code=303)


@app.post("/tags/{name}/merge")
def tag_merge(name: str, into: str = Form(...)):
    tagmod.merge_tags(get_conn(), name, into)
    return RedirectResponse(f"/tags/{into}", status_code=303)


@app.post("/tags/{name}/members")
def tag_members(name: str, members: list[str] = Form([])):
    conn = get_conn()
    tagmod.ensure_tag(conn, name, kind="team", members=list(members))
    return RedirectResponse(f"/tags/{name}", status_code=303)


# ---------- 設定 ----------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    from app import agent
    from app.llm import router
    conn = get_conn()
    items = [{"key": k, "label": lbl, "type": typ.__name__, "help": h, "value": getattr(config, k)}
             for k, (lbl, typ, h) in config.TUNABLE.items()]
    models = {t: router.model_for(t) for t in ("high", "mid", "embed")}
    scopes = [
        ("会議（Google Drive / 自作 Meet）", "drive.readonly", "読み取り"),
        ("チャット（Slack 想定）", "channels:history, channels:read", "読み取り・対象チャンネルのみ"),
        ("メール（Outlook 想定）", "Mail.Read", "読み取り・転送も保存もしない"),
        ("予定表（Outlook 想定）", "Calendars.Read", "読み取り"),
        ("成果物（SharePoint / OneDrive 想定）", "Files.Read.All / Sites.Read.All", "読み取り・指定フォルダのみ"),
        ("Wiki（Confluence 想定）", "read:page:confluence", "読み取り"),
        ("人・組織（Entra ID 想定）", "User.Read, User.ReadBasic.All", "読み取り"),
    ]
    writes = [("画面への通知表示", "不要"), ("矛盾の検知・記録", "不要"), ("Event の取得・保存", "不要"),
              ("タスクの状態変更（提案の適用）", "人の承認"), ("チャットへの投稿（外部ツール連携時）", "人の承認"),
              ("Wiki の書き換え", "実装しない"), ("ファイルの更新（外部ストレージ）", "実装しない"), ("Slack・メールへの外部送信", "実装しない")]
    return render("settings.html", request, conn, items=items, models=models, scopes=scopes, writes=writes,
                  agent_state=dict(agent.state), watch_dir=config.ARTIFACT_WATCH_DIR, gemini=bool(config.GEMINI_API_KEY),
                  orca=bool(config.ORCA_API_KEY), base_url=config.ORCA_BASE_URL)


@app.post("/settings")
async def settings_save(request: Request):
    conn = get_conn()
    form = await request.form()
    for key, (lbl, typ, h) in config.TUNABLE.items():
        if key in form and str(form[key]).strip():
            try:
                typ(form[key])
            except (TypeError, ValueError):
                continue
            db.set_setting(conn, key, str(form[key]).strip())
    config.apply_overrides(db.get_settings(conn))
    if "interval" in form:
        from app import agent
        agent.state["interval"] = max(10, int(form["interval"]))
    return RedirectResponse("/settings", status_code=303)


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
