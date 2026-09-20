"""設定・閾値の一元管理。数値はここ以外に直書きしない。"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- 環境変数 ---
_db = Path(os.getenv("DB_PATH", "./data/app.db"))
DB_PATH = _db if _db.is_absolute() else ROOT / _db   # 相対パスはリポジトリ直下基準
ORCA_API_KEY = os.getenv("ORCA_API_KEY", "")
ORCA_BASE_URL = os.getenv("ORCA_BASE_URL", "")
ORCA_MODEL_HIGH = os.getenv("ORCA_MODEL_HIGH", "")
ORCA_MODEL_MID = os.getenv("ORCA_MODEL_MID", "")
ORCA_MODEL_EMBED = os.getenv("ORCA_MODEL_EMBED", "")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")   # チャットに貼る会議 URL の基点
# 監視フォルダ（OneDrive の同期フォルダやデスクトップの任意フォルダ）。ここに置いた .xlsx を本物の Excel で保存すると
# 新しい版として取り込む。未設定なら監視しない
ARTIFACT_WATCH_DIR = os.getenv("ARTIFACT_WATCH_DIR", "")
ARTIFACT_WATCH_ACTOR = os.getenv("ARTIFACT_WATCH_ACTOR", "山田")   # 監視フォルダ経由の更新者（OneDrive は更新者を教えてくれない）

FIXTURES_DIR = ROOT / "fixtures"

# --- 紐付け ---
LINK_CONTEXT_WINDOW_MIN = 30      # 会話文脈を継承する時間
LINK_EMBED_TOP_K = 3
LINK_EMBED_THRESHOLD = 0.55

# --- 検知 ---
DETECT_CANDIDATE_TOP_K = 5
DETECT_LOOKBACK_DAYS = 30
DETECT_NOTIFY_THRESHOLD = 0.8     # これ以上で自動通知
DETECT_REVIEW_THRESHOLD = 0.5     # これ以上で確認キュー

TASK_AUTOGEN_CONFIDENCE = 0.7
DEDUPE_TASK_SIM = 0.82            # 既存タスクと同一とみなす埋め込み類似度
DEDUPE_DECISION_SIM = 0.88        # 同一会議内で同じ決定とみなす類似度
STALLED_DAYS = 7
DISMISS_PENALTY = 0.7
FALLBACK_PENALTY = 0.9

# --- LLM ---
# 単価（USD / 1M tokens）。OrcaRouter の実際の料金に合わせて書き換える
MODEL_PRICES = {
    "high":  {"input": 3.00, "output": 15.00},
    "mid":   {"input": 0.30, "output": 1.20},
    "embed": {"input": 0.02, "output": 0.0},
}
LLM_TIMEOUT_SEC = 60
LLM_CACHE_PATH = FIXTURES_DIR / "llm_cache.json"            # chat の応答
LLM_EMBED_CACHE_PATH = FIXTURES_DIR / "llm_embeddings.npz"   # 埋め込み（float16）

# --- 抽出 ---
CHUNK_SIZE_CHARS = 1500        # 800〜1200 トークン相当
CHUNK_OVERLAP_CHARS = 300      # 前後 200 トークン相当
CHAT_CHUNK_MESSAGES = 20       # チャットは同一チャンネルの連続 20 発言

# --- マスキング用の登場人物（fixtures の5名） ---
PERSONS = ["鈴木", "山田", "田中", "佐藤", "高橋"]
PERSON_INFO = {
    "鈴木": {"role": "プロジェクトリーダー", "dept": "商品企画部"},
    "山田": {"role": "担当（見込み表）", "dept": "商品企画部"},
    "田中": {"role": "担当（製造）", "dept": "商品企画部"},
    "佐藤": {"role": "営業", "dept": "営業部"},
    "高橋": {"role": "管理部", "dept": "管理部"},
}

# --- 検知（追加） ---
DETECT_CANDIDATE_THRESHOLD = 0.35  # 候補とみなす最低類似度（未満なら orphan_change）
DETECT_SUPERSEDE_SIM = 0.85        # これ以上似た新しい決定があれば古い決定は上書き済みとみなす

# --- 自作 Meet（音声文字起こし） ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_STT_MODEL = os.getenv("GEMINI_STT_MODEL", "gemini-2.5-flash")
MODEL_PRICES["stt"] = {"input": 1.00, "output": 2.50}   # 音声入力の単価（USD / 1M tokens。要確認）


# --- UI から変更できる設定（DB の settings テーブルで上書き） ---
TUNABLE = {
    "DETECT_NOTIFY_THRESHOLD": ("自動通知する確信度", float, "これ以上なら人の確認なしに通知"),
    "DETECT_REVIEW_THRESHOLD": ("確認キューに入れる確信度", float, "これ以上なら人の確認へ。未満は記録しない"),
    "STALLED_DAYS": ("停滞とみなす日数", int, "この日数、どこにも動きがなければ停滞"),
    "DETECT_LOOKBACK_DAYS": ("決定を遡る日数", int, "これより古い決定とは突き合わせない"),
    "TASK_AUTOGEN_CONFIDENCE": ("タスク自動生成の確信度", float, "抽出したタスク候補をこれ以上ならタスクにする"),
    "LINK_CONTEXT_WINDOW_MIN": ("会話文脈を継承する分数", int, "直前の発言と同じタスクとみなす時間"),
    "DISMISS_PENALTY": ("却下後の割引率", float, "却下された類似の指摘の確信度に掛ける"),
}


def apply_overrides(values: dict) -> None:
    """DB に保存された設定で config の値を上書きする（サーバー起動時・変更時に呼ぶ）"""
    import sys
    mod = sys.modules[__name__]
    for key, raw in values.items():
        if key in TUNABLE:
            try:
                setattr(mod, key, TUNABLE[key][1](raw))
            except (TypeError, ValueError):
                pass
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "50"))   # 成果物/チャット添付のアップロード上限
