"""CLI エントリポイント。

python -m app.cli seed       # 架空データを DB に投入
python -m app.cli run        # 1回 tick を実行
python -m app.cli watch      # 5分間隔で tick を繰り返す
python -m app.cli replay     # 記録済みデータを時系列で再生（デモ用）
python -m app.cli evaluate   # 評価セットを実行して精度を出す
python -m app.cli cost       # コスト集計を表示
"""
import argparse
import sys

from app import config, db


def cmd_seed(_args) -> int:
    conn = db.connect()
    db.init_db(conn)
    from fixtures import seed
    seed.run(conn)
    print(f"seed: {config.DB_PATH} に {len(db.table_names(conn))} テーブル")
    return 0


def _not_yet(name: str, milestone: str):
    def _run(_args) -> int:
        print(f"{name}: 未実装（{milestone} で実装）", file=sys.stderr)
        return 1
    return _run


COMMANDS = {
    "seed": cmd_seed,
    "run": _not_yet("run", "M7"),
    "watch": _not_yet("watch", "M7"),
    "replay": _not_yet("replay", "M8"),
    "evaluate": _not_yet("evaluate", "M8"),
    "cost": _not_yet("cost", "M6"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in COMMANDS:
        sub.add_parser(name)
    args = parser.parse_args(argv)
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
