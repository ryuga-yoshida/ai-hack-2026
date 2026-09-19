# Excelセル差分エンジン 実装書

2026-09-20

## 目的とスコープ

Excelファイルの2バージョンを比較し、**人間が読んで意味のわかる差分**を構造化データとして出力する。「B12が変わった」ではなく「第3四半期の売上見込が1200→1500に変更」という粒度まで持ち上げることがこのモジュールの責務。

下流の矛盾検知エンジンが、この出力を議事録の決定事項やチャットの進捗発言と突き合わせる。したがって出力は LLM にそのまま渡して自然言語化できる形であることが要件。

### やること

- セル単位の値の差分検出
- 数式の差分検出（値が同じでも数式が変わったケースを拾う）
- 行の挿入・削除の検出と、それによる座標ズレの吸収
- 行ラベルと列ラベルを付与した意味的な差分レコードの生成

### やらないこと（スコープ外）

- 書式・スタイル・セル結合の差分 — ノイズにしかならないため明示的に捨てる
- 列の挿入・削除への対応 — 行に比べ発生頻度が低く、実装コストに見合わない
- グラフ、ピボットテーブル、マクロの差分
- 3バージョン以上の系譜解析 — 常に2点間比較に限定する

### 制約

AI HACK 2026（提出 9/22 15:00）向け。実装に割ける時間は半日程度を想定し、外部依存は openpyxl のみとする。提出物は public リポジトリとなるため、**テストデータは全て架空企業のものを使い、実データは一切含めない**。

## 前提と入力

### 比較対象の取得

2つのバージョンの .xlsx をローカルパスで受け取ることだけを前提とする。**どのストレージから落としてきたかをこのモジュールは知らない。** Box、SharePoint、Google Drive のいずれを使うかは上位のコネクタ層の責務であり、差分エンジンはファイルパス2本だけを受け取る。

これによりコネクタの選定を保留したまま差分ロジックを先に完成させられる。ハッカソンの時間配分上、この分離は必須。

### 入力インターフェース

```python
diff_workbooks(
    old_path: str,      # 旧バージョンの .xlsx
    new_path: str,      # 新バージョンの .xlsx
    key_column: int = 1 # 行キーとして使う列番号（1始まり）
) -> list[DiffRecord]
```

### 想定するファイルの形

業務でよく使われる、**1行目がヘッダ、A列が項目名、B列以降が数値**という表形式を第一級でサポートする。売上見込表、予算表、タスク一覧などがこれに該当する。

この形から外れるファイル（複数の表が1シートに同居、ヘッダが複数行、セル結合が多用されている等）は、値の差分だけは出るが行ラベルが付かず、意味的な紐付けの精度は落ちる。今回はそれを許容する。

### テストデータ

架空の「青葉ビバレッジ株式会社」の四半期売上見込表を2版用意する。版間の変更は以下を必ず含めること。デモで見せたい検知が全て発火する構成になる。

- 数値のみの変更（議事録の決定と矛盾させる用）
- 数式の変更（合計範囲の拡張）
- 行の挿入（以降の座標が全てズレる）
- 行の削除

## 差分の3層モデル

差分を3つの層に分けて検出する。層を混ぜると下流の紐付けが機能しなくなるため、レコードにも `kind` として層を明示する。

### 第1層：値の差分

最も重要な層。セルの表示値が変わったケースを拾う。議事録の決定事項と直接突き合わせる対象はほぼこれ。

`data_only=True` で読み込んだワークブックの値を比較する。数式セルの場合、ここで得られるのは**計算結果のキャッシュ値**であることに注意（後述の罠を参照）。

### 第2層：数式の差分

値が同じでも数式が変わっているケースがある。逆に、数式が変わったせいで値が変わったのか、直接入力で値が変わったのかは、この層がないと区別できない。

```
=SUM(B2:B11)  →  =SUM(B2:B12)
```

これは「集計範囲に1行追加された」という構造的な意図を示しており、単なる数値変更とは意味が違う。監査の観点でも重要度が高いので、`kind: formula` として別レコードで出す。

### 第3層：行の挿入・削除

**この層を実装しないと差分全体が使い物にならない。** 1行挿入されただけで、それ以降の全セルが「変更された」と誤検知され、数百件のノイズレコードが生まれる。

行キーによる対応付け（次章）で吸収し、実際に追加・削除された行だけを `kind: row_added` / `row_removed` として出力する。

### 捨てる差分

以下は検出しない。検出してもノイズになるだけで、矛盾検知には一切寄与しない。

- フォント、色、罫線、セル幅などの書式
- セル結合の変更
- シート名の変更（ただしシート自体の追加・削除は警告として出す）
- 末尾の空白や改行のみの差異（正規化して同一とみなす）

## 行対応付けアルゴリズム

### 座標比較が壊れる理由

素朴に「旧B12と新B12を比較する」実装は、行が1つ挿入された瞬間に破綻する。挿入位置より下の全行が1つずつ下にずれ、実際には何も変わっていない行が全て「変更された」と報告される。

50行の表に1行挿入しただけで数百件の偽差分が出るため、LLM に渡す前の時点で情報として死ぬ。

### 解決：ラベル列をキーにする

座標ではなく**行の中身で対応を取る**。A列（`key_column`）の値を行の識別子として扱い、同じキーを持つ行同士を比較する。

```
旧: A列="商品A" 行3   →  新: A列="商品A" 行4     同一行として比較
旧: （存在しない）    →  新: A列="商品D" 行3     row_added
旧: A列="商品C" 行5   →  （存在しない）           row_removed
```

これにより行がどれだけ移動しても、比較は意味のある単位で行われる。

### 手順

1. 旧・新それぞれについて、`key_column` の値 → 行番号 の辞書を作る
2. 両方に存在するキー → 同一行として全列を比較（値・数式）
3. 新にのみ存在するキー → `row_added`
4. 旧にのみ存在するキー → `row_removed`
5. 列の対応はヘッダ行（1行目）の文字列で取る。列が移動しても追従できる

### キーが重複する場合

同じラベルの行が複数あるケース（「その他」が2行ある等）では、辞書が上書きされて片方が失われる。対策として**出現順を添えた複合キー** `("その他", 1)`, `("その他", 2)` を使う。

### キーが空の行

A列が空の行は対応付けできないため、**行番号そのものをキーにフォールバック**する。合計行や空行がこれに該当する。フォールバックした行は座標ズレの影響を受けるが、そもそも意味的な紐付けの対象にならないため実害は小さい。

### ヘッダ行の扱い

1行目は列ラベルの供給源であり、比較対象からは除外する。ヘッダ自体が変更された場合は `header_changed` として1件だけ出す。

## 出力スキーマ

### DiffRecord

```python
@dataclass
class DiffRecord:
    sheet: str           # シート名
    kind: str            # value | formula | row_added | row_removed | header_changed
    row_key: str         # 行ラベル（A列の値）。例: "商品A"
    column_label: str    # 列ラベル（ヘッダ行の値）。例: "第3四半期"
    cell: str            # 新版での座標。例: "D5"（監査用・出典表示用）
    old: Any             # 変更前の値または数式
    new: Any             # 変更後の値または数式
```

`row_key` と `column_label` が入っていることがこのスキーマの肝。これがあるだけで、LLM を通さずとも機械的に自然言語化できる。

### 自然言語化

```
{sheet}の{row_key}の{column_label}が {old} から {new} に変更されました
→ 売上見込の商品Aの第3四半期が 1200 から 1500 に変更されました
```

この文字列をそのまま埋め込みベクトル化し、議事録の決定事項やチャット発言と照合する。テンプレートで生成できるため、この段階では LLM を呼ばない。**コスト評価で効く設計判断なので、プレゼンでも明示すること。**

### `cell` を残す理由

紐付け結果を人間に提示する際、「どのセルの話か」を示せないと信用されない。矛盾を指摘するUIでは必ず `Sheet1!D5` のような出典を併記する。審査基準の「信頼性・堅牢性」に直接効く。

### 出力例

| kind | row\_key | column\_label | old | new |
| --- | --- | --- | --- | --- |
| value | 商品A | 第3四半期 | 1200 | 1500 |
| formula | 合計 | 第3四半期 | =SUM(D2:D11) | =SUM(D2:D12) |
| row\_added | 商品D | — | — | — |

## 実装コード

`excel_diff.py` 単体で完結する。依存は openpyxl のみ。

```python
from dataclasses import dataclass, asdict
from typing import Any
from openpyxl import load_workbook


@dataclass
class DiffRecord:
    sheet: str
    kind: str
    row_key: str
    column_label: str
    cell: str
    old: Any
    new: Any

    def to_sentence(self) -> str:
        if self.kind == "row_added":
            return f"{self.sheet}に行「{self.row_key}」が追加されました"
        if self.kind == "row_removed":
            return f"{self.sheet}から行「{self.row_key}」が削除されました"
        if self.kind == "formula":
            return (f"{self.sheet}の{self.row_key}の{self.column_label}の数式が "
                    f"{self.old} から {self.new} に変更されました")
        return (f"{self.sheet}の{self.row_key}の{self.column_label}が "
                f"{self.old} から {self.new} に変更されました")


def _norm(v: Any) -> Any:
    """空セルと空文字を同一視し、文字列の前後空白を落とす"""
    if v is None:
        return ""
    if isinstance(v, str):
        return v.strip()
    return v


def _load_pair(path: str):
    """値版と数式版の両方を読む。openpyxlは一度に両方取れない"""
    wb_val = load_workbook(path, data_only=True)
    wb_fml = load_workbook(path, data_only=False)
    return wb_val, wb_fml


def _row_index(ws, key_column: int) -> dict:
    """行キー -> 行番号。重複キーは出現順を添えて一意化"""
    index, seen = {}, {}
    for r in range(2, ws.max_row + 1):
        raw = _norm(ws.cell(row=r, column=key_column).value)
        key = raw if raw != "" else f"__row{r}__"  # 空キーは行番号にフォールバック
        seen[key] = seen.get(key, 0) + 1
        index[(key, seen[key])] = r
    return index


def _headers(ws) -> dict:
    """列番号 -> 列ラベル"""
    return {
        c: (_norm(ws.cell(row=1, column=c).value) or f"列{c}")
        for c in range(1, ws.max_column + 1)
    }


def diff_sheet(ws_old_v, ws_old_f, ws_new_v, ws_new_f,
               key_column: int = 1) -> list[DiffRecord]:
    out = []
    sheet = ws_new_v.title

    old_idx = _row_index(ws_old_v, key_column)
    new_idx = _row_index(ws_new_v, key_column)
    old_hdr = _headers(ws_old_v)
    new_hdr = _headers(ws_new_v)

    # ヘッダの変更を1件だけ報告
    if old_hdr != new_hdr:
        out.append(DiffRecord(sheet, "header_changed", "__header__", "",
                              "1:1", str(old_hdr), str(new_hdr)))

    # 列ラベル -> 新版の列番号
    new_cols = {label: c for c, label in new_hdr.items()}
    old_cols = {label: c for c, label in old_hdr.items()}

    for key in new_idx:
        if key not in old_idx:
            out.append(DiffRecord(sheet, "row_added", key[0], "",
                                  f"{new_idx[key]}", None, None))
            continue

        r_old, r_new = old_idx[key], new_idx[key]

        for label, c_new in new_cols.items():
            c_old = old_cols.get(label)
            if c_old is None or c_new == key_column:
                continue

            v_old = _norm(ws_old_v.cell(row=r_old, column=c_old).value)
            v_new = _norm(ws_new_v.cell(row=r_new, column=c_new).value)
            f_old = _norm(ws_old_f.cell(row=r_old, column=c_old).value)
            f_new = _norm(ws_new_f.cell(row=r_new, column=c_new).value)

            addr = ws_new_v.cell(row=r_new, column=c_new).coordinate

            if v_old != v_new:
                out.append(DiffRecord(sheet, "value", key[0], label,
                                      addr, v_old, v_new))

            is_formula = str(f_old).startswith("=") or str(f_new).startswith("=")
            if is_formula and f_old != f_new:
                out.append(DiffRecord(sheet, "formula", key[0], label,
                                      addr, f_old, f_new))

    for key in old_idx:
        if key not in new_idx:
            out.append(DiffRecord(sheet, "row_removed", key[0], "",
                                  f"{old_idx[key]}", None, None))

    return out


def diff_workbooks(old_path: str, new_path: str,
                   key_column: int = 1) -> list[DiffRecord]:
    ov, of = _load_pair(old_path)
    nv, nf = _load_pair(new_path)

    records = []
    for name in nv.sheetnames:
        if name not in ov.sheetnames:
            records.append(DiffRecord(name, "sheet_added", name, "",
                                      "", None, None))
            continue
        records += diff_sheet(ov[name], of[name], nv[name], nf[name],
                              key_column)

    for name in ov.sheetnames:
        if name not in nv.sheetnames:
            records.append(DiffRecord(name, "sheet_removed", name, "",
                                      "", None, None))

    return records


if __name__ == "__main__":
    import json, sys
    recs = diff_workbooks(sys.argv[1], sys.argv[2])
    for r in recs:
        print(r.to_sentence())
    print(json.dumps([asdict(r) for r in recs], ensure_ascii=False, indent=2))
```

### 動作確認

```bash
pip install openpyxl
python excel_diff.py fixtures/sales_v1.xlsx fixtures/sales_v2.xlsx
```

行を挿入した版を食わせて、**差分件数が実際の変更件数と一致すること**を必ず確認する。件数が膨らんでいたら行対応付けが効いていない。

## エッジケースと既知の罠

### 数式と値は一度に取れない

openpyxl は `data_only` の指定で挙動が変わり、**1回の `load_workbook` では値と数式の両方を取得できない**。そのため同じファイルを2回読む必要がある。実装の `_load_pair` がこれを担う。メモリは倍食うが、想定サイズ（数千行）では問題にならない。

### data\_only の値が None になる

`data_only=True` は Excel が最後に保存した**キャッシュ値**を返す。openpyxl や他のライブラリで生成されたファイル、あるいは Excel で開かずに保存されたファイルでは、このキャッシュが存在せず**全ての数式セルが None になる**。

これは実際に踏みやすい罠。テストデータは必ず Excel か LibreOffice で一度開いて保存したものを使うこと。API で取得したファイルで数式セルが軒並み None になったら、これを疑う。

### 空セルの扱い

空セルは `None`、空文字入力は `""` として返るため、正規化しないと差分が大量発生する。`_norm` で両者を同一視している。

### 浮動小数の比較

`1.1 + 2.2` のような計算結果は環境によって末尾桁が揺れる。数値比較は必要に応じて `round(v, 6)` を挟む。今回のデータでは発生しないが、実運用では必須。

### max\_row / max\_column が過大になる

一度でも触れたセルは空でも範囲に含まれるため、`max_row` が実データよりはるかに大きい値を返すことがある。行キーが空の行は `__row{n}__` にフォールバックするため誤検知はしないが、ループ回数が無駄に増える。気になる場合は末尾の空行をトリムする。

### シートの追加・削除

比較対象外として1件のレコードで報告するに留め、中身の差分は取らない。追加シートの全セルを差分として出すとノイズが支配的になる。

### パフォーマンス

全セル総当たりのため計算量は行×列。数千行までは一瞬で終わる。数万行を超える場合は、値のハッシュで行単位の変更有無を先に判定してから列をなめる二段構えにする。今回は不要。

## 次工程への接続

### 受け渡しの形

差分エンジンは `list[DiffRecord]` を返すだけで、判断は一切しない。矛盾検知エンジンは各レコードの `to_sentence()` を入力として受け取る。

```
[議事録] 「第3四半期は据え置きで合意」（2026-09-15 の定例）
[差分]   売上見込の商品Aの第3四半期が 1200 から 1500 に変更されました
[チャット] 「見込み上方修正しておきました」（9/18、発言者: 山田）
         ↓
[検知]   決定事項と成果物が食い違っています
```

この3点が揃ったときに初めて価値が出る。差分単体では「変わりました」以上のことは言えない。

### 紐付けの実装方針

1. 差分の文章と議事録の決定事項を埋め込みで粗く照合し、候補を絞る（**ここはLLMを使わない**）
2. 候補が出たものだけ、上位モデルに「この決定とこの変更は矛盾するか」を判定させる
3. 確信度が低いものは自動通知せず、人間への確認キューに回す

段階を分けることで、OrcaRouter のモデル振り分けがそのままコスト削減として数値に出る。**1会議あたりの処理コストを実測してプレゼンに載せる。**

### 出典の保持

矛盾を提示する画面では、必ず3つの出典を並べる。これがないと指摘を信用してもらえない。

- 議事録の該当発言（話者と時刻）
- 変更されたセル（`Sheet1!D5`）
- 関連するチャット発言

### 実装順序の推奨

1. この差分エンジン（半日）
2. 架空データの作成と、矛盾が発火する状態の確認（1〜2時間）
3. 紐付けと矛盾検知（本命。残り時間を全て投入）
4. 表示UI（最小限。矛盾カード1枚が出れば十分）

**接続するストレージの数を増やすことに時間を使わない。** 審査で見られるのは、繋いだ後に何が言えるか。
