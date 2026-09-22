# 展示・発表用の素材

| ファイル | 用途 | 状態 |
|---|---|---|
| `poster_A1.pdf`（`poster.html`） | ブース後ろに貼るポスター（A1 縦・594×841mm）。A2 に縮小印刷しても読める | 印刷するだけ |
| `flyer_A4.pdf`（`flyer.html`） | 配布チラシ（A4 両面）。表＝課題と画面、裏＝仕組み・コスト・セキュリティ・Q&A | 印刷するだけ（両面・短辺とじ） |
| `pitch.pdf`（`pitch.html`） | 最終ピッチ 11枚（16:9）。台本は `docs/10_ピッチ台本.md` | そのまま使える |
| `arch.svg` | アーキテクチャ図（ポスター・チラシ・スライド共通） | — |
| `screenshots/*.png` | 実画面のスクショ 13 枚（1440px 幅、`shoot.py` で再撮影可） | 記事・SNS 用 |
| `qr_github.png` / `qr_zenn.png` | GitHub リポジトリ／Zenn 記事の QR | Zenn: https://zenn.dev/leadus_nova/articles/c83d2577a48079 |

関連: `docs/11_展示デモ台本.md`（ブースでの話し方）、`docs/12_ループ動画_絵コンテ.md`（モニターで流す動画の絵コンテ）、`docs/zenn_draft.md`（Zenn 記事の下書き）

## 再生成

```bash
python promo/shoot.py     # スクショを撮り直す（サーバー起動中に。1枚 30〜60 秒）
# PDF: Chrome のヘッドレスで
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-pdf-header-footer \
  --print-to-pdf=promo/poster_A1.pdf file://$PWD/promo/poster.html
```

## デプロイ

本番: https://agent.leadus-nova.com/（Cloud Run `shinko-agent` @ leadus-nova-dev、GCS `aihack-agent-state-leadus` にバックアップ）。再デプロイは `gcloud run deploy shinko-agent --source . --project leadus-nova-dev --region asia-northeast1`（環境変数・シークレットは初回設定を引き継ぐ）。

## まだ決めていないこと（吉田さんの判断待ち）

- **顔写真・アイコン** — 入れるならポスター右上（`.who`）とスライド最終ページ
- **印刷サイズ** — A1 が高ければ A2（半分）でも文字は読める。コンビニなら A3 が上限
- **ループ動画** — 絵コンテどおり録画するだけ。字幕入れは iMovie / CapCut
- **チーム名／プロダクト名の英語表記** — いまは「進行管理エージェント」のみ

## デスクトップアプリとして使う（PWA）

https://agent.leadus-nova.com/ を Chrome / Edge で開き、アドレスバー右の「インストール」（または メニュー → アプリをインストール）を押すと、Dock / スタートメニューから単体ウィンドウで起動できる。デスクトップ通知は設定済み。Safari は「ファイル → Dock に追加」。
