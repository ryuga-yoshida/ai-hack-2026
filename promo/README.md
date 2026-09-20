# 展示・発表用の素材

| ファイル | 用途 | 状態 |
|---|---|---|
| `poster_A1.pdf`（`poster.html`） | ブース後ろに貼るポスター（A1 縦・594×841mm）。A2 に縮小印刷しても読める | 印刷するだけ |
| `flyer_A4.pdf`（`flyer.html`） | 配布チラシ（A4 両面）。表＝課題と画面、裏＝仕組み・コスト・セキュリティ・Q&A | 印刷するだけ（両面・短辺とじ） |
| `pitch.pdf`（`pitch.html`） | 最終ピッチ 8枚（16:9）。台本は `docs/10_ピッチ台本.md` | そのまま使える |
| `arch.svg` | アーキテクチャ図（ポスター・チラシ・スライド共通） | — |
| `screenshots/*.png` | 実画面のスクショ 13 枚（1440px 幅、`shoot.py` で再撮影可） | 記事・SNS 用 |
| `qr_github.png` | GitHub リポジトリの QR | public 化後もURLは同じ |

関連: `docs/11_展示デモ台本.md`（ブースでの話し方）、`docs/12_ループ動画_絵コンテ.md`（モニターで流す動画の絵コンテ）、`docs/zenn_draft.md`（Zenn 記事の下書き）

## 再生成

```bash
python promo/shoot.py     # スクショを撮り直す（サーバー起動中に。1枚 30〜60 秒）
# PDF: Chrome のヘッドレスで
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --no-pdf-header-footer \
  --print-to-pdf=promo/poster_A1.pdf file://$PWD/promo/poster.html
```

## まだ決めていないこと（吉田さんの判断待ち）

- **Zenn 記事の URL** — 公開後に `qr_zenn.png` を作ってポスター・チラシに追加（`python -c "import qrcode; qrcode.make(URL).save('promo/qr_zenn.png')"`）
- **顔写真・アイコン** — 入れるならポスター右上（`.who`）とスライド最終ページ
- **印刷サイズ** — A1 が高ければ A2（半分）でも文字は読める。コンビニなら A3 が上限
- **ループ動画** — 絵コンテどおり録画するだけ。字幕入れは iMovie / CapCut
- **チーム名／プロダクト名の英語表記** — いまは「進行管理エージェント」のみ
