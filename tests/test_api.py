import hashlib
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from backend.app import create_app


def xlsx_bytes(rows, title="明细"):
    book = Workbook()
    sheet = book.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "data")) as test_client:
        yield test_client


def upload(client, old, new):
    response = client.post("/api/jobs", files={
        "old_file": ("旧版.xlsx", old), "new_file": ("新版.xlsx", new)})
    assert response.status_code == 201, response.text
    return response.json()


def config(job):
    pairs = job["suggested_pairs"]
    for pair in pairs:
        pair["mode"] = "key"
        for column in pair["columns"]:
            column["is_key"] = column["old_column"] == "A"
    return {"pairs": pairs, "absolute_tolerance": "0", "ignore_outer_whitespace": True,
            "compare_formulas": True, "compare_formatting": False}


def test_upload_compare_locate_and_original_hash(client):
    old = xlsx_bytes([["编号", "金额"], ["001", 12000], ["002", 300]])
    new = xlsx_bytes([["编号", "金额"], ["002", 300], ["001", 12800]])
    job = upload(client, old, new)
    assert job["files"]["old"]["sha256"] == hashlib.sha256(old).hexdigest()
    response = client.post(f"/api/jobs/{job['id']}/runs", json=config(job))
    assert response.status_code == 201, response.text
    run = response.json()
    result = client.get(f"/api/runs/{run['id']}/diffs").json()
    value_diffs = [d for d in result["items"] if d["kind"] == "value_changed"]
    assert len(value_diffs) == 1
    difference = value_diffs[0]
    assert difference["old_source"]["cell"] == "B2"
    assert difference["new_source"]["cell"] == "B3"
    assert float(difference["delta"]) == 800
    region = client.get(f"/api/jobs/{job['id']}/sheet-region", params={
        "side": "new", "sheet": "明细", "start_row": 2, "start_column": 1})
    assert region.status_code == 200
    target = next(c for c in region.json()["cells"] if c["address"] == "B3")
    assert str(target["value"]) in ("12800", "12800.0")
    assert client.get(f"/api/jobs/{job['id']}/files/old").content == old
    assert client.get(f"/api/jobs/{job['id']}/files/new").content == new


def test_all_differences_pagination_search_and_safe_export(client):
    old = xlsx_bytes([["编号", "金额"]] + [[f"K{i:04d}", 100] for i in range(125)])
    new = xlsx_bytes([["编号", "金额"]] + [[f"K{i:04d}", 101] for i in range(125)])
    job = upload(client, old, new)
    run = client.post(f"/api/jobs/{job['id']}/runs", json=config(job)).json()
    first = client.get(f"/api/runs/{run['id']}/diffs", params={"limit": 100}).json()
    second = client.get(f"/api/runs/{run['id']}/diffs", params={"offset": 100, "limit": 100}).json()
    assert first["total"] == 125
    assert len(first["items"]) == 100 and len(second["items"]) == 25
    assert not {x["id"] for x in first["items"]} & {x["id"] for x in second["items"]}
    match = client.get(f"/api/runs/{run['id']}/diffs", params={"q": "K0124"}).json()
    assert match["total"] == 1
    response = client.get(f"/api/runs/{run['id']}/export")
    assert response.status_code == 200
    report = load_workbook(BytesIO(response.content))
    assert report["全部差异"].max_row == 126
    assert all(cell.data_type != "f" for sheet in report for row in sheet for cell in row)


def test_formula_strings_remain_text_in_export(client):
    old = xlsx_bytes([["编号", "金额"], ["X", "=1+1"]])
    new = xlsx_bytes([["编号", "金额"], ["X", "=2+1"]])
    job = upload(client, old, new)
    run_response = client.post(f"/api/jobs/{job['id']}/runs", json=config(job))
    assert run_response.status_code == 201, run_response.text
    run = run_response.json()
    report = load_workbook(BytesIO(client.get(f"/api/runs/{run['id']}/export").content))
    strings = [c for row in report["全部差异"] for c in row if c.value == "=1+1"]
    assert strings and all(c.data_type == "s" for c in strings)
    assert run["summary"]["issues"] > 0


def test_reject_bad_file_and_foreign_origin(client):
    book = xlsx_bytes([["编号", "金额"], ["1", 2]])
    response = client.post("/api/jobs", files={"old_file": ("old.pdf", b"%PDF"),
                                               "new_file": ("new.xlsx", book)})
    assert response.status_code == 400
    response = client.post("/api/jobs", files={"old_file": ("old.xlsx", b"not-an-excel-file"),
                                               "new_file": ("new.xlsx", book)})
    assert response.status_code == 422
    assert client.post("/api/jobs", headers={"Origin": "https://example.org"}).status_code == 403
    assert client.get("/api/jobs/not-found").status_code == 404


def test_saved_job_and_run_survive_new_app(tmp_path):
    data = tmp_path / "persistent"
    old = xlsx_bytes([["编号", "金额"], ["1", 10]])
    new = xlsx_bytes([["编号", "金额"], ["1", 20]])
    with TestClient(create_app(data)) as first:
        job = upload(first, old, new)
        run = first.post(f"/api/jobs/{job['id']}/runs", json=config(job)).json()
    with TestClient(create_app(data)) as second:
        assert second.get(f"/api/jobs/{job['id']}").json()["id"] == job["id"]
        assert second.get(f"/api/runs/{run['id']}/diffs").json()["total"] == 1


def test_export_preserves_large_configuration(client):
    headers = ["编号"] + [f"字段{i:03d}" for i in range(1, 400)]
    old = xlsx_bytes([headers, ["row-1"] + list(range(1, 400))])
    new = xlsx_bytes([headers, ["row-1"] + list(range(2, 401))])
    job = upload(client, old, new)
    response = client.post(f"/api/jobs/{job['id']}/runs", json=config(job))
    assert response.status_code == 201, response.text
    run = response.json()
    report = load_workbook(BytesIO(client.get(f"/api/runs/{run['id']}/export").content))
    values = dict(report["范围与规则"].iter_rows(min_row=2, values_only=True))
    assert values["config.pairs[0].columns[399].name"] == '"字段399"'
    assert all(len(str(v)) <= 32767 for v in values.values())

