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
LLM_CACHE_PATH = FIXTURES_DIR / "llm_cache.json"

# --- 抽出 ---
CHUNK_SIZE_CHARS = 1500        # 800〜1200 トークン相当
CHUNK_OVERLAP_CHARS = 300      # 前後 200 トークン相当
CHAT_CHUNK_MESSAGES = 20       # チャットは同一チャンネルの連続 20 発言

# --- マスキング用の登場人物（fixtures の5名） ---
PERSONS = ["鈴木", "山田", "田中", "佐藤", "高橋"]

# --- 検知（追加） ---
DETECT_CANDIDATE_THRESHOLD = 0.35  # 候補とみなす最低類似度（未満なら orphan_change）
DETECT_SUPERSEDE_SIM = 0.85        # これ以上似た新しい決定があれば古い決定は上書き済みとみなす

# --- 自作 Meet（音声文字起こし） ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_STT_MODEL = os.getenv("GEMINI_STT_MODEL", "gemini-2.5-flash")
MODEL_PRICES["stt"] = {"input": 1.00, "output": 2.50}   # 音声入力の単価（USD / 1M tokens。要確認）
