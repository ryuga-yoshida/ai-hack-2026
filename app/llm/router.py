"""OrcaRouter クライアント。全ての LLM 呼び出しはここを経由する。

- OpenAI 互換 API（/chat/completions, /embeddings）を想定
- high が失敗したら mid にフォールバックし、結果に _fallback=True を付ける
- プロンプトのハッシュをキーにレスポンスをキャッシュ（fixtures/llm_cache.json）。
  replay はキャッシュだけで動くため API キーなしで再現できる
"""
import hashlib
import json
import logging
import re
import sqlite3
import threading
from typing import Literal

import httpx
import numpy as np

from app import config
from app.llm import cost

log = logging.getLogger("router")

Tier = Literal["high", "mid"]

# replay 時に True。キャッシュにない呼び出しは None を返す
CACHE_ONLY = False
# コスト記録先。None のときは記録しない
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_cache: dict | None = None


class LLMError(Exception):
    pass


def bind(conn: sqlite3.Connection | None) -> None:
    global _conn
    _conn = conn


def model_for(tier: str) -> str:
    return {"high": config.ORCA_MODEL_HIGH, "mid": config.ORCA_MODEL_MID,
            "embed": config.ORCA_MODEL_EMBED}[tier]


# ---------- キャッシュ ----------

def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(config.LLM_CACHE_PATH.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    config.LLM_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.LLM_CACHE_PATH.write_text(json.dumps(_load_cache(), ensure_ascii=False, indent=1))


def _key(kind: str, tier: str, payload: str) -> str:
    return hashlib.sha256(f"{kind}|{tier}|{payload}".encode()).hexdigest()[:24]


# ---------- JSON ----------

def parse_json_or_none(raw: str) -> dict | list | None:
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


# ---------- HTTP ----------

def _headers() -> dict:
    if not config.ORCA_API_KEY:
        raise LLMError("ORCA_API_KEY が未設定")
    return {"Authorization": f"Bearer {config.ORCA_API_KEY}", "Content-Type": "application/json"}


def _chat(tier: Tier, system: str, user: str, json_mode: bool) -> tuple[str, dict]:
    body = {
        "model": model_for(tier),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = httpx.post(f"{config.ORCA_BASE_URL.rstrip('/')}/chat/completions",
                   headers=_headers(), json=body, timeout=config.LLM_TIMEOUT_SEC)
    if r.status_code == 429 or r.status_code >= 500:
        raise LLMError(f"{tier}: HTTP {r.status_code}")
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"], data.get("usage", {})


def _record(task: str, tier: str, usage: dict) -> None:
    if _conn is None:
        return
    with _lock:
        cost.record(_conn, task, model_for(tier), tier,
                    int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))


# ---------- 公開 API ----------

def complete(system: str, user: str, tier: Tier, task: str,
             json_mode: bool = True) -> dict | list | None:
    """LLM を呼び、JSON をパースして返す。失敗時は None。

    dict を返す場合は _model / _fallback を付ける。JSON パース失敗は1回だけ再試行する。
    """
    cache = _load_cache()
    key = _key("chat", tier, system + "\n\x00\n" + user)
    if key in cache:
        entry = cache[key]
        if CACHE_ONLY:   # replay では記録済み usage からコストを再現する
            _record(task, entry["tier"], entry.get("usage", {}))
        return _finish(entry["content"], entry["tier"], entry["tier"] != tier)
    if CACHE_ONLY:
        log.warning("cache miss in replay: task=%s", task)
        return None

    used, fallback = tier, False
    content, usage = None, {}
    for attempt in range(2):
        try:
            content, usage = _chat(used, system, user, json_mode)
        except (httpx.TimeoutException, httpx.HTTPError, LLMError) as e:
            if used == "high":
                log.warning("high failed (%s) → mid にフォールバック", e)
                used, fallback = "mid", True
                try:
                    content, usage = _chat(used, system, user, json_mode)
                except (httpx.TimeoutException, httpx.HTTPError, LLMError) as e2:
                    log.warning("mid failed too: %s", e2)
                    return None
            else:
                log.warning("%s failed: %s", used, e)
                return None
        _record(task, used, usage)
        if parse_json_or_none(content) is not None or not json_mode:
            break
        log.warning("JSON parse failed (attempt %d), retrying", attempt + 1)
    else:
        return None

    cache[key] = {"tier": used, "content": content, "usage": usage, "task": task}
    _save_cache()
    return _finish(content, used, fallback)


def _finish(content: str, used: str, fallback: bool) -> dict | list | None:
    parsed = parse_json_or_none(content)
    if isinstance(parsed, dict):
        parsed["_model"] = f"{used}(fallback)" if fallback else used
        parsed["_fallback"] = fallback
    return parsed


def embed(texts: list[str], task: str = "embed") -> list[np.ndarray]:
    """埋め込みを生成（バッチ）。キャッシュ済みのものは API を呼ばない。"""
    cache = _load_cache()
    out: list[np.ndarray | None] = [None] * len(texts)
    missing: list[int] = []
    for i, t in enumerate(texts):
        k = _key("embed", "embed", t)
        if k in cache:
            out[i] = np.asarray(cache[k]["vector"], dtype=np.float32)
            if CACHE_ONLY:
                _record(task, "embed", cache[k].get("usage", {}))
        else:
            missing.append(i)
    if missing and not CACHE_ONLY:
        r = httpx.post(f"{config.ORCA_BASE_URL.rstrip('/')}/embeddings", headers=_headers(),
                       json={"model": model_for("embed"), "input": [texts[i] for i in missing]},
                       timeout=config.LLM_TIMEOUT_SEC)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage", {})
        _record(task, "embed", usage)
        for j, item in enumerate(sorted(data["data"], key=lambda d: d["index"])):
            i = missing[j]
            vec = np.asarray(item["embedding"], dtype=np.float32)
            out[i] = vec
            # usage はバッチ全体分なので件数で按分して保存する
            share = {k: v // len(missing) for k, v in usage.items() if isinstance(v, int)}
            cache[_key("embed", "embed", texts[i])] = {"vector": vec.tolist(), "usage": share}
        _save_cache()
    elif missing:
        log.warning("embed cache miss in replay: %d texts", len(missing))
    return [v if v is not None else np.zeros(1, dtype=np.float32) for v in out]
