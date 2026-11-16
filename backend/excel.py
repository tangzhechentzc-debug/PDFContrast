from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter, range_boundaries


MAX_SHEET_AREA = 500_000
MAX_NONEMPTY_CELLS = 100_000


def _json_value(value: Any) -> tuple[Any, str]:
    if value is None:
        return None, "blank"
    if isinstance(value, bool):
        return value, "boolean"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat(), "date"
    if isinstance(value, timedelta):
        return str(value), "date"
    if isinstance(value, (int, float)):
        return str(value), "number"
    return str(value), "string"


def _color(color: Any) -> dict | None:
    if color is None:
        return None
    return {"type": color.type, "value": color.value, "tint": color.tint}


def _style(cell: Any) -> dict:
    return {
        "font": {"name": cell.font.name, "size": cell.font.sz, "bold": cell.font.b,
                 "italic": cell.font.i, "underline": cell.font.u, "color": _color(cell.font.color)},
        "fill": {"type": cell.fill.patternType, "foreground": _color(cell.fill.fgColor),
                 "background": _color(cell.fill.bgColor)},
        "alignment": {"horizontal": cell.alignment.horizontal, "vertical": cell.alignment.vertical,
                      "wrap_text": cell.alignment.wrap_text, "rotation": cell.alignment.textRotation},
        "border": {side: {"style": getattr(cell.border, side).style if getattr(cell.border, side) else None,
                          "color": _color(getattr(cell.border, side).color) if getattr(cell.border, side) else None}
                   for side in ("left", "right", "top", "bottom")},
        "number_format": cell.number_format,
    }


def extract_workbook(path: Path, progress=None) -> dict:
    def report(stage, current, total, completed, item_name=None):
        if progress is not None:
            progress({"stage": stage, "unit": "sheet", "current": current, "total": total, "completed": completed,
                      **({"item_name": item_name} if item_name is not None else {})})

    report("opening", 0, None, 0)
    formulas = load_workbook(path, data_only=False, keep_links=False)
    cached = None
    try:
        sheet_count = len(formulas.worksheets)
        report("opening", 0, sheet_count, 0)
        for ws in formulas.worksheets:
            if ws.max_row * ws.max_column > MAX_SHEET_AREA:
                raise ValueError(f"工作表“{ws.title}”区域过大（最多 {MAX_SHEET_AREA:,} 格），请缩小报表范围后另存为 .xlsx。")
        cached = load_workbook(path, data_only=True, keep_links=False)
        sheets = []
        warnings = []
        total_nonempty = 0
        for index, ws in enumerate(formulas.worksheets, 1):
            report("reading_sheet", index, sheet_count, index - 1, ws.title)
            value_sheet = cached[ws.title]
            merges = [str(r) for r in ws.merged_cells.ranges]
            merge_lookup: dict[str, str] = {}
            for merged in ws.merged_cells.ranges:
                if (merged.max_row - merged.min_row + 1) * (merged.max_col - merged.min_col + 1) > MAX_SHEET_AREA:
                    raise ValueError(f"工作表“{ws.title}”包含过大的合并区域。")
                for row in range(merged.min_row, merged.max_row + 1):
                    for col in range(merged.min_col, merged.max_col + 1):
                        merge_lookup[f"{get_column_letter(col)}{row}"] = str(merged)
            cells = {}
            nonempty = 0
            hidden_rows = {r for r, dimension in ws.row_dimensions.items() if dimension.hidden}
            hidden_columns = {col for dimension in ws.column_dimensions.values() if dimension.hidden
                              for col in range(dimension.min or 1, min(dimension.max or dimension.min or 1, ws.max_column) + 1)}
            for row in ws.iter_rows():
                for cell in row:
                    if cell.value is None and not cell.has_style and cell.coordinate not in merge_lookup:
                        continue
                    value, value_type = _json_value(cell.value)
                    formula = None
                    cached_value = None
                    cached_type = "blank"
                    cache_status = "not_formula"
                    if cell.data_type == "f":
                        if isinstance(cell.value, str):
                            formula = cell.value
                        else:
                            formula = getattr(cell.value, "text", None) or "[特殊公式]"
                            warnings.append(f"{ws.title}!{cell.coordinate} 为特殊/数组公式；记录锚点公式，未验证计算结果及整个作用范围。")
                        value, value_type = formula, "formula"
                        cached_cell = value_sheet[cell.coordinate]
                        cached_value, cached_type = _json_value(cached_cell.value)
                        if cached_cell.data_type == "e":
                            cached_type = "error"
                        cache_status = "missing" if cached_value is None else "available"
                    elif cell.data_type == "e":
                        value_type = "error"
                    if value is not None:
                        nonempty += 1
                        total_nonempty += 1
                        if total_nonempty > MAX_NONEMPTY_CELLS:
                            raise ValueError(f"工作簿非空单元格超过 {MAX_NONEMPTY_CELLS:,} 格，请拆分后比较。")
                    display_value = cached_value if formula and cache_status == "available" else value
                    display = "" if display_value is None else str(display_value)
                    payload = {
                        "address": cell.coordinate, "row": cell.row, "column": cell.column,
                        "value": value, "display": display, "value_type": value_type,
                        "formula": formula, "cached_value": cached_value,
                        "cached_value_type": cached_type, "cache_status": cache_status,
                        "number_format": cell.number_format,
                        "merged_range": merge_lookup.get(cell.coordinate), "style": _style(cell),
                        "hidden_row": cell.row in hidden_rows,
                        "hidden_column": cell.column in hidden_columns,
                    }
                    cells[cell.coordinate] = payload
            if getattr(ws, "_charts", None) or getattr(ws, "_images", None):
                warnings.append(f"工作表“{ws.title}”含图表或图片；本次仅比较单元格内容。")
            header_counts = {}
            for c in cells.values():
                if c["row"] <= 30 and c["value"] is not None:
                    text_count, count = header_counts.get(c["row"], (0, 0))
                    header_counts[c["row"]] = (text_count + int(c["value_type"] == "string"), count + 1)
            scores = []
            for r in range(1, min(ws.max_row, 30) + 1):
                text_count, count = header_counts.get(r, (0, 0))
                scores.append((text_count, count, -r))
            header = -max(scores)[2] if scores else 1
            sheets.append({"name": ws.title, "max_row": ws.max_row, "max_column": ws.max_column,
                           "hidden": ws.sheet_state != "visible", "suggested_header_row": header,
                           "nonempty_count": nonempty, "cells": cells, "merged_ranges": merges,
                           "hidden_rows": sorted(hidden_rows), "hidden_columns": sorted(hidden_columns)})
            report("reading_sheet", index, sheet_count, index, ws.title)
        report("file_completed", sheet_count, sheet_count, sheet_count)
        return {"sheets": sheets, "warnings": warnings}
    finally:
        formulas.close()
        if cached is not None:
            cached.close()


def get_sheet(workbook: dict, sheet_name: str) -> dict:
    for sheet in workbook["sheets"]:
        if sheet["name"] == sheet_name:
            return sheet
    raise ValueError(f"工作表“{sheet_name}”不存在。")


def parse_bounds(sheet: dict, cell_range: str | None = None) -> tuple[int, int, int, int]:
    if not cell_range:
        return 1, 1, sheet["max_column"], sheet["max_row"]
    try:
        bounds = range_boundaries(cell_range.strip().upper())
    except (TypeError, ValueError):
        raise ValueError("比较范围应为 A1:D20 形式的单元格区域。") from None
    c1, r1, c2, r2 = bounds
    if any(x is None for x in bounds) or not (1 <= c1 <= c2 <= sheet["max_column"] and 1 <= r1 <= r2 <= sheet["max_row"]):
        raise ValueError(f"比较范围必须位于工作表“{sheet['name']}”已读取的行列范围内。")
    return bounds


def empty_cell(row: int, column: int) -> dict:
    return {"address": f"{get_column_letter(column)}{row}", "row": row, "column": column,
            "value": None, "display": "", "value_type": "blank", "formula": None,
            "cached_value": None, "cached_value_type": "blank", "cache_status": "not_formula",
            "number_format": "General", "merged_range": None, "style": None,
            "hidden_row": False, "hidden_column": False}


def get_cell(sheet: dict, row: int, column: int) -> dict:
    existing = sheet["cells"].get(f"{get_column_letter(column)}{row}")
    if existing is not None:
        return existing
    cell = empty_cell(row, column)
    cell["hidden_row"] = row in sheet.get("hidden_rows", [])
    cell["hidden_column"] = column in sheet.get("hidden_columns", [])
    return cell


def _header_value(sheet: dict, row: int, column: int) -> str:
    cell = get_cell(sheet, row, column)
    if cell.get("merged_range"):
        c1, r1, _, _ = range_boundaries(cell["merged_range"])
        cell = get_cell(sheet, r1, c1)
    return str(cell["display"]).strip()


def sheet_columns(workbook: dict, sheet_name: str, header_start: int, header_end: int,
                  cell_range: str | None = None) -> dict:
    sheet = get_sheet(workbook, sheet_name)
    c1, r1, c2, r2 = parse_bounds(sheet, cell_range)
    if not (r1 <= header_start <= header_end <= r2):
        raise ValueError(f"工作表“{sheet_name}”的表头起止行不在所选范围内。")
    columns = []
    content_columns = {c["column"] for c in sheet["cells"].values()
                       if r1 <= c["row"] <= r2 and c["value"] is not None}
    for column in range(c1, c2 + 1):
        labels = []
        for row in range(header_start, header_end + 1):
            label = _header_value(sheet, row, column)
            if label and (not labels or label != labels[-1]):
                labels.append(label)
        if labels or column in content_columns:
            letter = get_column_letter(column)
            columns.append({"column": letter, "label": "/".join(labels) or f"未命名列 {letter}"})
    preview = [{"row": r, "values": {c["column"]: get_cell(sheet, r, column_index_from_string(c["column"]))["display"]
                                    for c in columns}}
               for r in range(header_end + 1, min(header_end + 5, r2) + 1)]
    return {"columns": columns, "preview": preview}


def suggest_pairs(old: dict, new: dict) -> list[dict]:
    old_by_name = {s["name"]: s for s in old["sheets"]}
    new_by_name = {s["name"]: s for s in new["sheets"]}
    names = list(old_by_name) + [name for name in new_by_name if name not in old_by_name]
    pairs = []
    for index, name in enumerate(names):
        left, right = old_by_name.get(name), new_by_name.get(name)
        lh, rh = (left or right)["suggested_header_row"], (right or left)["suggested_header_row"]
        lc = sheet_columns(old, name, lh, lh)["columns"] if left else []
        rc = sheet_columns(new, name, rh, rh)["columns"] if right else []
        mappings = []
        used = set()
        for c in lc:
            candidates = [d for d in rc if d["label"] == c["label"] and d["column"] not in used]
            match = candidates[0] if len(candidates) == 1 else None
            if match:
                used.add(match["column"])
            mappings.append({"id": f"m{len(mappings)}", "name": c["label"], "old_column": c["column"],
                             "new_column": match["column"] if match else None, "is_key": False})
        for c in rc:
            if c["column"] not in used:
                mappings.append({"id": f"m{len(mappings)}", "name": c["label"], "old_column": None,
                                 "new_column": c["column"], "is_key": False})
        common = [m for m in mappings if m["old_column"] and m["new_column"]]
        preferred = [m for m in common if any(word in m["name"].lower() for word in ("编码", "编号", "序号", "id", "code", "科目", "项目名称"))]
        if preferred:
            preferred[0]["is_key"] = True
        elif common:
            fallback_rows = {side: sorted({c["row"] for c in sheet["cells"].values()
                                           if c["row"] > header and c["value"] is not None})
                             for sheet, side, header in ((left, "old", lh), (right, "new", rh))}
            for mapping in common:
                usable = True
                for sheet, side, header in ((left, "old", lh), (right, "new", rh)):
                    column = column_index_from_string(mapping[f"{side}_column"])
                    cells = [get_cell(sheet, row, column) for row in fallback_rows[side]]
                    values = [str(cell["value"]).strip() for cell in cells]
                    usable = usable and bool(cells) and all(cell["value_type"] == "string" and value
                                                            for cell, value in zip(cells, values)) and len(set(values)) == len(values)
                if usable:
                    mapping["is_key"] = True
                    break
        pairs.append({"id": f"table-{index + 1}", "old_sheet": name if left else None,
                      "new_sheet": name if right else None, "old_header_start": lh, "old_header_end": lh,
                      "new_header_start": rh, "new_header_end": rh, "old_range": None, "new_range": None,
                      "mode": "key", "columns": mappings})
    return pairs


def sheet_region(workbook: dict, sheet_name: str, start_row: int, start_column: int,
                 rows: int, columns: int) -> dict:
    sheet = get_sheet(workbook, sheet_name)
    if start_row < 1 or start_column < 1 or not 1 <= rows <= 100 or not 1 <= columns <= 30:
        raise ValueError("预览区域参数无效：起始行列须为正数，每次最多 100 行、30 列。")
    start_row = min(start_row, sheet["max_row"])
    start_column = min(start_column, sheet["max_column"])
    end_row = min(start_row + rows - 1, sheet["max_row"])
    end_column = min(start_column + columns - 1, sheet["max_column"])
    merges = []
    anchors = []
    for merged in sheet["merged_ranges"]:
        c1, r1, c2, r2 = range_boundaries(merged)
        if r1 <= end_row and r2 >= start_row and c1 <= end_column and c2 >= start_column:
            merges.append(merged)
            anchors.append(get_cell(sheet, r1, c1))
    return {"sheet": sheet_name, "start_row": start_row, "start_column": start_column,
            "end_row": end_row, "end_column": end_column, "max_row": sheet["max_row"],
            "max_column": sheet["max_column"], "hidden": sheet["hidden"], "merged_ranges": merges,
            "merged_anchors": anchors,
            "cells": [get_cell(sheet, row, column) for row in range(start_row, end_row + 1)
                      for column in range(start_column, end_column + 1)]}
