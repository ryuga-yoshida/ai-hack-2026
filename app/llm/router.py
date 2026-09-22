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
EMBED_BATCH = 100   # OrcaRouter の embeddings は1リクエスト最大100件

# replay 時に True。キャッシュにない呼び出しは None を返す
CACHE_ONLY = False
# コスト記録先。None のときは記録しない
_conn: sqlite3.Connection | None = None
_lock = threading.Lock()
_cache: dict | None = None
_emb: dict[str, np.ndarray] | None = None       # 埋め込みキャッシュ（fixtures/llm_embeddings.npz）
_emb_usage: dict[str, dict] = {}
_replayed: set[str] = set()     # replay で既にコスト記録したキー（同じ文の再計算を二重計上しない）
# 直近に LLM へ送ったテキスト（マスク後）の記録。UI の「LLM に送った内容」表示用
recent_calls: list[dict] = []
RECENT_MAX = 30


def _remember(task: str, tier: str, system: str, user: str, content: str | None, cached: bool) -> None:
    from datetime import datetime as _dt
    recent_calls.append({"at": _dt.now().replace(microsecond=0).isoformat(), "task": task, "tier": tier,
                         "model": model_for(tier), "system": system[:400], "user": user[:1500],
                         "response": (content or "")[:800], "cached": cached})
    del recent_calls[:-RECENT_MAX]


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


def _load_emb() -> dict[str, np.ndarray]:
    global _emb, _emb_usage
    if _emb is None:
        _emb = {}
        try:
            with np.load(config.LLM_EMBED_CACHE_PATH, allow_pickle=False) as z:
                for k in z.files:
                    if k == "__usage__":
                        _emb_usage = json.loads(str(z[k]))
                    else:
                        _emb[k] = z[k].astype(np.float32)
        except FileNotFoundError:
            pass
    return _emb


def _save_emb() -> None:
    config.LLM_EMBED_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    arrays = {k: v.astype(np.float16) for k, v in _load_emb().items()}   # float16 で十分（サイズ半減）
    arrays["__usage__"] = np.array(json.dumps(_emb_usage))
    np.savez_compressed(config.LLM_EMBED_CACHE_PATH, **arrays)


def _key(kind: str, tier: str, payload: str) -> str:
    # モデルを変えたらキャッシュが無効になるよう、モデル名もキーに含める
    return hashlib.sha256(f"{kind}|{tier}|{model_for(tier)}|{payload}".encode()).hexdigest()[:24]


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
        try:
            cost.record(_conn, task, model_for(tier), tier,
                        int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        except sqlite3.ProgrammingError:
            # bind された接続が別スレッドのもの（巡回スレッド vs リクエスト）なら、この記録だけ別接続で行う
            from app import db as _db
            c = _db.connect()
            try:
                cost.record(c, task, model_for(tier), tier,
                            int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
            finally:
                c.close()


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
        _remember(task, entry["tier"], system, user, entry["content"], True)
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
    _remember(task, used, system, user, content, False)
    return _finish(content, used, fallback)


def _finish(content: str, used: str, fallback: bool) -> dict | list | None:
    parsed = parse_json_or_none(content)
    if isinstance(parsed, dict):
        parsed["_model"] = f"{used}(fallback)" if fallback else used
        parsed["_fallback"] = fallback
    return parsed


def embed(texts: list[str], task: str = "embed") -> list[np.ndarray]:
    """埋め込みを生成（バッチ）。キャッシュ済みのものは API を呼ばない。"""
    emb = _load_emb()
    out: list[np.ndarray | None] = [None] * len(texts)
    missing: list[int] = []
    for i, t in enumerate(texts):
        k = _key("embed", "embed", t)
        if k in emb:
            out[i] = emb[k]
            if CACHE_ONLY and k not in _replayed:
                _replayed.add(k)
                _record(task, "embed", _emb_usage.get(k, {}))
        else:
            missing.append(i)
    if missing and not CACHE_ONLY:
        missing = [i for i in missing if texts[i].strip()]     # 空文字は API が受け付けない
        for start in range(0, len(missing), EMBED_BATCH):
            idx = missing[start:start + EMBED_BATCH]
            try:
                r = httpx.post(f"{config.ORCA_BASE_URL.rstrip('/')}/embeddings", headers=_headers(),
                               json={"model": model_for("embed"), "input": [texts[i] for i in idx]},
                               timeout=config.LLM_TIMEOUT_SEC)
                r.raise_for_status()
                data = r.json()
            except (httpx.HTTPError, LLMError) as e:   # 埋め込みが取れなくてもシステムは止めない
                log.warning("embed failed: %s", e)
                continue
            usage = data.get("usage", {})
            _record(task, "embed", usage)
            share = {k: v // len(idx) for k, v in usage.items() if isinstance(v, int)}
            for j, item in enumerate(sorted(data["data"], key=lambda d: d["index"])):
                i = idx[j]
                vec = np.asarray(item["embedding"], dtype=np.float32)
                out[i] = vec
                k = _key("embed", "embed", texts[i])
                emb[k] = vec
                _emb_usage[k] = share   # usage はバッチ全体分なので件数で按分
        _save_emb()
    elif missing:
        log.warning("embed cache miss in replay: %d texts", len(missing))
    return [v if v is not None else np.zeros(1, dtype=np.float32) for v in out]
