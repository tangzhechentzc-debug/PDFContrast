import hashlib
from zipfile import ZIP_DEFLATED, ZipFile
import xml.etree.ElementTree as ET

import pytest
from openpyxl import Workbook
from openpyxl.styles import Border, PatternFill

from backend.excel import extract_workbook, sheet_columns, sheet_region, suggest_pairs


def test_extract_preserves_source_formula_hidden_and_original(tmp_path):
    path = tmp_path / "report.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "费用明细"
    ws.append(["项目编码", "金额"])
    ws.append(["001", 12.34])
    ws.append(["合计", "=SUM(B2:B2)"])
    ws.row_dimensions[2].hidden = True
    ws.column_dimensions["B"].hidden = True
    ws["B2"].fill = PatternFill("solid", fgColor="FFFFCC00")
    wb.save(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = extract_workbook(path)
    sheet = result["sheets"][0]
    assert sheet["suggested_header_row"] == 1
    assert sheet["cells"]["A2"]["value"] == "001"
    assert sheet["cells"]["B2"]["value"] == "12.34"
    assert sheet["cells"]["B2"]["hidden_row"] is True
    assert sheet["cells"]["B2"]["hidden_column"] is True
    assert sheet["cells"]["B2"]["style"]["fill"]["foreground"]["value"] == "FFFFCC00"
    assert sheet["cells"]["B3"]["formula"] == "=SUM(B2:B2)"
    assert sheet["cells"]["B3"]["cache_status"] == "missing"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_multirow_merged_headers_and_region(tmp_path):
    path = tmp_path / "merged.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["项目", "本期", None])
    ws.append([None, "数量", "金额"])
    ws.append(["001", 3, 30])
    ws.merge_cells("A1:A2")
    ws.merge_cells("B1:C1")
    wb.save(path)
    extracted = extract_workbook(path)
    cols = sheet_columns(extracted, "Sheet", 1, 2)
    assert [x["label"] for x in cols["columns"]] == ["项目", "本期/数量", "本期/金额"]
    assert cols["preview"][0]["row"] == 3
    region = sheet_region(extracted, "Sheet", 2, 1, 25, 10)
    assert region["end_row"] == 3 and region["end_column"] == 3
    assert "A1:A2" in region["merged_ranges"]
    assert next(x for x in region["cells"] if x["address"] == "A2")["merged_range"] == "A1:A2"
    with pytest.raises(ValueError):
        sheet_columns(extracted, "Sheet", 1, 5)
    with pytest.raises(ValueError):
        sheet_region(extracted, "Sheet", 0, 1, 25, 10)


def test_formula_cache_is_separate(tmp_path):
    path = tmp_path / "cached.xlsx"
    wb = Workbook()
    wb.active.append(["编码", "金额"])
    wb.active.append(["001", "=1+1"])
    wb.save(path)
    with ZipFile(path) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(entries["xl/worksheets/sheet1.xml"])
    cell = root.find('.//s:c[@r="B2"]', ns)
    cell.find("s:v", ns).text = "2"
    entries["xl/worksheets/sheet1.xml"] = ET.tostring(root)
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    cell = extract_workbook(path)["sheets"][0]["cells"]["B2"]
    assert cell["formula"] == "=1+1"
    assert cell["cached_value"] == "2"
    assert cell["cached_value_type"] == "number"
    assert cell["display"] == "2"


def test_empty_and_oversized_workbooks(tmp_path):
    path = tmp_path / "empty.xlsx"
    wb = Workbook()
    wb.save(path)
    result = extract_workbook(path)
    assert result["sheets"][0]["nonempty_count"] == 0
    assert sheet_columns(result, "Sheet", 1, 1)["columns"] == []
    wb.active["Z30000"] = "oversized"
    wb.save(path)
    with pytest.raises(ValueError, match="区域过大"):
        extract_workbook(path)


def test_none_border_sides_and_clipped_merged_anchor(tmp_path):
    path = tmp_path / "partial-border.xlsx"
    wb = Workbook()
    ws = wb.active
    ws["A1"] = "合并标题"
    ws["A1"].border = Border(left=None, right=None, top=None, bottom=None)
    ws.merge_cells("A1:A30")
    ws["B30"] = 1
    wb.save(path)
    extracted = extract_workbook(path)
    view = sheet_region(extracted, "Sheet", 20, 1, 5, 2)
    assert view["merged_anchors"][0]["address"] == "A1"
    assert view["merged_anchors"][0]["display"] == "合并标题"


def test_suggestions_match_labels_and_keep_unmatched_sheets(tmp_path):
    left = Workbook()
    left.active.append(["项目编码", "金额"])
    left.active.append(["001", 10])
    right = Workbook()
    right.active.append(["金额", "项目编码", "备注"])
    right.active.append([10, "001", "新增列"])
    right.create_sheet("新增表").append(["标题"])
    lp, rp = tmp_path / "old.xlsx", tmp_path / "new.xlsx"
    left.save(lp)
    right.save(rp)
    pairs = suggest_pairs(extract_workbook(lp), extract_workbook(rp))
    key = next(c for c in pairs[0]["columns"] if c["is_key"])
    assert key["old_column"] == "A" and key["new_column"] == "B"
    assert pairs[1]["old_sheet"] is None and pairs[1]["new_sheet"] == "新增表"
