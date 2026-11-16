"""Evidence-based PDF report inventory, independent of content comparison."""
from __future__ import annotations

import copy
import hashlib
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher

from openpyxl.utils import get_column_letter, range_boundaries

_CODE = re.compile(r"(?:表\s*)?([ABC]\s*[.．]\s*\d+(?:\s*[.．]\s*\d+)*)\s*(.*)", re.I)
_OBJECT = re.compile(r"(单项工程名称|单位工程名称|工程名称)\s*[:：]")
_BAD = re.compile(r"\(cid:\d+\)|�")


def _clean(value):
    return re.sub(r"\s+", "", str(value or "")).replace("（", "(").replace("）", ")")


def _id(*values):
    return hashlib.sha256("\x1f".join(map(str, values)).encode()).hexdigest()[:14]


def _box(sheet):
    if sheet.get("bbox"):
        return sheet["bbox"]
    boxes = [c["pdf_source"]["bbox"] for c in sheet.get("cells", {}).values()
             if c.get("pdf_source", {}).get("bbox") and c.get("row", 0) > 1]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else [0, 1, 1, 1]


def _lines(page, sheets):
    lines = page.get("evidence_lines") or page.get("native_lines") or []
    if not lines:
        lines = [{"text": c.get("display", ""), **c["pdf_source"]}
                 for s in sheets for c in s.get("cells", {}).values()
                 if c.get("pdf_source") and c.get("display") and c.get("row", 0) > 1]
    lines = [dict(line) for line in lines]
    for sheet in sheets:
        for cell in sheet.get("cells", {}).values():
            if "current_text" not in cell or cell.get("current_text") == cell.get("raw_text", cell.get("display")):
                continue
            source = cell.get("pdf_source", {})
            box = source.get("bbox")
            raw = cell.get("raw_text", "")
            for line in lines:
                bounds = line.get("bbox")
                if not box or not bounds or min(box[2], bounds[2]) <= max(box[0], bounds[0]) or min(box[3], bounds[3]) <= max(box[1], bounds[1]):
                    continue
                if raw and raw in line["text"]:
                    line["text"] = line["text"].replace(raw, cell["current_text"], 1)
                elif box == bounds:
                    line["text"] = cell["current_text"]
                else:
                    line["method"] = "ocr"
                if not cell.get("reviewed"):
                    line["method"] = "ocr"
    return sorted([dict(line, method=line.get("method", "text")) for line in lines],
                  key=lambda line: (line.get("bbox", [0, 0, 0, 0])[1], line.get("bbox", [0, 0, 0, 0])[0]))


def _titles(lines):
    result = []
    for index, line in enumerate(lines):
        text = _clean(line["text"])
        match = _CODE.fullmatch(text)
        if match:
            code, title = match.groups()
            if not title:
                following = _clean(lines[index + 1]["text"]) if index + 1 < len(lines) else ""
                if len(following) <= 60 and re.search(r"表$|封面$|扉页$|说明$", following) and not _OBJECT.search(following):
                    title = following
                else:
                    continue
            if title and len(title) > 90:
                continue
            result.append({**line, "type_code": code.upper().replace("．", "."), "title": title or code})
        elif (len(text) <= 65 and re.search(r"(?:报表|明细表|汇总表|清单表|费率表|计算程序|费用表)$", text)
              and not _OBJECT.search(text)):
            title = re.sub(r"^\d+(?:\.\d+)*", "", text)
            result.append({**line, "type_code": None, "title": title})
    return result


def _identity(title, context):
    values, slots, evidence = {}, set(), []
    for line in context:
        text = _clean(line["text"])
        matches = list(_OBJECT.finditer(text))
        for index, match in enumerate(matches):
            key = match.group(1)
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            value = re.split(r"第.{0,8}页|共.{0,8}页|编制人|编制日期|日期[:：]|单位[:：]", text[match.end():end])[0]
            value = value.strip("_—-:： ")
            slots.add(key)
            if value:
                values[key] = value
            evidence.append(line)
    # A local discipline subtitle distinguishes two rate tables on the same page.
    subtitle = next((line for line in reversed(context)
                     if re.fullmatch(r"(?:建筑|安装|市政公用|园林绿化|装饰装修|城市轨道交通).*工程", _clean(line["text"]))
                     and len(_clean(line["text"])) < 45 and not _OBJECT.search(_clean(line["text"]))), None)
    if subtitle:
        values["专业"] = _clean(subtitle["text"])
        evidence.append(subtitle)
    value = " / ".join(f"{key}：{values[key]}" for key in ("工程名称", "单项工程名称", "单位工程名称", "专业") if key in values)
    status = "known" if values else "template" if slots else "unknown"
    evidence = ([title] if title else []) + evidence
    reliable = bool(title and status == "known" and slots.issubset(values) and all(line.get("method", "text") == "text" and not _BAD.search(line["text"]) for line in evidence))
    return value, status, reliable, evidence, values


def _header(sheet):
    row = sheet.get("suggested_header_row", 1)
    return [_clean(sheet.get("cells", {}).get(f"{get_column_letter(c)}{row}", {}).get("display", ""))
            for c in range(1, sheet.get("max_column", 1) + 1)]


def _type(report):
    return report.get("type_code") or ("title:" + _clean(report["title"]) if report.get("title") != "未识别报表" else None)


def _object_fields(report):
    if "object_fields" in report:
        return report["object_fields"]
    fields = {}
    for part in report.get("object", "").split(" / "):
        match = re.match(r"(工程名称|单项工程名称|单位工程名称|专业)[:：](.*)", part)
        if match and _clean(match[2]):
            fields[match[1]] = _clean(match[2])
    if not fields and report.get("object"):
        fields["对象"] = _clean(report["object"])
    return fields


def _object_conflict(left, right):
    a, b = _object_fields(left), _object_fields(right)
    return any(a[field] != b[field] for field in a.keys() & b.keys())


def _key(report):
    return (_type(report), tuple(sorted(_object_fields(report).items())))


def _content(sheet):
    if not sheet or sheet.get("structure") != "table" or sheet.get("max_column", 0) < 2:
        return False
    header = sheet.get("suggested_header_row", 1)
    return any(c.get("row", 0) > header and str(c.get("display", "")).strip() for c in sheet.get("cells", {}).values())


def _intersects(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


def _unresolved_regions(book, target=None):
    unresolved = []
    for page in book.get("pages", []):
        number = page.get("number", page.get("page"))
        regions = list(page.get("ocr_regions", []))
        if page.get("status") == "unavailable":
            regions.append({"bbox": [0, 0, 1, 1], "status": "failed", "affects_content": True})
        for region in regions:
            if region.get("status") == "ok" or not region.get("affects_content", True):
                continue
            owners = [r for r in book.get("reports", []) if any(
                item["page"] == number and all((item["bbox"][i] - .005 <= region.get("bbox", [0, 0, 1, 1])[i]) if i < 2 else
                                                  (item["bbox"][i] + .005 >= region.get("bbox", [0, 0, 1, 1])[i]) for i in range(4))
                for item in r.get("regions", []))]
            if target and owners and all(r["identity_reliable"] and (_type(r) != _type(target) or target.get("object_status") == "known" and _object_conflict(r, target)) for r in owners):
                continue
            if target and region.get("type_code") and region["type_code"] != target.get("type_code"):
                continue
            unresolved.append({"kind": "pdf", "page": number, "bbox": region.get("bbox", [0, 0, 1, 1]), "method": "ocr"})
    return unresolved


def _key_blockers(old, new, left, right, pair):
    mappings = [c for c in pair.get("columns", []) if c.get("is_key") and c.get("old_column") and c.get("new_column")]
    if pair.get("mode", "key") != "key" or not mappings:
        return ["尚未配置可靠业务键或确认的组合键，不能按行位置比较"]
    reasons = []
    for side, book, report in (("old", old, left), ("new", new, right)):
        sheet = next((s for s in book["sheets"] if s["name"] == report["sheet_name"]), None)
        if not sheet:
            continue
        header = pair.get(f"{side}_header_end", 1)
        lower, upper = header + 1, sheet.get("max_row", 1)
        if pair.get(f"{side}_range"):
            try:
                _, r1, _, r2 = range_boundaries(pair[f"{side}_range"])
                lower, upper = max(lower, r1), min(upper, r2)
            except (ValueError, TypeError):
                reasons.append("业务键范围配置无效")
                continue
        rows = sorted({c["row"] for c in sheet.get("cells", {}).values() if lower <= c["row"] <= upper and _clean(c.get("display"))})
        keys = []
        for row in rows:
            values = []
            for mapping in mappings:
                cell = sheet["cells"].get(f"{mapping[f'{side}_column']}{row}", {})
                if cell.get("merged_range"):
                    c1, r1, _, _ = range_boundaries(cell["merged_range"])
                    cell = sheet["cells"].get(f"{get_column_letter(c1)}{r1}", {})
                values.append(_clean(cell.get("display")))
            keys.append(tuple(values))
        if any(not all(key) for key in keys):
            reasons.append(f"{'旧版' if side == 'old' else '新版'}业务键缺失，须核对行结构")
        if len(keys) != len(set(keys)):
            reasons.append(f"{'旧版' if side == 'old' else '新版'}业务键重复，须确认组合键")
    return reasons


def _merge_ranges(sheet):
    return list(dict.fromkeys(sheet.get("merged_ranges", []) + [c["merged_range"] for c in sheet.get("cells", {}).values() if c.get("merged_range")]))


def _header_crossing_merge(sheet):
    header = sheet.get("suggested_header_row", 1)
    return any(range_boundaries(merged)[1] <= header < range_boundaries(merged)[3] for merged in _merge_ranges(sheet))


def _content_blockers(book, report):
    sheets = [s for s in book["sheets"] if s["name"] in report["member_sheets"]]
    reasons = ["同一报表的结构片段表头不兼容，须核对结构"] if report.get("incompatible_fragments") else []
    for sheet in sheets:
        if _header_crossing_merge(sheet):
            reasons.append("合并单元格跨越表头与正文边界，须核对结构后再拼接比较")
        for cell in sheet.get("cells", {}).values():
            current_values = [cell.get(field, "") for field in ("value", "current_text", "display")]
            if any(_BAD.search(str(value)) for value in current_values):
                reasons.append("表格内容含未恢复乱码")
            if (cell.get("pdf_source", {}).get("method") == "ocr" or cell.get("requires_review")) and not cell.get("reviewed"):
                reasons.append("表格 OCR 或结构编辑内容尚未核验")
        box = _box(sheet)
        page = next((p for p in book.get("pages", []) if p.get("number") == sheet.get("page_number")), {})
        if any(c.get("pdf_source", {}).get("method") == "ocr" or c.get("requires_review") for c in sheet.get("cells", {}).values()) and not page.get("reviewed"):
            reasons.append("页面完整性尚未核验，不能排除漏行或漏格")
        if sheet.get("method") == "ocr" and page.get("structure_unconfirmed") and not sheet.get("structure_confirmed"):
            reasons.append("OCR 表格结构尚未确认")
        for region in page.get("ocr_regions", []):
            bounds = region.get("bbox", [0, 0, 0, 0])
            intersects = min(box[2], bounds[2]) > max(box[0], bounds[0]) and min(box[3], bounds[3]) > max(box[1], bounds[1])
            if intersects and region.get("affects_content") and region.get("status") != "ok":
                reasons.append("关联内容区域未能可靠恢复")
    return list(dict.fromkeys(reasons))


def _logical_report_sheet(report, physical_map):
    if len(report["member_sheets"]) < 2 or report.get("incompatible_fragments"):
        return None
    members = [physical_map[n] for n in report["member_sheets"]]
    if any(_header_crossing_merge(member) for member in members):
        return None
    sheet = copy.deepcopy(members[0])
    sheet.update(name=f"报表 · {report['title']} · {report['id']}", member_sheets=report["member_sheets"], candidate_merge=False)
    sheet["merged_ranges"] = _merge_ranges(sheet)
    next_row = sheet["max_row"] + 1
    for member in members[1:]:
        header = member.get("suggested_header_row", 1)
        row_offset = next_row - header - 1
        translated_ranges = {}
        for merged_range in _merge_ranges(member):
            c1, r1, c2, r2 = range_boundaries(merged_range)
            if r2 <= header:
                continue
            translated = f"{get_column_letter(c1)}{r1 + row_offset}:{get_column_letter(c2)}{r2 + row_offset}"
            translated_ranges[merged_range] = translated
            sheet["merged_ranges"].append(translated)
        for cell in member["cells"].values():
            if cell["row"] <= header:
                continue
            value = copy.deepcopy(cell)
            value["row"] = next_row + cell["row"] - header - 1
            value["address"] = f"{get_column_letter(value['column'])}{value['row']}"
            if value.get("merged_range"):
                value["merged_range"] = translated_ranges[value["merged_range"]]
            sheet["cells"][value["address"]] = value
        next_row += member["max_row"] - header
    sheet["max_row"] = next_row - 1
    sheet["nonempty_count"] = sum(bool(c.get("display")) for c in sheet["cells"].values())
    report["sheet_name"] = sheet["name"]
    return sheet


def _attach_report_inventory_v1(book: dict, title_detector=_titles) -> dict:
    result = copy.deepcopy(book)
    physical = [s for s in result.get("sheets", []) if not s.get("member_sheets")]
    by_page = defaultdict(list)
    for sheet in physical:
        by_page[sheet.get("page_number", 1)].append(sheet)
    pages = {p.get("number", p.get("page")): p for p in result.get("pages", [])}
    reports, logical, non_report = [], [], []
    previous_document = False
    for number in sorted(set(pages) | set(by_page)):
        sheets = by_page[number]
        lines = _lines(pages.get(number, {}), sheets)
        titles = title_detector(lines) if title_detector is _titles else title_detector(lines, sheets)
        tables = sorted([s for s in sheets if s.get("structure") == "table"], key=lambda s: (_box(s)[1], _box(s)[0]))
        meaningful = [line for line in lines if not re.fullmatch(r"[\d\s—-]+", line["text"].strip())]
        page = pages.get(number, {})
        numbered_clause = any(re.match(r"^\d+\.\d+(?:\.\d+)?[^\d.]", _clean(line["text"])) for line in meaningful)
        no_identity = not any(t["type_code"] for t in titles) and not any(_OBJECT.search(_clean(line["text"])) for line in lines)
        document = no_identity and (numbered_clause and (previous_document or any(len(_clean(line["text"])) > 35 for line in meaningful)) or
                                    previous_document and not tables and sum(len(_clean(line["text"])) for line in meaningful) > 40)
        empty_page = not meaningful and not tables and page.get("status") != "unavailable" and not page.get("ocr_regions")
        region_source = {"kind": "pdf", "page": number, "bbox": [0, 0, 1, 1], "method": page.get("method", "text")}
        if empty_page or document:
            non_report.append({"id": "region-" + _id(number), "type": "blank_page" if empty_page else "document_section",
                               "pages": [number], "source": region_source, "member_sheets": [s["name"] for s in sheets],
                               "basis": "仅有页码或空白" if empty_page else "数字章节与条款正文，无业务报表类型或工程对象"})
            previous_document = document
            continue
        previous_document = False
        if not titles and not tables:
            previous = reports[-1] if reports and reports[-1]["pages"][-1] == number - 1 else None
            note = previous and previous.get("has_notes") and meaningful and len(meaningful) <= 6 and len("".join(l["text"] for l in meaningful)) < 250 and re.match(r"^(?:注[:：]?|\d+[^\d.])", _clean(meaningful[0]["text"]))
            if note:
                previous["pages"].append(number)
                previous["sources"].extend({**region_source, "bbox": line["bbox"], "text": line["text"], "method": line.get("method", "text")} for line in meaningful)
                previous.setdefault("continuation_notes", []).append({"page": number, "basis": "前页注释的连续编号短注释，无新标题或表格"})
                non_report.append({"id": "region-" + _id(number), "type": "continuation_note", "pages": [number], "source": region_source,
                                   "report_id": previous["id"], "member_sheets": [s["name"] for s in sheets], "basis": previous["continuation_notes"][-1]["basis"]})
            else:
                non_report.append({"id": "region-" + _id(number), "type": "unclassified_region", "pages": [number], "source": region_source,
                                   "member_sheets": [s["name"] for s in sheets], "basis": "未识别出业务报表身份，保留文档区域待核对"})
            continue
        # A title owns evidence only up to the next independent title.
        leaf = [t for t in titles if not any(t.get("type_code") and u.get("type_code", "") and u["type_code"].startswith(t["type_code"] + ".") for u in titles)]
        targets = []
        for table in tables:
            top = _box(table)[1]
            candidates = [t for t in leaf if t.get("bbox", [0, 0, 0, 0])[1] <= top + .015]
            targets.append((table, top, candidates[-1] if candidates else None))
        for title in leaf:
            if not any(target[2] is title for target in targets):
                text_sheet = next((s for s in sheets if s.get("structure") == "text_blocks"), None) if len(leaf) == 1 and not tables else None
                targets.append((text_sheet, title.get("bbox", [0, 0, 0, 0])[1], title))
        targets.sort(key=lambda target: (target[2].get("bbox", [0, 0, 0, 0])[1] if target[2] else target[1], target[1]))
        prior_bottom = 0
        for sheet, top, title in targets:
            is_table = bool(sheet and sheet.get("structure") == "table")
            start = title.get("bbox", [0, 0, 0, 0])[1] if title else prior_bottom
            end = min((t.get("bbox", [0, 0, 0, 0])[1] for t in leaf if t.get("bbox", [0, 0, 0, 0])[1] > start + .001), default=1.01)
            local_lines = [line for line in lines if start <= line.get("bbox", [0, 0, 0, 0])[1] < end]
            context = [line for line in local_lines if not is_table or line.get("bbox", [0, 0, 0, 0])[1] <= top + .015]
            local_meaningful = [line for line in local_lines if not re.fullmatch(r"[\d\s—-]+", line["text"].strip())]
            obj, status, reliable, evidence, object_fields = _identity(title, context)
            report_id = "report-" + _id(number, sheet.get("name") if sheet else "page", title and title["title"], len(reports))
            issues = []
            if not title:
                issues.append("报表标题或类型未识别")
            if status != "known":
                issues.append("工程对象为空白模板" if status == "template" else "工程对象未识别")
            if any(e.get("method") == "ocr" or _BAD.search(e["text"]) for e in evidence):
                issues.append("身份依据含 OCR 或乱码，须人工确认")
            text_report = bool(title and re.search(r"封面|扉页|说明", title["title"]))
            identity_only = bool(title and "表" in title["title"] and not is_table and local_meaningful and not page.get("ocr_regions") and
                                 all(_clean(l["text"]) == _clean(title["text"]) or _CODE.fullmatch(_clean(l["text"])) or _OBJECT.search(_clean(l["text"])) or re.fullmatch(r"(?:第.*页)?(?:共.*页)?", _clean(l["text"])) for l in local_meaningful) and
                                 not any(_BAD.search(l["text"]) for l in local_meaningful))
            if not _content(sheet) and not text_report:
                issues.append("源页仅含标题、工程对象与页码，未见表体，无法比较字段" if identity_only else "未提取到可比较的多列表格内容")
            sources = [{"kind": "pdf", "page": number, "bbox": e.get("bbox", [0, 0, 1, 1]), "method": e.get("method", "text"), "text": e["text"]} for e in evidence]
            report = {"id": report_id, "type_code": title and title["type_code"], "title": title["title"] if title else "未识别报表",
                      "object": obj, "object_fields": object_fields, "object_status": status, "pages": [number], "member_sheets": [sheet["name"]] if sheet else [],
                      "source": sources[0] if sources else {"kind": "pdf", "page": number, "bbox": [0, 0, 1, 1], "method": pages.get(number, {}).get("method", "text")},
                      "sources": sources, "issues": issues, "header_signature": _header(sheet) if sheet else [],
                      "identity_reliable": reliable, "sheet_name": sheet["name"] if sheet else None,
                      "title_anchor": title.get("bbox") if title else None, "text_report": text_report,
                      "source_body_empty": identity_only, "has_notes": any(re.search(r"注\s*[:：]", l["text"]) for l in local_lines),
                      "regions": [{"page": number, "bbox": _box(sheet)}] if sheet and sheet.get("structure") == "table" else []}
            page_text = " ".join(line["text"] for line in local_lines)
            pagination = re.search(r"第\s*(\d+)\s*页\s*共\s*(\d+)\s*页", page_text)
            report["page_sequence"] = [int(pagination[1]), int(pagination[2])] if pagination else None
            report["continuation"] = bool(re.search(r"续表|接上页|续页|[（(]续[）)]", page_text) or (pagination and int(pagination[1]) > 1))
            report["issues"].extend(_content_blockers(result, report))
            same_page = next((r for r in reversed(reports) if r["pages"] == [number] and title and r.get("title_anchor") == title.get("bbox") and _key(r) == _key(report)), None)
            if same_page:
                same_page["member_sheets"].extend(report["member_sheets"])
                same_page["regions"].extend(report["regions"])
                same_page["sources"].extend(source for source in report["sources"] if source not in same_page["sources"])
                same_page["issues"] = list(dict.fromkeys(same_page["issues"] + report["issues"]))
                if same_page["header_signature"] != report["header_signature"]:
                    same_page["incompatible_fragments"] = True
                    same_page["issues"].append("同一报表的结构片段表头不兼容，须核对结构")
            else:
                reports.append(report)
            if is_table:
                prior_bottom = _box(sheet)[3]
    merged = []
    for report in reports:
        previous = [r for r in merged if r["identity_reliable"] and report["identity_reliable"] and _key(r) == _key(report)
                    and r["header_signature"] == report["header_signature"] and r["pages"][-1] == report["pages"][0] - 1]
        candidate = previous[0] if len(previous) == 1 and report["continuation"] else None
        if candidate and report["page_sequence"] and candidate.get("page_sequence"):
            a, b = candidate["page_sequence"], report["page_sequence"]
            if a[1] != b[1] or b[0] != a[0] + 1:
                candidate = None
        if candidate:
            candidate["pages"].extend(report["pages"])
            candidate["member_sheets"].extend(report["member_sheets"])
            candidate["sources"].extend(report["sources"])
            candidate["regions"].extend(report["regions"])
            candidate["page_sequence"] = report["page_sequence"]
            candidate["issues"] = list(dict.fromkeys(candidate["issues"] + report["issues"]))
        else:
            merged.append(report)
    counts = Counter(_key(r) for r in merged if _type(r))
    physical_map = {s["name"]: s for s in physical}
    for report in merged:
        if _type(report) and counts[_key(report)] > 1:
            report["issues"].append("同类型及对象存在多个独立实例，配对有歧义")
            report["duplicate"] = True
        sheet = _logical_report_sheet(report, physical_map)
        if sheet:
            logical.append(sheet)
    result.update(sheets=physical + logical, reports=merged, non_report_regions=non_report, report_inventory_version=1)
    return result


def _pair(left, right, number):
    columns = [{"id": f"m{i}", "name": label or f"列{i + 1}", "old_column": get_column_letter(i + 1),
                "new_column": get_column_letter(i + 1), "is_key": False}
               for i, label in enumerate(left["header_signature"])]
    key = next((c for c in columns if any(x in c["name"] for x in ("编码", "编号", "项目名称"))), None)
    if key:
        key["is_key"] = True
    return {"id": f"report-pair-{number}", "old_sheet": left["sheet_name"], "new_sheet": right["sheet_name"],
            "old_header_start": 1, "old_header_end": 1, "new_header_start": 1, "new_header_end": 1,
            "mode": "key" if key else "position", "columns": columns, "pairing_confirmed": False, "confirmation_reason": ""}


def _suggest_report_pairs_v1(old: dict, new: dict) -> list[dict]:
    old = old if old.get("report_inventory_version") == 1 else _attach_report_inventory_v1(old)
    new = new if new.get("report_inventory_version") == 1 else _attach_report_inventory_v1(new)
    pairs = []
    for left in old["reports"]:
        candidates = [r for r in new["reports"] if _key(r) == _key(left)]
        if left["identity_reliable"] and not left.get("duplicate") and len(candidates) == 1:
            right = candidates[0]
            if right["identity_reliable"] and not right.get("duplicate") and left["header_signature"] == right["header_signature"]:
                sheets = [next((s for s in b["sheets"] if s["name"] == r["sheet_name"]), None) for b, r in ((old, left), (new, right))]
                if all(_content(s) for s in sheets) and not _content_blockers(old, left) and not _content_blockers(new, right):
                    pair = _pair(left, right, len(pairs) + 1)
                    if not _key_blockers(old, new, left, right, pair):
                        pairs.append(pair)
    return pairs


def _compare_inventory_v1(old: dict, new: dict, config: dict) -> dict:
    books = {side: book if book.get("report_inventory_version") == 1 else _attach_report_inventory_v1(book) for side, book in (("old", old), ("new", new))}
    rows, issues, allowed, pair_ids = [], [], [], {}
    consumed = {"old": set(), "new": set()}
    lookup = {side: {name: r for r in book["reports"] for name in [r["sheet_name"], *r["member_sheets"]] if name} for side, book in books.items()}

    def add(left, right, pair=None, ambiguous=False):
        report = left or right
        row_id = "comparison-" + _id(left and left["id"], right and right["id"])
        blocking = []
        extra_sources = []
        confirmed = False
        if left and right:
            conflict = (_type(left) and _type(right) and _type(left) != _type(right)) or _object_conflict(left, right) or {left["object_status"], right["object_status"]} == {"template", "known"}
            manual = bool(pair and pair.get("pairing_confirmed") and pair.get("confirmation_reason", "").strip())
            reliable = left["identity_reliable"] and right["identity_reliable"] and _key(left) == _key(right) and not left.get("duplicate") and not right.get("duplicate")
            confirmed = not conflict and (reliable or manual)
            if conflict:
                blocking.append("明确的报表类型或已知工程对象冲突，不允许确认配对")
            if not confirmed:
                blocking.append("报表实例身份未可靠确认")
            if not left["header_signature"] or left["header_signature"] != right["header_signature"]:
                blocking.append("表头不兼容，不能比较内容")
            for side, r in (("old", left), ("new", right)):
                sheet = next((s for s in books[side]["sheets"] if s["name"] == r["sheet_name"]), None)
                blocking.extend(_content_blockers(books[side], r))
                if not _content(sheet):
                    blocking.append(("源页仅含标题、工程对象与页码，未见表体，无法比较字段" if r.get("source_body_empty") else
                                     "文本型报表尚未配置适用的内容比较方式" if r.get("text_report") else
                                     f"{'旧版' if side == 'old' else '新版'}缺少可比较的多列表格内容"))
            if pair:
                blocking.extend(_key_blockers(books["old"], books["new"], left, right, pair))
            else:
                blocking.append("此报表未纳入内容比较范围，须配置业务键和字段映射")
            presence = "shared" if confirmed else "uncertain"
            content_status = "reliable" if confirmed and not blocking and pair else "pending"
            basis = pair["confirmation_reason"] if manual and confirmed else "同类型、同对象，报表身份已确认" if confirmed else "类型清单相同不代表实例可可靠配对"
        else:
            side, opposite = ("old", "new") if left else ("new", "old")
            uncertain_opposite = any(not _type(r) or (_type(r) == _type(report) and
                                      (not r["identity_reliable"] or not _object_conflict(r, report)))
                                     for r in books[opposite]["reports"])
            extra_sources = [{**source, "side": opposite} for source in _unresolved_regions(books[opposite], report)]
            uncertain_opposite |= bool(extra_sources)
            if extra_sources:
                blocking.append("对侧存在尚未恢复且不能排除属于此报表的内容区域")
            uncertain_opposite |= any(region["type"] == "unclassified_region" for region in books[opposite].get("non_report_regions", []))
            confirmed = bool(report["identity_reliable"] and not report.get("duplicate") and not uncertain_opposite)
            presence = f"{side}_only" if confirmed else "uncertain"
            content_status = "not_applicable" if confirmed else "pending"
            basis = "对侧无对应类型或对象，身份依据完整" if confirmed else "单侧候选未匹配，尚不能确认新增或删除"
            if not confirmed:
                blocking.append(basis)
        if ambiguous:
            blocking.append("同类型且对象层级部分兼容的实例存在多个候选，配对有歧义")
        row = {"id": row_id, "title": report["title"], "type_code": report["type_code"], "object": report["object"], "object_fields": _object_fields(report),
               "presence": presence, "confirmation": "confirmed" if confirmed else "pending", "old": left, "new": right,
               "basis": basis, "blocking_reasons": list(dict.fromkeys(blocking)), "content_status": content_status, "change_count": 0}
        rows.append(row)
        if pair and content_status == "reliable":
            canonical_pair = {**pair, "old_sheet": left["sheet_name"], "new_sheet": right["sheet_name"]}
            allowed.append(canonical_pair)
            pair_ids[pair["id"]] = row_id
        for side, r in (("old", left), ("new", right)):
            if r:
                consumed[side].add(r["id"])
                for message in r["issues"]:
                    issues.append({"id": "issue-" + _id(row_id, side, message), "type": "source_body_empty" if r.get("source_body_empty") and "未见表体" in message else "identity" if "身份" in message or "对象" in message else "inventory",
                                   "message": message, "report_ids": [row_id], "side": side, "pages": r["pages"],
                                   "sources": r.get("sources", [r["source"]]), "actions": ["review_identity", "review_structure"]})
        if blocking:
            issues.append({"id": "issue-" + _id(row_id, "pairing"), "type": "pairing", "message": "；".join(dict.fromkeys(blocking)),
                           "report_ids": [row_id], "pages": sorted(set((left or {}).get("pages", []) + (right or {}).get("pages", []))),
                           "sources": [{**r["source"], "side": side} for side, r in (("old", left), ("new", right)) if r] + extra_sources, "actions": ["review_pairing"]})

    for pair in config.get("pairs", []):
        if hasattr(pair, "model_dump"):
            pair = pair.model_dump()
        left, right = lookup["old"].get(pair.get("old_sheet")), lookup["new"].get(pair.get("new_sheet"))
        if (pair.get("old_sheet") and not left) or (pair.get("new_sheet") and not right) or not left and not right:
            issues.append({"id": "issue-" + _id(pair.get("id")), "type": "pairing", "message": "配对引用的报表不在当前清单中", "report_ids": [], "pages": [], "sources": [], "actions": ["review_pairing"]})
            continue
        if any(r and r["id"] in consumed[side] for side, r in (("old", left), ("new", right))):
            issues.append({"id": "issue-" + _id(pair.get("id"), "duplicate"), "type": "pairing", "message": "同一报表被重复配对，后续配对未执行", "report_ids": [], "pages": [], "sources": [], "actions": ["review_pairing"]})
            continue
        add(left, right, pair)
    for left in books["old"]["reports"]:
        if left["id"] in consumed["old"]:
            continue
        candidates = [r for r in books["new"]["reports"] if r["id"] not in consumed["new"] and _type(left) and _type(r) == _type(left)
                      and not _object_conflict(left, r)]
        peers = [r for r in books["old"]["reports"] if _type(r) == _type(left)
                 and (not candidates or any(not _object_conflict(r, candidate) for candidate in candidates))]
        add(left, candidates[0] if len(candidates) == 1 and len(peers) == 1 else None,
            ambiguous=bool(candidates and (len(candidates) > 1 or len(peers) > 1)))
    for right in books["new"]["reports"]:
        if right["id"] not in consumed["new"]:
            add(None, right)
    types = {"shared": [], "old_only": [], "new_only": [], "uncertain": []}
    keys = sorted({_type(r) for b in books.values() for r in b["reports"] if _type(r)})
    for key in keys:
        members = {side: [r for r in book["reports"] if _type(r) == key] for side, book in books.items()}
        report = (members["old"] or members["new"])[0]
        category = "shared" if all(members.values()) else "old_only" if members["old"] else "new_only"
        opposite = "new" if members["old"] else "old"
        if category != "shared" and (_unresolved_regions(books[opposite], report) or
                                     any(not _type(r) for r in books[opposite]["reports"]) or
                                     any(r["type"] == "unclassified_region" for r in books[opposite].get("non_report_regions", []))):
            category = "uncertain"
        if any(any(s.get("method") == "ocr" or _BAD.search(s.get("text", "")) for s in r.get("sources", [])[:1]) for group in members.values() for r in group):
            category = "uncertain"
        types[category].append({"type_code": report["type_code"], "title": report["title"],
                                "old_pages": sorted({p for r in members["old"] for p in r["pages"]}), "new_pages": sorted({p for r in members["new"] for p in r["pages"]})})
    unknown = {side: [r for r in book["reports"] if not _type(r)] for side, book in books.items()}
    if any(unknown.values()):
        types["uncertain"].append({"type_code": None, "title": "未识别报表",
                                   "old_pages": sorted({p for r in unknown["old"] for p in r["pages"]}),
                                   "new_pages": sorted({p for r in unknown["new"] for p in r["pages"]})})
    non_report_regions = [{**region, "side": side} for side, book in books.items() for region in book.get("non_report_regions", [])]
    for region in non_report_regions:
        if region["type"] == "unclassified_region":
            issues.append({"id": "issue-" + _id(region["id"], region["side"]), "type": "document_scope", "message": region["basis"],
                           "report_ids": [], "side": region["side"], "pages": region["pages"], "sources": [region["source"]], "actions": ["review_identity"]})
    return {"report_inventory_version": 1, "reports": rows, "non_report_regions": non_report_regions, "issues": issues, "type_inventory": types, "pairs": allowed, "pair_report_ids": pair_ids}


def _title_reliable(report, book):
    source = report.get("source", {})
    page = next((p for p in book.get("pages", []) if p.get("number", p.get("page")) == source.get("page")), {})
    return bool(_type(report) and not _BAD.search(source.get("text", report.get("title", ""))) and
                (source.get("method", "text") == "text" or page.get("reviewed")))


def _family(report):
    if report.get("type_code"):
        return ("code", report["type_code"])
    if _type(report):
        return ("title", _clean(report["title"]), report.get("chapter", ""))
    return None


def _title_candidate(left, right, exact=False):
    if left.get("type_code") and right.get("type_code"):
        return False
    if not _family(left) or not _family(right):
        return False
    def chapter(value):
        match = re.match(r"^(?:附录)?([A-Z])(?:[^A-Z]|$)", value)
        return match[1] if match else value
    a, b = chapter(left.get("chapter", "")), chapter(right.get("chapter", ""))
    if a and b and a != b:
        return False
    a, b = _clean(left["title"]), _clean(right["title"])
    return a == b if exact else SequenceMatcher(None, a, b).ratio() >= .68


def _titles_v2(lines, sheets):
    titles = _titles(lines)
    for line in lines:
        text = _clean(line["text"])
        bounds = line.get("bbox", [0, 0, 0, 0])
        in_table = any(sheet.get("structure") == "table" and _intersects(bounds, _box(sheet)) for sheet in sheets)
        followed_by_object = any(_OBJECT.search(other["text"]) and bounds[1] < other.get("bbox", [0, 0, 0, 0])[1] < bounds[1] + .15 for other in lines)
        if (4 <= len(text) <= 65 and re.search(r"表$|封面$|扉页$|说明$", text) and not _OBJECT.search(text) and not re.search(r"详见|参见|注[:：]", text)
                and not in_table and (bounds[1] < .3 or followed_by_object) and not any(t.get("bbox") == bounds for t in titles)):
            titles.append({**line, "type_code": None, "title": text})
    return sorted(titles, key=lambda title: title.get("bbox", [0, 0, 0, 0])[1])


def _attach_report_inventory_v2(book):
    result = _attach_report_inventory_v1(book, _titles_v2)
    graphic_pages = set()
    for page in result.get("pages", []):
        number = page.get("number", page.get("page"))
        meaningful = [line for line in _lines(page, []) if not re.fullmatch(r"[\d\s—-]+", line["text"].strip())]
        if (page.get("has_graphics") and page.get("status") != "unavailable" and not meaningful and
                not any(region.get("affects_content", True) and region.get("status") != "ok" for region in page.get("ocr_regions", [])) and
                not any(number in report["pages"] for report in result["reports"])):
            source = {"kind": "pdf", "page": number, "bbox": [0, 0, 1, 1], "method": page.get("method", "text")}
            result["reports"].append({"id": "report-" + _id(number, "graphic-form"), "type_code": None,
                "title": "未识别报表", "object": "", "object_fields": {}, "object_status": "unknown",
                "pages": [number], "member_sheets": [], "source": source, "sources": [source],
                "issues": ["源页含无文字表框或图形，报表身份待人工确认"], "header_signature": [],
                "identity_reliable": False, "sheet_name": None, "title_anchor": None, "text_report": False,
                "source_body_empty": True, "has_notes": False, "regions": [], "page_sequence": None,
                "continuation": False, "graphic_form": True})
            graphic_pages.add(number)
    result["reports"].sort(key=lambda report: (report["pages"][0], (report.get("title_anchor") or [0, 0, 0, 0])[1]))
    result["non_report_regions"] = [region for region in result.get("non_report_regions", []) if not set(region["pages"]) <= graphic_pages]
    chapter = ""
    chapters = {}
    for page in result.get("pages", []):
        number = page.get("number", page.get("page"))
        inherited = chapter
        entries = []
        for line in _lines(page, []):
            text = _clean(line["text"])
            if re.match(r"^(?:附录[A-Z一二三四五六七八九十]+|第[一二三四五六七八九十百\d]+[章节])", text):
                chapter = text
                entries.append((line.get("bbox", [0, 0, 0, 0])[1], chapter))
        chapters[number] = (inherited, entries)
    for report in result["reports"]:
        page = report["pages"][0]
        inherited, anchors = chapters.get(page, ("", []))
        top = (report.get("title_anchor") or [0, 0, 1, 1])[1]
        preceding = [name for y, name in anchors if y <= top]
        report["chapter"] = report["type_code"].split(".")[0] if report.get("type_code") else (preceding[-1] if preceding else inherited)
        report["identity_reliable"] = _title_reliable(report, result)
        report["issues"] = [message for message in report["issues"] if not any(
            text in message for text in ("工程对象", "未提取到可比较的多列表格", "源页仅含", "多个独立实例"))]
        report.pop("duplicate", None)

    merged = []
    for report in result["reports"]:
        candidates = [r for r in merged if r["identity_reliable"] and report["identity_reliable"] and
                      _family(r) == _family(report) and not _object_conflict(r, report) and
                      r["pages"][-1] + 1 == report["pages"][0]]
        previous = candidates[0] if len(candidates) == 1 and report.get("continuation") else None
        if previous and previous.get("page_sequence") and report.get("page_sequence"):
            a, b = previous["page_sequence"], report["page_sequence"]
            if a[1] != b[1] or b[0] != a[0] + 1:
                previous = None
        if previous:
            for field in ("pages", "member_sheets", "sources", "regions"):
                previous[field].extend(item for item in report.get(field, []) if item not in previous[field])
            previous["issues"] = list(dict.fromkeys(previous["issues"] + report["issues"]))
            previous["page_sequence"] = report.get("page_sequence")
        else:
            merged.append(report)

    physical = [sheet for sheet in result["sheets"] if not sheet.get("member_sheets")]
    physical_map = {sheet["name"]: sheet for sheet in physical}
    logical = []
    for report in merged:
        members = [physical_map[name] for name in report["member_sheets"] if name in physical_map]
        if len(members) < 2 or not all(sheet.get("structure") == "table" for sheet in members):
            continue
        if any(_header(sheet) != report["header_signature"] for sheet in members):
            report["incompatible_fragments"] = True
            continue
        alias = _logical_report_sheet(report, physical_map)
        if alias:
            logical.append(alias)
    result["sheets"] = physical + logical
    page_reports = defaultdict(list)
    for report in merged:
        for page in report["pages"]:
            page_reports[page].append(report)
    occurrences = Counter()
    duplicates = Counter((_family(r), tuple(sorted(_object_fields(r).items()))) for r in merged)
    for report in merged:
        family = _family(report)
        occurrences[family] += 1
        report["occurrence"] = occurrences[family]
        report["comparison_regions"] = []
        for page in report["pages"]:
            peers = page_reports[page]
            if len(peers) == 1:
                box = [0, 0, 1, 1]
            else:
                def top(peer):
                    sources = [source for source in peer.get("sources", []) if source.get("page") == page]
                    return min((s.get("bbox", [0, 0, 1, 1])[1] for s in sources), default=0)
                ordered = sorted(peers, key=top)
                index = ordered.index(report)
                start = 0 if index == 0 else top(report)
                end = top(ordered[index + 1]) if index + 1 < len(ordered) else 1
                box = [0, start, 1, end]
            report["comparison_regions"].append({"page": page, "bbox": box})
        if family and duplicates[(family, tuple(sorted(_object_fields(report).items())))] > 1:
            report["duplicate"] = True
        report["report_kind"] = "text" if report.get("text_report") else "empty_form" if report.get("source_body_empty") else "table"
    result.update(reports=merged, report_inventory_version=2)
    return result


def _report_pair(left, right):
    return {"id": "report-pair-" + _id(left["id"], right["id"]),
            "old_report_id": left["id"], "new_report_id": right["id"],
            "old_sheet": left.get("sheet_name"), "new_sheet": right.get("sheet_name"),
            "old_header_start": 1, "old_header_end": 1, "new_header_start": 1, "new_header_end": 1,
            "mode": "position", "columns": [], "pairing_confirmed": False, "confirmation_reason": ""}


def _v2_unresolved(book, report):
    # A changed project name is content in v2, so it cannot exclude an unreadable region.
    target = {**report, "object_status": "unknown", "object_fields": {}}
    return _unresolved_regions(book, target)


def _report_content_blockers(book, report):
    reasons = []
    for region in report.get("comparison_regions", []):
        page = next((p for p in book.get("pages", []) if p.get("number", p.get("page")) == region["page"]), {})
        if page.get("status") == "unavailable":
            reasons.append("关联页面提取失败，完整报表内容待核对")
        for failure in page.get("ocr_regions", []):
            if failure.get("affects_content", True) and failure.get("status") != "ok" and _intersects(region["bbox"], failure.get("bbox", [0, 0, 1, 1])):
                reasons.append("关联内容区域未能可靠恢复")
        if page.get("method") == "ocr" and not page.get("reviewed"):
            reasons.append("报表 OCR 内容尚未核验")
        sheets = [sheet for sheet in book.get("sheets", []) if sheet.get("page_number") == region["page"] and not sheet.get("member_sheets")]
        for line in _lines(page, sheets):
            if _intersects(region["bbox"], line.get("bbox", [0, 0, 1, 1])) and _BAD.search(line.get("text", "")):
                reasons.append("报表内容含未恢复乱码")
            if _intersects(region["bbox"], line.get("bbox", [0, 0, 1, 1])) and line.get("method") == "ocr" and not page.get("reviewed"):
                reasons.append("报表 OCR 内容尚未核验")
    return list(dict.fromkeys(reasons))


def _page_region_issues(books, rows):
    issues = []
    for side, book in books.items():
        for page in book.get("pages", []):
            number = page.get("number", page.get("page"))
            for index, region in enumerate(page.get("ocr_regions", []), 1):
                unresolved = region.get("affects_content") and region.get("status") != "ok"
                unchecked = region.get("status") == "ok" and region.get("text_count", 0) > 0 and not page.get("reviewed")
                if not unresolved and not unchecked:
                    continue
                region_id = region.get("id", f"p{number}-ocr-{index}")
                bounds = region.get("bbox", [0, 0, 1, 1])
                report_ids = [row["id"] for row in rows if row.get(side) and any(
                    scope["page"] == number and _intersects(scope["bbox"], bounds)
                    for scope in row[side].get("comparison_regions", []))]
                issues.append({"id": f"issue-{side}-{region_id}",
                    "type": "extraction_unavailable" if unresolved and page.get("status") == "unavailable" else "region_unresolved" if unresolved else "ocr_review",
                    "message": "此区域文字尚未可靠恢复；原生文字和原图已保留，请校正或重新提取。" if unresolved else "此区域包含尚未人工核验的 OCR 文字。",
                    "report_ids": report_ids, "side": side, "pages": [number],
                    "sources": [{"kind": "pdf", "side": side, "page": number, "bbox": bounds,
                                 "method": "ocr", "region_id": region_id}],
                    "actions": ["reprocess", "review_structure"] if unresolved else ["review_ocr"]})
    return issues


def _compare_inventory_v2(old, new, config):
    books = {side: book if book.get("report_inventory_version") == 2 and "reports" in book else _attach_report_inventory_v2(book)
             for side, book in (("old", old), ("new", new))}
    consumed = {"old": set(), "new": set()}
    rows, issues, pairs, pair_ids = [], [], [], {}
    groups = {side: defaultdict(list) for side in books}
    lookups = {}
    cross_candidates = defaultdict(list)
    manual_unpaired = {"old": [], "new": []}
    for side, book in books.items():
        lookups[side] = {r["id"]: r for r in book["reports"]}
        for report in book["reports"]:
            groups[side][_family(report)].append(report)

    def add_issue(message, left=None, right=None, row_id=None, kind="pairing", extra_sources=None):
        sources = [{**r["source"], "side": side} for side, r in (("old", left), ("new", right)) if r]
        sources.extend(extra_sources or [])
        issues.append({"id": "issue-" + _id(row_id, message, len(issues)), "type": kind, "message": message,
                       "report_ids": [row_id] if row_id else [], "pages": sorted({s["page"] for s in sources}),
                       "sources": sources, "actions": ["review_pairing"] if kind == "pairing" else ["reprocess"]})

    def add(left, right, method="", pair=None, ambiguous=False, force_pending=False):
        report = left or right
        row_id = "comparison-" + _id(left and left["id"], right and right["id"])
        blocking, field_blocking = [], []
        confirmed = False
        if left and right:
            conflict = bool(left.get("type_code") and right.get("type_code") and left["type_code"] != right["type_code"])
            manual = bool(pair and pair.get("pairing_confirmed") and pair.get("confirmation_reason", "").strip())
            confirmed = not conflict and (manual or bool(method and left["identity_reliable"] and right["identity_reliable"]))
            if conflict:
                blocking.append("明确的报表编号不同，不允许确认配对")
            elif not confirmed:
                blocking.append("报表标题或多实例对应关系尚未确认")
            basis = pair["confirmation_reason"] if manual and confirmed else {
                "code": "唯一相同报表编号；工程名称、日期、表头和版式作为内容差异比较",
                "title": "同章节下唯一相同标题；工程名称和版式作为内容差异比较",
                "object": "同类型多个实例按完整工程对象一一对应",
                "sequence": "同类型剩余实例数量一致，按章节内出现顺序对应",
                "remaining": "同类型其他实例已匹配，剩余候选一一对应",
            }.get(method, "候选关系待人工确认")
            for side, item in (("old", left), ("new", right)):
                blocking.extend(_report_content_blockers(books[side], item))
                field_blocking.extend(_content_blockers(books[side], item))
            if left.get("header_signature") != right.get("header_signature"):
                field_blocking.append("表头不同，字段比较须另行映射；完整报表比较仍执行")
            presence = "shared" if confirmed else "uncertain"
            content_status = "pending"
            if confirmed:
                selected = {**_report_pair(left, right), **(pair or {})}
                pairs.append(selected)
                pair_ids[selected["id"]] = row_id
        else:
            side, opposite = ("old", "new") if left else ("new", "old")
            unresolved = [{**source, "side": opposite} for source in _v2_unresolved(books[opposite], report)]
            peers = groups[opposite].get(_family(report), [])
            remaining = [r for r in peers if r["id"] not in consumed[opposite]]
            unknown = any(not _family(r) for r in books[opposite]["reports"])
            unknown |= any(region["type"] == "unclassified_region" for region in books[opposite].get("non_report_regions", []))
            candidates = cross_candidates[(side, report["id"])]
            ambiguous = ambiguous or bool(remaining) or bool(peers and report.get("duplicate")) or bool(candidates)
            held_opposite = any(_family(item) == _family(report) or _title_candidate(item, report) for item in manual_unpaired[opposite])
            force_pending = force_pending or held_opposite
            confirmed = bool(report["identity_reliable"] and not unresolved and not unknown and not ambiguous and not force_pending)
            presence = f"{side}_only" if confirmed else "uncertain"
            content_status = "not_applicable" if confirmed else "pending"
            basis = "对侧完整清单无此报表或无此额外实例" if confirmed else "报表候选尚未匹配，不能确认新增或删除"
            if force_pending:
                if method == "manual_unpaired":
                    basis = (pair or {}).get("confirmation_reason", "").strip() or "已手动暂不配对，保留候选待确认"
                else:
                    basis = "对侧同类报表被手动保留待确认，不能认定缺失"
                blocking.append(basis)
            if ambiguous:
                blocking.append("编号缺失或标题近似，存在待确认的对应报表" if candidates else "同类型存在多个或数量不等的候选，须确认对应实例")
            if unresolved or unknown:
                blocking.append("对侧存在未恢复或未分类内容，不能确认报表缺失")
            if not report["identity_reliable"]:
                blocking.append("报表标题或编号依据尚未可靠确认")
            if unresolved:
                add_issue(blocking[-1], left, right, row_id, "extraction", unresolved)
        row = {"id": row_id, "title": report["title"], "type_code": report.get("type_code") or (right or {}).get("type_code"),
               "object": report.get("object", ""), "object_fields": _object_fields(report),
               "presence": presence, "confirmation": "confirmed" if confirmed else "pending", "old": left, "new": right,
               "basis": basis, "match_method": "manual" if pair and pair.get("pairing_confirmed") and confirmed else method,
               "blocking_reasons": list(dict.fromkeys(blocking)), "field_blocking_reasons": list(dict.fromkeys(field_blocking)),
               "content_status": content_status, "change_count": 0, "matching_version": 2,
               "candidate_report_ids": [] if left and right else [r["id"] for r in cross_candidates[("old" if left else "new", report["id"])]]}
        rows.append(row)
        if blocking:
            add_issue("；".join(dict.fromkeys(blocking)), left, right, row_id)
        for side, item in (("old", left), ("new", right)):
            if item:
                consumed[side].add(item["id"])

    def resolve(side, pair):
        report_id = pair.get(f"{side}_report_id")
        if report_id:
            return lookups[side].get(report_id)
        name = pair.get(f"{side}_sheet")
        candidates = [r for r in books[side]["reports"] if name and (name == r.get("sheet_name") or name in r.get("member_sheets", []))]
        return candidates[0] if len(candidates) == 1 else None

    for configured in config.get("pairs", []):
        pair = configured.model_dump() if hasattr(configured, "model_dump") else configured
        left, right = resolve("old", pair), resolve("new", pair)
        requested = {side: bool(pair.get(f"{side}_report_id") or pair.get(f"{side}_sheet")) for side in books}
        if (requested["old"] and not left) or (requested["new"] and not right) or not left and not right:
            add_issue("配对引用的完整报表不在当前清单中，请重新选择")
            continue
        if (left and left["id"] in consumed["old"]) or (right and right["id"] in consumed["new"]):
            add_issue("同一报表被重复配对，后续配对未执行", left, right)
            continue
        if not left or not right:
            manual_unpaired["old" if left else "new"].append(left or right)
            add(left, right, "manual_unpaired", pair, force_pending=True)
            continue
        unique = _family(left) and _family(left) == _family(right) and len(groups["old"][_family(left)]) == len(groups["new"][_family(right)]) == 1
        method = ("code" if left.get("type_code") else "title") if unique else ""
        if not method and not (pair.get("pairing_confirmed") and pair.get("confirmation_reason", "").strip()):
            if pair.get("id") != _report_pair(left, right)["id"]:
                add_issue("多实例或未识别标题的手工配对需要明确确认并填写依据", left, right)
            continue
        add(left, right, method, pair)

    for family in dict.fromkeys([*groups["old"], *groups["new"]]):
        if family is None:
            continue
        lefts = [r for r in groups["old"][family] if r["id"] not in consumed["old"]]
        rights = [r for r in groups["new"][family] if r["id"] not in consumed["new"]]
        if len(lefts) == len(rights) == 1 and len(groups["old"][family]) == len(groups["new"][family]) == 1:
            add(lefts[0], rights[0], "code" if family[0] == "code" else "title")
            continue
        for left in list(lefts):
            fields = _object_fields(left)
            candidates = [r for r in rights if fields and _object_fields(r) == fields]
            peers = [r for r in lefts if _object_fields(r) == fields]
            if len(candidates) == len(peers) == 1 and left["identity_reliable"] and candidates[0]["identity_reliable"]:
                right = candidates[0]
                add(left, right, "object")
                lefts.remove(left)
                rights.remove(right)
        if lefts and len(lefts) == len(rights) and all(r["identity_reliable"] for r in lefts + rights):
            for left, right in zip(lefts, rights):
                add(left, right, "remaining" if len(lefts) == 1 else "sequence")
    for left in books["old"]["reports"]:
        if left["id"] in consumed["old"] or not left["identity_reliable"]:
            continue
        rights = [r for r in books["new"]["reports"] if r["id"] not in consumed["new"] and r["identity_reliable"] and _title_candidate(left, r, exact=True)]
        if len(rights) == 1:
            peers = [r for r in books["old"]["reports"] if r["id"] not in consumed["old"] and _title_candidate(r, rights[0], exact=True)]
            if len(peers) == 1:
                add(left, rights[0], "title")
    for side, opposite in (("old", "new"), ("new", "old")):
        for report in books[side]["reports"]:
            if report["id"] not in consumed[side]:
                cross_candidates[(side, report["id"])] = [r for r in books[opposite]["reports"] if r["id"] not in consumed[opposite] and _title_candidate(report, r)]
    ambiguous_ids = set()
    for family in dict.fromkeys([*groups["old"], *groups["new"]]):
        remaining = {side: [r for r in groups[side].get(family, []) if r["id"] not in consumed[side]] for side in books}
        if all(remaining.values()):
            ambiguous_ids.update((side, r["id"]) for side, members in remaining.items() for r in members)
    for side, book in books.items():
        for report in book["reports"]:
            if report["id"] not in consumed[side]:
                add(report if side == "old" else None, report if side == "new" else None, ambiguous=(side, report["id"]) in ambiguous_ids)

    type_groups = {side: defaultdict(list) for side in books}
    for side, book in books.items():
        for report in book["reports"]:
            matched = next((row for row in rows if row["confirmation"] == "confirmed" and row["old"] and row["new"] and row[side]["id"] == report["id"]), None)
            canonical = (matched["old"] if matched["old"].get("type_code") else matched["new"]) if matched else report
            type_groups[side][_family(canonical)].append(report)
    types = {"shared": [], "old_only": [], "new_only": [], "uncertain": []}
    for family in dict.fromkeys([*type_groups["old"], *type_groups["new"]]):
        members = {side: type_groups[side].get(family, []) for side in books}
        report = (members["old"] or members["new"])[0]
        category = "shared" if all(members.values()) else "old_only" if members["old"] else "new_only"
        if family is None or any(not r["identity_reliable"] or cross_candidates[(side, r["id"])] for side, values in members.items() for r in values):
            category = "uncertain"
        elif category != "shared":
            opposite = "new" if members["old"] else "old"
            if (any(_family(item) == family for values in manual_unpaired.values() for item in values) or
                    _v2_unresolved(books[opposite], report) or any(not _family(r) for r in books[opposite]["reports"]) or
                    any(r["type"] == "unclassified_region" for r in books[opposite].get("non_report_regions", []))):
                category = "uncertain"
        types[category].append({"type_code": next((r.get("type_code") for values in members.values() for r in values if r.get("type_code")), None), "title": report["title"],
                                "old_pages": sorted({p for r in members["old"] for p in r["pages"]}),
                                "new_pages": sorted({p for r in members["new"] for p in r["pages"]})})
    non_report = [{**region, "side": side} for side, book in books.items() for region in book.get("non_report_regions", [])]
    for region in non_report:
        if region["type"] == "unclassified_region":
            add_issue(region["basis"], kind="document_scope", extra_sources=[{**region["source"], "side": region["side"]}])
    issues.extend(_page_region_issues(books, rows))
    return {"report_inventory_version": 2, "reports": rows, "issues": issues, "type_inventory": types,
            "non_report_regions": non_report, "pairs": pairs, "pair_report_ids": pair_ids}


def attach_report_inventory(book: dict) -> dict:
    return (_attach_report_inventory_v2 if book.get("report_inventory_version") == 2 else _attach_report_inventory_v1)(book)


def _use_v2(old, new):
    versions = {book.get("report_inventory_version", 1) for book in (old, new)}
    if versions == {1, 2}:
        raise ValueError("两侧报表清单版本不一致，请重新提取为同一版本后比较")
    return versions == {2}


def suggest_report_pairs(old: dict, new: dict) -> list[dict]:
    return _compare_inventory_v2(old, new, {"pairs": []})["pairs"] if _use_v2(old, new) else _suggest_report_pairs_v1(old, new)


def compare_inventory(old: dict, new: dict, config: dict) -> dict:
    return (_compare_inventory_v2 if _use_v2(old, new) else _compare_inventory_v1)(old, new, config)
