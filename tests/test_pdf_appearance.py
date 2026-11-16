import copy

import pdfplumber
import pytest
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfdoc
from reportlab.pdfgen import canvas

from backend.pdf_appearance import compare_report_appearance, extract_page_appearance


PAGE_SIZE = (600, 800)
CATEGORIES = {"content", "structure", "format", "layout"}


def make_book(tmp_path, name, painters, page_size=PAGE_SIZE, page_attributes=None):
    path = tmp_path / f"{name}.pdf"
    pdf = canvas.Canvas(str(path), pagesize=page_size, invariant=1, pdfVersion=(1, 7))
    for painter in painters:
        painter(pdf)
        pdf.showPage()
    for page in pdf._doc.Pages.pages:
        for key, value in (page_attributes or {}).items():
            setattr(page, key, value)
    pdf.save()
    pages = []
    with pdfplumber.open(path) as document:
        for number, page in enumerate(document.pages, 1):
            page_image = page.to_image(resolution=96, antialias=True, force_mediabox=True)
            pages.append({"number": number, "appearance": extract_page_appearance(page, page_image)})
    report = {
        "id": name,
        "title": "A.1 Project report",
        "type_code": "A.1",
        "pages": list(range(1, len(pages) + 1)),
        "comparison_regions": [{"page": page["number"], "bbox": [0, 0, 1, 1]} for page in pages],
    }
    return {"pages": pages, "sheets": [], "reports": [report]}


def compare(old, new, **entry_fields):
    entry = {"id": "pair", "title": "A.1 Project report", "confirmation": "confirmed",
             "old": old["reports"][0], "new": new["reports"][0]}
    entry.update(entry_fields)
    result = compare_report_appearance(old, new, entry)
    assert set(result["dimensions"]) == CATEGORIES
    for dimension in result["dimensions"].values():
        assert dimension["status"] in {"changed", "unchanged", "partial", "unverified", "not_applicable"}
        assert isinstance(dimension["changes"], int)
        assert isinstance(dimension["issues"], int)
        assert isinstance(dimension["summary"], str)
        assert isinstance(dimension["limitations"], list)
    for pair in result["page_pairs"]:
        assert pair["status"] in {"confirmed", "pending"}
        assert "old_page" in pair and "new_page" in pair and pair["basis"]
    for diff in result["diffs"]:
        assert {"kind", "id", "table_id", "table_name", "row_key", "field", "old_value", "new_value",
                "delta", "message", "severity", "old_source", "new_source"} <= diff.keys()
        assert diff["severity"] in {"change", "issue"}
        assert diff["category"] in CATEGORIES
        assert diff["layer"] == ("uncertainty" if diff["severity"] == "issue" else diff["category"])
        assert diff["report_id"] == "pair"
        assert diff["old_value"] is None or isinstance(diff["old_value"], str)
        assert diff["new_value"] is None or isinstance(diff["new_value"], str)
        for side in ("old", "new"):
            source = diff[f"{side}_source"]
            if source is not None:
                assert source["page"] in (old if side == "old" else new)["reports"][0]["pages"]
                x0, y0, x1, y1 = source["bbox"]
                assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    return result


def changes(result, category=None):
    return [diff for diff in result["diffs"] if diff["severity"] == "change"
            and (category is None or diff["category"] == category)]


def title(pdf):
    pdf.setFont("Helvetica", 14)
    pdf.drawString(40, 750, "A.1 Project report")


def text_page(text, font="Helvetica", size=12, color=(0, 0, 0), spacing=0, wrapped=False):
    def paint(pdf):
        title(pdf)
        pdf.setFillColorRGB(*color)
        body = pdf.beginText(40, 680)
        body.setFont(font, size)
        body.setCharSpace(spacing)
        if wrapped:
            first, second = text.split(" ", 1)
            body.textLine(first)
            body.textLine(second)
        else:
            body.textLine(text)
        pdf.drawText(body)
    return paint


def form_page(filled=False, line=True, box=True):
    def paint(pdf):
        title(pdf)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(40, 680, "Project:")
        pdf.drawString(40, 620, "Date:")
        if line:
            pdf.line(120, 675, 480, 675)
            pdf.line(120, 615, 480, 615)
        if box:
            pdf.rect(40, 400, 440, 120, fill=0, stroke=1)
        if filled:
            pdf.drawString(130, 680, "River bridge renewal")
            pdf.drawString(130, 620, "2026-11-05")
    return paint


def test_identical_native_report_has_no_changes(tmp_path):
    old = make_book(tmp_path, "same-old", [form_page(filled=True)])
    new = make_book(tmp_path, "same-new", [form_page(filled=True)])
    snapshot = copy.deepcopy((old, new))
    result = compare(old, new)
    assert not changes(result)
    assert all(dimension["status"] in {"unchanged", "not_applicable"}
               for dimension in result["dimensions"].values())
    assert (old, new) == snapshot


def test_non_grid_template_compares_with_completed_form(tmp_path):
    old = make_book(tmp_path, "blank-form", [form_page()])
    new = make_book(tmp_path, "completed-form", [form_page(filled=True)])
    result = compare(old, new)
    content = changes(result, "content")
    assert content
    values = " ".join(diff["new_value"] or "" for diff in content)
    assert "River" in values and "2026-11-05" in values
    assert not changes(result, "structure")


@pytest.mark.parametrize("old_text,new_text", [("Total: 100.00", "Total: 100,00"),
                                                  ("Code A-1", "Code A1")])
def test_punctuation_changes_are_not_normalized_away(tmp_path, old_text, new_text):
    old = make_book(tmp_path, "punctuation-old", [text_page(old_text)])
    new = make_book(tmp_path, "punctuation-new", [text_page(new_text)])
    assert changes(compare(old, new), "content")


@pytest.mark.parametrize("kwargs", [{"font": "Courier"}, {"size": 17}, {"color": (0.8, 0, 0)}])
def test_same_text_with_font_size_or_color_change_has_format_evidence(tmp_path, kwargs):
    old = make_book(tmp_path, "format-old", [text_page("Project Alpha total 1200")])
    new = make_book(tmp_path, "format-new", [text_page("Project Alpha total 1200", **kwargs)])
    result = compare(old, new)
    assert changes(result, "format")
    assert not changes(result, "content")


@pytest.mark.parametrize("kwargs", [{"spacing": 1.3}, {"wrapped": True}])
def test_spacing_and_line_break_changes_are_not_content_changes(tmp_path, kwargs):
    old = make_book(tmp_path, "spacing-old", [text_page("Project Alpha renewal")])
    new = make_book(tmp_path, "spacing-new", [text_page("Project Alpha renewal", **kwargs)])
    result = compare(old, new)
    assert changes(result, "format") or changes(result, "layout")
    assert not changes(result, "content")


@pytest.mark.parametrize("new_size", [(720, 960), (800, 600)])
def test_page_size_or_orientation_change_is_summarized(tmp_path, new_size):
    def proportional(pdf):
        width, height = pdf._pagesize
        pdf.setFont("Helvetica", 12)
        for row in range(15):
            pdf.drawString(width * .1, height * (.88 - row * .045), f"Project item {row:02d} amount 1200")
    old = make_book(tmp_path, "size-old", [proportional])
    new = make_book(tmp_path, "size-new", [proportional], page_size=new_size)
    result = compare(old, new)
    assert changes(result, "layout")
    assert not changes(result, "content")
    assert len(changes(result, "layout")) <= 3


@pytest.mark.parametrize("kwargs", [{"line": False}, {"box": False}])
def test_empty_frame_and_fill_in_line_removal_are_structural(tmp_path, kwargs):
    old = make_book(tmp_path, "geometry-old", [form_page()])
    new = make_book(tmp_path, "geometry-new", [form_page(**kwargs)])
    result = compare(old, new)
    assert changes(result, "structure")
    assert not changes(result, "content")


def test_native_merged_header_geometry_retains_source_regions(tmp_path):
    def merged_table(pdf):
        title(pdf)
        pdf.setFont("Helvetica", 12)
        pdf.rect(40, 460, 480, 240, stroke=1, fill=0)
        for y in (520, 580, 640):
            pdf.line(40, y, 520, y)
        pdf.line(200, 460, 200, 700)
        pdf.line(360, 460, 360, 640)
        for x, y, text in ((50, 665, "Item"), (210, 665, "Costs"),
                           (50, 605, "Code"), (210, 605, "Unit"), (370, 605, "Total"),
                           (50, 545, "A001"), (210, 545, "100"), (370, 545, "200"),
                           (50, 485, "A002"), (210, 485, "300"), (370, 485, "600")):
            pdf.drawString(x, y, text)
    book = make_book(tmp_path, "merged-header", [merged_table])
    appearance = book["pages"][0]["appearance"]
    assert appearance["tables"] and appearance["tables"][0]["merged_ranges"]
    assert appearance["cells"]
    for cell in appearance["cells"]:
        x0, y0, x1, y1 = cell["bbox"]
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
    assert any(cell.get("column_span", 1) == 2 for cell in appearance["cells"])


def test_reordered_distinct_pages_are_matched_by_content(tmp_path):
    alpha = text_page("Bridge foundation concrete quantities Alpha 100")
    beta = text_page("Road lighting electrical quantities Beta 900")
    old = make_book(tmp_path, "order-old", [alpha, beta])
    new = make_book(tmp_path, "order-new", [beta, alpha])
    result = compare(old, new)
    assert {(pair["old_page"], pair["new_page"]) for pair in result["page_pairs"]
            if pair["status"] == "confirmed"} == {(1, 2), (2, 1)}
    assert not changes(result, "content")


@pytest.mark.parametrize("paint", [lambda pdf: None, title], ids=["blank", "repeated-title"])
def test_duplicate_pages_do_not_get_spurious_confirmed_correspondence(tmp_path, paint):
    old = make_book(tmp_path, "duplicate-old", [paint, paint])
    new = make_book(tmp_path, "duplicate-new", [paint, paint])
    result = compare(old, new)
    assert result["page_pairs"]
    assert all(pair["status"] == "pending" for pair in result["page_pairs"])
    assert not changes(result)


def test_scanned_visual_change_has_region_evidence_without_invented_font_claims(tmp_path):
    def scanned(extra):
        def paint(pdf):
            image = Image.new("RGB", (500, 600), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((35, 50, 460, 550), outline="black", width=3)
            draw.rectangle((60, 95, 360, 125), fill="black")
            if extra:
                draw.rectangle((60, 220, 330, 270), fill="black")
            pdf.drawImage(ImageReader(image), 40, 100, width=500, height=600)
        return paint
    old = make_book(tmp_path, "scan-old", [scanned(False)])
    new = make_book(tmp_path, "scan-new", [scanned(True)])
    result = compare(old, new)
    assert not changes(result, "format")
    assert result["dimensions"]["format"]["status"] in {"partial", "unverified", "not_applicable"}
    evidence = [diff for diff in result["diffs"] if diff["severity"] == "issue"
                and diff["old_source"] and diff["new_source"]]
    assert evidence
    assert any(diff["old_source"]["bbox"] != [0, 0, 1, 1]
               or diff["new_source"]["bbox"] != [0, 0, 1, 1] for diff in evidence)


@pytest.mark.parametrize("mode", ["moved-pixels", "equal-gray-color"])
def test_mixed_native_and_image_change_has_visual_evidence(tmp_path, mode):
    def mixed(new):
        def paint(pdf):
            title(pdf)
            if mode == "equal-gray-color":
                colors = [(255, 0, 0), (0, 130, 0)]
                assert Image.new("RGB", (1, 1), colors[0]).convert("L").getpixel((0, 0)) == \
                    Image.new("RGB", (1, 1), colors[1]).convert("L").getpixel((0, 0))
                image = Image.new("RGB", (300, 160), colors[int(new)])
            else:
                image = Image.new("RGB", (300, 160), "white")
                x = 160 if new else 20
                ImageDraw.Draw(image).rectangle((x, 30, x + 80, 100), fill="black")
            pdf.drawImage(ImageReader(image), 80, 400, width=300, height=160)
        return paint
    old = make_book(tmp_path, "mixed-old", [mixed(False)])
    new = make_book(tmp_path, "mixed-new", [mixed(True)])
    result = compare(old, new)
    assert any(diff["kind"] == "visual_region_changed" and diff["old_source"] and diff["new_source"]
               for diff in result["diffs"])
    assert not changes(result, "content")


@pytest.mark.parametrize("line", [(120, 675, 400, 675), (120, 655, 480, 655)], ids=["length", "position"])
def test_same_count_fill_in_lines_preserve_geometry_changes(tmp_path, line):
    def paint(coords):
        def draw(pdf):
            title(pdf)
            pdf.line(*coords)
        return draw
    old = make_book(tmp_path, "line-old", [paint((120, 675, 480, 675))])
    new = make_book(tmp_path, "line-new", [paint(line)])
    assert changes(compare(old, new), "layout")


@pytest.mark.parametrize("mode", ["linewidth", "fill"])
def test_swapped_local_styles_are_detected_even_when_global_style_sets_match(tmp_path, mode):
    def paint(swapped):
        def draw(pdf):
            title(pdf)
            styles = [1, 4] if mode == "linewidth" else [(1, 0, 0), (0, 0, 1)]
            if swapped:
                styles.reverse()
            for y, style in zip((580, 400), styles):
                if mode == "linewidth":
                    pdf.setLineWidth(style)
                    pdf.line(60, y, 400, y)
                else:
                    pdf.setFillColorRGB(*style)
                    pdf.rect(60, y, 340, 80, stroke=0, fill=1)
        return draw
    old = make_book(tmp_path, "local-style-old", [paint(False)])
    new = make_book(tmp_path, "local-style-new", [paint(True)])
    assert changes(compare(old, new), "format")


def test_fill_only_rectangle_is_not_a_stroked_frame(tmp_path):
    def paint(pdf):
        title(pdf)
        pdf.setFillColorRGB(.9, .8, .7)
        pdf.rect(50, 450, 400, 100, fill=1, stroke=0)
    book = make_book(tmp_path, "fill-only", [paint])
    appearance = book["pages"][0]["appearance"]
    assert appearance["vectors"]
    assert not appearance["strokes"] and not appearance["frames"]


def test_default_black_and_explicit_black_do_not_create_false_format_changes(tmp_path):
    def paint(explicit):
        def draw(pdf):
            if explicit:
                pdf.setFillColorRGB(0, 0, 0)
                pdf.setStrokeColorRGB(0, 0, 0)
            title(pdf)
            pdf.line(40, 650, 500, 650)
        return draw
    old = make_book(tmp_path, "default-black", [paint(False)])
    new = make_book(tmp_path, "explicit-black", [paint(True)])
    assert not changes(compare(old, new))


@pytest.mark.parametrize("language", ["cjk", "ascii"])
def test_explicit_extra_spaces_are_formatting_evidence(tmp_path, language):
    if language == "cjk":
        from scripts.make_pdf_examples import register_font
        font, _ = register_font(None)
        before, after = "工程项目名称", "工 程 项 目 名 称"
    else:
        font = "Helvetica"
        before, after = "Project Alpha renewal", "Project  Alpha  renewal"
    old = make_book(tmp_path, "spaces-old", [text_page(before, font=font)])
    new = make_book(tmp_path, "spaces-new", [text_page(after, font=font)])
    result = compare(old, new)
    assert changes(result, "format")
    assert not changes(result, "content")


def test_native_text_rotation_does_not_invent_content_changes(tmp_path):
    def paint(angle):
        def draw(pdf):
            title(pdf)
            pdf.saveState()
            pdf.translate(300, 300)
            pdf.rotate(angle)
            pdf.setFont("Helvetica", 12)
            pdf.drawString(0, 0, "Project Alpha amount 1200")
            pdf.restoreState()
        return draw
    old = make_book(tmp_path, "rotation-old", [paint(0)])
    new = make_book(tmp_path, "rotation-new", [paint(90)])
    result = compare(old, new)
    assert not changes(result, "content")
    assert any(diff["kind"] == "content_rotation_changed" for diff in changes(result, "layout"))


@pytest.mark.parametrize("ocr_status", ["empty", "failed"])
def test_unreadable_ocr_region_is_not_treated_as_verified_empty_text(tmp_path, ocr_status):
    def unreadable(pdf):
        title(pdf)
        image = Image.new("RGB", (330, 64), "white")
        ImageDraw.Draw(image).rectangle((10, 10, 200, 35), fill="black")
        pdf.drawImage(ImageReader(image), 30, 640, width=330, height=64)
    old = make_book(tmp_path, "unreadable-old", [unreadable])
    new = make_book(tmp_path, "readable-new", [text_page("Amount 200")])
    old["pages"][0].update(method="ocr", ocr_unverified=True, ocr_regions=[{
        "id": "p1-ocr-1", "bbox": [.05, .12, .6, .2], "reason": "image",
        "status": ocr_status, "ink_pixels": 100, "affects_content": True, "text_count": 0,
    }])
    result = compare(old, new)
    assert not changes(result, "content")
    assert any(diff["category"] == "content" and diff["severity"] == "issue" for diff in result["diffs"])


def test_cropbox_change_is_reported_as_layout(tmp_path):
    old = make_book(tmp_path, "crop-old", [text_page("Project Alpha")])
    new = make_book(tmp_path, "crop-new", [text_page("Project Alpha")],
                    page_attributes={"CropBox": pdfdoc.PDFArray([20, 20, 580, 780])})
    result = compare(old, new)
    assert changes(result, "layout")
    assert not changes(result, "content")


def test_content_exclusion_does_not_hide_original_format_change(tmp_path):
    old = make_book(tmp_path, "excluded-old", [text_page("Amount 100")])
    new = make_book(tmp_path, "excluded-new", [text_page("Amount 200", color=(1, 0, 0))])
    exclusions = {side: [{"page": 1, "bbox": [.05, .12, .9, .2]}] for side in ("old", "new")}
    result = compare(old, new, content_exclusions=exclusions)
    assert not changes(result, "content")
    assert changes(result, "format")


def test_userunit_is_applied_to_physical_page_and_font_size(tmp_path, monkeypatch):
    monkeypatch.setattr(pdfdoc.PDFPage, "__NoDefault__", pdfdoc.PDFPage.__NoDefault__ + ["UserUnit"])
    old = make_book(tmp_path, "unit-old", [text_page("Project Alpha")])
    new = make_book(tmp_path, "unit-new", [text_page("Project Alpha")], page_attributes={"UserUnit": 2})
    appearance = new["pages"][0]["appearance"]
    assert appearance["width_pt"] == 1200 and appearance["height_pt"] == 1600
    assert appearance["user_unit"] == 2
    assert any(char["size_pt"] == 24 for char in appearance["chars"])
    result = compare(old, new)
    assert changes(result, "layout")
    assert not changes(result, "content")


@pytest.mark.parametrize("page_reviewed", [True, False], ids=["confirmed", "pending-review"])
def test_reviewed_current_text_affects_content_without_changing_original_font(tmp_path, page_reviewed):
    old = make_book(tmp_path, "review-old", [text_page("Amount 100")])
    new = copy.deepcopy(old)
    source = {"kind": "pdf", "page": 1, "bbox": [.05, .12, .4, .19], "method": "text"}
    cell = {"address": "A1", "row": 1, "column": 1, "raw_text": "Amount 100",
            "display": "Amount 100", "value": "Amount 100", "current_text": "Amount 100",
            "pdf_source": source}
    old["sheets"] = [{"name": "Page 1", "page_number": 1, "cells": {"A1": copy.deepcopy(cell)}}]
    cell.update(current_text="Amount 200", display="Amount 200", value="Amount 200",
                correction_revision=1, reviewed=True)
    new["sheets"] = [{"name": "Page 1", "page_number": 1, "cells": {"A1": cell}}]
    new["pages"][0]["reviewed"] = page_reviewed
    result = compare(old, new)
    content = [diff for diff in result["diffs"] if diff["category"] == "content" and diff["new_value"]]
    assert content and any("2" in diff["new_value"] for diff in content)
    assert all(diff["severity"] == ("change" if page_reviewed else "issue") for diff in content)
    assert not changes(result, "format")


def test_amount_change_does_not_hide_other_lines_double_space_change(tmp_path):
    def paint(new):
        def draw(pdf):
            title(pdf)
            pdf.setFont("Helvetica", 12)
            pdf.drawString(40, 680, "Amount 200" if new else "Amount 100")
            pdf.drawString(40, 500, "Project  Alpha" if new else "Project Alpha")
        return draw
    old = make_book(tmp_path, "independent-spacing-old", [paint(False)])
    new = make_book(tmp_path, "independent-spacing-new", [paint(True)])
    result = compare(old, new)
    assert changes(result, "content")
    assert changes(result, "format")


def test_smaller_independent_frame_movement_is_not_hidden_by_largest_frame(tmp_path):
    def paint(new):
        def draw(pdf):
            title(pdf)
            pdf.rect(40, 400, 480, 200, fill=0, stroke=1)
            pdf.rect(100 if new else 40, 200, 120, 50, fill=0, stroke=1)
        return draw
    old = make_book(tmp_path, "small-frame-old", [paint(False)])
    new = make_book(tmp_path, "small-frame-new", [paint(True)])
    assert changes(compare(old, new), "layout")


def test_equal_amount_and_footer_numbers_keep_separate_physical_line_evidence(tmp_path):
    def paint(value):
        def draw(pdf):
            title(pdf)
            pdf.setFont("Helvetica", 12)
            pdf.drawString(40, 680, f"Amount {value}")
            pdf.drawString(40, 50, value)
        return draw
    old = make_book(tmp_path, "physical-lines-old", [paint("10")])
    new = make_book(tmp_path, "physical-lines-new", [paint("20")])
    content = changes(compare(old, new), "content")
    assert len(content) == 2
    for diff in content:
        for side in ("old", "new"):
            source = diff[f"{side}_source"]
            assert source and source["bbox"][3] - source["bbox"][1] < .1
