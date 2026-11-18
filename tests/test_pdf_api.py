import hashlib
from io import BytesIO

import pdfplumber
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from reportlab.pdfgen import canvas
from scripts.make_pdf_examples import register_font

from backend.app import create_app, encode, public_pdf_pages


def text_pdf(amount="12000", password=None):
    stream = BytesIO()
    page = canvas.Canvas(stream, pagesize=(400, 300), encrypt=password)
    font, _ = register_font(None)
    page.setFont(font, 9)
    page.drawString(30, 286, "A.1 项目费用明细报表")
    page.drawString(30, 272, "工程名称：测试工程")
    for x in (30, 140, 260, 370):
        page.line(x, 180, x, 260)
    for y in (180, 220, 260):
        page.line(30, y, 370, y)
    for x, value in zip((40, 150, 270), ("编号", "名称", "金额")):
        page.drawString(x, 235, value)
    for x, value in zip((40, 150, 270), ("A001", "Equipment", amount)):
        page.drawString(x, 195, value)
    page.save()
    return stream.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as test_client:
        yield test_client


def upload_pdf(client, old, new):
    result = client.post("/api/jobs", files={"old_file": ("old.pdf", old, "application/pdf"),
                                            "new_file": ("new.pdf", new, "application/pdf")})
    assert result.status_code == 201, result.text
    return result.json()


def compare_pdf(client, job):
    config = {"pairs": job["suggested_pairs"], "compare_formulas": False, "compare_formatting": False}
    result = client.post(f"/api/jobs/{job['id']}/runs", json=config)
    assert result.status_code == 201, result.text
    return result.json()


def test_pdf_upload_compare_page_highlight_export_and_original(client):
    old, new = text_pdf(), text_pdf("12800")
    job = upload_pdf(client, old, new)
    assert job["kind"] == "pdf"
    assert job["files"]["old"]["sha256"] == hashlib.sha256(old).hexdigest()
    assert len(job["files"]["old"]["pages"]) == 1
    refreshed_job = client.get(f"/api/jobs/{job['id']}").json()
    for metadata in (job, refreshed_job):
        public_page = metadata["files"]["old"]["pages"][0]
        assert public_page["has_native_text"] is True
        assert all("cell_geometry" not in sheet for sheet in metadata["files"]["old"]["sheets"])
        assert not {"appearance", "native_lines", "native_text", "evidence_lines", "appearance_path"} & public_page.keys()
    original_book = client.app.state.store.workbook(job["id"], "old")
    assert original_book["pages"][0]["native_lines"]
    assert any(sheet.get("cell_geometry") for sheet in original_book["sheets"])
    run = compare_pdf(client, job)
    assert run["review_pages"]["old"][0]["has_native_text"] is True
    assert "native_lines" not in run["review_pages"]["old"][0]
    diffs = client.get(f"/api/runs/{run['id']}/diffs").json()["items"]
    amount = next(d for d in diffs if d["old_value"] == "12000" and d["new_value"] == "12800")
    assert amount["delta"] == "800"
    for side in ("old", "new"):
        source = amount[f"{side}_source"]
        assert source["kind"] == "pdf" and source["page"] == 1
        x0, y0, x1, y1 = source["bbox"]
        assert 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1
        assert x0 <= 270 / 400 <= x1 and y0 <= 105 / 300 <= y1
        response = client.get(f"/api/jobs/{job['id']}/pages/{side}/1.png")
        assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
    assert client.get(f"/api/jobs/{job['id']}/files/old").content == old
    assert client.get(f"/api/jobs/{job['id']}/files/new").content == new
    assert client.get(f"/api/jobs/{job['id']}/pages/old/0.png").status_code == 404
    assert client.get(f"/api/jobs/{job['id']}/pages/new/2.png").status_code == 404
    report = load_workbook(BytesIO(client.get(f"/api/runs/{run['id']}/export").content))
    assert any("第 1 页" in str(c.value) for row in report["全部差异"] for c in row)


def test_identical_text_pdf_has_no_content_differences(client):
    book = text_pdf()
    run = compare_pdf(client, upload_pdf(client, book, book))
    assert run["summary"]["changes"] == 0


def test_high_resolution_page_preserves_original_and_location_geometry(client):
    from PIL import Image

    original = text_pdf()
    job = upload_pdf(client, original, original)
    route = f"/api/jobs/{job['id']}/pages/old/1.png"
    standard = client.get(route).content
    high = client.get(route + "?dpi=288")
    assert high.status_code == 200
    with Image.open(BytesIO(standard)) as small, Image.open(BytesIO(high.content)) as large:
        assert large.size == (small.width * 2, small.height * 2)
        # Pixel geometry scales uniformly, so normalized highlight coordinates stay valid.
        assert large.width / large.height == small.width / small.height
    assert client.get(route + "?dpi=288").content == high.content
    assert client.get(route).content == standard
    assert client.get(f"/api/jobs/{job['id']}/files/old").content == original
    assert client.get(route + "?dpi=9999").status_code == 422
    assert client.get(f"/api/jobs/{job['id']}/pages/old/2.png?dpi=288").status_code == 404


def test_pdf_rejects_mixed_invalid_and_encrypted_files(client):
    response = client.post("/api/jobs", files={"old_file": ("old.pdf", text_pdf()),
                                              "new_file": ("new.xlsx", b"not-xlsx")})
    assert response.status_code == 400
    response = client.post("/api/jobs", files={"old_file": ("old.pdf", b"not-pdf"),
                                              "new_file": ("new.pdf", text_pdf())})
    assert response.status_code == 422
    response = client.post("/api/jobs", files={"old_file": ("old.pdf", text_pdf(password="secret-test")),
                                              "new_file": ("new.pdf", text_pdf())})
    assert response.status_code == 422


def test_scanned_pdf_is_real_ocr_and_remains_reviewable_when_identical(client):
    from reportlab.lib.utils import ImageReader

    with pdfplumber.open(BytesIO(text_pdf())) as book:
        image = book.pages[0].to_image(resolution=160, force_mediabox=True).original
    stream = BytesIO()
    page = canvas.Canvas(stream, pagesize=(400, 300))
    page.drawImage(ImageReader(image), 0, 0, width=400, height=300)
    page.save()
    scan = stream.getvalue()
    with pdfplumber.open(BytesIO(scan)) as book:
        assert not book.pages[0].chars
    job = upload_pdf(client, scan, scan)
    assert all(p["method"] == "ocr" for p in job["files"]["old"]["pages"])
    run = compare_pdf(client, job)
    assert run["status"] == "completed_with_issues"
    assert run["summary"]["changes"] == 0
    items = client.get(f"/api/runs/{run['id']}/diffs").json()["items"]
    assert any(d["severity"] == "issue" and (d["old_source"] or d["new_source"]) for d in items)
    assert not any(r["content_status"] == "reliable" for r in run["reports"])
    # The real OCR path must recover content, not only return a whole-page warning.
    metadata = job["files"]["old"]["sheets"]
    previews = [client.get(f"/api/jobs/{job['id']}/columns", params={
        "side": "old", "sheet": sheet["name"],
        "header_start": sheet["suggested_header_row"], "header_end": sheet["suggested_header_row"]}).json()
        for sheet in metadata]
    assert "12000" in str(previews)
    report = client.get(f"/api/runs/{run['id']}/export")
    assert report.status_code == 200
    assert load_workbook(BytesIO(report.content))["待核对"].max_row > 1


def test_blank_pdf_remains_viewable_and_never_reports_equal(client):
    stream = BytesIO()
    page = canvas.Canvas(stream, pagesize=(200, 200))
    page.showPage()
    page.save()
    job = upload_pdf(client, stream.getvalue(), stream.getvalue())
    run = compare_pdf(client, job)
    assert run["status"] == "completed_with_issues"
    items = client.get(f"/api/runs/{run['id']}/diffs").json()["items"]
    assert not any(d["kind"] == "extraction_unavailable" for d in items)
    assert job["files"]["old"]["pages"][0]["blank"]
    assert run["summary"]["changes"] == 0 and not run["reports"]
    assert client.get(f"/api/jobs/{job['id']}/pages/old/1.png").status_code == 200
    assert client.get(f"/api/runs/{run['id']}/export").status_code == 200


def test_small_scan_table_below_native_heading_is_not_skipped(client):
    from reportlab.lib.utils import ImageReader

    def mixed(amount):
        grid_stream = BytesIO()
        grid = canvas.Canvas(grid_stream, pagesize=(400, 100))
        for x in (10, 180, 390):
            grid.line(x, 10, x, 90)
        for y in (10, 50, 90):
            grid.line(10, y, 390, y)
        for x, y, text in ((20, 65, "Code"), (190, 65, "Amount"), (20, 25, "A001"), (190, 25, amount)):
            grid.drawString(x, y, text)
        grid.save()
        with pdfplumber.open(BytesIO(grid_stream.getvalue())) as book:
            image = book.pages[0].to_image(resolution=180, force_mediabox=True).original
        stream = BytesIO()
        pdf = canvas.Canvas(stream, pagesize=(595, 842))
        pdf.drawString(40, 780, "Native heading with enough text to select the text extraction route")
        pdf.drawImage(ImageReader(image), 40, 500, width=400, height=100)
        pdf.save()
        return stream.getvalue()

    job = upload_pdf(client, mixed("100"), mixed("200"))
    assert job["files"]["old"]["pages"][0]["method"] == "ocr"
    run = compare_pdf(client, job)
    items = client.get(f"/api/runs/{run['id']}/diffs").json()["items"]
    assert run["summary"]["changes"] == 0 and run["summary"]["issues"] > 0
    assert not any(d.get("layer") == "content" for d in items)
    for side, expected in (("old", "100"), ("new", "200")):
        book = client.app.state.store.workbook(job["id"], side)
        assert any(c["display"] == expected and c["pdf_source"]["method"] == "ocr" for sh in book["sheets"] for c in sh["cells"].values())
        assert any(c["pdf_source"]["method"] == "text" and "Native heading" in c["display"] for sh in book["sheets"] for c in sh["cells"].values())


def test_public_pdf_pages_keeps_viewer_metadata_without_text_or_storage_details():
    page = {
        "number": 1, "width": 400, "height": 300, "physical_width": 400, "physical_height": 300,
        "image_width": 800, "image_height": 600, "method": "ocr", "status": "ok",
        "reviewed": False, "reviewable": True, "reviewed_cells": 1, "total_cells": 2,
        "structure_unconfirmed": False, "blank": False, "rotation": 0,
        "native_lines": [{"text": "Native heading"}], "native_text": "Native heading",
        "evidence_lines": [{"text": "Extracted content"}], "appearance": {"chars": ["private"]},
        "ocr_regions": [{"text": "Recognized content"}], "ocr_annotations": [{"text": "Annotation"}],
        "appearance_path": "/private/appearance.json", "image_path": "/private/page.png",
        "future_internal_payload": {"text": "Do not expose"},
    }
    result = public_pdf_pages({"pages": [page]})[0]
    assert result == {key: value for key, value in page.items() if key in {
        "number", "width", "height", "physical_width", "physical_height", "image_width", "image_height",
        "method", "status", "reviewed", "reviewable", "reviewed_cells", "total_cells",
        "structure_unconfirmed", "blank", "rotation",
    }} | {"has_native_text": True}
    assert page["native_text"] == "Native heading" and page["appearance_path"] == "/private/appearance.json"
    assert public_pdf_pages({"pages": [{"number": 2, "method": "ocr"}]}) == [
        {"number": 2, "method": "ocr", "has_native_text": False}]


def test_unmodified_pdf_job_uses_saved_metadata_without_loading_evidence(client, monkeypatch):
    from unittest.mock import Mock

    original = text_pdf()
    uploaded = upload_pdf(client, original, original)
    store = client.app.state.store
    legacy_metadata = store.job(uploaded["id"])
    for side in ("old", "new"):
        for sheet in legacy_metadata["files"][side]["sheets"]:
            sheet["cell_geometry"] = [{"internal_geometry": "large legacy payload"}]
    with store.connect() as db:
        db.execute("UPDATE jobs SET metadata=? WHERE id=?", (encode(legacy_metadata), uploaded["id"]))
    read_job = Mock(wraps=store.job)
    monkeypatch.setattr(store, "job", read_job)
    for name in ("workbook", "reviewed_book", "reviews"):
        monkeypatch.setattr(store, name, Mock(side_effect=AssertionError("must not reload PDF evidence")))
    response = client.get(f"/api/jobs/{uploaded['id']}")
    assert response.status_code == 200, response.text
    current = response.json()
    assert current["suggested_pairs"] == uploaded["suggested_pairs"]
    assert current["files"] == uploaded["files"]
    assert current["warnings"] == uploaded["warnings"]
    assert current["review_revision"] == current["structure_revision"] == 0
    read_job.assert_called_once_with(uploaded["id"])
