from __future__ import annotations

import copy
import hashlib
import re

from openpyxl.utils import get_column_letter

from .excel import empty_cell
from .pdf import _decimal_text, _logical_tables


MAX_STRUCTURE_ROWS = 500
MAX_STRUCTURE_COLUMNS = 60
MAX_STRUCTURE_CELLS = 10000
MAX_CELL_TEXT = 10000


def _review_id(book: dict, sheet: dict, cell: dict) -> str:
    if cell.get("review_id"):
        return cell["review_id"]
    identity = f"{book.get('review_namespace', '')}\x1f{sheet['name']}\x1f{cell['address']}"
    return hashlib.sha256(identity.encode()).hexdigest()[:20]


def review_catalog(book: dict) -> list[dict]:
    catalog = []
    for sheet in book["sheets"]:
        if sheet.get("member_sheets"):
            continue
        for cell in sheet["cells"].values():
            source = cell.get("pdf_source", {})
            if (source.get("method") != "ocr" and not cell.get("requires_review")) or (sheet.get("structure") == "text_blocks" and cell["row"] == 1):
                continue
            target_id = _review_id(book, sheet, cell)
            catalog.append({"id": target_id, "sheet": sheet["name"], "cell": cell["address"],
                            "page": source["page"], "bbox": copy.deepcopy(source["bbox"]),
                            "original_text": cell.get("raw_text", cell.get("display", "")),
                            "current_text": cell.get("current_text", cell.get("raw_text", cell.get("display", ""))),
                            "reviewed": bool(cell.get("reviewed")), "correction_revision": cell.get("correction_revision"),
                            "manual": bool(cell.get("manual", source.get("manual", False))),
                            "value_type": cell["value_type"], "source": copy.deepcopy(source),
                            "structure": sheet["structure"], "editable": True})
    return catalog


def structure_tables(book: dict) -> list[dict]:
    tables = []
    pages = {page["number"]: page for page in book.get("pages", [])}
    for sheet in book["sheets"]:
        if sheet.get("member_sheets") or sheet.get("method") != "ocr":
            continue
        rows, columns = sheet["max_row"], sheet["max_column"]
        page = pages.get(sheet["page_number"], {})
        limit = None
        if page.get("status") != "ok" or page.get("method") != "ocr":
            limit = "提取失败或页面不可用，不能手工修复该页结构。"
        elif rows > MAX_STRUCTURE_ROWS or columns > MAX_STRUCTURE_COLUMNS or rows * columns > MAX_STRUCTURE_CELLS:
            limit = "结构编辑上限为 500 行、60 列、10000 格；本区域超限，不会自动裁剪。"
        elif any(len(cell.get("current_text", cell.get("raw_text", cell.get("display", "")))) > MAX_CELL_TEXT
                 for cell in sheet["cells"].values()):
            limit = "本区域存在超过 10000 字符的单元格，不能结构编辑，不会自动裁剪。"
        tables.append({"sheet": sheet["name"], "page": sheet["page_number"], "structure": sheet["structure"],
                       "rows": rows, "columns": columns, "editable": limit is None,
                       **({"edit_limit": limit} if limit else {})})
    return tables


def structure_table(book: dict, sheet: str) -> dict:
    entry = next((item for item in structure_tables(book) if item["sheet"] == sheet), None)
    if entry is None:
        raise ValueError("只能编辑已有 OCR 物理区域，不能编辑逻辑候选或文本型页面。")
    if not entry["editable"]:
        raise ValueError(entry["edit_limit"])
    current = next(item for item in book["sheets"] if item["name"] == sheet)
    rows = []
    for row in range(1, current["max_row"] + 1):
        output = []
        for column in range(1, current["max_column"] + 1):
            address = f"{get_column_letter(column)}{row}"
            cell = current["cells"].get(address)
            if cell is None:
                cell = empty_cell(row, column)
                cell.update(raw_text="", manual=True, pdf_source={"kind": "pdf", "page": current["page_number"],
                            "bbox": [0, 0, 1, 1], "method": "ocr", "manual": True, "location_precision": "page"})
            source = copy.deepcopy(cell["pdf_source"])
            if current["structure"] == "text_blocks" and row == 1:
                source.update(bbox=[0, 0, 1, 1], location_precision="page")
            output.append({"id": _review_id(book, current, cell),
                           "text": cell.get("current_text", cell.get("raw_text", cell.get("display", ""))),
                           "original_text": cell.get("raw_text", cell.get("display", "")),
                           "source": source,
                           "manual": bool(cell.get("manual", cell["pdf_source"].get("manual", False))),
                           "reviewed": bool(cell.get("reviewed"))})
        rows.append(output)
    return {"sheet": sheet, "page": current["page_number"], "structure": current["structure"], "rows": rows,
            "structure_confirmed": bool(current.get("structure_confirmed"))}


def prepare_structure_event(current_book: dict, event: dict) -> dict:
    if event.get("action") != "table_update":
        raise ValueError("结构保存必须使用 table_update 操作。")
    if not isinstance(event.get("reason"), str) or not event["reason"].strip():
        raise ValueError("结构修改必须填写原因。")
    if event.get("structure_confirmed") is not True:
        raise ValueError("请明确确认该区域行列结构已按原图核对；这不等于内容已核验。")
    if not isinstance(event.get("revision"), int) or isinstance(event["revision"], bool) or event["revision"] < 1:
        raise ValueError("结构修订必须包含有效的服务端修订号。")
    before = structure_table(current_book, event.get("sheet"))
    if event.get("page") != before["page"]:
        raise ValueError("所选页面与物理区域不一致。")
    rows = event.get("rows")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_STRUCTURE_ROWS:
        raise ValueError("结构矩阵必须包含 1 至 500 行。")
    if not isinstance(rows[0], list) or not 1 <= len(rows[0]) <= MAX_STRUCTURE_COLUMNS:
        raise ValueError("结构矩阵必须包含 1 至 60 列。")
    columns = len(rows[0])
    if len(rows) * columns > MAX_STRUCTURE_CELLS:
        raise ValueError("结构矩阵最多包含 10000 格。")
    existing = {cell["id"]: cell for row in before["rows"] for cell in row}
    used = set()
    after_rows = []
    for row_index, row in enumerate(rows, 1):
        if not isinstance(row, list) or len(row) != columns:
            raise ValueError("结构矩阵必须为矩形，每行列数相同。")
        output = []
        for column, proposed in enumerate(row, 1):
            if not isinstance(proposed, dict) or set(proposed) - {"id", "text"}:
                raise ValueError("结构单元格只能提交 id 和 text，不能提交来源或核验状态。")
            if not isinstance(proposed.get("text"), str) or len(proposed["text"]) > MAX_CELL_TEXT:
                raise ValueError("单元格文本必须是字符串且不超过 10000 字符。")
            target = proposed.get("id")
            if target is not None:
                if not isinstance(target, str) or target not in existing:
                    raise ValueError("结构编辑只能引用当前物理区域仍存在的单元格 ID。")
                if target in used:
                    raise ValueError("同一单元格 ID 在结构矩阵中只能出现一次。")
                used.add(target)
                cell = copy.deepcopy(existing[target])
            else:
                identity = (f"manual\x1f{current_book.get('review_namespace', '')}\x1f{event.get('side', '')}"
                            f"\x1f{before['sheet']}\x1f{event['revision']}\x1f{row_index}\x1f{column}")
                cell = {"id": hashlib.sha256(identity.encode()).hexdigest()[:20], "original_text": "", "manual": True,
                        "source": {"kind": "pdf", "page": before["page"], "bbox": [0, 0, 1, 1],
                                   "method": "ocr", "manual": True, "location_precision": "page"}}
            cell.update(text=proposed["text"], reviewed=False)
            output.append(cell)
        after_rows.append(output)
    after = {"sheet": before["sheet"], "page": before["page"], "structure": "table",
             "rows": after_rows, "structure_confirmed": True}
    prepared = copy.deepcopy(event)
    prepared.update(table_before=before, table_after=after, structure_revision=event["revision"],
                    before_text=f"{len(before['rows'])} 行 × {len(before['rows'][0])} 列",
                    after_text=f"{len(after_rows)} 行 × {columns} 列")
    return prepared


def _set_text(sheet: dict, cell: dict, text: str) -> None:
    cell.update(value=text or None, display=text, current_text=text, value_type="string" if text else "blank")
    header = sheet["cells"].get(f"{get_column_letter(cell['column'])}1", {})
    # 始终优先读取第 1 行表头的当前修订文本，依次回退原始文本与展示文本
    label = header.get("current_text", header.get("raw_text", header.get("display", "")))
    identifier = any(part in label.lower() for part in ("编号", "编码", "代码", "序号", "id", "code"))
    if sheet.get("structure_revision"):
        numeric_header = any(part in label.lower() for part in ("金额", "数量", "单价", "合价", "税率", "比例", "数值",
                                                               "amount", "price", "quantity", "qty", "total", "rate", "cost"))
        original_identifier = cell.get("original_value_type") == "string" and _decimal_text(cell.get("raw_text", "")) is not None
        identifier |= not numeric_header and (original_identifier or bool(re.fullmatch(r"[+-]?0\d+", text.strip())))
    else:
        identifier |= cell["column"] == 1
    if cell["row"] > 1 and sheet["structure"] == "table" and not identifier:
        number = _decimal_text(text)
        if number is not None:
            cell.update(value=number, value_type="number")


def _review_state(book: dict, originally_unconfirmed: set[int]) -> tuple[dict, dict]:
    targets = {}
    by_page = {}
    physical = [sheet for sheet in book["sheets"] if not sheet.get("member_sheets")]
    sheets = {sheet["name"]: sheet for sheet in physical}
    for item in review_catalog(book):
        sheet = sheets[item["sheet"]]
        cell = sheet["cells"][item["cell"]]
        targets[item["id"]] = (sheet, cell, item["page"])
        by_page.setdefault(item["page"], []).append(cell)
    for page in book.get("pages", []):
        number = page["number"]
        if number in originally_unconfirmed:
            regions = [sheet for sheet in physical if sheet.get("page_number") == number and sheet.get("method") == "ocr"]
            page["structure_unconfirmed"] = not (page["status"] == "ok" and regions and
                all(sheet["structure"] == "table" and sheet.get("structure_confirmed") for sheet in regions))
        page["reviewable"] = (page["method"] == "ocr" and page["status"] == "ok" and
                              bool(by_page.get(number)) and
                              (book.get("report_inventory_version", 0) >= 2 or not page.get("structure_unconfirmed", False)))
    return targets, by_page


def apply_reviews(book: dict, events: list[dict], rebuild_inventory: bool = True) -> dict:
    result = copy.deepcopy(book)
    sheets = {sheet["name"]: sheet for sheet in result["sheets"]}
    pages = {page["number"]: page for page in result.get("pages", [])}
    originally_unconfirmed = {number for number, page in pages.items() if page.get("structure_unconfirmed")}
    for page in pages.values():
        page["reviewed"] = False
    for sheet in result["sheets"]:
        if sheet.get("member_sheets"):
            continue
        for cell in sheet["cells"].values():
            if cell.get("pdf_source", {}).get("method") == "ocr" or cell.get("requires_review"):
                cell.setdefault("original_value_type", cell["value_type"])
                _set_text(sheet, cell, cell.get("raw_text", cell.get("display", "")))
                cell.update(review_id=_review_id(result, sheet, cell), reviewed=False, correction_revision=None)
    targets, by_page = _review_state(result, originally_unconfirmed)
    structure_changed = False
    for event in sorted(events, key=lambda item: item["revision"]):
        action = event["action"]
        if action == "table_update":
            prepared = event if event.get("table_after") else prepare_structure_event(result, event)
            before = structure_table(result, prepared.get("sheet"))
            after = prepared["table_after"]
            if after["sheet"] != before["sheet"] or after["page"] != before["page"]:
                raise ValueError("结构历史快照与当前物理区域不一致。")
            sheet = sheets[before["sheet"]]
            prior = {_review_id(result, sheet, cell): cell for cell in sheet["cells"].values()}
            cells = {}
            for row, values in enumerate(after["rows"], 1):
                for column, snapshot in enumerate(values, 1):
                    previous = prior.get(snapshot["id"])
                    cell = copy.deepcopy(previous) if previous else empty_cell(row, column)
                    address = f"{get_column_letter(column)}{row}"
                    changed_text = previous is None or snapshot["text"] != previous.get("current_text", previous.get("display", ""))
                    cell.update(row=row, column=column, address=address, review_id=snapshot["id"],
                                raw_text=snapshot["original_text"], current_text=snapshot["text"],
                                pdf_source=copy.deepcopy(snapshot["source"]), manual=bool(snapshot["manual"]), reviewed=False,
                                correction_revision=event["revision"] if changed_text else cell.get("correction_revision"))
                    if result.get("report_inventory_version"):
                        cell["requires_review"] = True
                    cells[address] = cell
            sheet.update(cells=cells, max_row=len(after["rows"]), max_column=len(after["rows"][0]),
                         structure="table", structure_confirmed=True, structure_revision=event["revision"],
                         suggested_header_row=1, merged_ranges=[])
            for cell in cells.values():
                _set_text(sheet, cell, cell["current_text"])
            targets, by_page = _review_state(result, originally_unconfirmed)
            for cell in by_page.get(before["page"], []):
                cell["reviewed"] = False
            pages[before["page"]]["reviewed"] = False
            structure_changed = True
        elif action in ("page_verify", "page_unverify"):
            number = event.get("page")
            page = pages.get(number)
            if not page or page["method"] != "ocr":
                raise ValueError("没有找到可核验的 OCR 页面。")
            verified = action == "page_verify"
            if verified and not page["reviewable"]:
                raise ValueError("该页提取失败或结构尚未确认，不能通过文字核验将整页标为已确认。")
            page["reviewed"] = verified
            for cell in by_page.get(number, []):
                cell["reviewed"] = verified
        elif action in ("cell_update", "cell_verify", "cell_unverify"):
            target = targets.get(event.get("target_id"))
            if target is None:
                raise ValueError("没有找到对应的 OCR 单元格。")
            sheet, cell, number = target
            if action == "cell_update":
                if not isinstance(event.get("value"), str):
                    raise ValueError("校正内容必须为文本；清空单元格请使用空字符串。")
                _set_text(sheet, cell, event["value"])
                cell.update(reviewed=False, correction_revision=event["revision"])
                # 无论是否经历结构重建，第 1 行表头变更后都需基于数据格原文本重算同列类型
                if cell["row"] == 1:
                    for dependent in sheet["cells"].values():
                        if dependent["row"] > 1 and dependent["column"] == cell["column"]:
                            _set_text(sheet, dependent, dependent.get("current_text", dependent.get("raw_text", dependent.get("display", ""))))
            else:
                cell["reviewed"] = action == "cell_verify"
            if action != "cell_verify":
                pages[number]["reviewed"] = False
        else:
            raise ValueError("不支持的 OCR 核验操作。")
    if result.get("report_inventory_version"):
        result["sheets"] = [sheet for sheet in result["sheets"] if not sheet.get("member_sheets")]
    elif structure_changed:
        physical = [sheet for sheet in result["sheets"] if not sheet.get("member_sheets")]
        result["warnings"] = [warning for warning in result.get("warnings", [])
                              if "页相同表头已拼接为候选报表，请在配置预览确认；不同业务表可能具有相同表头。" not in warning]
        result["sheets"] = physical + _logical_tables(physical, result["warnings"])
    else:
        for sheet in result["sheets"]:
            members = sheet.get("member_sheets")
            if not members:
                continue
            next_row = 1
            for index, name in enumerate(members):
                member = sheets[name]
                for cell in member["cells"].values():
                    if index and cell["row"] == 1:
                        continue
                    row = cell["row"] if not index else next_row + cell["row"] - 2
                    address = f"{get_column_letter(cell['column'])}{row}"
                    if address in sheet["cells"]:
                        updated = copy.deepcopy(cell)
                        updated.update(row=row, address=address)
                        sheet["cells"][address] = updated
                next_row = member["max_row"] + 1 if not index else next_row + member["max_row"] - 1
    for sheet in result["sheets"]:
        sheet["nonempty_count"] = sum(c["value"] is not None for c in sheet["cells"].values())
    for number, page in pages.items():
        page["total_cells"] = len(by_page.get(number, []))
        page["reviewed_cells"] = sum(cell["reviewed"] for cell in by_page.get(number, []))
        if page["method"] == "ocr":
            page["ocr_unverified"] = not page["reviewed"]
        if result.get("report_inventory_version") and page.get("reviewed"):
            for region in page.get("ocr_regions", []):
                if region.get("status") == "ok" or not region.get("affects_content"):
                    continue
                affected = [(sheet, cell) for sheet in result["sheets"] if not sheet.get("member_sheets")
                            for cell in sheet["cells"].values()
                            if region.get("id") in cell.get("pdf_source", {}).get("region_ids", [])]
                if not affected or not all(cell.get("reviewed") and not any(re.search(r"\(cid:\d+\)|�|\x00", str(cell.get(key) or ""))
                    for key in ("value", "display", "current_text")) for _, cell in affected):
                    continue
                bounds = region.get("bbox", [0, 0, 1, 1])
                reconstructed = any(sheet.get("structure_revision") and
                    (box := sheet.get("bbox", [0, 0, 0, 0]))[0] <= bounds[0] and box[1] <= bounds[1] and
                    box[2] >= bounds[2] and box[3] >= bounds[3] for sheet, _ in affected)
                if reconstructed or region.get("reason") == "invalid_text" and any(cell.get("correction_revision") for _, cell in affected):
                    region.update(status="ok", resolved_by_review=True)
                    result["warnings"] = [f"原始提取提示：{warning} 当前该区域已根据人工修订恢复并完成核验。"
                                          if region["id"] in warning else warning for warning in result.get("warnings", [])]
    reviewed_pages = {number for number, page in pages.items() if page["reviewed"]}
    result["warnings"] = [warning for warning in result.get("warnings", [])
                          if not any(warning == f"第 {number} 页使用本地 OCR；全部识别结果尚未人工核验。" or
                                     (number in originally_unconfirmed and not pages[number].get("structure_unconfirmed") and
                                      warning == f"第 {number} 页未能可靠恢复表格结构，仅保存单列文字块并按位置比较，须核对错列或漏行。")
                                     for number in reviewed_pages)]
    for index, warning in enumerate(result["warnings"]):
        for number in originally_unconfirmed:
            old_warning = f"第 {number} 页未能可靠恢复表格结构，仅保存单列文字块并按位置比较，须核对错列或漏行。"
            if warning == old_warning and not pages[number].get("structure_unconfirmed"):
                result["warnings"][index] = (f"第 {number} 页已按人工确认的行列重建表格；" +
                    ("内容已完成整页人工核验。" if pages[number]["reviewed"] else "结构确认不等于内容核验，请继续核对文字与漏项。"))
        if any(warning.startswith(f"第 {number} 页同时包含文本和图像，") for number in reviewed_pages):
            result["warnings"][index] = warning.replace("包括原有文本在内均需核验", "包括原有文本在内已完成整页人工核验")
    if rebuild_inventory and result.get("report_inventory_version"):
        from .reports import attach_report_inventory
        result = attach_report_inventory(result)
    return result
