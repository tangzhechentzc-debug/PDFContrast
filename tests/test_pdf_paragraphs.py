import pdfplumber
import pytest
from reportlab.lib.utils import ImageReader

from test_pdf_appearance import changes, compare, make_book, title


TEXT = (
    "This paragraph explains the project delivery schedule.",
    "The team reviews each stage before approval.",
    "A final inspection completes the work.",
)
PARAGRAPH_KINDS = {
    "paragraph_line_spacing_changed", "paragraph_alignment_changed", "paragraph_measurement_unverified",
}


def paragraph(leading=18, alignment="left", size=12, offset=(0, 0), lines=TEXT,
              inset=20, proportional=False, font="Helvetica"):
    def paint(pdf):
        sx, sy = (pdf._pagesize[0] / 600, pdf._pagesize[1] / 800) if proportional else (1, 1)
        pdf.saveState()
        pdf.scale(sx, sy)
        title(pdf)
        pdf.restoreState()
        dx, dy = offset
        pdf.rect((60 + dx) * sx, (420 + dy) * sy, 480 * sx, 280 * sy, stroke=1, fill=0)
        pdf.setFont(font, size)
        for index, text in enumerate(lines):
            y = (660 + dy - index * leading) * sy
            if alignment == "left":
                pdf.drawString((60 + dx + inset) * sx, y, text)
            elif alignment == "center":
                pdf.drawCentredString((300 + dx) * sx, y, text)
            else:
                pdf.drawRightString((540 + dx - inset) * sx, y, text)
    return paint


def paragraph_events(result, kind=None):
    return [diff for diff in result["diffs"] if diff["kind"] in PARAGRAPH_KINDS
            and (kind is None or diff["kind"] == kind)]


def assert_measurements(diff, old_gaps=None, new_gaps=None, alignments=None):
    assert diff["category"] == "format" and diff["severity"] == "change"
    measurements = diff["measurements"]
    assert measurements["unit"] == "pt"
    for side in ("old", "new"):
        source = diff[f"{side}_source"]
        assert source and source["page"] == 1
        x0, y0, x1, y1 = source["bbox"]
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
        assert measurements[side]["line_count"] == 3
    if old_gaps is not None:
        assert measurements["old"]["baseline_gaps_pt"] == pytest.approx(old_gaps, abs=.15)
        assert measurements["new"]["baseline_gaps_pt"] == pytest.approx(new_gaps, abs=.15)
    if alignments:
        assert (measurements["old"]["alignment"], measurements["new"]["alignment"]) == alignments


def test_native_three_line_spacing_change_has_physical_measurements(tmp_path):
    old = make_book(tmp_path, "paragraph-leading-old", [paragraph(18)])
    new = make_book(tmp_path, "paragraph-leading-new", [paragraph(28)])
    result = compare(old, new)
    spacing = paragraph_events(result, "paragraph_line_spacing_changed")
    assert len(spacing) == 1
    assert_measurements(spacing[0], [18, 18], [28, 28])
    assert not changes(result, "content")


def test_paragraph_and_frame_translation_only_changes_layout(tmp_path):
    old = make_book(tmp_path, "paragraph-move-old", [paragraph()])
    new = make_book(tmp_path, "paragraph-move-new", [paragraph(offset=(25, -50))])
    result = compare(old, new)
    assert changes(result, "layout")
    assert not paragraph_events(result, "paragraph_line_spacing_changed")
    assert not paragraph_events(result, "paragraph_alignment_changed")
    assert not changes(result, "content")


def test_proportional_paper_size_change_reports_measured_spacing_with_context(tmp_path):
    old = make_book(tmp_path, "paragraph-paper-old", [paragraph(proportional=True)])
    new = make_book(tmp_path, "paragraph-paper-new", [paragraph(proportional=True)], page_size=(720, 960))
    result = compare(old, new)
    assert changes(result, "layout")
    spacing = paragraph_events(result, "paragraph_line_spacing_changed")
    assert len(spacing) == 1
    assert_measurements(spacing[0], [18, 18], [21.6, 21.6])
    assert spacing[0]["measurements"]["context"]["page_size_changed"] is True
    assert not changes(result, "content")


def test_paper_size_change_without_coordinate_change_preserves_spacing(tmp_path):
    old = make_book(tmp_path, "paragraph-paper-only-old", [paragraph()])
    new = make_book(tmp_path, "paragraph-paper-only-new", [paragraph()], page_size=(720, 960))
    result = compare(old, new)
    assert changes(result, "layout")
    assert not paragraph_events(result, "paragraph_line_spacing_changed")
    assert not changes(result, "content")


def test_font_size_change_reports_measured_spacing_with_context(tmp_path):
    old = make_book(tmp_path, "paragraph-font-old", [paragraph(18, size=12)])
    new = make_book(tmp_path, "paragraph-font-new", [paragraph(24, size=16)])
    result = compare(old, new)
    assert changes(result, "format")
    spacing = paragraph_events(result, "paragraph_line_spacing_changed")
    assert len(spacing) == 1
    assert_measurements(spacing[0], [18, 18], [24, 24])
    assert spacing[0]["measurements"]["context"]["font_size_changed"] is True
    assert not changes(result, "content")


@pytest.mark.parametrize("old_alignment,new_alignment", [("left", "center"), ("left", "right"), ("center", "right")])
def test_multiline_alignment_change_in_fixed_frame_has_measurements(tmp_path, old_alignment, new_alignment):
    old = make_book(tmp_path, "paragraph-align-old", [paragraph(alignment=old_alignment)])
    new = make_book(tmp_path, "paragraph-align-new", [paragraph(alignment=new_alignment)])
    result = compare(old, new)
    alignment = paragraph_events(result, "paragraph_alignment_changed")
    assert len(alignment) == 1
    assert_measurements(alignment[0], alignments=(old_alignment, new_alignment))
    assert not paragraph_events(result, "paragraph_line_spacing_changed")
    assert not changes(result, "content")


def _has_unverified_count(value, path=""):
    if isinstance(value, dict):
        return any(_has_unverified_count(child, f"{path}.{key}") for key, child in value.items())
    return isinstance(value, int) and value > 0 and any(word in path for word in ("unverified", "ambiguous", "unsupported"))


@pytest.mark.parametrize("lines", [("A short paragraph line",), ("Equal width text",) * 3], ids=["single-line", "equal-width"])
def test_unknown_inset_movement_does_not_claim_certain_alignment(tmp_path, lines):
    old = make_book(tmp_path, "paragraph-ambiguous-old", [paragraph(lines=lines, inset=20)])
    new = make_book(tmp_path, "paragraph-ambiguous-new", [paragraph(lines=lines, inset=50)])
    result = compare(old, new)
    assert not [diff for diff in paragraph_events(result, "paragraph_alignment_changed") if diff["severity"] == "change"]
    issues = paragraph_events(result, "paragraph_measurement_unverified")
    assert any(diff["severity"] == "issue" for diff in issues) or _has_unverified_count(result.get("paragraph_coverage", {}))


def test_single_line_center_to_flush_right_has_explicit_frame_evidence(tmp_path):
    lines = ("One short line",)
    old = make_book(tmp_path, "paragraph-single-center", [paragraph(lines=lines, alignment="center", inset=0)])
    new = make_book(tmp_path, "paragraph-single-right", [paragraph(lines=lines, alignment="right", inset=0)])
    alignment = paragraph_events(compare(old, new), "paragraph_alignment_changed")
    assert len(alignment) == 1 and alignment[0]["severity"] == "change"
    assert alignment[0]["measurements"]["old"]["alignment"] == "center"
    assert alignment[0]["measurements"]["new"]["alignment"] == "right"


def test_single_line_table_fields_are_not_combined_into_a_paragraph(tmp_path):
    def table(gap):
        def paint(pdf):
            title(pdf)
            top = 700
            for x in (60, 240, 420, 540):
                pdf.line(x, top - 3 * gap, x, top)
            for row in range(4):
                pdf.line(60, top - row * gap, 540, top - row * gap)
            pdf.setFont("Helvetica", 12)
            for row, values in enumerate((("Name", "Unit", "Total"), ("Concrete", "20", "400"), ("Steel", "30", "900"))):
                for x, value in zip((70, 250, 430), values):
                    pdf.drawString(x, top - row * gap - 20, value)
        return paint
    old = make_book(tmp_path, "paragraph-table-old", [table(45)])
    new = make_book(tmp_path, "paragraph-table-new", [table(60)])
    result = compare(old, new)
    assert not paragraph_events(result, "paragraph_line_spacing_changed")
    assert not paragraph_events(result, "paragraph_alignment_changed")


def test_multiline_table_cell_keeps_spacing_evidence_separate_from_neighbor(tmp_path):
    def table(leading):
        def paint(pdf):
            title(pdf)
            pdf.rect(60, 400, 480, 300, stroke=1, fill=0)
            pdf.line(300, 400, 300, 700)
            pdf.line(60, 550, 540, 550)
            pdf.setFont("Helvetica", 12)
            for index, text in enumerate(("The first cell explains work.", "Its scope requires approval.", "Inspection follows delivery.")):
                pdf.drawString(75, 675 - index * leading, text)
            for index, text in enumerate(("The second cell is unchanged.", "It describes another stage.", "This block remains fixed.")):
                pdf.drawString(315, 675 - index * 18, text)
        return paint
    old = make_book(tmp_path, "paragraph-cell-old", [table(18)])
    new = make_book(tmp_path, "paragraph-cell-new", [table(28)])
    spacing = paragraph_events(compare(old, new), "paragraph_line_spacing_changed")
    assert len(spacing) == 1
    assert_measurements(spacing[0], [18, 18], [28, 28])
    for side in ("old", "new"):
        assert spacing[0][f"{side}_source"]["bbox"][2] <= .5


def scanned_paragraph_book(tmp_path, name, leading):
    native = make_book(tmp_path, name + "-native", [paragraph(leading)])
    with pdfplumber.open(tmp_path / f"{name}-native.pdf") as document:
        image = document.pages[0].to_image(resolution=96, antialias=True, force_mediabox=True).original.copy()
    def paint(pdf):
        pdf.drawImage(ImageReader(image), 0, 0, width=600, height=800)
    book = make_book(tmp_path, name, [paint])
    lines = [line for line in native["pages"][0]["appearance"]["lines"] if line["text"] in TEXT]
    assert len(lines) == 3
    cells = {}
    for index, line in enumerate(lines, 2):
        address = f"A{index}"
        cells[address] = {"address": address, "row": index, "column": 1, "raw_text": line["text"],
                          "display": line["text"], "current_text": line["text"], "reviewed": False,
                          "pdf_source": {"kind": "pdf", "page": 1, "bbox": line["bbox"], "method": "ocr"}}
    book["sheets"] = [{"name": "OCR page", "page_number": 1, "structure": "text_blocks", "cells": cells}]
    book["pages"][0].update(method="ocr", reviewed=False, ocr_unverified=True)
    return book


def test_ocr_paragraph_measurement_is_an_issue_not_native_format_fact(tmp_path):
    old = scanned_paragraph_book(tmp_path, "paragraph-ocr-old", 18)
    new = scanned_paragraph_book(tmp_path, "paragraph-ocr-new", 28)
    result = compare(old, new)
    assert not [diff for diff in paragraph_events(result) if diff["severity"] == "change"]
    assert any(diff["severity"] == "issue" for diff in paragraph_events(result, "paragraph_measurement_unverified"))


def test_missing_glyph_makes_paragraph_measurement_unverified(tmp_path):
    from scripts.make_pdf_examples import register_font
    font, _ = register_font(None)
    lines = (TEXT[0].replace("project", "pro\ufffdject"), *TEXT[1:])
    old = make_book(tmp_path, "paragraph-bad-glyph-old", [paragraph(18, lines=lines, font=font)])
    new = make_book(tmp_path, "paragraph-bad-glyph-new", [paragraph(28, lines=lines, font=font)])
    assert any(char["bad_unicode"] for char in old["pages"][0]["appearance"]["chars"])
    result = compare(old, new)
    assert not [diff for diff in paragraph_events(result) if diff["severity"] == "change"]
    assert any(diff["severity"] == "issue" for diff in paragraph_events(result, "paragraph_measurement_unverified"))


def test_unchanged_native_paragraph_has_no_paragraph_events(tmp_path):
    old = make_book(tmp_path, "paragraph-identical-old", [paragraph()])
    new = make_book(tmp_path, "paragraph-identical-new", [paragraph()])
    result = compare(old, new)
    assert not paragraph_events(result)
    assert not changes(result)


@pytest.mark.parametrize("lines", [
    ("Prepared by: Project team", "Reviewed by: Quality manager", "Date: 2026-11-05"),
    ("1. Check the material quantities.", "2. Confirm the delivery schedule.", "3. Approve the final inspection."),
], ids=["separate-fields", "separate-numbered-items"])
def test_spacing_between_independent_fields_or_list_items_is_not_paragraph_leading(tmp_path, lines):
    old = make_book(tmp_path, "paragraph-separate-old", [paragraph(18, lines=lines)])
    new = make_book(tmp_path, "paragraph-separate-new", [paragraph(28, lines=lines)])
    result = compare(old, new)
    assert not paragraph_events(result, "paragraph_line_spacing_changed")
    assert not changes(result, "content")
