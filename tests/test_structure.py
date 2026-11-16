import copy

import pytest

from backend.pdf import _logical_tables, _make_sheet, compare_pdfs, suggest_pdf_pairs
from backend.review import apply_reviews, prepare_structure_event, review_catalog, structure_table, structure_tables


def book_with_tables():
    sheets = []
    for page in (1, 2):
        matrix = []
        for row, values in enumerate([["项目编码", "Amount"], [f"00{page}", str(page * 10)], [f"01{page}", str(page * 20)]], 1):
            matrix.append([(value, {"kind": "pdf", "page": page, "bbox": [(col - 1) / 2, row / 5, col / 2, (row + 1) / 5],
                                    "method": "ocr", "confidence": .95}) for col, value in enumerate(values, 1)])
        sheets.append(_make_sheet(f"第 {page} 页 · 表 1", matrix, structure="table", page_number=page, method="ocr"))
    warnings = []
    logical = _logical_tables(sheets, warnings)
    return {"format": "pdf", "review_namespace": "job-1:old", "sheets": sheets + logical, "warnings": warnings,
            "pages": [{"number": page, "method": "ocr", "status": "ok"} for page in (1, 2)]}


def payload(table):
    return [[{"id": cell["id"], "text": cell["text"]} for cell in row] for row in table["rows"]]


def change(book, rows, sheet="第 1 页 · 表 1", revision=1, **overrides):
    page = next(s["page_number"] for s in book["sheets"] if s["name"] == sheet)
    event = {"action": "table_update", "side": "old", "sheet": sheet, "page": page, "rows": rows,
             "structure_confirmed": True, "reason": "按原图补齐并核对行列", "revision": revision}
    event.update(overrides)
    return prepare_structure_event(book, event)


def test_structure_snapshots_move_existing_ids_and_create_page_only_manual_sources():
    book = book_with_tables()
    before = structure_table(book, "第 1 页 · 表 1")
    rows = payload(before)
    rows[1], rows[2] = rows[2], rows[1]
    rows.append([{"text": "099"}, {"text": "99.50"}])
    event = change(book, rows)
    assert event["table_before"] == before
    assert event["before_text"] == "3 行 × 2 列" and event["after_text"] == "4 行 × 2 列"
    assert event["structure_revision"] == 1
    assert event == change(book, rows)
    assert event["table_after"]["rows"][2][1]["source"] == before["rows"][1][1]["source"]
    for cell in event["table_after"]["rows"][-1]:
        assert cell["manual"] and cell["original_text"] == "" and not cell["reviewed"]
        assert cell["source"] == {"kind": "pdf", "page": 1, "bbox": [0, 0, 1, 1], "method": "ocr",
                                  "manual": True, "location_precision": "page"}
    updated = apply_reviews(book, [event])
    table = structure_table(updated, before["sheet"])
    assert table["rows"][2][1]["id"] == before["rows"][1][1]["id"]
    assert table["rows"][2][1]["original_text"] == "10"
    assert updated["sheets"][0]["cells"]["B4"]["value"] == "99.50"
    assert updated["sheets"][0]["cells"]["A4"]["value"] == "099"
    assert structure_table(book, before["sheet"]) == before
    moved = before["rows"][1][1]["id"]
    new = table["rows"][-1][1]["id"]
    further = apply_reviews(book, [event, {"action": "cell_update", "target_id": moved, "value": "11", "revision": 2},
                                  {"action": "cell_update", "target_id": new, "value": "100.50", "revision": 3}])
    assert further["sheets"][0]["cells"]["B3"]["value"] == "11"
    assert further["sheets"][0]["cells"]["B4"]["value"] == "100.50"
    assert next(row for row in review_catalog(further) if row["id"] == new)["original_text"] == ""


def test_deleted_rows_are_removed_from_catalog_and_rebuilt_logical_tables():
    book = book_with_tables()
    before = structure_table(book, "第 1 页 · 表 1")
    rows = payload(before)
    removed = rows.pop()[0]["id"]
    event = change(book, rows)
    updated = apply_reviews(book, [event])
    logical = next(s for s in updated["sheets"] if s.get("member_sheets"))
    assert logical["max_row"] == 4
    assert not any(c.get("review_id") == removed for s in updated["sheets"] for c in s["cells"].values())
    assert len(event["table_before"]["rows"]) == 3
    with pytest.raises(ValueError, match="单元格"):
        apply_reviews(book, [event, {"action": "cell_verify", "target_id": removed, "revision": 2}])
    original_logical = next(s for s in book["sheets"] if s.get("member_sheets"))
    assert original_logical["max_row"] == 5


def test_column_reordering_rebuilds_candidates_and_preserves_amount_and_identifier_types():
    old = book_with_tables()
    table = structure_table(old, "第 1 页 · 表 1")
    rows = [[row[1], row[0]] for row in payload(table)]
    event = change(old, rows)
    new = apply_reviews(old, [event])
    sheet = new["sheets"][0]
    assert sheet["cells"]["A2"]["value_type"] == "number"
    assert sheet["cells"]["B2"]["value_type"] == "string" and sheet["cells"]["B2"]["value"] == "001"
    logical = [s for s in new["sheets"] if s.get("member_sheets")]
    assert len(logical) == 2 and all(len(s["member_sheets"]) == 1 for s in logical)
    assert not any("已拼接" in warning for warning in new["warnings"])
    pair = {"id": "physical", "old_sheet": table["sheet"], "new_sheet": table["sheet"], "mode": "key",
            "old_header_start": 1, "old_header_end": 1, "new_header_start": 1, "new_header_end": 1,
            "columns": [{"id": "key", "name": "项目编码", "old_column": "A", "new_column": "B", "is_key": True},
                        {"id": "amount", "name": "Amount", "old_column": "B", "new_column": "A", "is_key": False}]}
    compared = compare_pdfs(old, new, {"pairs": [pair]})
    assert compared["summary"]["by_kind"] == {"ocr_review": 4}


def test_table_updates_invalidate_all_regions_on_same_page_and_preserve_old_header_edit_names():
    book = book_with_tables()
    another = copy.deepcopy(book["sheets"][0])
    another["name"] = "第 1 页 · 表 2"
    book["sheets"].insert(1, another)
    checked = apply_reviews(book, [{"action": "page_verify", "page": 1, "revision": 1}])
    event = change(checked, payload(structure_table(checked, "第 1 页 · 表 1")), revision=2)
    updated = apply_reviews(book, [{"action": "page_verify", "page": 1, "revision": 1}, event])
    assert not updated["pages"][0]["reviewed"] and updated["pages"][0]["reviewed_cells"] == 0
    assert not any(row["reviewed"] for row in review_catalog(updated) if row["page"] == 1)
    book = book_with_tables()
    old_names = [sheet["name"] for sheet in book["sheets"]]
    header = structure_table(book, "第 1 页 · 表 1")["rows"][0][1]["id"]
    text_only = apply_reviews(book, [{"action": "cell_update", "target_id": header, "value": "本期金额", "revision": 1}])
    assert [sheet["name"] for sheet in text_only["sheets"]] == old_names


def unconfirmed_book(regions=1):
    sheets = []
    for i in range(regions):
        source = {"kind": "pdf", "page": 1, "bbox": [0, .1 + i * .2, 1, .2 + i * .2], "method": "ocr"}
        sheets.append(_make_sheet(f"区域 {i + 1}", [[("文本内容", source)], [("001 10", source)]],
                                  structure="text_blocks", page_number=1, method="ocr"))
    return {"sheets": sheets, "format": "pdf", "pages": [{"number": 1, "method": "ocr", "status": "ok", "structure_unconfirmed": True}],
            "warnings": ["第 1 页未能可靠恢复表格结构，仅保存单列文字块并按位置比较，须核对错列或漏行。"]}


def test_rebuilding_every_unconfirmed_region_unlocks_page_review_and_resolves_only_its_warning():
    book = unconfirmed_book(2)
    events = []
    for region in (1, 2):
        current = apply_reviews(book, events)
        before = structure_table(current, f"区域 {region}")
        assert before["rows"][0][0]["text"] == "文本内容"
        rows = [[{"id": before["rows"][0][0]["id"], "text": "项目编码"}, {"text": "Amount"}],
                [{"id": before["rows"][1][0]["id"], "text": f"00{region}"}, {"text": "10"}]]
        events.append(change(current, rows, sheet=before["sheet"], revision=region))
        updated = apply_reviews(book, events)
        assert updated["pages"][0]["reviewable"] is (region == 2)
        if region == 1:
            with pytest.raises(ValueError, match="不能"):
                apply_reviews(book, events + [{"action": "page_verify", "page": 1, "revision": 3}])
    assert not updated["pages"][0]["structure_unconfirmed"]
    assert len(review_catalog(updated)) == 8
    assert any("结构确认不等于内容核验" in warning for warning in updated["warnings"])
    checked = apply_reviews(book, events + [{"action": "page_verify", "page": 1, "revision": 3}])
    assert checked["pages"][0]["reviewed"]
    assert not any("未能可靠恢复" in warning or "结构确认不等于" in warning for warning in checked["warnings"])
    book["warnings"].append("区域之外仍有未比较内容。")
    checked = apply_reviews(book, events + [{"action": "page_verify", "page": 1, "revision": 3}])
    assert "区域之外仍有未比较内容。" in checked["warnings"]
    single = unconfirmed_book()
    event = change(single, [[{"text": "项目编码"}, {"text": "Amount"}], [{"text": "001"}, {"text": "10"}]], sheet="区域 1")
    checked = apply_reviews(single, [event, {"action": "page_verify", "page": 1, "revision": 2}])
    assert checked["warnings"] == []
    result = compare_pdfs(checked, checked, {"pairs": suggest_pdf_pairs(checked, checked)})
    assert result["summary"]["total"] == 0 and result["coverage"]["warnings"] == []


def test_directory_limits_and_failed_or_native_pages_cannot_be_edited():
    book = book_with_tables()
    assert len(structure_tables(book)) == 2
    original_name = book["sheets"][-1]["name"]
    with pytest.raises(ValueError, match="物理区域"):
        structure_table(book, original_name)
    book["sheets"][0]["max_row"] = 501
    entry = structure_tables(book)[0]
    assert not entry["editable"] and "不会自动裁剪" in entry["edit_limit"]
    with pytest.raises(ValueError, match="上限"):
        structure_table(book, entry["sheet"])
    failed = unconfirmed_book()
    failed["pages"][0]["status"] = "unavailable"
    assert not structure_tables(failed)[0]["editable"]
    with pytest.raises(ValueError, match="提取失败"):
        change(failed, [[{"text": "header"}]], sheet="区域 1")
    native = book_with_tables()
    for sheet in native["sheets"]:
        sheet["method"] = "text"
    assert structure_tables(native) == []
    lengthy = book_with_tables()
    lengthy["sheets"][0]["cells"]["B2"]["raw_text"] = "x" * 10001
    assert not structure_tables(lengthy)[0]["editable"]


@pytest.mark.parametrize("invalid,match", [
    ([], "1 至 500"),
    ([[{"text": "x"}], []], "矩形"),
    ([[{"text": "x", "source": {}}]], "来源"),
    ([[{"text": "x", "reviewed": True}]], "核验"),
    ([[{"text": "x" * 10001}]], "10000 字符"),
    ([[{"text": "x"}] * 61], "60 列"),
    ([[{"text": "x"}] * 60] * 167, "10000 格"),
    ([[{"text": "x"}]] * 501, "500 行"),
])
def test_structure_matrix_validation(invalid, match):
    with pytest.raises(ValueError, match=match):
        change(book_with_tables(), invalid)


def test_duplicate_foreign_ids_and_missing_attestation_are_rejected():
    book = book_with_tables()
    table = structure_table(book, "第 1 页 · 表 1")
    rows = payload(table)
    rows[1][1]["id"] = rows[1][0]["id"]
    with pytest.raises(ValueError, match="只能出现一次"):
        change(book, rows)
    rows[1][1]["id"] = structure_table(book, "第 2 页 · 表 1")["rows"][1][1]["id"]
    with pytest.raises(ValueError, match="当前物理区域"):
        change(book, rows)
    for values, match in (({"reason": " "}, "填写原因"), ({"structure_confirmed": False}, "明确确认"), ({"page": 2}, "不一致")):
        with pytest.raises(ValueError, match=match):
            change(book, payload(table), **values)


def test_rows_without_snapshot_replay_and_new_manual_ids_are_revision_scoped():
    book = book_with_tables()
    rows = [[{"text": "项目编码"}, {"text": "Amount"}], [{"text": "001"}, {"text": "10"}]]
    event = change(book, rows)
    other = change(book, rows, revision=2)
    assert event["table_after"]["rows"][0][0]["id"] != other["table_after"]["rows"][0][0]["id"]
    raw = {key: value for key, value in event.items() if key not in ("table_before", "table_after")}
    assert apply_reviews(book, [raw]) == apply_reviews(book, [event])


def test_manual_added_row_stays_suspected_until_page_checks_and_keeps_page_precision():
    book = book_with_tables()
    rows = payload(structure_table(book, "第 1 页 · 表 1"))
    rows.append([{"text": "099"}, {"text": "5.00"}])
    event = change(book, rows)
    updated = apply_reviews(book, [event])
    pending = compare_pdfs(book, updated, {"pairs": suggest_pdf_pairs(book, updated)})
    assert pending["summary"]["by_kind"] == {"ocr_suspected": 1, "ocr_review": 4}
    verification = [{"action": "page_verify", "page": number, "revision": number + 1} for number in (1, 2)]
    old, new = apply_reviews(book, verification), apply_reviews(book, [event, *verification])
    result = compare_pdfs(old, new, {"pairs": suggest_pdf_pairs(old, new)})
    assert result["summary"]["by_kind"] == {"row_added": 1}
    source = result["diffs"][0]["new_source"]
    assert source["manual"] and source["location_precision"] == "page" and source["bbox"] == [0, 0, 1, 1]
    assert source["reviewed"] and source["review_id"] == event["table_after"]["rows"][-1][0]["id"]


def test_second_structure_revision_keeps_moved_manual_id_and_earlier_snapshot_immutable():
    book = book_with_tables()
    rows = payload(structure_table(book, "第 1 页 · 表 1"))
    rows.append([{"text": "099"}, {"text": "5.00"}])
    first = change(book, rows)
    current = apply_reviews(book, [first])
    before = structure_table(current, "第 1 页 · 表 1")
    revised = payload(before)
    revised[1], revised[3] = revised[3], revised[1]
    second = change(current, revised, revision=2)
    latest = apply_reviews(book, [first, second])
    table = structure_table(latest, before["sheet"])
    assert table["rows"][1][0]["id"] == first["table_after"]["rows"][3][0]["id"]
    assert table["rows"][1][0]["manual"]
    assert first["table_after"]["rows"][3][0]["text"] == "099"
    assert structure_table(apply_reviews(book, [first]), before["sheet"]) == before
