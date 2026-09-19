# 進行管理エージェント

議事録・チャット・タスク・成果物を横断し、「決定されたこと」と「実際に作られているもの」の食い違いを自律的に検知する AI エージェント。AI HACK 2026 提出作品。

サンプルデータは全て架空の「青葉ビバレッジ株式会社」のもの。

## 動かし方

```bash
pip install -r requirements.txt
python -m app.cli seed      # 架空データを投入
python -m app.cli replay    # 検知までを再生
uvicorn app.main:app        # UI を見る場合
```

`.env.example` を `.env` にコピーして OrcaRouter のキーを設定する。`replay` は API キーなしで動く。

## アーキテクチャ

（M7 以降で記載）

## ドキュメント

設計書は [docs/](docs/) にある。実装の正本は [docs/01_実装仕様書.md](docs/01_実装仕様書.md)。
