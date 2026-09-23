"""まっさらな環境から「会議 → 議事録 → タスク → チャット → 成果物 → 矛盾の指摘 → 修正 → 完了 → Wiki」を
一周して動画に残す（あとから解説を入れる用）。

  python promo/record_flow.py            # → promo/video/flow.webm

- 空の DB / 空のライブラリでサーバーを別ポートに起動（BLANK_START=1）
- 画面は 鈴木（管理者）の操作を Playwright で録画。人が操作している見え方にするため、
  画面上にカーソルを描いて移動・クリック・入力を見せ、サイドバーのクリックで画面を移る
- 解説の吹き出しは、操作している要素の近くに出す（要素は光る枠で示す）
- 他の人（山田・田中・佐藤）の投稿やアップロードは API で行い、鈴木の画面に届く様子を撮る
- 会議の発言はライブ字幕として流し（別接続から送る）、文字起こし結果は直接入れる（LLM 抽出は本物を呼ぶ）
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
               ARTIFACT_WATCH_DIR="", GCS_BUCKET="")
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


def wait_idle(s: requests.Session, timeout: float = 300) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout and agent_status(s)["running"]:
        time.sleep(1)


def wait_tick(s: requests.Session, ticks_before: int, timeout: float = 300) -> None:
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = agent_status(s)
        if st["ticks"] > ticks_before and not st["running"]:
            return
        time.sleep(1)
    raise RuntimeError("tick timeout")


# ---------- 画面の演出（カーソル・吹き出し・ハイライト） ----------

INIT_JS = r"""
(() => {
  const setup = () => {
    if (document.getElementById('rec-cursor')) return;
    const pos = JSON.parse(sessionStorage.getItem('rec-cursor') || '{"x":720,"y":450}');
    const cur = document.createElement('div'); cur.id = 'rec-cursor';
    cur.innerHTML = '<svg width="30" height="34" viewBox="0 0 30 34"><path d="M3 2l22 15-9 2 6 11-4 2-6-11-7 7z" fill="#111827" stroke="#fff" stroke-width="2" stroke-linejoin="round"/></svg>';
    cur.style.cssText = `position:fixed;left:${pos.x}px;top:${pos.y}px;z-index:2147483647;pointer-events:none;transition:left .6s cubic-bezier(.2,.7,.2,1),top .6s cubic-bezier(.2,.7,.2,1);filter:drop-shadow(0 2px 4px rgba(0,0,0,.35))`;
    document.body.appendChild(cur);
    const ring = document.createElement('div'); ring.id = 'rec-ring';
    ring.style.cssText = 'position:fixed;z-index:2147483640;pointer-events:none;border:4px solid #f59e0b;border-radius:14px;box-shadow:0 0 0 6px rgba(245,158,11,.25),0 0 0 9999px rgba(17,24,39,.18);opacity:0;transition:all .35s';
    document.body.appendChild(ring);
    const box = document.createElement('div'); box.id = 'rec-callout';
    box.style.cssText = 'position:fixed;z-index:2147483646;max-width:520px;background:#111827;color:#fff;border-radius:16px;padding:14px 18px 14px 16px;box-shadow:0 12px 40px rgba(0,0,0,.35);font-family:"Hiragino Sans","Noto Sans JP",sans-serif;border-left:6px solid #4F46E5;opacity:0;transition:opacity .3s;pointer-events:none';
    document.body.appendChild(box);
    window.__rec = {
      moveTo(x, y) { cur.style.left = x + 'px'; cur.style.top = y + 'px'; sessionStorage.setItem('rec-cursor', JSON.stringify({x, y})); },
      click(x, y) {
        const r = document.createElement('div');
        r.style.cssText = `position:fixed;left:${x - 22}px;top:${y - 22}px;width:44px;height:44px;border-radius:50%;border:4px solid #4F46E5;z-index:2147483645;pointer-events:none;opacity:.9;transition:transform .45s ease-out,opacity .45s;transform:scale(.3)`;
        document.body.appendChild(r); requestAnimationFrame(() => { r.style.transform = 'scale(1.4)'; r.style.opacity = '0'; }); setTimeout(() => r.remove(), 500);
      },
      say(title, body, step, rect) {
        box.innerHTML = '<div style="font-size:11px;letter-spacing:.15em;color:#a5b4fc;font-weight:700;margin-bottom:4px">' + (step ? 'STEP ' + step + '　' : '') + '解説</div>'
          + '<div style="font-size:18px;font-weight:700;line-height:1.35">' + title + '</div>'
          + (body ? '<div style="font-size:13.5px;color:#d1d5db;line-height:1.5;margin-top:6px">' + body + '</div>' : '');
        const vw = innerWidth, vh = innerHeight;
        if (rect) {
          ring.style.left = (rect.x - 8) + 'px'; ring.style.top = (rect.y - 8) + 'px'; ring.style.width = (rect.w + 16) + 'px'; ring.style.height = (rect.h + 16) + 'px'; ring.style.opacity = '1';
          const bw = Math.min(520, vw - 48); box.style.maxWidth = bw + 'px';
          let left = Math.max(24, Math.min(rect.x, vw - bw - 24));
          box.style.left = left + 'px'; box.style.right = 'auto'; box.style.opacity = '0';
          requestAnimationFrame(() => {
            const bh = box.offsetHeight || 120;
            let top = rect.y + rect.h + 22;
            if (top + bh > vh - 16) top = rect.y - bh - 22;
            if (top < 16) { top = Math.max(16, Math.min(rect.y, vh - bh - 16)); left = Math.min(rect.x + rect.w + 22, vw - bw - 24); box.style.left = left + 'px'; }
            box.style.top = top + 'px'; box.style.bottom = 'auto'; box.style.opacity = '1';
          });
        } else {
          ring.style.opacity = '0';
          box.style.left = '24px'; box.style.top = 'auto'; box.style.bottom = '24px'; box.style.right = 'auto'; box.style.maxWidth = '560px';
          requestAnimationFrame(() => { box.style.opacity = '1'; });
        }
      },
      hide() { box.style.opacity = '0'; ring.style.opacity = '0'; },
    };
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup); else setup();
})();
"""


class Actor:
    """録画される鈴木の画面。カーソルを描きながら操作する"""

    def __init__(self, page, api: requests.Session):
        self.page, self.api = page, api

    def _box(self, locator):
        locator.first.wait_for(state="visible", timeout=15000)
        locator.first.scroll_into_view_if_needed()
        return locator.first.bounding_box()

    def move(self, locator, dwell: float = 0.7):
        b = self._box(locator)
        x, y = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        self.page.evaluate("([x, y]) => window.__rec && window.__rec.moveTo(x, y)", [x, y])
        self.page.mouse.move(x, y, steps=12)
        pause(dwell)
        return x, y

    def click(self, locator, after: float = 0.8, dwell: float = 0.7):
        x, y = self.move(locator, dwell)
        self.page.evaluate("([x, y]) => window.__rec && window.__rec.click(x, y)", [x, y])
        pause(0.25)
        locator.first.click()
        pause(after)

    def type(self, locator, text: str, delay: int = 55):
        self.click(locator, after=0.4)
        self.page.keyboard.type(text, delay=delay)
        pause(0.6)

    def nav(self, label: str, wait: float = 1.2):
        self.click(self.page.locator(f'aside nav a:has-text("{label}")').first, after=0.2)
        self.page.wait_for_load_state("networkidle")
        pause(wait)

    def goto(self, path: str, wait: float = 1.2):
        self.page.goto(f"{BASE}{path}")
        self.page.wait_for_load_state("networkidle")
        pause(wait)

    def scroll(self, dy: int, steps: int = 6, dwell: float = 0.35):
        for _ in range(steps):
            self.page.mouse.wheel(0, dy / steps)
            pause(dwell)

    def say(self, title: str, body: str = "", step: str = "", at=None, hold: float = 0):
        rect = None
        if at is not None:
            try:
                b = self._box(at)
                rect = {"x": b["x"], "y": b["y"], "w": b["width"], "h": b["height"]}
            except Exception:
                rect = None
        try:
            self.page.evaluate("([t, b, s, r]) => window.__rec && window.__rec.say(t, b, s, r)", [title, body, step, rect])
        except Exception:
            pass
        if hold:
            pause(hold)

    def hide(self):
        try:
            self.page.evaluate("() => window.__rec && window.__rec.hide()")
        except Exception:
            pass

    def tick_via_button(self, before: int, watch_agent: bool = True):
        """「今すぐ巡回」を押して巡回を待つ。イベントで既に走っていればそれを待つ"""
        time.sleep(3)
        wait_idle(self.api)
        if agent_status(self.api)["ticks"] <= before:
            btn = self.page.locator('header button:has-text("今すぐ巡回")').first
            self.say("「今すぐ巡回」を押す", "普段は 60 秒ごと＋投稿・保存・会議終了で自動で動きます。動画では手で押しています", at=btn, hold=1.2)
            self.click(btn, after=0.5)
        if watch_agent:
            self.nav("エージェント", wait=0.5)
            self.say("エージェントが動いている", "取込 → 抽出 → 紐付け → 判定 → 通知 → 資料 の 6 担当が順に実行。ログの左が担当のバッジ", at=self.page.locator("main .bg-gray-900").first, hold=3)
        wait_tick(self.api, before)
        time.sleep(2)
        wait_idle(self.api)
        pause(1.5)


# ---------- 会議の中身 ----------

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


def inject_tracks(meeting_id: str) -> None:
    """話者ごとの文字起こし結果を直接入れる（音声は無し）"""
    conn = db()
    started = datetime.now().replace(microsecond=0) - timedelta(minutes=4)
    conn.execute("UPDATE meetings SET started_at=? WHERE id=?", (started.isoformat(), meeting_id))
    by: dict[str, list] = {}
    for t, sp, text in LINES:
        by.setdefault(sp, []).append({"t": t, "text": text})
    for sp, utts in by.items():
        conn.execute("INSERT INTO meeting_tracks (id, meeting_id, speaker, mime, audio, rec_started_at, uploaded_at, utterances) VALUES (?,?,?,?,?,?,?,?)",
                     (os.urandom(6).hex(), meeting_id, sp, "audio/webm", b"", started.isoformat(), datetime.now().replace(microsecond=0).isoformat(),
                      json.dumps(utts, ensure_ascii=False)))
    conn.commit()
    conn.close()


CAP_JS = """
async ([base, mid, name, text]) => {
  window.__wss = window.__wss || {};
  let ws = window.__wss[name];
  if (!ws || ws.readyState > 1) {
    ws = new WebSocket(base.replace('http', 'ws') + '/ws/meet/' + mid + '?name=' + encodeURIComponent(name));
    window.__wss[name] = ws;
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  }
  ws.send(JSON.stringify({ type: 'caption', text, final: true }));
}
"""


# ---------- 成果物・チャット（他の人の操作は API） ----------

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


def upload(s: requests.Session, actor: str, data: bytes, note: str = "") -> None:
    s.post(f"{BASE}/artifacts/upload", data={"actor": actor, "note": note, "path": FOLDER, "channel": ""},
           files={"file": (FILE, data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})


def post(s: requests.Session, actor: str, text: str, quote_of: str = "") -> str:
    s.post(f"{BASE}/chat", data={"channel": "general", "actor": actor, "text": text, "quote_of": quote_of})
    return last_message_id(actor)


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
        yamada, suzuki_api = session("山田"), session("鈴木")
        with sync_playwright() as pw:
            browser = pw.chromium.launch()      # マイク・カメラ無し → 会議室は「閲覧のみ」。録音のアップロードは発生しない
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, record_video_dir=str(OUT),
                                      record_video_size={"width": 1440, "height": 900}, locale="ja-JP")
            ctx.add_init_script(INIT_JS)
            page = ctx.new_page()
            page.on("dialog", lambda d: d.accept())
            a = Actor(page, suzuki_api)
            ctx2 = browser.new_context(locale="ja-JP")     # 他の参加者の字幕を送る裏の画面（録画されない）
            back = ctx2.new_page()
            back.goto(f"{BASE}/login/as/%E5%B1%B1%E7%94%B0?next=/")

            # ===== 0. まっさらな状態 =====
            a.goto("/login", wait=0.8)
            a.say("まっさらな状態から始めます", "データは何も入っていません。会議も、チャットも、成果物も、Wiki もゼロ。管理者の鈴木としてサインインします", hold=4)
            a.click(page.locator("text=鈴木").first, after=0.3)
            page.wait_for_url(f"{BASE}/")
            page.wait_for_load_state("networkidle")
            a.say("ダッシュボード: 検知 0・タスク 0", "エージェントはまだ何も見つけていません", "0", at=page.locator("main .grid").first, hold=4)
            for label, title in [("検知結果", "検知結果: 0 件"), ("タスク", "タスク: 0 件"), ("チャット", "チャット: 投稿なし"),
                                 ("会議室", "会議室: 会議なし"), ("成果物", "成果物: ファイルなし"), ("Wiki", "Wiki: ページなし")]:
                a.nav(label, wait=0.6)
                a.say(title, "すべて空です。ここから 1 つのプロジェクトを立ち上げます", "0", hold=2.6)

            # ===== 1. チャットで宣言 =====
            a.nav("チャット", wait=0.8)
            ed = page.locator('[contenteditable="true"]').first
            a.say("プロジェクトの立ち上げを宣言", "普段どおりチャットに書くだけです", "1", at=ed, hold=1.5)
            a.type(ed, f"{PROJECT}の立ち上げを始めます。これからキックオフ会議をやります。")
            a.click(page.locator('button:has-text("送信")').first, after=1.5)
            a.say("投稿をきっかけにエージェントが巡回", "60 秒待たなくても、投稿・保存・会議終了で即時に動きます", "1", at=page.locator("#msg-" + last_message_id("鈴木")), hold=3)

            # ===== 2. 会議 =====
            a.nav("会議室", wait=0.8)
            btn = page.locator('button:has-text("今すぐ会議")').first
            a.say("会議室を作る", "自作の会議室。参加者の発言が話者別に文字起こしされます", "2", at=btn, hold=1.5)
            a.click(btn, after=0.6)
            title_in = page.locator('input[name="title"]').first
            a.click(title_in, after=0.2)
            page.keyboard.press("Meta+A")
            page.keyboard.type(MEETING_TITLE, delay=70)
            pause(0.6)
            a.click(page.locator('form[action="/meet"] button:has-text("開始")').first, after=0.3)
            page.wait_for_url("**/meet/**")
            meeting_id = page.url.split("/meet/")[1].split("/")[0].split("?")[0]
            page.wait_for_load_state("networkidle")
            pause(1.5)
            a.say("会議中: 参加者の発言がライブ字幕になる", "山田・田中・佐藤が別の端末から参加。発言は話者ごとに記録されます", "2", hold=2)
            for t, sp, text in LINES:
                if sp == "鈴木":
                    page.evaluate("([n, t]) => { showCaption(n, t, true); if (typeof ws !== 'undefined' && ws && ws.readyState === 1) ws.send(JSON.stringify({type:'caption', text: t, final: true})); }", [sp, text])
                else:
                    back.evaluate(CAP_JS, [BASE, meeting_id, sp, text])
                pause(2.2)
            inject_tracks(meeting_id)
            pause(1)
            end_btn = page.locator("#endBtn").first
            a.say("会議を終了する", "人がするのはこのボタンを押すだけ。ここから議事録づくりが始まります", "2", at=end_btn, hold=2.5)
            a.click(end_btn, after=0.5)
            for _ in range(120):
                st = suzuki_api.get(f"{BASE}/meet/{meeting_id}/status").json()
                if st.get("status") == "done":
                    break
                time.sleep(1)
            else:
                raise RuntimeError(f"meeting not finalized: {st}")
            page.wait_for_url(f"{BASE}/meet/{meeting_id}", timeout=60000)
            page.wait_for_load_state("networkidle")
            a.say("文字起こしが確定", "ここからエージェントが自動で決定・タスク・懸念を抽出します（会議終了がトリガー）", "3", hold=3)
            n0 = agent_status(suzuki_api)["ticks"]
            a.tick_via_button(n0, watch_agent=True)

            # ===== 3. 議事録サマリー =====
            a.goto(f"/meet/{meeting_id}", wait=0.8)
            link = page.locator('a[href$="/minutes"]').first
            a.say("議事録サマリーを開く", "", "3", at=link, hold=1.2)
            a.click(link, after=0.3)
            page.wait_for_load_state("networkidle")
            pause(1)
            a.say("議事録サマリーができた", "決定・タスク・懸念の 3 区分。各項目に根拠の発言（原文）が付きます。人は書いていません", "3", hold=4)
            a.scroll(700, steps=8, dwell=0.5)
            a.say("根拠の引用が原文に無い項目は捨てられる", "幻覚チェック。指示文の混入も弾きます", "3", hold=3)

            # ===== 4. タスク =====
            a.nav("タスク", wait=0.8)
            a.say("タスクが担当者つきで登録済み", "「山田さん、今週中に」の発言から担当と期限を読み取り。起票の作業がなくなります", "4", at=page.locator("main").first, hold=4)
            conn = db()
            task = conn.execute("SELECT id FROM tasks WHERE title LIKE '%見込%' ORDER BY created_at LIMIT 1").fetchone()
            wiki_min = conn.execute("SELECT id FROM wiki_pages WHERE space='議事録' ORDER BY created_at DESC LIMIT 1").fetchone()
            conn.close()
            if task:
                card = page.locator(f'a[href="/tasks/{task["id"]}"]').first
                a.say("タスクを開く", "", "4", at=card, hold=1)
                a.click(card, after=0.3)
                page.wait_for_load_state("networkidle")
                a.say("タスク詳細: 根拠の発言と紐付いている", "どの会議のどの発言から生まれたタスクかを遡れます", "4", hold=3)
                a.scroll(500, steps=6)
                pause(1)
            a.nav("Wiki", wait=0.8)
            if wiki_min:
                lnk = page.locator(f'a[href="/wiki/{wiki_min["id"]}"]').first
                a.say("議事録ページが自動作成されている", "資料係が Wiki「議事録」スペースに置きます", "4", at=lnk, hold=2)
                a.click(lnk, after=0.3)
                page.wait_for_load_state("networkidle")
                a.scroll(400, steps=5)
                pause(1.5)

            # ===== 5. 山田が初版を共有 =====
            a.nav("チャット", wait=0.8)
            a.say("別の端末で、山田が成果物の初版を上げて共有します", "URL を貼るだけ。エージェントがどのタスクの話かを推論して紐付けます", "5", hold=2)
            t_before = agent_status(suzuki_api)["ticks"]
            upload(yamada, "山田", forecast_xlsx(0, 450), "初版")
            m1 = post(yamada, "山田", f"売上見込表の初版を上げました。会議で決めた 第4四半期 450 で入れています {BASE}/artifacts/file/{FOLDER}/{FILE}")
            live = page.get_by_text("売上見込表の初版を上げました").first
            live.wait_for(timeout=20000)
            a.say("山田の投稿が届いた", "リンク先はライブラリの版。投稿がトリガーになり、エージェントがタスクに紐付けます", "5", at=live, hold=3.5)
            a.tick_via_button(t_before, watch_agent=False)
            a.nav("成果物", wait=0.8)
            a.say("ライブラリに v1 として登録", "保存するたびに版が採番されます。ファイル名の _v2 や _final は不要", "5", at=page.locator("main").first, hold=3.5)

            # ===== 6. 山田が数字を変えた版を共有 → 矛盾検知 =====
            a.nav("チャット", wait=0.8)
            a.say("山田が更新版を共有（第4四半期を 450 → 500 に）", "会議では「第4四半期は 450 から変更しない」と決めたはず。チャットには数字の変更が書かれておらず、人は気づけません", "6", hold=2.5)
            t_before = agent_status(suzuki_api)["ticks"]
            upload(yamada, "山田", forecast_xlsx(0, 500), "")
            m2 = post(yamada, "山田", f"見込表を更新しました {BASE}/artifacts/file/{FOLDER}/{FILE}")
            live = page.get_by_text("見込表を更新しました").first
            live.wait_for(timeout=20000)
            a.say("「更新しました」だけの投稿", "", "6", at=live, hold=2.5)
            a.tick_via_button(t_before, watch_agent=True)
            a.nav("ダッシュボード", wait=0.8)
            a.say("エージェントが矛盾を見つけた", "保存されたセルの差分（450 → 500）を会議の決定と突き合わせ、根拠つきで指摘。人は誰も「確認して」と頼んでいません", "6", at=page.locator("main .grid").first, hold=4)
            a.nav("検知結果", wait=0.8)
            card = page.locator("main .rounded-xl").first
            a.say("検知カード: 決定 → 発言 → 変更 の根拠 3 点", "判定理由も表示。対応はタスク化・通知・確認・却下から人が選びます", "6", at=card, hold=5)
            a.scroll(300, steps=4)
            pause(1.5)

            # ===== 7. 鈴木が引用して差し戻し → 山田が修正 =====
            a.nav("チャット", wait=0.8)
            page.locator(f"#msg-{m2}").wait_for(timeout=15000)
            a.move(page.locator(f"#msg-{m2}"), dwell=0.4)
            a.say("山田の投稿を引用して差し戻す", "引用返信にすると、エージェントはこの会話を同じタスクの続きとして追えます", "7", at=page.locator(f"#msg-{m2}"), hold=2)
            page.locator(f"#msg-{m2}").hover()
            pause(0.4)
            a.click(page.locator(f'#msg-{m2} button[data-tip*="引用"]').first, after=0.6)
            ed = page.locator('[contenteditable="true"]').first
            a.type(ed, "エージェントの指摘どおりです。会議で今期の見込みは変更しないと決めたので、第4四半期は会議の数字に戻してください。", delay=45)
            a.click(page.locator('button:has-text("送信")').first, after=1.5)
            a.say("山田が修正版を上げて返信します", "「直しました。確認お願いします」", "7", hold=2)
            t_before = agent_status(suzuki_api)["ticks"]
            upload(yamada, "山田", forecast_xlsx(0, 450), "")
            m3 = post(yamada, "山田", f"直しました。会議の数字に戻しています。確認お願いします {BASE}/artifacts/file/{FOLDER}/{FILE}", quote_of=last_message_id("鈴木"))
            live = page.get_by_text("直しました。会議の数字に戻しています").first
            live.wait_for(timeout=20000)
            a.say("修正版が届いた", "エージェントは修正が決定と一致していることを確認し、「確認お願いします」から状態変更を提案します", "7", at=live, hold=3.5)
            a.tick_via_button(t_before, watch_agent=False)

            # ===== 8. 提案を承認 → 完了 =====
            a.nav("検知結果", wait=0.8)
            a.click(page.locator('a:has-text("状態更新の提案")').first, after=0.8)
            btn = page.locator('button:has-text("適用する")').first
            if btn.count():
                a.say("状態更新の提案を人が承認", "エージェントは提案まで。適用ボタンを押すのは人です", "8", at=btn, hold=2.5)
                a.click(btn, after=1.2)
            a.nav("チャット", wait=0.8)
            a.type(page.locator('[contenteditable="true"]').first, "確認しました。これで OK です。完了にしてください。", delay=45)
            a.click(page.locator('button:has-text("送信")').first, after=1.5)
            t_before = agent_status(suzuki_api)["ticks"]
            a.tick_via_button(t_before, watch_agent=False)
            a.nav("検知結果", wait=0.8)
            a.click(page.locator('a:has-text("状態更新の提案")').first, after=0.8)
            btn = page.locator('button:has-text("適用する")').first
            if btn.count():
                a.say("「完了にしてください」→ 完了の提案 → 承認", "チャットを普通に使うだけで、タスク管理ツールを開いて更新する作業がなくなります", "8", at=btn, hold=2.5)
                a.click(btn, after=1.2)
            if task:
                a.goto(f"/tasks/{task['id']}", wait=0.8)
                a.say("タスク完了。履歴が 1 本に繋がっている", "会議の決定 → タスク → チャット → v1 → v2（矛盾）→ v3（修正）→ 完了。なぜこの数字になったかを誰でも遡れます", "8", hold=4)
                a.scroll(600, steps=8, dwell=0.45)
                pause(1)

            # ===== 9. 成果物の履歴と Wiki =====
            a.goto(f"/artifacts/diff/{FOLDER}/{FILE.replace('.xlsx', '_v3.xlsx')}", wait=0.8)
            a.say("成果物の版差分", "どのセルがいつ誰によって変わったか。検知と紐付いています", "9", at=page.locator("main").first, hold=4)
            conn = db()
            wiki_art = conn.execute("SELECT id FROM wiki_pages WHERE space='成果物' ORDER BY updated_at DESC LIMIT 1").fetchone()
            conn.close()
            a.nav("Wiki", wait=0.8)
            if wiki_art:
                lnk = page.locator(f'a[href="/wiki/{wiki_art["id"]}"]').first
                a.say("成果物ページも自動生成されている", "", "9", at=lnk, hold=1.5)
                a.click(lnk, after=0.3)
                page.wait_for_load_state("networkidle")
                a.say("Wiki: 中身を構造化した本文と版の履歴", "概要・表・主な数値はファイルの中身から。履歴・変更点・関連する決定はエージェントが記録。人は Wiki を書いていません", "9", hold=4)
                a.scroll(900, steps=10, dwell=0.45)
                pause(1.5)
            a.nav("ダッシュボード", wait=0.8)
            a.say("ここまで、人がしたこと", "会議で話す・チャットに書く・ファイルを保存する・提案を承認する。議事録・起票・進捗確認・版管理・Wiki・照合はエージェントが行いました", "", hold=7)

            ctx2.close()
            ctx.close()
            browser.close()
        vids = sorted(OUT.glob("*.webm"), key=lambda p: p.stat().st_mtime)
        if vids:
            dest = OUT / "flow.webm"
            shutil.move(str(vids[-1]), dest)
            print("video:", dest, dest.stat().st_size // 1024, "KB")
    finally:
        server.terminate()


if __name__ == "__main__":
    main()
