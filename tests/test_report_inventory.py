import copy

import pytest

from backend.excel import empty_cell
from backend.reports import attach_report_inventory, compare_inventory, suggest_report_pairs


def line(text, y=.1, method="text"):
    return {"text": text, "bbox": [.05, y, .95, y + .025], "method": method}


def sheet(name, page=1, top=.3, headers=("项目编码", "金额"), content=True):
    cells = {}
    for r, values in enumerate([headers, ("X", "10") if content else ("", "")], 1):
        for c, value in enumerate(values, 1):
            cell = empty_cell(r, c)
            cell.update(display=value, value=value or None, value_type="string" if value else "blank",
                        pdf_source={"kind": "pdf", "page": page, "bbox": [.1, top + r * .03, .9, top + (r + 1) * .03], "method": "text"})
            cells[cell["address"]] = cell
    return {"name": name, "page_number": page, "bbox": [.1, top, .9, top + .2], "structure": "table",
            "max_column": len(headers), "max_row": 2, "suggested_header_row": 1, "cells": cells, "method": "text", "merged_ranges": []}


def book(code="A.5.2", obj="一号楼", pages=1, continuation=False, content=True):
    return {"format": "pdf", "sheets": [sheet(f"p{p}", p, content=content) for p in range(1, pages + 1)],
            "pages": [{"number": p, "method": "text", "status": "ok", "evidence_lines":
                       [line(f"表 {code} 材料暂估单价表"), line(f"工程名称：{obj}", .2),
                        line(f"第{p if continuation else 1}页 共{pages if continuation else 1}页", .9)]} for p in range(1, pages + 1)]}


def test_same_header_different_type_never_merges_or_compares():
    old = book(pages=2)
    old["pages"][1]["evidence_lines"][0] = line("表 B.8.3 材料暂估单价表")
    result = attach_report_inventory(old)
    assert len(result["reports"]) == 2
    assert not any(s.get("member_sheets") for s in result["sheets"])
    assert not suggest_report_pairs(book("A.5.2"), book("B.8.3"))
    pair = {"id": "x", "old_sheet": "p1", "new_sheet": "p1", "mode": "key", "columns": [{"id": "key", "name": "项目编码", "is_key": True, "old_column": "A", "new_column": "A"}], "pairing_confirmed": True, "confirmation_reason": "人工选择"}
    output = compare_inventory(book("A.5.2"), book("B.8.3"), {"pairs": [pair]})
    assert not output["pairs"]
    assert "冲突" in output["reports"][0]["blocking_reasons"][0]


def test_known_same_identity_suggests_content_pair_and_maps_report_id():
    old, new = book(), book()
    pairs = suggest_report_pairs(old, new)
    assert len(pairs) == 1
    output = compare_inventory(old, new, {"pairs": pairs})
    assert output["reports"][0]["content_status"] == "reliable"
    assert output["pairs"] == pairs
    assert output["pair_report_ids"][pairs[0]["id"]] == output["reports"][0]["id"]


def test_blank_object_template_shares_type_but_requires_explicit_pairing():
    old = book(obj="")
    reports = attach_report_inventory(old)["reports"]
    assert reports[0]["object_status"] == "template"
    assert not reports[0]["identity_reliable"]
    assert not suggest_report_pairs(old, old)
    result = compare_inventory(old, old, {"pairs": []})
    assert len(result["type_inventory"]["shared"]) == 1
    assert result["reports"][0]["confirmation"] == "pending"
    pair = {"id": "x", "old_sheet": "p1", "new_sheet": "p1", "mode": "key", "columns": [{"id": "key", "name": "项目编码", "is_key": True, "old_column": "A", "new_column": "A"}], "pairing_confirmed": True, "confirmation_reason": "核对原图确认模板对象一致"}
    assert compare_inventory(old, old, {"pairs": [pair]})["pairs"] == [pair]
    pair["confirmation_reason"] = "   "
    assert not compare_inventory(old, old, {"pairs": [pair]})["pairs"]


def test_explicit_object_conflict_cannot_be_overridden():
    pair = {"id": "x", "old_sheet": "p1", "new_sheet": "p1", "mode": "key", "columns": [{"id": "key", "name": "项目编码", "is_key": True, "old_column": "A", "new_column": "A"}], "pairing_confirmed": True, "confirmation_reason": "确认"}
    assert not compare_inventory(book(obj="一号楼"), book(obj="二号楼"), {"pairs": [pair]})["pairs"]


def test_merge_requires_consecutive_continuation_and_identity():
    result = attach_report_inventory(book(pages=2, continuation=True))
    assert len(result["reports"]) == 1
    assert result["reports"][0]["pages"] == [1, 2]
    logical = next(s for s in result["sheets"] if s.get("member_sheets"))
    assert logical["max_row"] == 3
    assert logical["cells"]["A3"]["pdf_source"]["page"] == 2
    duplicate = attach_report_inventory(book(pages=2))
    assert len(duplicate["reports"]) == 2
    assert all(r["duplicate"] for r in duplicate["reports"])
    assert not suggest_report_pairs(duplicate, book())


def test_same_page_rate_tables_use_local_subtitle_objects():
    value = {"sheets": [sheet("building", top=.25), sheet("municipal", top=.65)],
             "pages": [{"number": 1, "evidence_lines": [line("2.2.2参考费率表", .1), line("建筑工程", .2),
                                                         line("市政公用(城市轨道交通)工程", .6)]}]}
    reports = attach_report_inventory(value)["reports"]
    assert len(reports) == 2
    assert reports[0]["title"] == reports[1]["title"] == "参考费率表"
    assert reports[0]["object"] != reports[1]["object"]
    assert all(r["identity_reliable"] for r in reports)


def test_unknown_identity_and_empty_tables_never_content_compare():
    unknown = book()
    unknown["pages"][0]["evidence_lines"] = []
    assert not suggest_report_pairs(unknown, unknown)
    empty = book(content=False)
    pair = {"id": "x", "old_sheet": "p1", "new_sheet": "p1", "mode": "key", "columns": [{"id": "key", "name": "项目编码", "is_key": True, "old_column": "A", "new_column": "A"}], "pairing_confirmed": True, "confirmation_reason": "确认"}
    assert not compare_inventory(empty, empty, {"pairs": [pair]})["pairs"]


def test_ocr_title_is_pending_but_unrelated_watermark_does_not_block_native_table():
    value = book()
    value["pages"][0]["evidence_lines"][0]["method"] = "ocr"
    assert not attach_report_inventory(value)["reports"][0]["identity_reliable"]
    assert not suggest_report_pairs(value, value)
    native = book()
    native["pages"][0]["ocr_regions"] = [{"bbox": [0, 0, 1, 1], "reason": "image", "status": "empty", "affects_content": False}]
    assert len(suggest_report_pairs(native, native)) == 1
    native["pages"][0]["ocr_regions"][0].update(reason="invalid_text", affects_content=True)
    assert not suggest_report_pairs(native, native)


def test_inventory_rebuild_discards_legacy_merge_and_keeps_original_immutable():
    value = book(pages=2)
    legacy = copy.deepcopy(value["sheets"][0])
    legacy.update(name="wrong merge", member_sheets=["p1", "p2"])
    value["sheets"].append(legacy)
    snapshot = copy.deepcopy(value)
    result = attach_report_inventory(value)
    assert value == snapshot
    assert len(result["sheets"]) == 2
    assert attach_report_inventory(result) == result


def test_reviewed_title_edit_replaces_stale_page_evidence():
    value = book()
    text = empty_cell(2, 1)
    text.update(display="表 B.8.3 材料暂估单价表", current_text="表 B.8.3 材料暂估单价表", raw_text="表 A.5.2 材料暂估单价表", reviewed=True,
                pdf_source={"kind": "pdf", "page": 1, "bbox": [.05, .1, .95, .125], "method": "text"})
    value["sheets"].append({"name": "text", "page_number": 1, "structure": "text_blocks", "cells": {"A2": text}})
    assert attach_report_inventory(value)["reports"][0]["type_code"] == "B.8.3"


def test_duplicate_header_only_cover_pages_remain_independent_instances():
    value = book(code="C.6.2", obj="", pages=2)
    value["sheets"] = []
    result = attach_report_inventory(value)
    assert len(result["reports"]) == 2
    assert all(r["duplicate"] and not r["sheet_name"] for r in result["reports"])
    compared = compare_inventory(result, result, {"pairs": []})
    assert compared["type_inventory"]["shared"][0]["old_pages"] == [1, 2]
    assert not compared["pairs"]


def test_missing_type_is_pending_when_opposite_extraction_unavailable():
    opposite = {"sheets": [], "pages": [{"number": 1, "status": "unavailable", "method": "ocr"}]}
    result = compare_inventory(book(), opposite, {"pairs": []})
    assert result["reports"][0]["presence"] == "uncertain"
    assert not result["pairs"]
    assert not result["type_inventory"]["old_only"]
    assert any(i["type_code"] == "A.5.2" for i in result["type_inventory"]["uncertain"])


def test_known_missing_report_has_inventory_event_only():
    result = compare_inventory(book(), book("B.8.3"), {"pairs": []})
    assert {r["presence"] for r in result["reports"]} == {"old_only", "new_only"}
    assert all(r["content_status"] == "not_applicable" for r in result["reports"])
    assert result["pairs"] == []


def test_two_named_reports_on_single_page_keep_distinct_titles():
    value = {"sheets": [sheet("a", top=.25), sheet("b", top=.7)],
             "pages": [{"number": 1, "evidence_lines": [line("表 A.5.2 材料暂估单价表", .1), line("工程名称：项目甲", .2),
                                                         line("表 B.8.3 材料暂估单价表", .55), line("工程名称：项目甲", .65)]}]}
    reports = attach_report_inventory(value)["reports"]
    assert [r["type_code"] for r in reports] == ["A.5.2", "B.8.3"]
    assert all(r["identity_reliable"] for r in reports)


def test_plain_title_and_hierarchical_object_fields_are_supported():
    value = book()
    value["pages"][0]["evidence_lines"] = [line("项目费用明细报表"), line("工程名称：园区 单项工程名称：一期 单位工程名称：一号楼", .2)]
    report = attach_report_inventory(value)["reports"][0]
    assert report["type_code"] is None and report["title"] == "项目费用明细报表"
    assert report["object"] == "工程名称：园区 / 单项工程名称：一期 / 单位工程名称：一号楼"
    assert len(suggest_report_pairs(value, value)) == 1


def test_header_conflict_cannot_be_overridden_by_identity_confirmation():
    left, right = book(), book()
    right["sheets"][0]["cells"]["B1"]["display"] = "费率"
    pair = {"id": "x", "old_sheet": "p1", "new_sheet": "p1", "mode": "key", "columns": [{"id": "key", "name": "项目编码", "is_key": True, "old_column": "A", "new_column": "A"}], "pairing_confirmed": True, "confirmation_reason": "确认身份"}
    result = compare_inventory(left, right, {"pairs": [pair]})
    assert not result["pairs"]
    assert result["reports"][0]["content_status"] == "pending"
    assert "表头不兼容" in " ".join(result["reports"][0]["blocking_reasons"])


def test_confirmation_reason_is_snapshotted_and_ocr_contents_need_review():
    value = book(obj="")
    pair = {"id": "x", "old_sheet": "p1", "new_sheet": "p1", "mode": "key", "columns": [{"id": "key", "name": "项目编码", "is_key": True, "old_column": "A", "new_column": "A"}], "pairing_confirmed": True, "confirmation_reason": "已对照工程原件确认"}
    result = compare_inventory(value, value, {"pairs": [pair]})
    pair["confirmation_reason"] = "after"
    assert result["reports"][0]["basis"] == "已对照工程原件确认"
    assert result["pairs"][0]["confirmation_reason"] == "已对照工程原件确认"
    value["sheets"][0]["cells"]["B2"]["pdf_source"]["method"] = "ocr"
    assert not compare_inventory(value, value, {"pairs": [pair]})["pairs"]


def test_same_title_multiple_regions_are_one_report_even_when_headers_differ():
    value = book()
    second = sheet("fragment", top=.6)
    second["cells"]["A2"]["display"] = "Y"
    value["sheets"].append(second)
    result = attach_report_inventory(value)
    assert len(result["reports"]) == 1
    assert result["reports"][0]["member_sheets"] == ["p1", "fragment"]
    assert len(suggest_report_pairs(result, result)) == 1
    value["sheets"][1]["cells"]["B1"]["display"] = "数量"
    result = attach_report_inventory(value)
    assert len(result["reports"]) == 1
    assert result["reports"][0]["incompatible_fragments"]
    assert not result["reports"][0].get("duplicate")
    assert not suggest_report_pairs(result, result)


def test_document_sections_blank_pages_and_unclassified_text_are_not_reports():
    value = {"sheets": [sheet("embedded")], "pages": [
        {"number": 1, "status": "ok", "evidence_lines": [line("1 总则"), line("1.0.1 为规范本省建设工程工程量清单计价活动，进一步健全工程造价市场形成机制，制定本规程。", .2)]},
        {"number": 2, "status": "ok", "evidence_lines": [line("37", .95)]},
        {"number": 3, "status": "ok", "evidence_lines": [line("尚未确定用途的短文")]}]}
    result = attach_report_inventory(value)
    assert not result["reports"]
    assert [r["type"] for r in result["non_report_regions"]] == ["document_section", "blank_page", "unclassified_region"]
    compared = compare_inventory(result, result, {"pairs": []})
    assert not compared["reports"]
    assert all(i["type"] == "document_scope" for i in compared["issues"])


def test_continuation_note_attaches_source_page_without_new_report():
    value = book()
    value["pages"][0]["evidence_lines"].append(line("注：1 相同大型机械进出场价格不同时，应分别列项", .8))
    value["pages"].append({"number": 2, "status": "ok", "evidence_lines": [line("3 有厂家特别说明要求的，可在备注栏列明。"), line("64", .95)]})
    result = attach_report_inventory(value)
    assert len(result["reports"]) == 1
    assert result["reports"][0]["pages"] == [1, 2]
    assert result["reports"][0]["continuation_notes"][0]["page"] == 2
    assert any(s["page"] == 2 for s in result["reports"][0]["sources"])


def test_missing_key_position_mode_and_duplicate_keys_never_reliable():
    value = book()
    pair = suggest_report_pairs(value, value)[0]
    pair["mode"] = "position"
    assert not compare_inventory(value, value, {"pairs": [pair]})["pairs"]
    pair["mode"] = "key"
    pair["columns"][0]["is_key"] = False
    assert not compare_inventory(value, value, {"pairs": [pair]})["pairs"]
    value["sheets"][0]["cells"]["A3"] = {**value["sheets"][0]["cells"]["A2"], "row": 3, "address": "A3"}
    value["sheets"][0]["max_row"] = 3
    assert not suggest_report_pairs(value, value)
    value["sheets"][0]["cells"]["A1"]["display"] = "说明"
    assert not suggest_report_pairs(value, value)


def test_template_to_known_object_confirmation_is_blocked():
    pair = suggest_report_pairs(book(), book())[0]
    pair.update(pairing_confirmed=True, confirmation_reason="人工确认")
    result = compare_inventory(book(obj=""), book(), {"pairs": [pair]})
    assert not result["pairs"]
    assert result["reports"][0]["confirmation"] == "pending"
    assert "冲突" in " ".join(result["reports"][0]["blocking_reasons"])


def test_unconfigured_shared_report_and_invalid_reference_have_issues():
    value = book()
    result = compare_inventory(value, value, {"pairs": []})
    assert any("未纳入内容比较范围" in i["message"] for i in result["issues"])
    pair = suggest_report_pairs(value, value)[0]
    pair["new_sheet"] = "nonexistent"
    result = compare_inventory(value, value, {"pairs": [pair]})
    assert not result["pairs"]
    assert not any(r["presence"] in ("old_only", "new_only") for r in result["reports"])
    assert any("不在当前清单" in i["message"] for i in result["issues"])


def test_partial_failed_region_blocks_absence_unless_known_other_report():
    left, right = book(), book("B.8.3")
    right["pages"][0]["ocr_regions"] = [{"bbox": [0, .65, 1, .9], "status": "failed", "affects_content": True, "reason": "scan"}]
    result = compare_inventory(left, right, {"pairs": []})
    missing = next(r for r in result["reports"] if r["type_code"] == "A.5.2")
    assert missing["presence"] == "uncertain"
    assert any(s.get("side") == "new" and s["bbox"] == [0, .65, 1, .9] for i in result["issues"] for s in i["sources"])
    right["pages"][0]["ocr_regions"][0]["bbox"] = [.1, .3, .9, .5]
    result = compare_inventory(left, right, {"pairs": []})
    assert next(r for r in result["reports"] if r["type_code"] == "A.5.2")["presence"] == "old_only"


def test_source_empty_body_distinct_from_extraction_failure_and_text_report():
    value = book("C.6.2")
    value["sheets"] = []
    result = attach_report_inventory(value)
    assert result["reports"][0]["source_body_empty"]
    assert any("源页仅含" in i for i in result["reports"][0]["issues"])
    value["pages"][0]["ocr_regions"] = [{"bbox": [0, .3, 1, 1], "status": "failed", "reason": "image", "affects_content": True}]
    assert not attach_report_inventory(value)["reports"][0]["source_body_empty"]
    value["pages"][0]["evidence_lines"][0] = line("A.1 招标工程量清单封面")
    value["pages"][0]["ocr_regions"] = []
    report = attach_report_inventory(value)["reports"][0]
    assert report["text_report"] and not report["source_body_empty"]
    assert not any("多列表格" in issue for issue in report["issues"])


def test_native_structure_edit_and_ocr_need_cell_and_page_review():
    value = book()
    value["sheets"][0]["cells"]["B2"].update(requires_review=True, reviewed=False)
    assert not suggest_report_pairs(value, value)
    value["sheets"][0]["cells"]["B2"]["reviewed"] = True
    assert not suggest_report_pairs(value, value)
    value["pages"][0]["reviewed"] = True
    assert len(suggest_report_pairs(value, value)) == 1


def test_table_body_code_reference_does_not_create_another_report():
    value = book("B.10.5")
    value["sheets"] = []
    value["pages"][0]["evidence_lines"].extend([line("详见发包人提供材料一览表", .6), line("B.10.7", .7), line("2 专业分包工程", .75)])
    reports = attach_report_inventory(value)["reports"]
    assert len(reports) == 1 and reports[0]["type_code"] == "B.10.5"


def test_failed_region_extending_beyond_known_table_is_not_assigned_to_it():
    left, right = book(), book("B.8.3")
    right["pages"][0]["ocr_regions"] = [{"bbox": [0, 0, 1, 1], "status": "failed", "affects_content": True}]
    result = compare_inventory(left, right, {"pairs": []})
    assert next(r for r in result["reports"] if r["type_code"] == "A.5.2")["presence"] == "uncertain"


def test_verified_garbled_text_stays_blocked_but_corrected_current_text_can_pass():
    value = book()
    cell = value["sheets"][0]["cells"]["B2"]
    cell.update(raw_text="(cid:123)", display="(cid:123)", current_text="(cid:123)", requires_review=True, reviewed=True)
    cell["pdf_source"]["unresolved_text"] = True
    value["pages"][0]["reviewed"] = True
    assert not suggest_report_pairs(value, value)
    cell.update(current_text="10", display="10", value="10")
    assert len(suggest_report_pairs(value, value)) == 1


def test_same_page_text_reports_have_separate_object_evidence():
    value = {"sheets": [], "pages": [{"number": 1, "status": "ok", "evidence_lines": [
        line("A.1 招标工程量清单封面", .1), line("工程名称：项目甲", .2),
        line("B.1 最高投标限价封面", .55), line("工程名称：项目乙", .65)]}]}
    reports = attach_report_inventory(value)["reports"]
    assert [(r["type_code"], r["object"]) for r in reports] == [("A.1", "工程名称：项目甲"), ("B.1", "工程名称：项目乙")]
    assert all(r["identity_reliable"] for r in reports)
    value["sheets"] = [sheet("table", top=.7)]
    value["pages"][0]["evidence_lines"][2] = line("B.5 最高投标限价公布表", .55)
    reports = attach_report_inventory(value)["reports"]
    assert [(r["type_code"], r["object"]) for r in reports] == [("A.1", "工程名称：项目甲"), ("B.5", "工程名称：项目乙")]
    assert reports[0]["member_sheets"] == [] and reports[1]["member_sheets"] == ["table"]


def test_same_page_reports_use_own_pagination_not_first_page_marker():
    value = {"sheets": [], "pages": []}
    for page in (1, 2):
        value["sheets"].extend([sheet(f"a{page}", page, top=.25), sheet(f"b{page}", page, top=.7)])
        value["pages"].append({"number": page, "status": "ok", "evidence_lines": [
            line("A.1 费用汇总表", .05), line("工程名称：项目甲", .1), line(f"第{page}页 共2页", .2),
            line("B.1 费用汇总表", .5), line("工程名称：项目乙", .55), line("第1页 共1页", .65)]})
    reports = attach_report_inventory(value)["reports"]
    assert len(reports) == 3
    assert next(r for r in reports if r["type_code"] == "A.1")["pages"] == [1, 2]
    repeated = [r for r in reports if r["type_code"] == "B.1"]
    assert len(repeated) == 2 and all(r["page_sequence"] == [1, 1] and r["duplicate"] for r in repeated)


def test_partial_object_hierarchy_is_pending_and_manual_reason_can_resolve():
    old, new = book(), book()
    old["pages"][0]["evidence_lines"][1] = line("工程名称：园区 单位工程名称：一号楼", .2)
    new["pages"][0]["evidence_lines"][1] = line("单位工程名称：一号楼", .2)
    old_report = attach_report_inventory(old)["reports"][0]
    assert old_report["object_fields"] == {"工程名称": "园区", "单位工程名称": "一号楼"}
    assert not suggest_report_pairs(old, new)
    result = compare_inventory(old, new, {"pairs": []})
    assert len(result["reports"]) == 1
    assert result["reports"][0]["presence"] == "uncertain"
    assert result["reports"][0]["confirmation"] == "pending"
    assert not any("冲突" in message for message in result["reports"][0]["blocking_reasons"])
    pair = suggest_report_pairs(book(), book())[0]
    pair.update(pairing_confirmed=True, confirmation_reason="核对完整文件确认新版一号楼属于园区")
    result = compare_inventory(old, new, {"pairs": [pair]})
    assert len(result["pairs"]) == 1 and result["reports"][0]["confirmation"] == "confirmed"
    new["pages"][0]["evidence_lines"][1] = line("单位工程名称：二号楼", .2)
    result = compare_inventory(old, new, {"pairs": [pair]})
    assert not result["pairs"]
    assert any("冲突" in message for message in result["reports"][0]["blocking_reasons"])


def test_partial_hierarchy_multiple_candidates_remain_ambiguous():
    old, new = book(pages=2), book()
    for page, project in zip(old["pages"], ("园区甲", "园区乙")):
        page["evidence_lines"][1] = line(f"工程名称：{project} 单位工程名称：一号楼", .2)
    new["pages"][0]["evidence_lines"][1] = line("单位工程名称：一号楼", .2)
    result = compare_inventory(old, new, {"pairs": []})
    assert not result["pairs"]
    assert len(result["reports"]) == 3
    assert all(r["presence"] == "uncertain" for r in result["reports"])
    assert not any(r["old"] and r["new"] for r in result["reports"])


def test_continuation_merge_translates_merge_anchor_and_preserves_pdf_geometry():
    from backend.compare import _key_cell

    value = book(pages=2, continuation=True)
    second = value["sheets"][1]
    second["max_row"] = 3
    second["merged_ranges"] = ["A2:A3"]
    second["cells"]["A2"].update(display="SECOND", value="SECOND", merged_range="A2:A3")
    second["cells"]["A3"] = {**copy.deepcopy(second["cells"]["A2"]), "row": 3, "address": "A3", "display": "", "value": None}
    original_source = copy.deepcopy(second["cells"]["A2"]["pdf_source"])
    snapshot = copy.deepcopy(value)
    result = attach_report_inventory(value)
    logical = next(s for s in result["sheets"] if s.get("member_sheets"))
    assert logical["merged_ranges"] == ["A3:A4"]
    assert logical["cells"]["A3"]["merged_range"] == logical["cells"]["A4"]["merged_range"] == "A3:A4"
    assert _key_cell(logical, 4, 1)["display"] == "SECOND"
    assert _key_cell(logical, 4, 1)["pdf_source"] == original_source
    assert value == snapshot
    physical = next(s for s in result["sheets"] if s["name"] == "p2")
    assert physical["merged_ranges"] == ["A2:A3"]
    assert physical["cells"]["A2"]["pdf_source"] == original_source


def test_cross_header_body_merged_cells_block_alias_creation_and_comparison():
    value = book(pages=2, continuation=True)
    value["sheets"][1]["merged_ranges"] = ["A1:A2"]
    value["sheets"][1]["cells"]["A2"]["merged_range"] = "A1:A2"
    result = attach_report_inventory(value)
    assert len(result["reports"]) == 1
    assert not any(s.get("member_sheets") for s in result["sheets"])
    assert not suggest_report_pairs(result, result)
    assert any("跨越表头与正文边界" in issue for issue in result["reports"][0]["issues"])


def test_composite_key_validation_reads_translated_merged_anchor():
    value = book(pages=2, continuation=True)
    second = value["sheets"][1]
    second["max_row"] = 3
    second["merged_ranges"] = ["A2:A3"]
    second["cells"]["A2"].update(display="SECOND", value="SECOND", merged_range="A2:A3")
    second["cells"]["A3"] = {**copy.deepcopy(second["cells"]["A2"]), "row": 3, "address": "A3", "display": "", "value": None}
    second["cells"]["B3"] = {**copy.deepcopy(second["cells"]["B2"]), "row": 3, "address": "B3", "display": "20", "value": "20"}
    result = attach_report_inventory(value)
    name = result["reports"][0]["sheet_name"]
    pair = {"id": "composite", "old_sheet": name, "new_sheet": name, "mode": "key", "columns": [
        {"id": "group", "is_key": True, "old_column": "A", "new_column": "A"},
        {"id": "detail", "is_key": True, "old_column": "B", "new_column": "B"}]}
    assert compare_inventory(result, result, {"pairs": [pair]})["pairs"] == [pair]


def v2(value):
    return {**copy.deepcopy(value), "report_inventory_version": 2}


@pytest.mark.parametrize("code,title", [
    ("A.1", "招标工程量清单封面"), ("A.2", "招标工程量清单扉页"),
    ("A.3", "招标工程量清单编制说明"), ("A.4", "工程量清单计算规则说明"),
])
def test_v2_full_text_reports_match_template_to_filled_project_without_sheets(code, title):
    old, new = v2(book(code, obj="")), v2(book(code, obj="空工程"))
    for value in (old, new):
        value["sheets"] = []
        value["pages"][0]["evidence_lines"][0] = line(f"{code} {title}")
    new["pages"][0]["evidence_lines"].append(line("2026年11月04日", .7))
    pair = suggest_report_pairs(old, new)[0]
    assert pair["old_report_id"] and pair["new_report_id"]
    assert pair["old_sheet"] is None and pair["new_sheet"] is None
    assert pair["columns"] == []
    result = compare_inventory(old, new, {"pairs": []})
    row = result["reports"][0]
    assert row["presence"] == "shared" and row["confirmation"] == "confirmed"
    assert row["match_method"] == "code" and not row["blocking_reasons"]
    assert row["content_status"] == "pending"
    assert row["old"]["comparison_regions"] == row["new"]["comparison_regions"] == [{"page": 1, "bbox": [0, 0, 1, 1]}]


def test_v2_changed_project_header_and_empty_table_are_content_not_identity_conflicts():
    old, new = v2(book(obj="原项目")), v2(book(obj="新项目", content=False))
    new["sheets"][0]["cells"]["B1"].update(display="单价", value="单价")
    result = compare_inventory(old, new, {"pairs": []})
    row = result["reports"][0]
    assert row["confirmation"] == "confirmed" and row["presence"] == "shared"
    assert row["old"]["object"] != row["new"]["object"]
    assert row["blocking_reasons"] == []
    assert any("表头不同" in reason for reason in row["field_blocking_reasons"])
    assert len(result["pairs"]) == 1


def test_v2_same_header_different_codes_still_cannot_be_manually_paired():
    old, new = v2(book("A.5.2")), v2(book("B.8.3"))
    left, right = attach_report_inventory(old)["reports"][0], attach_report_inventory(new)["reports"][0]
    pair = {"id": "manual", "old_report_id": left["id"], "new_report_id": right["id"],
            "pairing_confirmed": True, "confirmation_reason": "手工指定"}
    result = compare_inventory(old, new, {"pairs": [pair]})
    assert not result["pairs"]
    assert result["reports"][0]["confirmation"] == "pending"
    assert "编号不同" in result["reports"][0]["blocking_reasons"][0]


def test_v2_multiple_instances_follow_objects_before_order_and_allow_changed_names():
    old, new = v2(book(pages=2)), v2(book(pages=2))
    for value, names in ((old, ["项目甲", "项目乙"]), (new, ["项目乙", "项目甲"])):
        for page, name in zip(value["pages"], names):
            page["evidence_lines"][1]["text"] = f"工程名称：{name}"
    result = compare_inventory(old, new, {"pairs": []})
    assert [(r["old"]["pages"], r["new"]["pages"], r["match_method"]) for r in result["reports"]] == [([1], [2], "object"), ([2], [1], "object")]
    for page in new["pages"]:
        page["evidence_lines"][1]["text"] += "改名"
    pairs = suggest_report_pairs(old, new)
    result = compare_inventory(old, new, {"pairs": pairs})
    assert len(result["reports"]) == 2
    assert all(r["match_method"] == "sequence" and r["confirmation"] == "confirmed" for r in result["reports"])
    assert result["issues"] == []


def test_v2_count_mismatch_preserves_all_candidates_and_manual_report_id_selection():
    old, new = v2(book("C.6.2", obj="")), v2(book("C.6.2", obj="空工程", pages=2))
    old["sheets"] = new["sheets"] = []
    result = compare_inventory(old, new, {"pairs": []})
    assert len(result["reports"]) == 3 and not result["pairs"]
    assert all(r["confirmation"] == "pending" and r["presence"] == "uncertain" for r in result["reports"])
    assert len(result["type_inventory"]["shared"]) == 1
    left = attach_report_inventory(old)["reports"][0]
    right = attach_report_inventory(new)["reports"][1]
    pair = {"id": "manual", "old_report_id": left["id"], "new_report_id": right["id"],
            "pairing_confirmed": True, "confirmation_reason": "对照原件选择第二份，另一份重复页另行核对"}
    result = compare_inventory(old, new, {"pairs": [pair]})
    assert result["reports"][0]["new"]["pages"] == [2]
    assert result["reports"][0]["confirmation"] == "confirmed"
    assert result["reports"][1]["confirmation"] == "pending"


@pytest.mark.parametrize("code", ["B.8.2", "C.6.2"])
def test_v2_identical_header_only_pages_do_not_prove_instance_identity_or_choose_first(code):
    old, new = v2(book(code, obj="同一工程")), v2(book(code, obj="同一工程", pages=2))
    new["sheets"] = []
    assert new["pages"][0]["evidence_lines"] == new["pages"][1]["evidence_lines"]
    old, new = attach_report_inventory(old), attach_report_inventory(new)
    repeated = new["reports"]
    assert len(repeated) == 2 and all(r["source_body_empty"] and r["duplicate"] for r in repeated)
    assert all(r["page_sequence"] == [1, 1] and not r["continuation"] and len(r["pages"]) == 1 for r in repeated)
    for ordered in (new, {**new, "reports": list(reversed(repeated))}):
        assert suggest_report_pairs(old, ordered) == []
        result = compare_inventory(old, ordered, {"pairs": []})
        assert len(result["reports"]) == 3 and not result["pairs"]
        assert all(r["presence"] == "uncertain" and r["confirmation"] == "pending" for r in result["reports"])
        assert all(not (r["old"] and r["new"]) for r in result["reports"])
        assert {r["new"]["id"] for r in result["reports"] if r["new"]} == {r["id"] for r in repeated}
        assert len(result["type_inventory"]["shared"]) == 1
        assert not result["type_inventory"]["old_only"] and not result["type_inventory"]["new_only"]


def test_v2_two_old_instances_one_new_partial_object_does_not_confirm_last_candidate_added():
    old, new = v2(book(pages=2)), v2(book(obj=""))
    old["pages"][1]["evidence_lines"][1]["text"] = "工程名称：另一项目"
    result = compare_inventory(old, new, {"pairs": []})
    assert len(result["reports"]) == 3
    assert all(r["presence"] == "uncertain" for r in result["reports"])


def test_v2_full_regions_separate_same_page_reports_and_include_continuation_note():
    value = v2({"sheets": [sheet("a", top=.25), sheet("b", top=.7)], "pages": [
        {"number": 1, "status": "ok", "evidence_lines": [line("A.1 招标工程量清单封面", .1), line("工程名称：项目甲", .2),
          line("B.5 最高投标限价公布表", .55), line("工程名称：项目乙", .65), line("注：1 其他说明", .95)]},
        {"number": 2, "status": "ok", "evidence_lines": [line("2 延续上一页的说明。", .1)]}]})
    reports = attach_report_inventory(value)["reports"]
    assert reports[0]["comparison_regions"] == [{"page": 1, "bbox": [0, 0, 1, .55]}]
    assert reports[1]["comparison_regions"] == [{"page": 1, "bbox": [0, .55, 1, 1]}, {"page": 2, "bbox": [0, 0, 1, 1]}]
    assert reports[0]["regions"][0]["bbox"] != reports[0]["comparison_regions"][0]["bbox"]


def test_v2_plain_title_and_manual_unknown_title_can_pair_without_business_keys():
    old, new = v2(book()), v2(book(obj="改名"))
    for value in (old, new):
        value["sheets"] = []
        value["pages"][0]["evidence_lines"][0] = line("招标工程量清单封面")
    result = compare_inventory(old, new, {"pairs": []})
    assert result["reports"][0]["match_method"] == "title"
    assert result["reports"][0]["confirmation"] == "confirmed"
    unknown = v2(book())
    unknown["pages"][0]["evidence_lines"] = []
    report = attach_report_inventory(unknown)["reports"][0]
    assert not suggest_report_pairs(unknown, unknown)
    pair = {"id": "manual", "old_report_id": report["id"], "new_report_id": report["id"],
            "pairing_confirmed": True, "confirmation_reason": "人工识别无标题报表并确认对应"}
    result = compare_inventory(unknown, unknown, {"pairs": [pair]})
    assert result["reports"][0]["confirmation"] == "confirmed"


def test_v2_template_missing_type_can_be_confirmed_but_failed_regions_block_absence():
    old, new = v2(book(obj="")), v2(book("B.8.3", obj=""))
    result = compare_inventory(old, new, {"pairs": []})
    assert {r["presence"] for r in result["reports"]} == {"old_only", "new_only"}
    new["pages"][0]["ocr_regions"] = [{"bbox": [0, .6, 1, 1], "status": "failed", "affects_content": True}]
    result = compare_inventory(old, new, {"pairs": []})
    assert result["reports"][0]["presence"] == "uncertain"
    assert not result["type_inventory"]["old_only"]


def test_v2_failed_content_and_ocr_title_are_never_equal_claims():
    old, new = v2(book()), v2(book())
    new["pages"][0]["ocr_regions"] = [{"bbox": [0, .6, 1, 1], "status": "failed", "affects_content": True}]
    result = compare_inventory(old, new, {"pairs": []})
    assert result["reports"][0]["content_status"] == "pending"
    assert any("未能可靠恢复" in reason for reason in result["reports"][0]["blocking_reasons"])
    new["pages"][0]["evidence_lines"][0]["method"] = "ocr"
    result = compare_inventory(old, new, {"pairs": []})
    assert result["reports"][0]["confirmation"] == "pending"


def test_v2_template_continuation_builds_alias_and_preserves_decimal_keys_across_pages():
    from backend.compare import compare_workbooks
    from backend.pdf import _make_sheet
    from backend.reports import _pair

    books = []
    for amounts in ([[("A001", "12000"), ("A002", "1.000")], [("A004", "20")]],
                    [[("A004", "20"), ("A002", "1.003")], [("A001", "12800")]]):
        value = v2(book(obj="", pages=2, continuation=True))
        for n, rows in enumerate(amounts, 1):
            source = {"kind": "pdf", "page": n, "bbox": [.1, .3, .9, .5], "method": "text"}
            matrix = [[(text, copy.deepcopy(source)) for text in row] for row in [("项目编码", "金额"), *rows]]
            value["sheets"][n - 1] = _make_sheet(f"p{n}", matrix, structure="table", page_number=n, method="text")
            value["sheets"][n - 1]["bbox"] = [.1, .3, .9, .5]
        books.append(attach_report_inventory(value))
    old, new = books
    assert all(len(value["reports"]) == 1 and value["reports"][0]["pages"] == [1, 2] for value in books)
    pair = _pair(old["reports"][0], new["reports"][0], 1)
    result = compare_workbooks(old, new, {"pairs": [pair], "absolute_tolerance": "0", "ignore_outer_whitespace": True,
                                          "compare_formulas": False, "compare_formatting": False})
    values = {d["row_key"]: d for d in result["diffs"]}
    assert set(values) == {"A001", "A002"}
    assert values["A001"]["delta"] == "800" and values["A002"]["delta"] == "0.003"
    for side, expected in (("old", 1), ("new", 2)):
        source = values["A001"][f"{side}_source"]
        logical = next(s for s in (old if side == "old" else new)["sheets"] if s["name"] == source["sheet"])
        assert logical["cells"][source["cell"]]["pdf_source"]["page"] == expected


def test_v2_rebuild_is_immutable_and_v1_pairing_semantics_stay_unchanged():
    old, new = book(obj=""), book(obj="填报工程")
    snapshot = copy.deepcopy(old)
    assert not suggest_report_pairs(old, new)
    assert compare_inventory(old, new, {"pairs": []})["reports"][0]["confirmation"] == "pending"
    revised = attach_report_inventory(v2(old))
    assert attach_report_inventory(revised) == revised
    assert old == snapshot
    assert len(suggest_report_pairs(v2(old), v2(new))) == 1
    with pytest.raises(ValueError, match="版本不一致"):
        compare_inventory(attach_report_inventory(old), revised, {"pairs": []})


def test_v2_missing_code_exact_title_pairs_and_near_title_is_candidate_not_missing():
    old, new = v2(book("A.5.2", obj="")), v2(book("A.5.2", obj="填报工程"))
    new["pages"][0]["evidence_lines"][0] = line("材料暂估单价表")
    result = compare_inventory(old, new, {"pairs": []})
    assert len(result["reports"]) == 1 and result["reports"][0]["confirmation"] == "confirmed"
    assert result["reports"][0]["match_method"] == "title"
    assert len(result["type_inventory"]["shared"]) == 1
    assert result["type_inventory"]["shared"][0]["type_code"] == "A.5.2"
    new["pages"][0]["evidence_lines"][0] = line("材料暂估价格明细表")
    result = compare_inventory(old, new, {"pairs": []})
    assert len(result["reports"]) == 2 and not result["pairs"]
    assert all(r["presence"] == "uncertain" and r["candidate_report_ids"] for r in result["reports"])
    assert not result["type_inventory"]["old_only"] and not result["type_inventory"]["new_only"]
    left, right = result["reports"][0]["old"], result["reports"][1]["new"]
    pair = {"id": "manual", "old_report_id": left["id"], "new_report_id": right["id"],
            "pairing_confirmed": True, "confirmation_reason": "标题改名，已核对为同一报表"}
    assert compare_inventory(old, new, {"pairs": [pair]})["reports"][0]["confirmation"] == "confirmed"


def test_v2_title_fallback_does_not_override_two_explicit_codes_or_conflicting_chapters():
    old, new = v2(book("A.5.2")), v2(book("B.8.3"))
    result = compare_inventory(old, new, {"pairs": []})
    assert {r["presence"] for r in result["reports"]} == {"old_only", "new_only"}
    assert all(not r["candidate_report_ids"] for r in result["reports"])
    new["pages"][0]["evidence_lines"] = [line("附录B最高投标限价文件", .01), line("材料暂估单价表"), line("工程名称：项目", .2)]
    assert not suggest_report_pairs(old, new)


def test_v2_title_fallback_does_not_steal_unique_code_match():
    old, new = v2(book(pages=2)), v2(book())
    old["pages"][1]["evidence_lines"][0] = line("材料暂估单价表")
    result = compare_inventory(old, new, {"pairs": []})
    matched = next(row for row in result["reports"] if row["new"])
    assert matched["old"]["pages"] == [1] and matched["match_method"] == "code"


def test_v2_graphic_blank_form_remains_manual_candidate_and_plain_blank_does_not():
    raw = {"report_inventory_version": 2, "sheets": [], "pages": [
        {"number": 1, "status": "ok", "method": "text", "has_graphics": True, "evidence_lines": []},
        {"number": 2, "status": "ok", "method": "text", "has_graphics": False, "evidence_lines": []}]}
    result = attach_report_inventory(raw)
    assert len(result["reports"]) == 1
    report = result["reports"][0]
    assert report["pages"] == [1] and report["graphic_form"] and not report["identity_reliable"]
    assert report["comparison_regions"] == [{"page": 1, "bbox": [0, 0, 1, 1]}]
    assert [(r["type"], r["pages"]) for r in result["non_report_regions"]] == [("blank_page", [2])]
    compared = compare_inventory(result, result, {"pairs": []})
    assert not compared["pairs"] and all(row["confirmation"] == "pending" for row in compared["reports"])
    pair = {"id": "manual", "old_report_id": report["id"], "new_report_id": report["id"],
            "pairing_confirmed": True, "confirmation_reason": "核对两侧空白表框，为同一表单"}
    compared = compare_inventory(result, result, {"pairs": [pair]})
    assert compared["reports"][0]["presence"] == "shared"
    assert compared["reports"][0]["blocking_reasons"] == []
    assert attach_report_inventory(result) == result


@pytest.mark.parametrize("side", ["old", "new"])
def test_v2_manual_single_side_stays_pending_and_does_not_turn_other_side_into_missing(side):
    old, new = attach_report_inventory(v2(book(obj=""))), attach_report_inventory(v2(book(obj="新工程")))
    selected = (old if side == "old" else new)["reports"][0]
    pair = {"id": "hold", f"{side}_report_id": selected["id"], "pairing_confirmed": False, "confirmation_reason": ""}
    result = compare_inventory(old, new, {"pairs": [pair]})
    assert len(result["reports"]) == 2 and not result["pairs"]
    assert all(row["presence"] == "uncertain" and row["confirmation"] == "pending" for row in result["reports"])
    assert result["reports"][0]["match_method"] == "manual_unpaired"
    assert not result["reports"][0]["new" if side == "old" else "old"]
    assert any("手动" in reason for row in result["reports"] for reason in row["blocking_reasons"])
    restored = compare_inventory(old, new, {"pairs": []})
    assert len(restored["reports"]) == 1 and restored["reports"][0]["confirmation"] == "confirmed"


def test_v2_invalid_nonempty_report_id_is_not_treated_as_manual_single_side():
    value = attach_report_inventory(v2(book()))
    pair = {"id": "bad", "old_report_id": value["reports"][0]["id"], "new_report_id": "not-a-current-report"}
    result = compare_inventory(value, value, {"pairs": [pair]})
    assert any("不在当前清单" in issue["message"] for issue in result["issues"])
    assert not any(row["match_method"] == "manual_unpaired" for row in result["reports"])


def test_v2_page_region_issues_keep_local_sources_actions_and_native_pairing():
    value = v2(book())
    region = {"id": "p1-ocr-1", "bbox": [.85, .1, .95, .2], "status": "failed", "affects_content": True}
    value["pages"][0]["ocr_regions"] = [region]
    result = compare_inventory(value, value, {"pairs": []})
    assert result["reports"][0]["confirmation"] == "confirmed"
    failures = [issue for issue in result["issues"] if issue["type"] == "region_unresolved"]
    assert len(failures) == 2 and {issue["side"] for issue in failures} == {"old", "new"}
    assert all(issue["sources"][0]["bbox"] == region["bbox"] and "reprocess" in issue["actions"] for issue in failures)
    assert all(issue["report_ids"] == [result["reports"][0]["id"]] for issue in failures)
    value["sheets"] = []
    value["pages"][0]["evidence_lines"] = [line("无法分类的文字")]
    result = compare_inventory(value, value, {"pairs": []})
    failures = [issue for issue in result["issues"] if issue["type"] == "region_unresolved"]
    assert len(failures) == 2 and all(not issue["report_ids"] for issue in failures)


def test_v2_successful_unreviewed_ocr_region_is_separate_issue_but_watermark_is_not():
    value = v2(book())
    value["pages"][0]["ocr_regions"] = [
        {"id": "ocr", "bbox": [.8, .1, .9, .2], "status": "ok", "text_count": 1, "affects_content": True},
        {"id": "watermark", "bbox": [.1, .5, .9, .8], "status": "empty", "affects_content": False}]
    result = compare_inventory(value, value, {"pairs": []})
    assert len([issue for issue in result["issues"] if issue["type"] == "ocr_review"]) == 2
    assert not any(issue["type"] == "region_unresolved" for issue in result["issues"])
    value["pages"][0]["reviewed"] = True
    assert not any(issue["type"] == "ocr_review" for issue in compare_inventory(value, value, {"pairs": []})["issues"])
