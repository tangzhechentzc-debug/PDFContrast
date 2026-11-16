import pytest

from backend.pdf import _logical_tables, _make_sheet, compare_pdfs, suggest_pdf_pairs
from backend.review import apply_reviews, review_catalog


def workbook():
    sheets = []
    for page, rows in ((1, [["A001", "10.00", "0001"], ["A002", "20.00", "0002"]]),
                       (2, [["A003", "30.00", "0003"], ["A004", "", "0004"]])):
        matrix = []
        for row, values in enumerate([["项目编码", "Amount", "ItemCode"], *rows], 1):
            matrix.append([(value, {"kind": "pdf", "page": page, "bbox": [(column - 1) / 3, (row - 1) / 4, column / 3, row / 4],
                                    "method": "ocr", "confidence": .9}) for column, value in enumerate(values, 1)])
        sheets.append(_make_sheet(f"第 {page} 页 · 表 1", matrix, structure="table", page_number=page, method="ocr"))
    warnings = []
    logical = _logical_tables(sheets, warnings)
    return {"format": "pdf", "sheets": sheets + logical, "warnings": warnings,
            "pages": [{"number": page, "method": "ocr", "status": "ok"} for page in (1, 2)]}


def target(book, page, address):
    return next(item["id"] for item in review_catalog(book) if item["page"] == page and item["cell"] == address)


def event(action, revision=1, **values):
    return {"action": action, "revision": revision, "reason": "核对原文", "created_at": "2026-11-04", **values}


def verified(book, previous=None):
    events = previous or []
    start = max((e["revision"] for e in events), default=0)
    return apply_reviews(book, events + [event("page_verify", start + i, page=i) for i in (1, 2)])


def compare(old, new):
    return compare_pdfs(old, new, {"pairs": suggest_pdf_pairs(old, new), "absolute_tolerance": "0"})


def test_catalog_is_canonical_stable_and_includes_blank_cells():
    book = workbook()
    rows = review_catalog(book)
    assert len(rows) == 18
    assert len({row["id"] for row in rows}) == 18
    assert all(len(row["id"]) == 20 and row["editable"] for row in rows)
    assert next(row for row in rows if row["page"] == 2 and row["cell"] == "B3")["original_text"] == ""
    applied = apply_reviews(book, [])
    assert [row["id"] for row in review_catalog(applied)] == [row["id"] for row in rows]
    assert all(p["reviewable"] and p["total_cells"] == 9 and p["reviewed_cells"] == 0 for p in applied["pages"])
    assert not any("review_id" in c for s in book["sheets"] for c in s["cells"].values())
    book["review_namespace"] = "job-1:old"
    namespaced = target(book, 1, "A2")
    book["review_namespace"] = "job-1:new"
    assert target(book, 1, "A2") != namespaced
    book["review_namespace"] = "job-2:old"
    assert target(book, 1, "A2") != namespaced
    with pytest.raises(ValueError, match="单元格"):
        apply_reviews(book, [event("cell_verify", target_id=namespaced)])


def test_correction_preserves_original_and_updates_logical_alias_and_numeric_type():
    book = workbook()
    changed = apply_reviews(book, [event("cell_update", 3, target_id=target(book, 2, "B2"), value="1,234.50"),
                                   event("cell_update", 1, target_id=target(book, 2, "A2"), value="0009"),
                                   event("cell_update", 2, target_id=target(book, 2, "C2"), value="0008")])
    physical = changed["sheets"][1]["cells"]
    assert physical["B2"]["value"] == "1234.50" and physical["B2"]["value_type"] == "number"
    assert physical["B2"]["raw_text"] == "30.00" and physical["B2"]["current_text"] == "1,234.50"
    assert physical["B2"]["correction_revision"] == 3 and not physical["B2"]["reviewed"]
    assert physical["A2"]["value"] == "0009" and physical["C2"]["value_type"] == "string"
    logical = next(s for s in changed["sheets"] if s.get("member_sheets"))
    assert logical["name"] == book["sheets"][-1]["name"]
    assert logical["cells"]["B4"]["review_id"] == physical["B2"]["review_id"]
    assert logical["cells"]["B4"]["value"] == "1234.50"
    item = next(item for item in review_catalog(changed) if item["id"] == target(book, 2, "B2"))
    assert item["original_text"] == "30.00" and item["current_text"] == "1,234.50"
    cleared = apply_reviews(book, [event("cell_update", target_id=target(book, 1, "B2"), value="")])
    assert cleared["sheets"][0]["cells"]["B2"]["value"] is None
    assert cleared["sheets"][0]["cells"]["B2"]["value_type"] == "blank"


def test_update_and_unverify_invalidate_page_and_page_unverify_clears_cells():
    book = workbook()
    key = target(book, 1, "B2")
    history = [event("page_verify", page=1), event("cell_update", 2, target_id=key, value="11.00")]
    changed = apply_reviews(book, history)
    assert not changed["pages"][0]["reviewed"]
    assert changed["pages"][0]["reviewed_cells"] == 8
    history.append(event("cell_verify", 3, target_id=key))
    checked = apply_reviews(book, history)
    assert checked["pages"][0]["reviewed_cells"] == 9 and not checked["pages"][0]["reviewed"]
    history.extend([event("page_verify", 4, page=1), event("cell_unverify", 5, target_id=key)])
    assert not apply_reviews(book, history)["pages"][0]["reviewed"]
    history.append(event("page_unverify", 6, page=1))
    assert apply_reviews(book, history)["pages"][0]["reviewed_cells"] == 0


def test_full_page_verification_removes_review_events_without_changing_ocr_method():
    book = workbook()
    book["warnings"].extend(["第 1 页使用本地 OCR；全部识别结果尚未人工核验。",
                             "第 1 页同时包含文本和图像，整页使用 OCR；包括原有文本在内均需核验。",
                             "所选范围之外的内容未比较。"])
    assert compare(book, book)["summary"]["by_kind"] == {"ocr_review": 4}
    checked = verified(book)
    result = compare(checked, checked)
    assert result["summary"]["total"] == 0
    assert all(p["method"] == "ocr" and p["reviewed"] for p in checked["pages"])
    assert all(not p["ocr_unverified"] for p in checked["pages"])
    assert not any("尚未人工核验" in w or "均需核验" in w for w in checked["warnings"])
    assert "所选范围之外的内容未比较。" in checked["warnings"]


def test_only_confirmed_values_keys_and_pages_upgrade_to_definite_changes():
    book = workbook()
    history = [event("cell_update", target_id=target(book, 1, "B2"), value="99.00")]
    edited = apply_reviews(book, history)
    suspected = compare(book, edited)
    assert suspected["summary"]["by_kind"] == {"ocr_suspected": 1, "ocr_review": 4}
    old, new = verified(book), verified(book, history)
    result = compare(old, new)
    assert result["summary"]["by_kind"] == {"value_changed": 1}
    diff = result["diffs"][0]
    assert diff["delta"] == "89" and diff["severity"] == "change"
    assert diff["new_source"]["reviewed"] and diff["new_source"]["review_id"] == target(book, 1, "B2")
    # A stale per-cell key confirmation must not inherit a page's verified status.
    logical = next(s for s in new["sheets"] if s.get("member_sheets"))
    logical["cells"]["A2"]["reviewed"] = False
    assert compare(old, new)["summary"]["by_kind"] == {"ocr_suspected": 1}


def test_structural_changes_wait_for_all_ocr_page_completeness_checks():
    book = workbook()
    changes = [event("cell_update", target_id=target(book, 2, "A3"), value="A005")]
    old_partial = apply_reviews(book, [event("page_verify", page=2)])
    new_partial = apply_reviews(book, changes + [event("page_verify", 2, page=2)])
    assert compare(old_partial, new_partial)["summary"]["by_kind"] == {"ocr_suspected": 2, "ocr_review": 2}
    result = compare(verified(book), verified(book, changes))
    assert result["summary"]["by_kind"] == {"row_removed": 1, "row_added": 1}


def test_unavailable_and_unconfirmed_pages_cannot_be_verified():
    book = workbook()
    book["pages"][0]["status"] = "unavailable"
    with pytest.raises(ValueError, match="不能"):
        apply_reviews(book, [event("page_verify", page=1)])
    book = workbook()
    book["pages"][0]["structure_unconfirmed"] = True
    with pytest.raises(ValueError, match="不能"):
        apply_reviews(book, [event("page_verify", page=1)])
    checked = apply_reviews(book, [event("cell_verify", target_id=target(book, 1, "B2"))])
    assert not checked["pages"][0]["reviewable"]
    assert compare(checked, checked)["summary"]["by_kind"] == {"ocr_review": 4}
    assert any("结构" in d["message"] for d in compare(checked, checked)["diffs"])
    with pytest.raises(ValueError, match="单元格"):
        apply_reviews(book, [event("cell_verify", target_id="missing")])
    with pytest.raises(ValueError, match="页面"):
        apply_reviews(book, [event("page_verify", page=3)])


def test_text_block_synthetic_header_is_not_editable_and_raw_spacing_is_preserved():
    book = workbook()
    source = {"kind": "pdf", "page": 1, "bbox": [0, 0, 1, 1], "method": "ocr"}
    book["sheets"] = [_make_sheet("正文", [[("文本内容", source)], [("Original body", source)]],
                                  structure="text_blocks", page_number=1, method="ocr")]
    rows = review_catalog(book)
    assert len(rows) == 1 and rows[0]["cell"] == "A2"
    changed = apply_reviews(book, [event("cell_update", target_id=rows[0]["id"], value="Corrected body")])
    diff = next(d for d in compare(book, changed)["diffs"] if d["kind"] == "ocr_suspected")
    assert diff["new_value"] == "Corrected body"
