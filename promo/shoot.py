"""展示・記事用スクリーンショット（ヘッドレス Chrome）。python promo/shoot.py"""
import subprocess, sys, pathlib, shutil, urllib.parse
CH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROF = "/tmp/chrome-shot-profile"
OUT = pathlib.Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)
BASE = "http://localhost:8781"
SHOTS = [
    ("dashboard", "/", 1440, 1100),
    ("findings", "/findings", 1440, 1000),
    ("chat", "/chat?channel=general", 1440, 900),
    ("tasks_board", "/tasks?view=board", 1440, 900),
    ("task_detail", None, 1440, 1100),
    ("artifacts", "/artifacts?view=cards&path=%E5%95%86%E5%93%81%E4%BC%81%E7%94%BB", 1440, 900),
    ("artifact_diff", "/artifacts/diff/%E5%95%86%E5%93%81%E4%BC%81%E7%94%BB/%E5%A3%B2%E4%B8%8A%E8%A6%8B%E8%BE%BC_v3.xlsx", 1440, 900),
    ("minutes", "/meet/2026-09-15_teirei/minutes", 1440, 1200),
    ("calendar", "/calendar?view=week&d=2026-09-18", 1440, 900),
    ("wiki", "/wiki/wiki-rules", 1440, 900),
    ("agent", "/agent", 1440, 900),
    ("cost", "/cost", 1440, 900),
    ("mail", "/mail", 1440, 900),
]
def shot(name, url, w, h):
    dest = OUT / f"{name}.png"
    cmd = [CH, "--headless=new", f"--user-data-dir={PROF}", f"--window-size={w},{h}", "--hide-scrollbars",
           "--virtual-time-budget=6000", f"--screenshot={dest}", url]
    try:
        subprocess.run(cmd, timeout=60, capture_output=True)
    except subprocess.TimeoutExpired:
        print("timeout", name)
    print(name, dest.exists() and dest.stat().st_size)
if __name__ == "__main__":
    shutil.rmtree(PROF, ignore_errors=True)
    shot("_login", BASE + "/login/as/%E9%88%B4%E6%9C%A8?next=/", 800, 600)
    import sqlite3
    conn = sqlite3.connect("data/app.db")
    tid = conn.execute("select task_id from findings where kind='contradiction' and task_id is not null limit 1").fetchone()[0]
    for name, url, w, h in SHOTS:
        if url is None:
            url = f"/tasks/{tid}"
        shot(name, BASE + url, w, h)
