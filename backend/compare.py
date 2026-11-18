from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation, localcontext

from openpyxl.utils import column_index_from_string, range_boundaries

from .excel import DEFAULT_STYLE, get_cell, get_sheet, parse_bounds, sheet_columns


def _effective(cell: dict) -> tuple[str, object]:
    if cell["formula"]:
        if cell["cache_status"] == "missing":
            return "unavailable", None
        return cell.get("cached_value_type", "string"), cell["cached_value"]
    return cell["value_type"], cell["value"]


def _decimal(value: object) -> Decimal:
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError("数值必须为有限十进制数字。")
    return number


def _number_text(number: Decimal) -> str:
    if not number:
        return "0"
    value = format(number, "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _token(cell: dict, trim: bool) -> str:
    kind, value = _effective(cell)
    if kind == "number":
        value = _number_text(_decimal(value))
    elif kind == "string" and trim:
        value = str(value).strip()
    return json.dumps([kind, value], ensure_ascii=False, separators=(",", ":"))


def _source(sheet: dict | None, row: int | None, column: int = 1) -> dict | None:
    if sheet is None or row is None:
        return None
    cell = get_cell(sheet, row, column)
    source = {"sheet": sheet["name"], "cell": cell["address"]}
    if cell.get("merged_range"):
        source["range"] = cell["merged_range"]
    return source


def _display(cell: dict | None) -> str | None:
    if cell is None:
        return None
    kind, value = _effective(cell)
    if kind == "unavailable":
        return "计算结果未保存"
    if value is None:
        return "（空白）"
    return str(value)


def _data_rows(sheet: dict, header_end: int, bounds: tuple[int, int, int, int]) -> list[int]:
    c1, _, c2, r2 = bounds
    return sorted({c["row"] for c in sheet["cells"].values()
                   if header_end < c["row"] <= r2 and c1 <= c["column"] <= c2 and c["value"] is not None})


def _key_cell(sheet: dict, row: int, column: int) -> dict:
    cell = get_cell(sheet, row, column)
    if cell.get("merged_range"):
        c1, r1, _, _ = range_boundaries(cell["merged_range"])
        return get_cell(sheet, r1, c1)
    return cell


def compare_workbooks(old: dict, new: dict, config: dict) -> dict:
    try:
        tolerance = _decimal(config.get("absolute_tolerance", "0"))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("绝对容差须为非负有限数字。") from None
    if tolerance < 0:
        raise ValueError("绝对容差须为非负有限数字。")
    pairs = config.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("请至少选择一组工作表进行比较。")
    trim = bool(config.get("ignore_outer_whitespace", True))
    formulas = bool(config.get("compare_formulas", True))
    formatting = bool(config.get("compare_formatting", False))
    diffs = []
    warnings = list(dict.fromkeys(old.get("warnings", []) + new.get("warnings", [])))
    compared = {"old": [], "new": []}
    stats = {"matched_rows": 0, "unchanged_rows": 0, "compared_cells": 0}
    seen_pairs = set()
    selected_rectangles = {"old": defaultdict(list), "new": defaultdict(list)}

    def add(pair: dict, kind: str, field: str, old_value=None, new_value=None,
            old_source=None, new_source=None, row_key="", delta=None, message="", severity="change"):
        diffs.append({"id": f"diff-{len(diffs) + 1:07d}", "kind": kind, "table_id": pair["id"],
                      "table_name": pair.get("old_sheet") or pair.get("new_sheet") or "报表",
                      "row_key": str(row_key), "field": str(field), "old_value": old_value,
                      "new_value": new_value, "delta": delta, "message": message,
                      "severity": severity, "old_source": old_source, "new_source": new_source})

    for pair in pairs:
        if not isinstance(pair, dict) or not pair.get("id") or pair["id"] in seen_pairs:
            raise ValueError("报表配对 ID 必须非空且不重复。")
        seen_pairs.add(pair["id"])
        left_name, right_name = pair.get("old_sheet"), pair.get("new_sheet")
        if not left_name and not right_name:
            raise ValueError("报表配对至少需要一侧工作表。")
        left = get_sheet(old, left_name) if left_name else None
        right = get_sheet(new, right_name) if right_name else None
        for side, sheet in (("old", left), ("new", right)):
            if sheet:
                rect = parse_bounds(sheet, pair.get(f"{side}_range"))
                for prior in selected_rectangles[side][sheet["name"]]:
                    if rect[0] <= prior[2] and rect[2] >= prior[0] and rect[1] <= prior[3] and rect[3] >= prior[1]:
                        raise ValueError(f"{side} 工作表“{sheet['name']}”的比较范围与其他配对重叠，请删除重复配对或设置不重叠区域。")
                selected_rectangles[side][sheet["name"]].append(rect)
            if sheet and sheet["name"] not in compared[side]:
                compared[side].append(sheet["name"])
        if left is None or right is None:
            sheet = left or right
            address = next((c["address"] for c in sheet["cells"].values() if c["value"] is not None), "A1")
            source = {"sheet": sheet["name"], "cell": address}
            add(pair, "sheet_removed" if left else "sheet_added", "工作表",
                sheet["name"] if left else None, sheet["name"] if right else None,
                source if left else None, source if right else None,
                message="按已确认的工作表配对，本工作表仅存在于一侧。")
            continue
        bounds = {}
        header_end = {}
        header_start = {}
        all_columns = {}
        for side, sheet, workbook in (("old", left, old), ("new", right, new)):
            bounds[side] = parse_bounds(sheet, pair.get(f"{side}_range"))
            try:
                header_start[side] = int(pair.get(f"{side}_header_start", 1))
                header_end[side] = int(pair.get(f"{side}_header_end", header_start[side]))
            except (ValueError, TypeError):
                raise ValueError("表头行号必须是正整数。") from None
            all_columns[side] = sheet_columns(workbook, sheet["name"], header_start[side], header_end[side],
                                              pair.get(f"{side}_range"))["columns"]
            c1, r1, c2, r2 = bounds[side]
            outside = sum(1 for c in sheet["cells"].values() if c["value"] is not None and
                          not (c1 <= c["column"] <= c2 and header_start[side] <= c["row"] <= r2))
            if outside:
                warnings.append(f"{side} 工作表“{sheet['name']}”有 {outside} 个非空单元格位于所选表头和数据范围之外，本配对未比较。")
            if any(range_boundaries(merged)[3] > header_end[side] and
                   range_boundaries(merged)[1] <= r2 and
                   range_boundaries(merged)[0] <= c2 and range_boundaries(merged)[2] >= c1
                   for merged in sheet["merged_ranges"]):
                warnings.append(f"{side} 工作表“{sheet['name']}”正文含合并区域：仅业务键沿锚点继承，其余字段按原单元格存储值比较，不自动展开合并值。")
        mappings = pair.get("columns", [])
        if not isinstance(mappings, list):
            raise ValueError("字段映射必须是列表。")
        ids = set()
        used_columns = {"old": set(), "new": set()}
        for m in mappings:
            if not m.get("id") or m["id"] in ids:
                raise ValueError("每组字段映射 ID 必须非空且不重复。")
            ids.add(m["id"])
            if not m.get("old_column") and not m.get("new_column"):
                raise ValueError("每个字段映射至少需要一侧列。")
            for side in ("old", "new"):
                letter = m.get(f"{side}_column")
                if letter is None:
                    continue
                try:
                    col = column_index_from_string(letter)
                except (ValueError, TypeError):
                    raise ValueError("字段列应为 A、B 等 Excel 列字母。") from None
                if not bounds[side][0] <= col <= bounds[side][2]:
                    raise ValueError(f"映射列 {letter} 不在所选工作表范围内。")
                if col in used_columns[side]:
                    raise ValueError(f"同一配对中 {side} 侧的列 {letter} 被重复映射。")
                used_columns[side].add(col)
            if m.get("is_key") and (not m.get("old_column") or not m.get("new_column")):
                raise ValueError("业务键字段必须在两侧都有对应列。")
        for side, sheet in (("old", left), ("new", right)):
            unmapped = [c["label"] for c in all_columns[side]
                        if column_index_from_string(c["column"]) not in used_columns[side]]
            if unmapped:
                warnings.append(f"{side} 工作表“{sheet['name']}”未映射列：{'、'.join(unmapped)}；这些列未比较。")
        common = [m for m in mappings if m.get("old_column") and m.get("new_column")]
        header_labels = {side: {c["column"]: c["label"] for c in all_columns[side]}
                         for side in ("old", "new")}
        for m in common:
            old_label = header_labels["old"].get(m["old_column"], "")
            new_label = header_labels["new"].get(m["new_column"], "")
            if (old_label.strip() if trim else old_label) != (new_label.strip() if trim else new_label):
                sources = []
                for side, sheet in (("old", left), ("new", right)):
                    column = m[f"{side}_column"]
                    sources.append({"sheet": sheet["name"], "cell": f"{column}{header_end[side]}",
                                    "range": f"{column}{header_start[side]}:{column}{header_end[side]}"})
                add(pair, "value_changed", m.get("name") or m["id"], old_label, new_label,
                    sources[0], sources[1], row_key="表头", message="已映射字段的表头文字不同，按配置继续比较对应数据。")
        for m in mappings:
            if m in common:
                continue
            is_old = bool(m.get("old_column"))
            side, sheet = ("old", left) if is_old else ("new", right)
            col = column_index_from_string(m[f"{side}_column"])
            src = _source(sheet, header_end[side], col)
            add(pair, "column_removed" if is_old else "column_added", m.get("name") or m["id"],
                m.get("name") if is_old else None, m.get("name") if not is_old else None,
                src if is_old else None, src if not is_old else None,
                message="整列增删记为一个结构事件，可在原工作表中查看列内数据。")
        if not common:
            warnings.append(f"工作表配对“{left_name} / {right_name}”没有共同字段，未进行行级比较。")
            continue
        mode = pair.get("mode", "key")
        if mode not in ("key", "position"):
            raise ValueError("比较方式必须为业务键或固定位置。")
        keys = [m for m in common if m.get("is_key")]
        if mode == "key" and not keys:
            raise ValueError(f"工作表“{left_name}”至少需要一个业务键，或明确选择固定位置比较。")
        if mode == "position":
            warnings.append(f"工作表“{left_name}”按固定位置比较，插入或删除行可能产生后续位置差异。")
        rows_by_key = {"old": defaultdict(list), "new": defaultdict(list)}
        unknown_keys = {"old": 0, "new": 0}
        labels = {}
        row_lists = {}
        for side, sheet in (("old", left), ("new", right)):
            row_lists[side] = _data_rows(sheet, header_end[side], bounds[side])
            if not row_lists[side]:
                warnings.append(f"{side} 工作表“{sheet['name']}”的所选范围没有数据行。")
            for row in row_lists[side]:
                if mode == "position":
                    token = str(row - header_end[side])
                    label = f"数据区第 {token} 行"
                else:
                    key_cells = [_key_cell(sheet, row, column_index_from_string(m[f"{side}_column"])) for m in keys]
                    invalid = any(_effective(c)[0] in ("blank", "unavailable", "error") or
                                  (_effective(c)[0] == "string" and not str(_effective(c)[1]).strip()) for c in key_cells)
                    if invalid:
                        unknown_keys[side] += 1
                        col = column_index_from_string(keys[0][f"{side}_column"])
                        src = _source(sheet, row, col)
                        add(pair, "match_ambiguous", "业务键", None, None,
                            src if side == "old" else None, src if side == "new" else None,
                            row_key=f"第 {row} 行", message="业务键为空、公式结果不可用或含错误值，此行未进行确定配对。", severity="issue")
                        continue
                    key_tokens = [_token(c, trim) for c in key_cells]
                    if config.get("_pdf_key_whitespace"):
                        for index, cell in enumerate(key_cells):
                            kind, value = _effective(cell)
                            if kind == "string":
                                value = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", str(value)).strip()
                                key_tokens[index] = json.dumps([kind, value], ensure_ascii=False, separators=(",", ":"))
                    token = json.dumps(key_tokens, ensure_ascii=False)
                    label = " / ".join(_display(c) for c in key_cells)
                rows_by_key[side][token].append(row)
                labels[token] = label
        duplicate_keys = {k for side in ("old", "new") for k, rs in rows_by_key[side].items() if len(rs) > 1}
        for token in sorted(duplicate_keys):
            for side, sheet in (("old", left), ("new", right)):
                for row in rows_by_key[side].pop(token, []):
                    col = column_index_from_string((keys or common)[0][f"{side}_column"])
                    src = _source(sheet, row, col)
                    add(pair, "match_ambiguous", "业务键", None, None,
                        src if side == "old" else None, src if side == "new" else None,
                        row_key=labels[token], message="业务键在至少一侧重复；请补充复合键后重新比较，此行未被强制配对。", severity="issue")
        valid = {side: {key: rs[0] for key, rs in groups.items()} for side, groups in rows_by_key.items()}
        # 使用原生 Python 字典集合运算与 _token 比对两侧唯一行及共有行，保持原有行遍历顺序
        unique_col = {side: column_index_from_string((keys or common)[0][f"{side}_column"]) for side in ("old", "new")}
        for side, opposite, sheet in (("old", "new", left), ("new", "old", right)):
            for key, row in valid[side].items():
                if key in valid[opposite]:
                    continue
                src = _source(sheet, row, unique_col[side])
                unresolved_opposite = unknown_keys[opposite] > 0
                add(pair, "match_ambiguous" if unresolved_opposite else "row_removed" if side == "old" else "row_added", "整行",
                    labels[key] if side == "old" else None, labels[key] if side == "new" else None,
                    src if side == "old" else None, src if side == "new" else None,
                    row_key=labels[key],
                    message="对侧存在空白或不可用业务键，无法确定本行是否为增删；请修正业务键后重跑。" if unresolved_opposite else "按当前匹配规则，该项目只存在于一侧；整行增删记为一个事件。",
                    severity="issue" if unresolved_opposite else "change")
        common_columns = [
            (m.get("name") or m["id"], column_index_from_string(m["old_column"]), column_index_from_string(m["new_column"]))
            for m in common
        ]
        for key, lr in valid["old"].items():
            if key not in valid["new"]:
                continue
            rr = valid["new"][key]
            before = len(diffs)
            stats["matched_rows"] += 1
            for field, lc, rc in common_columns:
                a, b = get_cell(left, lr, lc), get_cell(right, rr, rc)
                old_src, new_src = _source(left, lr, lc), _source(right, rr, rc)
                ka, va = _effective(a)
                kb, vb = _effective(b)
                stats["compared_cells"] += 1
                if ka in ("unavailable", "error") or kb in ("unavailable", "error"):
                    add(pair, "formula_result_unavailable", field, _display(a), _display(b), old_src, new_src,
                        row_key=labels[key], message="至少一侧的公式缓存缺失或单元格为 Excel 错误值，无法确认结果一致；未重算公式。", severity="issue")
                elif _token(a, trim) != _token(b, trim):
                    delta = None
                    equal = False
                    if ka == kb == "number":
                        na, nb = _decimal(va), _decimal(vb)
                        with localcontext() as context:
                            context.prec = max(64, len(na.as_tuple().digits) + abs(na.as_tuple().exponent) +
                                               len(nb.as_tuple().digits) + abs(nb.as_tuple().exponent) + 10)
                            difference = nb - na
                            equal = abs(difference) <= tolerance
                            delta = _number_text(difference)
                    if not equal:
                        add(pair, "value_changed", field, _display(a), _display(b), old_src, new_src,
                            row_key=labels[key], delta=delta,
                            message=f"绝对容差 {tolerance}；差额为新版减旧版。" if delta is not None else "内容或值类型不同；空白、零和文本分别比较。")
                if formulas and a["formula"] != b["formula"]:
                    add(pair, "formula_changed", field, a["formula"], b["formula"], old_src, new_src,
                        row_key=labels[key], message="公式文本发生变化；文件保存的结果单独比较，不执行公式。")
                if formatting:
                    # 空白格或缺失样式先兜底为统一默认样式，再判不等，避免误报 5 项样式差异或空属性列表
                    old_style = a.get("style") or DEFAULT_STYLE
                    new_style = b.get("style") or DEFAULT_STYLE
                    if old_style != new_style:
                        categories = [label for attr, label in (("font", "字体"), ("fill", "填充"), ("border", "边框"),
                                                                ("alignment", "对齐"), ("number_format", "数字格式"))
                                      if old_style.get(attr) != new_style.get(attr)]
                        add(pair, "format_changed", field, a.get("number_format"), b.get("number_format"), old_src, new_src,
                            row_key=labels[key], message=f"变化属性：{'、'.join(categories)}。旧值/新值栏显示数字格式，其他属性可在两侧原单元格查看；数值独立比较。")
            if len(diffs) == before:
                stats["unchanged_rows"] += 1
    excluded = {"old": [s["name"] for s in old["sheets"] if s["name"] not in compared["old"]],
                "new": [s["name"] for s in new["sheets"] if s["name"] not in compared["new"]]}
    by_kind = dict(Counter(d["kind"] for d in diffs))
    if formatting:
        warnings.append("格式比较覆盖已匹配的数据单元格常用样式；表头样式、行高列宽和条件格式未比较。")
    issues = sum(d["severity"] == "issue" for d in diffs)
    return {"diffs": diffs, "summary": {"total": len(diffs), "changes": len(diffs) - issues,
                                        "issues": issues, "by_kind": by_kind, **stats},
            "coverage": {"compared_sheets": compared, "excluded_sheets": excluded,
                         "warnings": list(dict.fromkeys(warnings))}}
