"""LLM 呼び出しコストの記録と集計。全呼び出しで cost_logs に1行 INSERT する。"""
import sqlite3
from datetime import datetime

from app import config, db
from app.models import CostLog, new_id


def price(tier: str, input_tokens: int, output_tokens: int) -> float:
    p = config.MODEL_PRICES[tier]
    return (input_tokens * p["input"] + output_tokens * p["output"]) / 1_000_000


def record(conn: sqlite3.Connection, task: str, model: str, tier: str,
           input_tokens: int, output_tokens: int) -> CostLog:
    log = CostLog(
        id=new_id(), task=task, model=model, tier=tier,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=price(tier, input_tokens, output_tokens),
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
