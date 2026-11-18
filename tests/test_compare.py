from copy import deepcopy

import pytest
from openpyxl import Workbook

from backend.compare import compare_workbooks
from backend.excel import extract_workbook, suggest_pairs


def extract_pair(tmp_path, left_rows, right_rows, headers=None, right_headers=None):
    results = []
    for side, rows, cols in (("old", left_rows, headers or ["项目编码", "金额"]),
                             ("new", right_rows, right_headers or headers or ["项目编码", "金额"])):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "费用明细"
        sheet.append(cols)
        for row in rows:
            sheet.append(row)
        path = tmp_path / f"{side}.xlsx"
        workbook.save(path)
        results.append(extract_workbook(path))
    return results


def configuration(old, new, **kwargs):
    return {"pairs": suggest_pairs(old, new), "absolute_tolerance": "0",
            "ignore_outer_whitespace": True, "compare_formulas": True,
            "compare_formatting": False, **kwargs}


def test_reordered_rows_only_report_true_value_change_and_sources(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 12], ["002", 20]], [["002", 21], ["001", 12]])
    result = compare_workbooks(old, new, configuration(old, new))
    assert len(result["diffs"]) == 1
    diff = result["diffs"][0]
    assert diff["kind"] == "value_changed" and diff["delta"] == "1"
    assert diff["old_source"] == {"sheet": "费用明细", "cell": "B3"}
    assert diff["new_source"] == {"sheet": "费用明细", "cell": "B2"}
    assert result["summary"]["matched_rows"] == 2
    assert result["summary"]["unchanged_rows"] == 1


def test_row_and_column_additions_are_not_duplicated_as_cell_changes(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 12], ["002", 20]],
                            [["001", 12, "备注"], ["003", 30, "新项目"]],
                            right_headers=["项目编码", "金额", "备注"])
    result = compare_workbooks(old, new, configuration(old, new))
    assert result["summary"]["by_kind"] == {"column_added": 1, "row_removed": 1, "row_added": 1}
    addition = next(d for d in result["diffs"] if d["kind"] == "row_added")
    assert addition["old_source"] is None and addition["new_source"]["cell"] == "A3"


def test_decimal_tolerance_boundary_and_display_precision(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 100000000.01], ["002", 1.231]],
                            [["001", 100000000.02], ["002", 1.234]])
    for workbook in (old, new):
        workbook["sheets"][0]["cells"]["B3"]["number_format"] = "0.00"
    exact = compare_workbooks(old, new, configuration(old, new))
    assert [d["delta"] for d in exact["diffs"]] == ["0.01", "0.003"]
    tolerant = compare_workbooks(old, new, configuration(old, new, absolute_tolerance="0.01"))
    assert tolerant["diffs"] == []
    with pytest.raises(ValueError, match="容差"):
        compare_workbooks(old, new, configuration(old, new, absolute_tolerance="NaN"))


def test_duplicate_and_empty_keys_are_explicit_issues(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 10], ["001", 20], [None, 1]],
                            [["001", 20], [None, 1]])
    result = compare_workbooks(old, new, configuration(old, new))
    assert result["summary"]["issues"] == 5
    assert result["summary"]["changes"] == 0
    assert all(d["kind"] == "match_ambiguous" for d in result["diffs"])


def test_unmatched_row_is_not_definite_addition_when_other_key_missing(tmp_path):
    old, new = extract_pair(tmp_path, [[None, 10]], [["001", 10]])
    result = compare_workbooks(old, new, configuration(old, new))
    assert result["summary"]["issues"] == 2
    assert result["summary"]["changes"] == 0


def test_manual_mapping_keeps_header_change_visible(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 1]], [["001", 1]], right_headers=["项目编码", "本期金额"])
    config = configuration(old, new)
    config["pairs"][0]["columns"] = [
        {"id": "key", "name": "项目编码", "old_column": "A", "new_column": "A", "is_key": True},
        {"id": "amount", "name": "金额", "old_column": "B", "new_column": "B", "is_key": False},
    ]
    result = compare_workbooks(old, new, config)
    assert len(result["diffs"]) == 1
    assert result["diffs"][0]["row_key"] == "表头"
    assert result["diffs"][0]["new_source"]["cell"] == "B1"


def test_blank_zero_string_and_identifier_are_distinct(tmp_path):
    old, new = extract_pair(tmp_path, [["001", None], ["002", "0"], ["003", " 备注 "]],
                            [["001", 0], ["002", 0], ["003", "备注"]])
    result = compare_workbooks(old, new, configuration(old, new))
    assert len(result["diffs"]) == 2
    assert {d["row_key"] for d in result["diffs"]} == {"001", "002"}
    strict = compare_workbooks(old, new, configuration(old, new, ignore_outer_whitespace=False))
    assert len(strict["diffs"]) == 3


def test_formula_change_and_missing_cache_are_separate(tmp_path):
    old, new = extract_pair(tmp_path, [["001", "=SUM(1,2)"]], [["001", "=1+2"]])
    result = compare_workbooks(old, new, configuration(old, new))
    assert result["summary"]["by_kind"] == {"formula_result_unavailable": 1, "formula_changed": 1}
    for workbook in (old, new):
        cell = workbook["sheets"][0]["cells"]["B2"]
        cell.update(cache_status="available", cached_value="3", cached_value_type="number")
    cached = compare_workbooks(old, new, configuration(old, new))
    assert cached["summary"]["by_kind"] == {"formula_changed": 1}
    new["sheets"][0]["cells"]["B2"]["formula"] = old["sheets"][0]["cells"]["B2"]["formula"]
    new["sheets"][0]["cells"]["B2"]["cached_value"] = "4"
    changed_result = compare_workbooks(old, new, configuration(old, new))
    assert changed_result["summary"]["by_kind"] == {"value_changed": 1}
    assert changed_result["diffs"][0]["delta"] == "1"


def test_all_mismatches_not_default_report_sample(tmp_path):
    old, new = extract_pair(tmp_path, [[str(i), i] for i in range(125)], [[str(i), i + 1] for i in range(125)])
    result = compare_workbooks(old, new, configuration(old, new))
    assert len(result["diffs"]) == 125
    assert {d["old_source"]["cell"] for d in result["diffs"]} == {f"B{row}" for row in range(2, 127)}
    assert len({d["id"] for d in result["diffs"]}) == 125


def test_missing_sheets_and_excluded_ranges_are_visible(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 1]], [["001", 1]])
    extra = deepcopy(new["sheets"][0])
    extra["name"] = "新增表"
    new["sheets"].append(extra)
    config = configuration(old, new)
    result = compare_workbooks(old, new, config)
    assert result["summary"]["by_kind"] == {"sheet_added": 1}
    config["pairs"] = config["pairs"][:1]
    excluded = compare_workbooks(old, new, config)
    assert excluded["coverage"]["excluded_sheets"]["new"] == ["新增表"]
    config["pairs"][0]["columns"] = config["pairs"][0]["columns"][:1]
    unmapped = compare_workbooks(old, new, config)
    assert any("未映射列" in s for s in unmapped["coverage"]["warnings"])


def test_invalid_mapping_requires_a_real_key(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 1]], [["001", 1]])
    config = configuration(old, new)
    for column in config["pairs"][0]["columns"]:
        column["is_key"] = False
    with pytest.raises(ValueError, match="业务键"):
        compare_workbooks(old, new, config)
    config["pairs"][0]["mode"] = "position"
    result = compare_workbooks(old, new, config)
    assert result["diffs"] == []
    assert any("固定位置" in x for x in result["coverage"]["warnings"])


def test_formatting_is_optional_and_does_not_change_number(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 1]], [["001", 1]])
    new["sheets"][0]["cells"]["B2"]["style"]["number_format"] = "0.00"
    new["sheets"][0]["cells"]["B2"]["number_format"] = "0.00"
    assert compare_workbooks(old, new, configuration(old, new))["diffs"] == []
    result = compare_workbooks(old, new, configuration(old, new, compare_formatting=True))
    assert result["summary"]["by_kind"] == {"format_changed": 1}
    assert "数字格式" in result["diffs"][0]["message"]


def test_overlapping_pairs_rejected_but_disjoint_regions_allowed(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 1], ["002", 2], ["项目编码", "金额"], ["003", 3]],
                            [["001", 1], ["002", 2], ["项目编码", "金额"], ["003", 4]])
    config = configuration(old, new)
    second = deepcopy(config["pairs"][0])
    second["id"] = "other-pair"
    config["pairs"].append(second)
    with pytest.raises(ValueError, match="重叠"):
        compare_workbooks(old, new, config)
    for side in ("old", "new"):
        config["pairs"][0][f"{side}_range"] = "A1:B3"
        second[f"{side}_range"] = "A4:B5"
        second[f"{side}_header_start"] = 4
        second[f"{side}_header_end"] = 4
    result = compare_workbooks(old, new, config)
    assert len(result["diffs"]) == 1
    assert result["diffs"][0]["new_source"]["cell"] == "B5"


def test_font_change_names_the_actual_property(tmp_path):
    old, new = extract_pair(tmp_path, [["001", 1]], [["001", 1]])
    new["sheets"][0]["cells"]["B2"]["style"]["font"]["bold"] = True
    result = compare_workbooks(old, new, configuration(old, new, compare_formatting=True))
    assert result["summary"]["by_kind"] == {"format_changed": 1}
    assert "变化属性：字体。" in result["diffs"][0]["message"]


def test_filling_blank_cell_with_default_style_does_not_report_format_change(tmp_path):
    # 开启格式比较时，默认样式单元格从空白填值或反向清空均不应误报 format_changed
    old, new = extract_pair(tmp_path, [["001", 10, None]], [["001", 10, "新增说明"]],
                            headers=["项目编码", "金额", "备注"])
    result = compare_workbooks(old, new, configuration(old, new, compare_formatting=True))
    assert result["summary"]["by_kind"] == {"value_changed": 1}
    assert [d["kind"] for d in result["diffs"]] == ["value_changed"]

    cleared = compare_workbooks(new, old, configuration(new, old, compare_formatting=True))
    assert cleared["summary"]["by_kind"] == {"value_changed": 1}

    # 真实样式修改（如字体加粗）仍须准确报出 format_changed，且仅列出发生变化的属性类别
    from openpyxl.styles import Font

    styled_wb = Workbook()
    ws = styled_wb.active
    ws.title = "费用明细"
    ws.append(["项目编码", "金额", "备注"])
    ws.append(["001", 10, "新增说明"])
    ws["C2"].font = Font(bold=True)
    styled_path = tmp_path / "styled.xlsx"
    styled_wb.save(styled_path)
    styled_new = extract_workbook(styled_path)

    real_change = compare_workbooks(old, styled_new, configuration(old, styled_new, compare_formatting=True))
    assert real_change["summary"]["by_kind"] == {"value_changed": 1, "format_changed": 1}
    fmt_diff = next(d for d in real_change["diffs"] if d["kind"] == "format_changed")
    assert "变化属性：字体。" in fmt_diff["message"]
