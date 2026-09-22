"""まっさらな環境から「会議 → 議事録 → タスク → チャット → 成果物 → 矛盾の指摘 → 修正 → 完了 → Wiki」を
一周して動画に残す（あとから解説を入れる用）。

  python promo/record_flow.py            # → promo/video/flow.webm

- 空の DB / 空のライブラリでサーバーを別ポートに起動（BLANK_START=1）
- 画面は 鈴木（管理者）の操作を Playwright で録画。他の人（山田・田中・佐藤）の投稿やアップロードは API で行う
- 会議の音声は用意せず、話者ごとの文字起こし結果を直接入れる（LLM 抽出は本物を呼ぶ）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.getenv("REC_PORT", "8790"))
BASE = f"http://localhost:{PORT}"
WORK = Path("/tmp/followup-blank")
OUT = ROOT / "promo" / "video"
PROJECT = "商品X（青葉りんごソーダ）"
FOLDER = "商品企画"
FILE = "商品X_売上見込.xlsx"
PACE = float(os.getenv("REC_PACE", "1.0"))    # 待ち時間の倍率


def pause(sec: float) -> None:
    time.sleep(sec * PACE)


# ---------- サーバー ----------

def start_server() -> subprocess.Popen:
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "lib").mkdir(parents=True)
    env = dict(os.environ, BLANK_START="1", DB_PATH=str(WORK / "app.db"), LIBRARY_DIR=str(WORK / "lib"),
               DEMO_MODE="1", AGENT_AUTOSTART="1", AGENT_INTERVAL="3600", APP_BASE_URL=BASE,
               ARTIFACT_WATCH_DIR="", GCS_BUCKET="", GCS_STATE_BUCKET="")
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT)], cwd=ROOT, env=env,
                         stdout=open(WORK / "server.log", "w"), stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            if requests.get(f"{BASE}/login", timeout=2).status_code == 200:
                return p
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError("server did not start")


def session(name: str) -> requests.Session:
    s = requests.Session()
    s.get(f"{BASE}/login/as/{name}?next=/", allow_redirects=True)
    return s


def db() -> sqlite3.Connection:
    c = sqlite3.connect(WORK / "app.db", timeout=10)
    c.row_factory = sqlite3.Row
    return c


def agent_status(s: requests.Session) -> dict:
    return s.get(f"{BASE}/api/agent/status").json()["state"]


def wait_tick(s: requests.Session, ticks_before: int, timeout: float = 240) -> None:
    """巡回が始まって終わるまで待つ"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = agent_status(s)
        if st["ticks"] > ticks_before and not st["running"]:
            return
        time.sleep(1)
    raise RuntimeError("tick timeout")


def trigger_and_wait(s: requests.Session) -> None:
    n = agent_status(s)["ticks"]
    s.post(f"{BASE}/api/agent/trigger", json={"reason": "録画"})
    wait_tick(s, n)


def ensure_tick(page, s: requests.Session, before: int) -> None:
    """投稿・保存のイベントで巡回が走っていればそれを待ち、走っていなければ画面の「今すぐ巡回」を押す"""
    time.sleep(3)                       # イベントトリガーが巡回スレッドに拾われるまで待つ
    for _ in range(300):
        st = agent_status(s)
        if not st["running"]:
            break
        time.sleep(1)
    st = agent_status(s)
    if st["ticks"] > before:
        return
    page.click('button:has-text("今すぐ巡回")')
    wait_tick(s, before)
    time.sleep(2)
    for _ in range(300):                # 続けてもう 1 本走っていたらそれも待つ
        if not agent_status(s)["running"]:
            break
        time.sleep(1)


# ---------- 会議の中身（文字起こしを直接入れる） ----------

MEETING_TITLE = "商品X キックオフ"
LINES = [
    (0,   "鈴木", f"{PROJECT}の立ち上げキックオフを始めます。11月の発売に向けて、今日は数字と担当を決めます。"),
    (20,  "田中", "製造から聞いた条件だと、初回ロットは300ケースが上限です。"),
    (35,  "鈴木", "分かりました。初回ロットは300ケースで確定します。"),
    (55,  "山田", "売上見込ですが、第3四半期は発売前なので0、第4四半期は450ケースで見ています。"),
    (75,  "佐藤", "得意先からはもっと欲しいと言われていますが、初回は450で十分だと思います。"),
    (90,  "鈴木", "では第4四半期は450で確定。得意先から増量の要望が来ても、今期の見込みは450から変更しないでください。"),
    (110, "鈴木", "山田さん、売上見込表の初版を今週中に作ってライブラリに上げてください。"),
    (120, "山田", "はい、今週中に上げます。"),
    (130, "鈴木", "田中さん、原価試算を来週の定例までにお願いします。"),
    (140, "田中", "承知しました。来週の定例までに出します。"),
    (150, "鈴木", "佐藤さんは主要得意先3社にヒアリングして、結果を来週共有してください。"),
    (160, "佐藤", "分かりました。3社に当たります。"),
    (175, "鈴木", "決定事項を確認します。初回ロット300ケース、第4四半期の見込みは450で据え置き。以上です。"),
]


def inject_meeting(meeting_id: str) -> None:
    conn = db()
    started = datetime.now().replace(microsecond=0) - timedelta(minutes=4)
    conn.execute("UPDATE meetings SET started_at=? WHERE id=?", (started.isoformat(), meeting_id))
    by: dict[str, list] = {}
    for t, sp, text in LINES:
        by.setdefault(sp, []).append({"t": t, "text": text})
        conn.execute("INSERT INTO meeting_captions (id, meeting_id, speaker, text, at) VALUES (?,?,?,?,?)",
                     (os.urandom(6).hex(), meeting_id, sp, text, (started + timedelta(seconds=t)).isoformat()))
    for sp, utts in by.items():
        conn.execute("INSERT INTO meeting_tracks (id, meeting_id, speaker, mime, audio, rec_started_at, uploaded_at, utterances) VALUES (?,?,?,?,?,?,?,?)",
                     (os.urandom(6).hex(), meeting_id, sp, "audio/webm", b"", started.isoformat(), datetime.now().replace(microsecond=0).isoformat(),
                      json.dumps(utts, ensure_ascii=False)))
    conn.commit()
    conn.close()


# ---------- 成果物 ----------

def forecast_xlsx(q3: int, q4: int) -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "売上見込"
    ws.append(["商品名", "第1四半期", "第2四半期", "第3四半期", "第4四半期"])
    ws.append(["商品X", 0, 0, q3, q4])
    ws.append(["合計", "=SUM(B2:B2)", "=SUM(C2:C2)", "=SUM(D2:D2)", "=SUM(E2:E2)"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def upload(s: requests.Session, actor: str, data: bytes, note: str) -> None:
    s.post(f"{BASE}/artifacts/upload", data={"actor": actor, "note": note, "path": FOLDER, "channel": "general"},
           files={"file": (FILE, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def post(s: requests.Session, actor: str, text: str, quote_of: str = "", reply_to: str = "") -> str:
    s.post(f"{BASE}/chat", data={"channel": "general", "actor": actor, "text": text, "quote_of": quote_of, "reply_to": reply_to})
    conn = db()
    mid = conn.execute("SELECT id FROM chat_messages WHERE actor=? ORDER BY posted_at DESC, rowid DESC LIMIT 1", (actor,)).fetchone()["id"]
    conn.close()
    return mid


def last_message_id(actor: str) -> str:
    conn = db()
    r = conn.execute("SELECT id FROM chat_messages WHERE actor=? ORDER BY posted_at DESC, rowid DESC LIMIT 1", (actor,)).fetchone()
    conn.close()
    return r["id"]


# ---------- 録画本体 ----------

def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    server = start_server()
    try:
        yamada, tanaka, sato, suzuki_api = session("山田"), session("田中"), session("佐藤"), session("鈴木")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"])
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(OUT),
                                      record_video_size={"width": 1440, "height": 900}, locale="ja-JP")
            ctx.grant_permissions(["microphone", "camera", "notifications"], origin=BASE)
            page = ctx.new_page()
            page.on("dialog", lambda d: d.accept())

            # 1. サインイン（まっさらなダッシュボード）
            page.goto(f"{BASE}/login")
            pause(2.5)
            page.click("text=鈴木")
            page.wait_for_url(f"{BASE}/")
            pause(4)

            # 2. チャットで立ち上げを宣言（鈴木が入力）
            page.goto(f"{BASE}/chat?channel=general")
            pause(1.5)
            ed = page.locator('[contenteditable="true"]').first
            ed.click()
            page.keyboard.type(f"{PROJECT}の立ち上げを始めます。これからキックオフ会議をやります。", delay=40)
            pause(0.8)
            page.keyboard.press("Enter")
            pause(3)

            # 3. 会議室を作って入る → 文字起こしが並ぶ → 終了して議事録を作る
            page.goto(f"{BASE}/meet")
            pause(1.5)
            page.click('button:has-text("今すぐ会議")')
            pause(0.8)
            page.fill('input[name="title"]', MEETING_TITLE)
            pause(0.6)
            page.click('form[action="/meet"] button:has-text("開始")')
            page.wait_for_url("**/meet/**")
            meeting_id = page.url.split("/meet/")[1].split("/")[0].split("?")[0]
            pause(3)
            inject_meeting(meeting_id)
            page.reload()
            page.wait_for_load_state("networkidle")
            pause(6)
            page.click("#endBtn")
            page.wait_for_url(f"{BASE}/meet/{meeting_id}", timeout=60000)
            pause(3)
            # 会議終了で即時巡回が走る（抽出 → タスク → 議事録 Wiki）。終わるまで待つ
            n0 = agent_status(suzuki_api)["ticks"]
            for _ in range(180):
                st = agent_status(suzuki_api)
                if st["ticks"] > n0 and not st["running"]:
                    break
                time.sleep(1)
            page.reload()
            pause(3)

            # 4. 議事録サマリー → タスク → Wiki（議事録）
            page.goto(f"{BASE}/meet/{meeting_id}/minutes")
            page.wait_for_load_state("networkidle")
            pause(6)
            page.goto(f"{BASE}/tasks?view=board")
            pause(5)
            conn = db()
            task = conn.execute("SELECT id FROM tasks WHERE title LIKE '%見込%' ORDER BY created_at LIMIT 1").fetchone()
            wiki_min = conn.execute("SELECT id FROM wiki_pages WHERE space='議事録' ORDER BY created_at DESC LIMIT 1").fetchone()
            conn.close()
            if task:
                page.goto(f"{BASE}/tasks/{task['id']}")
                pause(4)
            if wiki_min:
                page.goto(f"{BASE}/wiki/{wiki_min['id']}")
                pause(4)

            # 5. 山田が初版を作って共有（API）→ 鈴木の画面で確認
            t_before = agent_status(suzuki_api)["ticks"]
            upload(yamada, "山田", forecast_xlsx(0, 450), "初版")
            post(yamada, "山田", f"売上見込表の初版を上げました。会議で決めた 第4四半期 450 で入れています {BASE}/artifacts/file/{FOLDER}/{FILE}")
            pause(1)
            page.goto(f"{BASE}/chat?channel=general")
            pause(4)
            ensure_tick(page, suzuki_api, t_before)
            page.goto(f"{BASE}/artifacts?path={FOLDER}&view=cards")
            pause(4)

            # 6. 山田が得意先の要望で第4四半期を 500 に変更（決定と食い違う）→ エージェントが指摘
            t_before = agent_status(suzuki_api)["ticks"]
            upload(yamada, "山田", forecast_xlsx(0, 500), "得意先の要望で第4四半期を増量")
            m_yamada = post(yamada, "山田", f"得意先の要望を反映した版を上げました {BASE}/artifacts/file/{FOLDER}/{FILE}")
            pause(1)
            page.goto(f"{BASE}/chat?channel=general")
            pause(3)
            ensure_tick(page, suzuki_api, t_before)
            pause(1)
            page.goto(f"{BASE}/")
            pause(6)
            page.goto(f"{BASE}/findings")
            pause(6)

            # 7. 鈴木が引用して差し戻し → 山田が修正版 → 「直しました。確認お願いします」
            post(suzuki_api, "鈴木", "エージェントの指摘どおりです。会議で今期の見込みは変更しないと決めたので、第4四半期は会議の数字に戻してください。", quote_of=m_yamada)
            page.goto(f"{BASE}/chat?channel=general")
            pause(4)
            t_before = agent_status(suzuki_api)["ticks"]
            upload(yamada, "山田", forecast_xlsx(0, 450), "第4四半期を 450 に戻した")
            m_fix = post(yamada, "山田", f"直しました。会議の数字に戻しています。確認お願いします {BASE}/artifacts/file/{FOLDER}/{FILE}", quote_of=last_message_id("鈴木"))
            pause(1)
            page.reload()
            pause(4)
            ensure_tick(page, suzuki_api, t_before)
            pause(1)

            # 8. 状態更新の提案（レビュー中）を承認 → 確認して完了
            page.goto(f"{BASE}/findings?kind=status_suggestion")
            pause(4)
            if page.locator('button:has-text("適用する")').count():
                page.click('button:has-text("適用する")')
                pause(3)
            t_before = agent_status(suzuki_api)["ticks"]
            post(suzuki_api, "鈴木", "確認しました。これで OK です。完了にしてください。", quote_of=m_fix)
            page.goto(f"{BASE}/chat?channel=general")
            pause(4)
            ensure_tick(page, suzuki_api, t_before)
            page.goto(f"{BASE}/findings?kind=status_suggestion")
            pause(3)
            if page.locator('button:has-text("適用する")').count():
                page.click('button:has-text("適用する")')
                pause(3)
            if task:
                page.goto(f"{BASE}/tasks/{task['id']}")
                pause(5)

            # 9. 成果物の履歴と Wiki（中身の構造化を含む）
            page.goto(f"{BASE}/artifacts/diff/{FOLDER}/{FILE.replace('.xlsx', '_v3.xlsx')}")
            pause(4)
            conn = db()
            wiki_art = conn.execute("SELECT id FROM wiki_pages WHERE space='成果物' ORDER BY updated_at DESC LIMIT 1").fetchone()
            conn.close()
            if wiki_art:
                page.goto(f"{BASE}/wiki/{wiki_art['id']}")
                pause(7)
            page.goto(f"{BASE}/")
            pause(5)

            ctx.close()
            browser.close()
        # 動画ファイルを固定名に
        vids = sorted(OUT.glob("*.webm"), key=lambda p: p.stat().st_mtime)
        if vids:
            dest = OUT / "flow.webm"
            shutil.move(str(vids[-1]), dest)
            print("video:", dest, dest.stat().st_size // 1024, "KB")
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
