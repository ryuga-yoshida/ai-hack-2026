"""架空企業の文書ファイル（docx）を2版生成する（文書差分のデモ用）。

    python fixtures/excel/make_docs.py
"""
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HERE = Path(__file__).parent

CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""


def make_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>" for p in paragraphs)
    doc = ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>"
           "<w:document xmlns:w=\"http://schemas.openxmlformats.org/wordprocessingml/2006/main\"><w:body>"
           f"{body}<w:sectPr/></w:body></w:document>")
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CT)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", doc)


V1 = [
    "商品D（青葉ゆず炭酸）企画書",
    "1. 位置づけ: 秋冬の炭酸ラインナップを強化する新商品。9月8日の定例で売上見込への追加を決定。",
    "2. 初回ロット: 第3四半期の初回ロットは300ケースとする。",
    "3. 販売計画: 第3四半期 300、第4四半期 400 を売上見込に計上する。",
    "4. 得意先: 主要得意先5社にヒアリング中。前向きな反応が多い。",
    "5. パッケージ: ゆずの黄色を基調としたデザイン。",
]
# v2（9/19 16:30 田中）: 初回ロットを 350 に修正（9/17 のチャット決定に沿う）、パッケージの色味の注記を追加
V2 = [
    "商品D（青葉ゆず炭酸）企画書",
    "1. 位置づけ: 秋冬の炭酸ラインナップを強化する新商品。9月8日の定例で売上見込への追加を決定。",
    "2. 初回ロット: 第3四半期の初回ロットは350ケースとする（製造の都合で50ケース増）。",
    "3. 販売計画: 第3四半期 350、第4四半期 450 を売上見込に計上する。",
    "4. 得意先: 主要得意先5社のうち4社が前向き。2社は正式に確定。",
    "5. パッケージ: ゆずの黄色を基調としたデザイン。初回ロットは印刷の色味が想定と異なるため、次ロットから修正する。",
]

if __name__ == "__main__":
    make_docx(HERE / "商品企画" / "商品D_企画書_v1.docx", V1)
    make_docx(HERE / "商品企画" / "商品D_企画書_v2.docx", V2)
    print("wrote 商品企画/商品D_企画書_v1.docx, _v2.docx")
