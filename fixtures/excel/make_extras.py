"""チャット・メールの文面で言及されている「共有フォルダに置いた」ファイルを実際に用意する。
    python fixtures/excel/make_extras.py
- 商品企画/商品D_原価試算_v1.xlsx  … 田中 9/17 のメール添付
- 管理部/予算会議資料_テンプレ_v1.docx … 高橋 9/11 のチャット「資料テンプレを共有フォルダに置きました」
"""
import json
from pathlib import Path
from openpyxl import Workbook
import sys
sys.path.insert(0, str(Path(__file__).parent))
from make_docs import make_docx

HERE = Path(__file__).parent

wb = Workbook(); ws = wb.active; ws.title = "原価試算"
ws.append(["項目", "単価（円/ケース）", "備考"])
for r in [["原料（ゆず果汁）", 420, "国産・秋口の相場"], ["原料（炭酸水・糖類）", 180, ""], ["容器・ラベル", 260, "新ラベル"],
          ["製造加工費", 310, "初回ロット300ケース前提"], ["物流", 140, ""], ["合計原価", "=SUM(B2:B6)", ""],
          ["想定売価", 1980, "希望小売"], ["原価率", "=B7/B8", "想定より2ポイント高い"]]:
    ws.append(r)
(HERE / "商品企画").mkdir(exist_ok=True)
wb.save(HERE / "商品企画" / "商品D_原価試算_v1.xlsx")

(HERE / "管理部").mkdir(exist_ok=True)
make_docx(HERE / "管理部" / "予算会議資料_テンプレ_v1.docx", [
    "予算会議 資料テンプレート（2026年度 下期）",
    "1. 部門サマリー: 上期の実績と下期の見込みを1ページで。",
    "2. 売上見込: 商品別・四半期別の表を貼り付ける（売上見込表の最新版から流し込み）。",
    "3. 新商品: 商品Dの位置づけと初回ロット、販売計画。",
    "4. リスクと対応: 得意先の増量要請、原価率の上振れなど。",
    "5. 依頼事項: 予算の増減があれば理由とともに記載。",
])

vp = HERE / "versions.json"
v = json.loads(vp.read_text(encoding="utf-8"))
v.setdefault("商品企画/商品D_原価試算_v1.xlsx", {"actor": "田中", "at": "2026-09-17T17:35:00", "note": "メールで山田へ送付",
    "url": "https://aoba-beverage-example.sharepoint.com/sites/planning/Shared%20Documents/商品企画/商品D_原価試算.xlsx"})
v.setdefault("管理部/予算会議資料_テンプレ_v1.docx", {"actor": "高橋", "at": "2026-09-11T15:15:00", "note": "予算会議用のテンプレ",
    "url": "https://aoba-beverage-example.sharepoint.com/sites/planning/Shared%20Documents/管理部/予算会議資料_テンプレ.docx"})
vp.write_text(json.dumps(v, ensure_ascii=False, indent=2), encoding="utf-8")
print("ok")
