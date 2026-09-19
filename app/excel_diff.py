"""Excel セル差分エンジン。

2版の .xlsx を比較し、人間が読める粒度（行ラベル×列ラベル）の差分を返す。
座標ではなく A 列（key_column）の値で行を対応付けるため、行の挿入・削除で
座標がズレても偽差分を出さない。依存は openpyxl のみ。
"""
from dataclasses import dataclass, asdict
from typing import Any

from openpyxl import load_workbook


@dataclass
class DiffRecord:
    sheet: str
    kind: str            # value | formula | row_added | row_removed | header_changed | sheet_added | sheet_removed
    row_key: str         # 行ラベル（A列の値）。例: "商品A"
    column_label: str    # 列ラベル（ヘッダ行の値）。例: "第3四半期"
    cell: str            # 新版での座標。例: "D5"
    old: Any
    new: Any

    def to_sentence(self) -> str:
        if self.kind == "row_added":
            return f"{self.sheet}に行「{self.row_key}」が追加されました"
        if self.kind == "row_removed":
            return f"{self.sheet}から行「{self.row_key}」が削除されました"
        if self.kind == "sheet_added":
            return f"シート「{self.sheet}」が追加されました"
        if self.kind == "sheet_removed":
            return f"シート「{self.sheet}」が削除されました"
        if self.kind == "header_changed":
            return f"{self.sheet}のヘッダ行が変更されました"
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
    if isinstance(v, float):
        return round(v, 6)
    return v


def _load_pair(path: str):
    """値版と数式版の両方を読む。openpyxl は一度に両方取れない"""
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

    # 列ラベル -> 列番号
    new_cols = {label: c for c, label in new_hdr.items()}
    old_cols = {label: c for c, label in old_hdr.items()}

    for key in new_idx:
        if key not in old_idx:
            out.append(DiffRecord(sheet, "row_added", key[0], "",
                                  f"A{new_idx[key]}", None, None))
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
                                  f"A{old_idx[key]}", None, None))

    return out


def diff_workbooks(old_path: str, new_path: str,
                   key_column: int = 1) -> list[DiffRecord]:
    ov, of = _load_pair(old_path)
    nv, nf = _load_pair(new_path)

    records = []
    for name in nv.sheetnames:
        if name not in ov.sheetnames:
            records.append(DiffRecord(name, "sheet_added", name, "", "", None, None))
            continue
        records += diff_sheet(ov[name], of[name], nv[name], nf[name], key_column)

    for name in ov.sheetnames:
        if name not in nv.sheetnames:
            records.append(DiffRecord(name, "sheet_removed", name, "", "", None, None))

    return records


if __name__ == "__main__":
    import json
    import sys
    recs = diff_workbooks(sys.argv[1], sys.argv[2])
    for r in recs:
        print(r.to_sentence())
    print(json.dumps([asdict(r) for r in recs], ensure_ascii=False, indent=2))
