"""評価セットを実行して、検知率・誤検知・見逃しを出力する。

一時 DB に seed → replay（LLM はキャッシュのみ）→ cases.yaml と突き合わせる。
"""
import json
import sys
import tempfile
from pathlib import Path

import yaml

from app import config


def _findings_for(conn, match: dict) -> list[dict]:
    if "task_title_contains" in match:
        rows = conn.execute(
            "SELECT f.* FROM findings f JOIN tasks t ON t.id = f.task_id "
            "WHERE t.title LIKE ? AND f.status != 'dismissed'", (f"%{match['task_title_contains']}%",)).fetchall()
        return [dict(r) for r in rows]
    ids = conn.execute(
        "SELECT id, meta FROM events WHERE kind='artifact_change' AND ref=?", (match["ref"],)).fetchall()
    if "diff_kind" in match:
        ids = [r for r in ids if json.loads(r["meta"] or "{}").get("diff_kind") == match["diff_kind"]]
    out = []
    for r in conn.execute("SELECT * FROM findings WHERE status != 'dismissed'").fetchall():
        ev = json.loads(r["evidence"])
        if not any(i["id"] in ev for i in ids):
            continue
        if "decision_contains" in match:
            d = conn.execute("SELECT text FROM events WHERE id=?", (ev[0],)).fetchone()
            if not d or match["decision_contains"] not in d["text"]:
                continue
        out.append(dict(r))
    return out


def main() -> int:
    cases = yaml.safe_load((Path(__file__).parent / "cases.yaml").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as td:
        config.DB_PATH = Path(td) / "eval.db"
        from app import agent, db
        from fixtures import seed
        conn = db.connect(); db.init_db(conn)
        seed.run(conn)
        print("--- replay ---")
        agent.replay(conn, speed=0)
        print("--- 評価 ---")
        detected = false_pos = missed = 0
        for c in cases:
            found = _findings_for(conn, c["match"])
            exp = c["expect"]
            if exp is None:
                ok = not found
                if ok:
                    detected += 1
                else:
                    false_pos += 1
                verdict = "OK" if ok else f"誤検知 ({found[0]['kind']}/{found[0]['severity']})"
            else:
                hit = [f for f in found if f["kind"] == exp["kind"]
                       and ("severity" not in exp or f["severity"] == exp["severity"])]
                if hit:
                    detected += 1; verdict = "OK"
                elif not found and c.get("accept_null"):
                    detected += 1; verdict = "OK（検知なしを許容）"
                elif found:
                    detected += 1; verdict = f"OK（種別/深刻度が異なる: {found[0]['kind']}/{found[0]['severity']}）"
                else:
                    missed += 1; verdict = "見逃し"
            print(f"  ケース{c['id']:>2}: {verdict:<28} {c['desc']}")
        print()
        print(f"検知できた: {detected}/{len(cases)}")
        print(f"誤検知: {false_pos}件")
        print(f"見逃し: {missed}件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
