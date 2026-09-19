"""自作 Meet の音声文字起こし。Gemini Files API に話者ごとの録音を丸ごと渡して一括で文字起こしする。

（会議中のライブ文字起こしではなく、終了時のバッチ処理。精度が高く、通信量も少ない）
"""
import logging
import sqlite3
import time

import httpx

from app import config
from app.llm import cost

log = logging.getLogger("stt")

_BASE = "https://generativelanguage.googleapis.com"

TRACK_SCHEMA = {
    "type": "object",
    "properties": {
        "utterances": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "number"}, "text": {"type": "string"}},
                "required": ["t", "text"],
            },
        }
    },
    "required": ["utterances"],
}


def _key() -> str:
    if not config.GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY が未設定")
    return config.GEMINI_API_KEY


def upload(audio: bytes, mime: str) -> dict:
    r = httpx.post(f"{_BASE}/upload/v1beta/files", params={"key": _key()},
                   headers={"X-Goog-Upload-Protocol": "raw", "X-Goog-Upload-Content-Type": mime,
                            "Content-Type": mime},
                   content=audio, timeout=120)
    r.raise_for_status()
    return r.json()["file"]   # {name, uri, state, mimeType}


def wait_active(name: str) -> None:
    for _ in range(40):
        r = httpx.get(f"{_BASE}/v1beta/{name}", params={"key": _key()}, timeout=30)
        state = r.json().get("state")
        if state == "ACTIVE":
            return
        if state == "FAILED":
            raise RuntimeError("Gemini file processing failed")
        time.sleep(1.5)
    raise RuntimeError("Gemini file processing timeout")


def transcribe_track(audio: bytes, mime: str, speaker: str,
                     conn: sqlite3.Connection | None = None) -> list[dict]:
    """1人分の録音を [{t: 秒, text}] に。要約・補完はさせない（逐語）。"""
    f = upload(audio, mime)
    wait_active(f["name"])
    prompt = (f"これは会議参加者「{speaker}」1人のマイク音声です。話している日本語を逐語で正確に文字起こしし、"
              "発話のまとまりごとに区切ってJSONで出力してください。各発話に t（音声先頭からの秒数・数値）と text を付ける。"
              "要約・言い換え・補完はしない。無音やノイズだけの区間は出力しない。")
    body = {
        "contents": [{"parts": [{"file_data": {"mime_type": mime, "file_uri": f["uri"]}}, {"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json",
                             "responseSchema": TRACK_SCHEMA},
    }
    r = httpx.post(f"{_BASE}/v1beta/models/{config.GEMINI_STT_MODEL}:generateContent",
                   params={"key": _key()}, json=body, timeout=300)
    r.raise_for_status()
    j = r.json()
    usage = j.get("usageMetadata", {})
    if conn is not None:
        cost.record(conn, "stt", config.GEMINI_STT_MODEL, "stt",
                    int(usage.get("promptTokenCount", 0)), int(usage.get("candidatesTokenCount", 0)))
    text = "".join(p.get("text", "") for p in j.get("candidates", [{}])[0].get("content", {}).get("parts", []))
    import json
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        log.warning("STT JSON parse failed for %s", speaker)
        return []
    out = parsed.get("utterances", []) if isinstance(parsed, dict) else []
    return [u for u in out if isinstance(u, dict) and u.get("text")]
