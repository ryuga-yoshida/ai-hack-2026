"""CLI エントリポイント。

python -m app.cli seed       # 架空データを DB に投入
python -m app.cli run        # 1回 tick を実行
python -m app.cli watch      # 5分間隔で tick を繰り返す
python -m app.cli replay     # 記録済みデータを時系列で再生（デモ用・API キー不要）
python -m app.cli evaluate   # 評価セットを実行して精度を出す
python -m app.cli cost       # コスト集計を表示
"""
import argparse
import logging
import sys

from app import config, db


def cmd_seed(args) -> int:
    if args.reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    conn = db.connect()
    db.init_db(conn)
    from fixtures import seed
    from app import auth
    seed.run(conn)
    auth.seed_demo_users(conn)
    print(f"seed: {config.DB_PATH} に {len(db.table_names(conn))} テーブル・デモ用アカウント5件（パスワード: DEMO_PASSWORD）")
    return 0


def cmd_run(_args) -> int:
    from app import agent
    conn = db.connect(); db.init_db(conn)
    r = agent.tick(conn)
    print(f"run: 取込{r.fetched} 抽出{r.extracted} Task生成{r.tasks_created} 埋め込み{r.embedded} "
          f"紐付け{r.linked} Finding{len(r.findings)} actions={r.actions}")
    return 0


def cmd_watch(args) -> int:
    from app import agent
    conn = db.connect(); db.init_db(conn)
    try:
        agent.watch(conn, interval_sec=args.interval)
    except KeyboardInterrupt:
        print("\nwatch: 停止")
    return 0


def cmd_replay(args) -> int:
    from app import agent
    if args.reset and config.DB_PATH.exists():
        config.DB_PATH.unlink()
    conn = db.connect(); db.init_db(conn)
    from fixtures import seed
    seed.run(conn)
    results = agent.replay(conn, speed=args.speed, record=args.record)
    n = sum(len(r.findings) for r in results)
    print(f"replay: Finding {n}件")
    return 0


def cmd_evaluate(_args) -> int:
    from eval import run_eval
    return run_eval.main()


def cmd_cost(_args) -> int:
    from app.llm import cost
    conn = db.connect(); db.init_db(conn)
    s = cost.summary(conn)
    print(f"本デモの全処理コスト: ${s['total_usd']:.4f}")
    if s["by_task"]:
        print("  " + " / ".join(f"{k} ${v:.4f}" for k, v in s["by_task"].items()))
    print("呼び出し回数: " + " / ".join(f"{k} {v}" for k, v in s["calls"].items()) if s["calls"] else "呼び出し回数: 0")
    print("LLM を使わなかった処理: 分割・正規化・差分の自然言語化・候補絞り込み・紐付け第1〜3段・時系列ガード・stalled/orphan 検知")
    if s["if_all_high"]:
        ratio = s["if_all_high"] / s["total_usd"] if s["total_usd"] else 0
        print(f"全て high で実行した場合: ${s['if_all_high']:.4f}（{ratio:.1f}倍、削減率 {s['savings_ratio']*100:.0f}%）")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("seed"); p.add_argument("--reset", action="store_true", help="DB を作り直す")
    sub.add_parser("run")
    p = sub.add_parser("watch"); p.add_argument("--interval", type=int, default=300)
    p = sub.add_parser("replay"); p.add_argument("--speed", type=float, default=1.0)
    p.add_argument("--reset", action="store_true", default=True)
    p.add_argument("--record", action="store_true", help="API を呼んで LLM キャッシュを作る（通常はキャッシュのみ）")
    sub.add_parser("evaluate")
    sub.add_parser("cost")
    args = parser.parse_args(argv)
    return {"seed": cmd_seed, "run": cmd_run, "watch": cmd_watch, "replay": cmd_replay,
            "evaluate": cmd_evaluate, "cost": cmd_cost}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
