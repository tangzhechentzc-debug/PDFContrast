import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from io import BytesIO
from threading import Event
from time import monotonic

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

import backend.app as app_module
from backend.app import create_app, encode
from test_api import xlsx_bytes
from test_pdf_api import text_pdf
from test_review import workbook


PDF_BYTES = {"old": b"%PDF-1.4\nold-import-fixture", "new": b"%PDF-1.4\nnew-import-fixture"}
PDF_NAMES = {"old": "原始报表.pdf", "new": "更新报表.pdf"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as active:
        yield active


def pdf_upload(client, **kwargs):
    return client.post("/api/jobs?background=true", files={
        f"{side}_file": (PDF_NAMES[side], PDF_BYTES[side], "application/pdf")
        for side in ("old", "new")}, **kwargs)


def status(client, import_id):
    response = client.get(f"/api/imports/{import_id}")
    assert response.status_code == 200, response.text
    return response.json()


def terminal_status(client, import_id):
    deadline = monotonic() + 5
    last = None
    while monotonic() < deadline:
        last = status(client, import_id)
        if last["status"] in ("ready", "failed"):
            return last
        Event().wait(0.01)
    pytest.fail(f"Import did not reach a terminal state: {last}")


def detail(stage, current, total, completed, unit="page", **extra):
    return {"stage": stage, "unit": unit, "current": current, "total": total,
            "completed": completed, **extra}


def test_background_202_and_polling_stay_responsive_through_both_files(client, monkeypatch):
    old_started, finish_old = Event(), Event()
    new_opened, start_new_page = Event(), Event()
    new_page_started, finish_new = Event(), Event()
    calls = []

    def controlled_extract(path, asset_dir, progress=None):
        side = path.stem
        calls.append(side)
        assert path.read_bytes() == PDF_BYTES[side]
        assert callable(progress)
        if side == "old":
            progress(detail("opening", 0, 2, 0))
            progress(detail("table_extract", 1, 2, 1))
            progress(detail("ocr", 2, 2, 1))
            old_started.set()
            assert finish_old.wait(10)
        else:
            # ingest has switched files and reported opening, before totals are known.
            new_opened.set()
            assert start_new_page.wait(10)
            progress(detail("opening", 0, 2, 0))
            progress(detail("ocr", 1, 2, 0))
            new_page_started.set()
            assert finish_new.wait(10)
        progress(detail("file_completed", 2, 2, 2))
        return workbook()

    monkeypatch.setattr(app_module, "extract_pdf", controlled_extract)
    with ThreadPoolExecutor(max_workers=2) as requests:
        try:
            upload = requests.submit(pdf_upload, client)
            assert old_started.wait(3)
            # Returning before the explicit release proves this is not a synchronous wait.
            response = upload.result(timeout=2)
            assert response.status_code == 202, response.text
            queued = response.json()
            import_id = queued["id"]
            assert queued["status"] == "queued"
            assert queued["files"] == {side: {"name": PDF_NAMES[side]} for side in PDF_NAMES}
            assert queued["progress"]["total"] is None
            active = requests.submit(status, client, import_id).result(timeout=2)
            assert active["status"] == "processing"
            assert active["progress"] == {
                "side": "old", "file_index": 1, "file_count": 2,
                **detail("ocr", 2, 2, 1)}
            assert client.get(f"/api/jobs/{import_id}").status_code == 404
            assert calls == ["old"]
            # A fresh browser connection can resume by ID without another POST or lifespan.
            refreshed = TestClient(client.app)
            try:
                assert status(refreshed, import_id) == active
            finally:
                refreshed.close()
            finish_old.set()
            assert new_opened.wait(3)
            opening = status(client, import_id)
            assert opening["progress"] == {
                "side": "new", "file_index": 2, "file_count": 2,
                **detail("opening", 0, None, 0)}
            start_new_page.set()
            assert new_page_started.wait(3)
            reading = status(client, import_id)
            assert reading["progress"] == {
                "side": "new", "file_index": 2, "file_count": 2,
                **detail("ocr", 1, 2, 0)}
            assert client.get(f"/api/jobs/{import_id}").status_code == 404
            finish_new.set()
            ready = terminal_status(client, import_id)
            assert ready["status"] == "ready" and ready["job_id"] == import_id
            assert ready["progress"]["stage"] == "completed"
            assert ready["progress"]["completed"] == ready["progress"]["total"] == 2
            assert ready["created_at"] == queued["created_at"]
            assert ready["updated_at"] >= queued["updated_at"]
            response = client.get(f"/api/jobs/{import_id}")
            assert response.status_code == 200, response.text
            job = response.json()
            assert job["id"] == import_id and job["status"] == "ready" and job["kind"] == "pdf"
            assert job["suggested_pairs"]
            for side in ("old", "new"):
                assert job["files"][side]["name"] == PDF_NAMES[side]
                assert job["files"][side]["sha256"] == hashlib.sha256(PDF_BYTES[side]).hexdigest()
                assert len(job["files"][side]["pages"]) == 2
                assert job["files"][side]["sheets"]
                assert client.get(f"/api/jobs/{import_id}/files/{side}").content == PDF_BYTES[side]
            assert calls == ["old", "new"]
        finally:
            finish_old.set()
            start_new_page.set()
            finish_new.set()


@pytest.mark.parametrize("failure", ["expected_input_error", "unexpected_exception"])
def test_failed_import_preserves_last_page_originals_and_safe_error(client, monkeypatch, failure):
    internal_detail = "private-import-probe /Users/private/example.pdf INTERNAL_CREDENTIAL_VALUE"

    def failing_extract(path, asset_dir, progress=None):
        if path.stem == "old":
            progress(detail("file_completed", 2, 2, 2))
            return workbook()
        progress(detail("opening", 0, 4, 0))
        progress(detail("ocr", 2, 4, 1))
        if failure == "expected_input_error":
            raise ValueError("第 2 页无法渲染，不能保证原文定位。")
        raise RuntimeError(internal_detail)

    monkeypatch.setattr(app_module, "extract_pdf", failing_extract)
    response = pdf_upload(client)
    assert response.status_code == 202, response.text
    import_id = response.json()["id"]
    failed = terminal_status(client, import_id)
    assert failed["status"] == "failed"
    assert failed["progress"] == {
        "side": "new", "file_index": 2, "file_count": 2,
        **detail("failed", 2, 4, 1)}
    assert failed["error"] and not failed.get("job_id")
    assert internal_detail not in response.text and internal_detail not in str(failed)
    assert "/Users/private" not in failed["error"] and "RuntimeError" not in failed["error"]
    if failure == "expected_input_error":
        assert "第 2 页无法渲染" in failed["error"]
    assert client.get(f"/api/jobs/{import_id}").status_code == 404
    folder = client.app.state.store.folder / import_id
    assert all((folder / f"{side}.pdf").read_bytes() == PDF_BYTES[side] for side in PDF_BYTES)
    assert status(client, import_id) == failed


def test_xlsx_progress_reports_actual_sheet_name_and_count(client, monkeypatch):
    book = Workbook()
    book.active.title = "项目明细"
    book.active.append(["编号", "金额"])
    book.active.append(["A001", 10])
    book.create_sheet("汇总").append(["合计", 10])
    stream = BytesIO()
    book.save(stream)
    original_extractor = app_module.extract_workbook
    entered, release = Event(), Event()

    def controlled_extract(path, progress=None):
        extracted = original_extractor(path)
        sheets = extracted["sheets"]
        progress(detail("opening", 0, None, 0, unit="sheet"))
        for number, sheet in enumerate(sheets, 1):
            progress(detail("reading_sheet", number, len(sheets), number - 1,
                            unit="sheet", item_name=sheet["name"]))
            if path.stem == "new" and sheet["name"] == "汇总":
                entered.set()
                assert release.wait(10)
            progress(detail("reading_sheet", number, len(sheets), number,
                            unit="sheet", item_name=sheet["name"]))
        progress(detail("file_completed", len(sheets), len(sheets), len(sheets), unit="sheet"))
        return extracted

    monkeypatch.setattr(app_module, "extract_workbook", controlled_extract)
    try:
        response = client.post("/api/jobs?background=true", files={
            "old_file": ("旧版.xlsx", stream.getvalue()), "new_file": ("新版.xlsx", stream.getvalue())})
        assert response.status_code == 202, response.text
        assert entered.wait(3)
        import_id = response.json()["id"]
        progress = status(client, import_id)["progress"]
        assert progress == {"side": "new", "file_index": 2, "file_count": 2,
                            **detail("reading_sheet", 2, 2, 1, unit="sheet", item_name="汇总")}
        release.set()
        ready = terminal_status(client, import_id)
        assert ready["status"] == "ready"
        job = client.get(f"/api/jobs/{import_id}").json()
        assert [sheet["name"] for sheet in job["files"]["new"]["sheets"]] == ["项目明细", "汇总"]
    finally:
        release.set()


def test_ready_import_can_be_resumed_after_new_app_without_reupload(tmp_path, monkeypatch):
    calls = []

    def extract(path, asset_dir, progress=None):
        calls.append(path.stem)
        progress(detail("file_completed", 2, 2, 2))
        return workbook()

    monkeypatch.setattr(app_module, "extract_pdf", extract)
    data = tmp_path / "persistent"
    with TestClient(create_app(data)) as first:
        response = pdf_upload(first)
        assert response.status_code == 202, response.text
        import_id = response.json()["id"]
        before = terminal_status(first, import_id)
        assert before["status"] == "ready"
        saved_job = first.get(f"/api/jobs/{import_id}").json()
    with TestClient(create_app(data)) as refreshed:
        assert status(refreshed, import_id) == before
        assert refreshed.get(f"/api/jobs/{import_id}").json() == saved_job
        assert calls == ["old", "new"]


def interrupted_metadata(import_id, state):
    now = datetime.now(timezone.utc).isoformat()
    progress = {"side": None, "file_index": 0, "file_count": 2,
                **detail("queued", 0, None, 0, unit=None)} if state == "queued" else {
                    "side": "new", "file_index": 2, "file_count": 2, **detail("ocr", 3, 5, 2)}
    return {"id": import_id, "status": state,
            "files": {side: {"name": PDF_NAMES[side]} for side in PDF_NAMES},
            "progress": progress, "created_at": now, "updated_at": now}


@pytest.mark.parametrize("previous_state", ["queued", "processing"])
def test_restart_marks_unfinished_import_failed_and_keeps_last_progress(tmp_path, monkeypatch, previous_state):
    app = create_app(tmp_path / "interrupted")
    store = app.state.store
    import_id = "interrupted-import"
    record = interrupted_metadata(import_id, previous_state)
    folder = store.folder / import_id
    folder.mkdir()
    for side, content in PDF_BYTES.items():
        (folder / f"{side}.pdf").write_bytes(content)
    with store.connect() as db:
        db.execute("INSERT INTO imports VALUES (?,?,?)", (import_id, previous_state, encode(record)))

    def must_not_restart(*args, **kwargs):
        pytest.fail("Interrupted work must not silently restart extraction")

    monkeypatch.setattr(app_module, "extract_pdf", must_not_restart)
    with TestClient(app) as resumed:
        failed = status(resumed, import_id)
        assert failed["status"] == "failed"
        assert "中断" in failed["error"] or "重启" in failed["error"]
        expected_progress = {**record["progress"], "stage": "failed"}
        assert failed["progress"] == expected_progress
        assert failed["created_at"] == record["created_at"]
        assert resumed.get(f"/api/jobs/{import_id}").status_code == 404
        assert all((folder / f"{side}.pdf").read_bytes() == value for side, value in PDF_BYTES.items())


@pytest.mark.parametrize("previous_state", ["queued", "processing"])
def test_restart_recovers_ready_when_complete_job_was_already_written(tmp_path, previous_state):
    data = tmp_path / "committed"
    contents = xlsx_bytes([["编号", "金额"], ["A001", 10]])
    with TestClient(create_app(data)) as first:
        response = first.post("/api/jobs", files={
            "old_file": ("old.xlsx", contents), "new_file": ("new.xlsx", contents)})
        assert response.status_code == 201, response.text
        job = response.json()
        record = interrupted_metadata(job["id"], previous_state)
        record["files"] = {side: {"name": job["files"][side]["name"]} for side in ("old", "new")}
        with first.app.state.store.connect() as db:
            db.execute("INSERT INTO imports VALUES (?,?,?)", (job["id"], previous_state, encode(record)))
    with TestClient(create_app(data)) as resumed:
        ready = status(resumed, job["id"])
        assert ready["status"] == "ready" and ready["job_id"] == job["id"]
        assert ready["progress"]["stage"] == "completed"
        assert not ready.get("error")
        assert resumed.get(f"/api/jobs/{job['id']}").json() == job
        for side in ("old", "new"):
            assert resumed.get(f"/api/jobs/{job['id']}/files/{side}").content == contents


@pytest.mark.parametrize("kind", ["pdf", "xlsx"])
def test_legacy_synchronous_upload_returns_complete_201_job(client, kind):
    contents = text_pdf() if kind == "pdf" else xlsx_bytes([["编号", "金额"], ["A001", 10]])
    response = client.post("/api/jobs", files={
        "old_file": (f"old.{kind}", contents), "new_file": (f"new.{kind}", contents)})
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["kind"] == kind and job["status"] == "ready"
    assert job["suggested_pairs"]
    assert all(job["files"][side]["sheets"] for side in ("old", "new"))
    assert client.get(f"/api/jobs/{job['id']}").status_code == 200
    assert client.get(f"/api/imports/{job['id']}").status_code == 404


def test_unknown_import_status_is_404(client):
    response = client.get("/api/imports/not-an-import")
    assert response.status_code == 404
    assert isinstance(response.json()["detail"], str)
