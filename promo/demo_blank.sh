#!/usr/bin/env bash
# 展示用: まっさらな環境で「会議 → 議事録 → タスク → チャット → 成果物 → 矛盾検知 → 修正 → 完了 → Wiki」を回す。
#
#   bash promo/demo_blank.sh start     # 空のサーバーを起動（ポート 8790）
#   bash promo/demo_blank.sh meeting   # 会議を作って発言を入れ、終了まで（議事録・タスクができる）
#   bash promo/demo_blank.sh share     # 山田が初版 v1 を上げてチャットで共有
#   bash promo/demo_blank.sh break     # 山田が決定に反する v2（450→500）を上げる → 矛盾検知
#   bash promo/demo_blank.sh fix       # 差し戻し → 山田が v3 で修正 → 完了の提案
#   bash promo/demo_blank.sh tick      # 今すぐ巡回して終わるまで待つ
#   bash promo/demo_blank.sh state     # いまの状態（タスク・検知・Wiki）を表示
#   bash promo/demo_blank.sh stop      # サーバーを止める
#
# 本番（agent.leadus-nova.com / 架空データ入り）を初期化したいときは stop/start ではなく
#   curl -s -b cookie -X POST https://agent.leadus-nova.com/api/agent/reset
# を使う（架空データの 15 件が再生される）。
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${REC_PORT:-8790}"
BASE="http://localhost:$PORT"
WORK=/tmp/followup-blank
PY=.venv/bin/python
FOLDER=商品企画
FILE=商品X_売上見込.xlsx
COOKIE=$WORK/cookie.txt

login() { mkdir -p "$WORK"; curl -s -c "$COOKIE" -o /dev/null "$BASE/login/as/$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$1")?next=/"; }
api()   { curl -s -b "$COOKIE" "$@"; }

xlsx() {  # $1=第3四半期 $2=第4四半期 → /tmp に書き出し
  $PY - "$1" "$2" <<'EOF'
import sys, openpyxl
q3, q4 = int(sys.argv[1]), int(sys.argv[2])
wb = openpyxl.Workbook(); ws = wb.active; ws.title = "売上見込"
ws.append(["商品名", "第1四半期", "第2四半期", "第3四半期", "第4四半期"])
ws.append(["商品X", 0, 0, q3, q4])
ws.append(["合計", "=SUM(B2:B2)", "=SUM(C2:C2)", "=SUM(D2:D2)", "=SUM(E2:E2)"])
wb.save("/tmp/followup-demo.xlsx")
EOF
}

upload() {  # $1=誰が $2=メモ
  curl -s -b "$COOKIE" -o /dev/null -X POST "$BASE/artifacts/upload" \
    -F "actor=$1" -F "note=$2" -F "path=$FOLDER" -F "channel=" \
    -F "file=@/tmp/followup-demo.xlsx;filename=$FILE;type=application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
}

post() {  # $1=誰が $2=本文 [$3=引用する発言 id]
  curl -s -b "$COOKIE" -o /dev/null -X POST "$BASE/chat" \
    --data-urlencode "channel=general" --data-urlencode "actor=$1" --data-urlencode "text=$2" --data-urlencode "quote_of=${3:-}"
}

last_msg() { sqlite3 "$WORK/app.db" "select id from chat_messages where actor='$1' order by posted_at desc, rowid desc limit 1"; }

wait_tick() {  # 巡回が終わるまで待つ（最大 5 分）
  local before="$1"
  for _ in $(seq 1 300); do
    local st; st=$(api "$BASE/api/agent/status")
    local n running
    n=$(echo "$st" | python3 -c "import sys,json;print(json.load(sys.stdin)['state']['ticks'])")
    running=$(echo "$st" | python3 -c "import sys,json;print(json.load(sys.stdin)['state']['running'])")
    [ "$n" -gt "$before" ] && [ "$running" = "False" ] && return 0
    sleep 1
  done
  echo "（巡回がまだ終わりません）"
}

ticks() { api "$BASE/api/agent/status" | python3 -c "import sys,json;print(json.load(sys.stdin)['state']['ticks'])"; }

case "${1:-}" in

start)
  pkill -f "uvicorn app.main:app --port $PORT" 2>/dev/null || true
  rm -rf "$WORK"; mkdir -p "$WORK/lib"
  BLANK_START=1 DB_PATH=$WORK/app.db LIBRARY_DIR=$WORK/lib DEMO_MODE=1 \
  AGENT_AUTOSTART=1 AGENT_INTERVAL=60 APP_BASE_URL=$BASE ARTIFACT_WATCH_DIR= GCS_BUCKET= \
    nohup $PY -m uvicorn app.main:app --port "$PORT" > "$WORK/server.log" 2>&1 &
  for _ in $(seq 1 60); do curl -s -o /dev/null "$BASE/login" && break; sleep 1; done
  login 鈴木
  echo "起動しました → $BASE"
  echo "ブラウザで $BASE を開き、鈴木でサインインしてください（データは空です）"
  ;;

meeting)
  login 鈴木
  MID=$(api -X POST "$BASE/meet" --data-urlencode "title=商品X キックオフ" --data-urlencode "me=鈴木" -o /dev/null -w '%{redirect_url}' | sed 's|.*/meet/||')
  echo "会議を作りました: $BASE/meet/$MID"
  echo "→ ブラウザでこの URL を開いてください（発言が字幕として流れます）"
  $PY - "$MID" <<'EOF'
import sys, os, json, sqlite3
from datetime import datetime, timedelta
mid = sys.argv[1]
LINES = [
    (0,   "鈴木", "商品X（青葉りんごソーダ）の立ち上げキックオフを始めます。11月の発売に向けて、今日は数字と担当を決めます。"),
    (20,  "田中", "製造から聞いた条件だと、初回ロットは300ケースが上限です。"),
    (35,  "鈴木", "分かりました。初回ロットは300ケースで確定します。"),
    (55,  "山田", "売上見込ですが、第3四半期は発売前なので0、第4四半期は450ケースで見ています。"),
    (75,  "佐藤", "得意先からはもっと欲しいと言われていますが、初回は450で十分だと思います。"),
    (90,  "鈴木", "では第4四半期は450で確定。得意先から増量の要望が来ても、今期の見込みは450から変更しないでください。"),
    (110, "鈴木", "山田さん、売上見込表の初版を今週中に作ってライブラリに上げてください。"),
    (120, "山田", "はい、今週中に上げます。"),
    (130, "鈴木", "田中さん、原価試算を来週の定例までにお願いします。"),
    (140, "田中", "承知しました。来週の定例までに出します。"),
    (150, "鈴木", "佐藤さんは主要得意先3社にヒアリングして、結果を来週共有してください。"),
    (160, "佐藤", "分かりました。3社に当たります。"),
    (175, "鈴木", "決定事項を確認します。初回ロット300ケース、第4四半期の見込みは450で据え置き。以上です。"),
]
conn = sqlite3.connect("/tmp/followup-blank/app.db")
started = datetime.now().replace(microsecond=0) - timedelta(minutes=4)
conn.execute("UPDATE meetings SET started_at=? WHERE id=?", (started.isoformat(), mid))
by = {}
for t, sp, text in LINES:
    by.setdefault(sp, []).append({"t": t, "text": text})
    conn.execute("INSERT INTO meeting_captions (id, meeting_id, speaker, text, at) VALUES (?,?,?,?,?)",
                 (os.urandom(6).hex(), mid, sp, text, (started + timedelta(seconds=t)).isoformat()))
for sp, utts in by.items():
    conn.execute("INSERT INTO meeting_tracks (id, meeting_id, speaker, mime, audio, rec_started_at, uploaded_at, utterances) VALUES (?,?,?,?,?,?,?,?)",
                 (os.urandom(6).hex(), mid, sp, "audio/webm", b"", started.isoformat(),
                  datetime.now().replace(microsecond=0).isoformat(), json.dumps(utts, ensure_ascii=False)))
conn.commit(); conn.close()
print("発言 13 件を入れました")
EOF
  read -r -p "画面で発言を見せたら Enter（会議を終了して議事録を作ります）" _
  api -X POST "$BASE/meet/$MID/finalize" -o /dev/null
  for _ in $(seq 1 120); do
    s=$(api "$BASE/meet/$MID/status" | python3 -c "import sys,json;print(json.load(sys.stdin).get('status'))")
    [ "$s" = "done" ] && break; sleep 1
  done
  echo "文字起こしが確定 → エージェントが抽出します"
  B=$(ticks); api -X POST "$BASE/api/agent/trigger" -H 'content-type: application/json' -d '{"reason":"会議終了"}' -o /dev/null; wait_tick "$B"
  echo "議事録サマリー: $BASE/meet/$MID/minutes"
  echo "タスク: $BASE/tasks?view=board"
  echo "$MID" > "$WORK/meeting_id"
  ;;

share)
  login 山田
  xlsx 0 450
  B=$(ticks)
  upload 山田 "初版"
  post 山田 "売上見込表の初版を上げました。会議で決めた 第4四半期 450 で入れています $BASE/artifacts/file/$FOLDER/$FILE"
  echo "山田が v1 を上げて共有しました → 紐付けの巡回を待ちます"
  wait_tick "$B"
  echo "成果物: $BASE/artifacts?path=$FOLDER&view=cards"
  ;;

break)
  login 山田
  xlsx 0 500
  B=$(ticks)
  upload 山田 ""
  post 山田 "見込表を更新しました $BASE/artifacts/file/$FOLDER/$FILE"
  echo "山田が v2（第4四半期 450→500）を上げました。理由はチャットに書いていません"
  wait_tick "$B"
  echo "検知結果: $BASE/findings"
  ;;

fix)
  login 鈴木
  Q=$(last_msg 山田)
  post 鈴木 "エージェントの指摘どおりです。会議で今期の見込みは変更しないと決めたので、第4四半期は会議の数字に戻してください。" "$Q"
  echo "鈴木が引用して差し戻しました"
  sleep 2
  login 山田
  xlsx 0 450
  B=$(ticks)
  upload 山田 ""
  post 山田 "直しました。会議の数字に戻しています。確認お願いします $BASE/artifacts/file/$FOLDER/$FILE" "$(last_msg 鈴木)"
  echo "山田が v3 で修正しました → 状態更新の提案を待ちます"
  wait_tick "$B"
  echo "提案: $BASE/findings?kind=status_suggestion （「適用する」を押して完了にしてください）"
  ;;

done)
  login 鈴木
  B=$(ticks)
  post 鈴木 "確認しました。これで OK です。完了にしてください。" "$(last_msg 山田)"
  wait_tick "$B"
  echo "提案: $BASE/findings?kind=status_suggestion （「適用する」で完了）"
  ;;

tick)
  login 鈴木
  B=$(ticks); api -X POST "$BASE/api/agent/trigger" -H 'content-type: application/json' -d '{"reason":"手動"}' -o /dev/null; wait_tick "$B"
  echo "巡回が終わりました"
  ;;

state)
  sqlite3 -header -column "$WORK/app.db" \
    "select substr(title,1,34) タスク, assignee 担当, status 状態 from tasks;
     select kind 種別, substr(summary,1,50) 内容, status 状態, round(confidence,2) 確信度 from findings;
     select space スペース, title ページ from wiki_pages;"
  ;;

stop)
  pkill -f "uvicorn app.main:app --port $PORT" 2>/dev/null && echo "止めました" || echo "動いていません"
  ;;

*)
  sed -n '2,20p' "$0"
  ;;
esac
