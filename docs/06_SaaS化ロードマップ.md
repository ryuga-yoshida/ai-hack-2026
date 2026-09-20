# SaaS 化ロードマップ（ハッカソン後）

最終イメージ: **アカウント → 組織（有料）→ プロジェクト → メンバー、人数課金**。いまの構成からの距離を、段階ごとに書く。

## 現状（2026-09-21）
- 1 デプロイ = 1 会社（青葉ビバレッジ）。SQLite 1 ファイル、Cloud Run 1 インスタンス、GCS へ定期バックアップ
- 認証: メール＋パスワード、署名付きセッション。ロール admin / member / viewer、監査ログ
- 情報源は自作ツール（チャット・メール・予定表・会議室・Wiki・成果物）。外部連携はアダプタ形式の空実装

## 段階 1: テナント分離（1〜2週）
| やること | 方法 |
|---|---|
| 組織（org）とプロジェクト（project）のモデル | `orgs`, `projects`, `memberships(user, org, role)`。既存テーブルに `org_id`（必要なら `project_id`）を追加。**最初は「1 org = 1 SQLite ファイル」**の方が安全（漏洩の形が構造的に起きない）。`data/<org_id>/app.db` を接続時に切り替える |
| サインアップ | メール確認 → 個人アカウント → 組織を作る or 招待を受ける |
| 招待 | 招待トークンつきリンク（メール送信は SendGrid）。ロールは招待時に指定 |
| セッション | 今の署名 cookie に `org_id` を含める。組織切替は再発行 |
| 監査ログ | org スコープに。エクスポートは org 管理者のみ |

## 段階 2: 課金（1週）
| やること | 方法 |
|---|---|
| Stripe | Customer = org、Subscription = 座席数（`quantity` = アクティブメンバー数）。メンバー追加/無効化のたびに quantity を更新 |
| プラン | Free（1 プロジェクト・3 人・LLM は mid のみ）／ Team（人数課金・high 判定あり）／ Enterprise（SSO・自社 LLM ゲートウェイ） |
| LLM コストの見える化 | 今の cost_logs を org 別に集計し、プランの上限（月額の LLM 予算）に達したら high → mid に自動降格 |
| 請求の締め | Stripe Webhook で `invoice.paid` を受けて org の状態を更新。未払いは viewer 化して読み取りのみ |

## 段階 3: 実データ接続（2〜4週）
| 情報源 | 接続 |
|---|---|
| Teams / Slack | Graph（`ChannelMessage.Read.All`）／ Slack `channels:history`。org ごとに OAuth トークンを Secret Manager に |
| Outlook | Graph `Mail.Read`, `Calendars.Read`。差分は delta query |
| SharePoint / OneDrive | Graph driveItem versions → 今の `versions.json` の形に正規化 |
| Confluence | REST v2 の page versions |
| SSO | Entra ID（OIDC）。`auth.verify_session()` の差し替えで済む |

## 段階 4: 運用（継続）
- Postgres 化（`db.py` の差し替え。SQL は標準寄りにしてある）
- Cloud Run を複数インスタンスに（巡回ループを Cloud Scheduler + 単一ワーカーに分離。WebSocket は Pub/Sub でファンアウト）
- 判定プロンプトのバージョン管理と、org ごとの評価セット（誤検知の学習）
- データ保持ポリシー（録音・録画は 30 日で削除など）

## 料金の目安（1 org・10 人・成果物の変更が月 200 件）
- LLM: 判定 200 件 × $0.02 ≒ $4、抽出・埋め込み ≒ $2 → **月 $6 程度**
- インフラ: Cloud Run 常時 1 インスタンス $30〜50 を全 org で共有
- → 1 人 ¥1,000/月 でも原価率は低い。高い方の価値（矛盾を早く見つける）で価格を決められる
