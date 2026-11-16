import copy
import io

import pdfplumber
import pytest
from reportlab.pdfbase import pdfdoc
from reportlab.pdfgen import canvas

from backend.pdf_glyphs import PdfGlyphGeometry


def make_pdf(tmp_path, painter, rotation=0, offset=False):
    path = tmp_path / "glyphs.pdf"
    writer = canvas.Canvas(str(path), pagesize=(600, 800), invariant=1)
    writer.setPageRotation(rotation)
    painter(writer)
    writer.showPage()
    if offset:
        writer._doc.Pages.pages[0].MediaBox = pdfdoc.PDFArray([10, 20, 610, 820])
    writer.save()
    return path


def appearance_chars(page):
    left, top = page.bbox[:2]
    return [{"id": f"c{i}", "text": char["text"], "matrix": list(char["matrix"]),
             "bbox": [(char["x0"] - left) / page.width, (char["top"] - top) / page.height,
                      (char["x1"] - left) / page.width, (char["bottom"] - top) / page.height]}
            for i, char in enumerate(page.chars)]


def test_renderer_bounds_recover_shifted_metrics_without_changing_layout(tmp_path):
    def paint(pdf):
        pdf.setFont("Helvetica", 30)
        pdf.drawString(100, 700, "A")
    path = make_pdf(tmp_path, paint)
    with pdfplumber.open(path) as document, PdfGlyphGeometry(path) as renderer:
        page = document.pages[0]
        chars = appearance_chars(page)
        chars[0]["bbox"][1] += .05
        chars[0]["bbox"][3] += .05
        before = copy.deepcopy(chars)
        result = renderer.resolve(page, chars)["chars"]["c0"]
        assert chars == before
        assert result["status"] == "verified" and result["metric_bbox_conflict"]
        image = page.to_image(resolution=144, antialias=True, force_mediabox=True).original.convert("L")
        mask = image.point(lambda value: 255 if value < 200 else 0)
        pixel_box = mask.getbbox()
        glyph_box = result["glyph_bbox"]
        expected = [glyph_box[i] * (image.width if i % 2 == 0 else image.height) for i in range(4)]
        assert max(abs(a - b) for a, b in zip(pixel_box, expected)) <= 1.5


def test_repeated_characters_match_by_origin_and_wrong_unicode_is_not_guessed(tmp_path):
    def paint(pdf):
        pdf.setFont("Helvetica", 30)
        pdf.drawString(100, 700, "A")
        pdf.drawString(300, 500, "A")
    path = make_pdf(tmp_path, paint)
    with pdfplumber.open(path) as document, PdfGlyphGeometry(path) as renderer:
        chars = appearance_chars(document.pages[0])
        chars[0]["bbox"] = chars[1]["bbox"][:]
        result = renderer.resolve(document.pages[0], chars)["chars"]
        assert result["c0"]["glyph_bbox"][0] < .2
        assert result["c1"]["glyph_bbox"][0] >= .49
        chars[0]["text"] = "B"
        result = renderer.resolve(document.pages[0], chars)["chars"]["c0"]
        assert result["status"] == "unverified" and "glyph_bbox" not in result


def test_ambiguous_overprinted_glyphs_are_not_silently_paired(tmp_path):
    def paint(pdf):
        for size in (20, 40):
            pdf.setFont("Helvetica", size)
            pdf.drawString(100, 700, "A")
    path = make_pdf(tmp_path, paint)
    with pdfplumber.open(path) as document, PdfGlyphGeometry(path) as renderer:
        result = renderer.resolve(document.pages[0], appearance_chars(document.pages[0]))
        assert all(value["status"] == "unverified" for value in result["chars"].values())


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotation_preserves_renderer_to_layout_coordinate_mapping(tmp_path, rotation):
    def paint(pdf):
        pdf.setFont("Helvetica", 24)
        pdf.drawString(100, 200, "A")
    path = make_pdf(tmp_path, paint, rotation=rotation)
    with pdfplumber.open(path) as document, PdfGlyphGeometry(path) as renderer:
        page = document.pages[0]
        result = renderer.resolve(page, appearance_chars(page))["chars"]["c0"]
        assert result["status"] == "verified"
        assert not result["metric_bbox_conflict"]
        image = page.to_image(resolution=144, antialias=True, force_mediabox=True).original.convert("L")
        pixel_box = image.point(lambda value: 255 if value < 200 else 0).getbbox()
        expected = [result["glyph_bbox"][i] * (image.width if i % 2 == 0 else image.height) for i in range(4)]
        assert max(abs(a - b) for a, b in zip(pixel_box, expected)) <= 1.5


def test_stream_position_and_nonzero_media_origin_are_preserved(tmp_path):
    def paint(pdf):
        pdf.setFont("Helvetica", 24)
        pdf.drawString(100, 200, "A")
    path = make_pdf(tmp_path, paint, offset=True)
    stream = io.BytesIO(path.read_bytes())
    stream.seek(17)
    with pdfplumber.open(path) as document, PdfGlyphGeometry(stream) as renderer:
        assert stream.tell() == 17 and not stream.closed
        page = document.pages[0]
        result = renderer.resolve(page, appearance_chars(page))["chars"]["c0"]
        assert result["status"] == "verified" and not result["metric_bbox_conflict"]
        image = page.to_image(resolution=144, antialias=True, force_mediabox=True).original.convert("L")
        pixel_box = image.point(lambda value: 255 if value < 200 else 0).getbbox()
        expected = [result["glyph_bbox"][i] * (image.width if i % 2 == 0 else image.height) for i in range(4)]
        assert max(abs(a - b) for a, b in zip(pixel_box, expected)) <= 1.5
    assert stream.tell() == 17 and not stream.closed
