"""LLM 呼び出しコストの記録と集計。全呼び出しで cost_logs に1行 INSERT する。"""
import json
import logging
import sqlite3
from datetime import datetime

import httpx

from app import config, db
from app.models import CostLog, new_id

log = logging.getLogger("cost")


_pricing: dict[str, dict] | None = None


def model_prices() -> dict[str, dict]:
    """{model_id: {"input": USD/1M, "output": USD/1M}}。OrcaRouter の /v1/models から取得し
    fixtures/llm_pricing.json に保存する（replay ではファイルだけを使う）。"""
    global _pricing
    if _pricing is not None:
        return _pricing
    path = config.FIXTURES_DIR / "llm_pricing.json"
    try:
        _pricing = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        _pricing = {}
    if not _pricing and config.ORCA_API_KEY and config.ORCA_BASE_URL:
        try:
            r = httpx.get(f"{config.ORCA_BASE_URL.rstrip('/')}/models",
                          headers={"Authorization": f"Bearer {config.ORCA_API_KEY}"}, timeout=30)
            r.raise_for_status()
            for m in r.json().get("data", []):
                p = m.get("pricing") or {}
                if p.get("prompt") is not None:
                    _pricing[m["id"]] = {"input": float(p["prompt"]) * 1_000_000,
                                         "output": float(p.get("completion") or 0) * 1_000_000}
            path.write_text(json.dumps(_pricing, ensure_ascii=False, indent=1))
        except Exception as e:   # 料金が取れなくても処理は止めない
            log.warning("pricing fetch failed: %s", e)
    return _pricing


def price(tier: str, input_tokens: int, output_tokens: int, model: str | None = None) -> float:
    from app.llm.router import model_for   # 循環 import 回避
    p = model_prices().get(model or model_for(tier)) or config.MODEL_PRICES[tier]
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def record(conn: sqlite3.Connection, task: str, model: str, tier: str,
           input_tokens: int, output_tokens: int) -> CostLog:
    log = CostLog(
        id=new_id(), task=task, model=model, tier=tier,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=price(tier, input_tokens, output_tokens, model=model),
        occurred_at=datetime.now().replace(microsecond=0),
    )
    db.save_cost_log(conn, log)
    return log


def summary(conn: sqlite3.Connection) -> dict:
    """デモ用の集計。if_all_high は実測トークン数に high の単価を掛けた試算。"""
    rows = conn.execute(
        "SELECT task, tier, COUNT(*) n, SUM(input_tokens) i, SUM(output_tokens) o, SUM(cost_usd) c "
        "FROM cost_logs GROUP BY task, tier").fetchall()
    total = 0.0
    by_task: dict[str, float] = {}
    by_tier: dict[str, float] = {}
    calls: dict[str, int] = {}
    if_all_high = 0.0
    for r in rows:
        total += r["c"]
        by_task[r["task"]] = by_task.get(r["task"], 0.0) + r["c"]
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0.0) + r["c"]
        calls[r["tier"]] = calls.get(r["tier"], 0) + r["n"]
        if_all_high += price("high", r["i"], r["o"])
    return {
        "total_usd": round(total, 6),
        "by_task": {k: round(v, 6) for k, v in by_task.items()},
        "by_tier": {k: round(v, 6) for k, v in by_tier.items()},
        "calls": calls,
        "if_all_high": round(if_all_high, 6),
        "savings_ratio": round(1 - total / if_all_high, 3) if if_all_high else 0.0,
    }
