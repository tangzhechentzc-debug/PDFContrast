import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

import backend.app as app_module
from backend.app import encode
from test_review_api import catalog, client, export_rows, save


JOB = "/api/jobs/test-job"
FIRST_SHEET = "第 1 页 · 表 1"
SECOND_SHEET = "第 2 页 · 表 1"


def read_table(client, *, side="new", sheet=FIRST_SHEET, page=1):
    response = client.get(f"{JOB}/review-tables", params={"side": side, "page": page, "sheet": sheet})
    assert response.status_code == 200, response.text
    return response.json()


def editable_rows(table):
    return [[{"id": cell["id"], "text": cell["text"]} for cell in row] for row in table["rows"]]


def update_table(client, rows, revision=0, *, side="new", sheet=FIRST_SHEET, page=1, **overrides):
    payload = {"expected_revision": revision, "side": side, "action": "table_update",
               "sheet": sheet, "page": page, "rows": rows, "structure_confirmed": True,
               "reason": "按原图补行并核对表格结构"}
    payload.update(overrides)
    return client.post(f"{JOB}/review", json=payload)


def current_config(client):
    response = client.get(JOB)
    assert response.status_code == 200, response.text
    job = response.json()
    return {"pairs": job["suggested_pairs"], "review_revision": job["review_revision"],
            "absolute_tolerance": "0", "compare_formulas": False, "compare_formatting": False}


def compare_current(client):
    response = client.post(f"{JOB}/runs", json=current_config(client))
    assert response.status_code == 201, response.text
    return response.json()


def all_diffs(client, run):
    response = client.get(f"/api/runs/{run['id']}/diffs", params={"limit": 500})
    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data["items"]) == data["total"]
    return data["items"]


def grid(rows, columns):
    return [[{"text": f"R{row}C{column}"} for column in range(columns)] for row in range(rows)]


def replace_fixture_book(client, mutate):
    store = client.app.state.store
    path = store.folder / "test-job" / "new.json"
    book = json.loads(path.read_text(encoding='utf-8'))
    mutate(book)
    path.write_text(encode(book), encoding='utf-8')


def test_insert_move_keep_identity_sources_and_later_cell_correction(client):
    directory = client.get(f"{JOB}/review-tables", params={"side": "new"}).json()
    assert directory["revision"] == directory["structure_revision"] == 0
    assert {table["sheet"] for table in directory["tables"]} == {FIRST_SHEET, SECOND_SHEET}
    assert all(table["editable"] for table in directory["tables"])
    before = read_table(client)["table"]
    rows = editable_rows(before)
    original_amount = before["rows"][1][1]
    rows[1], rows[2] = rows[2], rows[1]
    rows.append([{"text": text} for text in ("A005", "55.00", "0005")])
    saved = update_table(client, rows)
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    event = saved.json()["event"]
    assert len(event["table_before"]["rows"]) == 3
    assert len(event["table_after"]["rows"]) == 4
    assert event["structure_revision"] == 1
    after_response = read_table(client)
    assert after_response["revision"] == after_response["structure_revision"] == 1
    after = after_response["table"]
    assert after["structure_confirmed"] is True
    assert len({cell["id"] for row in after["rows"] for cell in row}) == 12
    moved = after["rows"][2][1]
    assert moved["id"] == original_amount["id"]
    assert moved["original_text"] == original_amount["original_text"] == "10.00"
    assert moved["source"]["bbox"] == original_amount["source"]["bbox"]
    for cell in after["rows"][3]:
        assert cell["manual"] is True and cell["original_text"] == ""
        assert cell["source"]["manual"] is True
        assert cell["source"]["location_precision"] == "page"
        assert cell["source"]["bbox"] == [0, 0, 1, 1]
        assert cell["source"]["page"] == 1 and cell["source"]["method"] == "ocr"
        assert cell["reviewed"] is False
    moved_catalog = next(item for item in catalog(client)["items"] if item["id"] == moved["id"])
    assert moved_catalog["cell"] == "B3"
    correction = save(client, "cell_update", 1, target_id=moved["id"], value="99.00")
    assert correction.status_code == 200, correction.text
    assert read_table(client)["table"]["rows"][2][1]["text"] == "99.00"
    result = compare_current(client)
    diffs = all_diffs(client, result)
    changed = [d for d in diffs if d["row_key"] == "A001" and d["field"] == "Amount"]
    assert len(changed) == 1 and changed[0]["delta"] == "89"
    assert changed[0]["new_source"]["review_id"] == moved["id"]
    inserted = [d for d in diffs if d["row_key"] == "A005"]
    assert len(inserted) == 1 and inserted[0]["original_kind"] == "row_added"
    assert inserted[0]["new_source"]["manual"] is True
    assert inserted[0]["new_source"]["location_precision"] == "page"


@pytest.mark.parametrize("foreign", ["other_table", "other_side", "other_job"])
def test_foreign_cell_ids_are_rejected_without_history(client, foreign):
    rows = editable_rows(read_table(client)["table"])
    if foreign == "other_table":
        foreign_id = read_table(client, sheet=SECOND_SHEET, page=2)["table"]["rows"][1][1]["id"]
    elif foreign == "other_side":
        foreign_id = read_table(client, side="old")["table"]["rows"][1][1]["id"]
    else:
        store = client.app.state.store
        second = copy.deepcopy(store.job("test-job"))
        second["id"] = "other-job"
        folder = store.folder / second["id"]
        folder.mkdir()
        for side in ("old", "new"):
            (folder / f"{side}.json").write_bytes((store.folder / "test-job" / f"{side}.json").read_bytes())
        with store.connect() as db:
            db.execute("INSERT INTO jobs VALUES (?,?)", (second["id"], encode(second)))
        response = client.get("/api/jobs/other-job/review-tables", params={"side": "new", "sheet": FIRST_SHEET})
        assert response.status_code == 200, response.text
        foreign_id = response.json()["table"]["rows"][1][1]["id"]
    rows[1][1]["id"] = foreign_id
    response = update_table(client, rows)
    assert response.status_code == 422, response.text
    assert catalog(client)["revision"] == 0
    assert client.get(f"{JOB}/review-history").json()["total"] == 0


def test_duplicate_ids_and_forged_source_or_review_fields_are_rejected(client):
    original = editable_rows(read_table(client)["table"])
    duplicate = copy.deepcopy(original)
    duplicate[2][1]["id"] = duplicate[1][1]["id"]
    assert update_table(client, duplicate).status_code == 422
    for injected in ({"source": {"method": "text", "page": 99, "bbox": [0, 0, 1, 1]}},
                     {"reviewed": True}, {"manual": False}, {"original_text": "forged"}):
        rows = copy.deepcopy(original)
        rows[1][1].update(injected)
        response = update_table(client, rows)
        assert response.status_code == 422, (injected, response.text)
    assert update_table(client, original, table_after={"rows": []}).status_code == 422
    assert update_table(client, original, page=2).status_code == 422
    assert update_table(client, original, structure_confirmed=False).status_code == 422
    assert update_table(client, original, reason="   ").status_code == 422
    assert catalog(client)["revision"] == 0


@pytest.mark.parametrize("case", ["no_rows", "no_columns", "ragged", "too_many_rows",
                                  "too_many_columns", "too_many_cells", "long_text"])
def test_matrix_limits_reject_without_partial_save(client, case):
    invalid = {
        "no_rows": lambda: [],
        "no_columns": lambda: [[]],
        "ragged": lambda: [[{"text": "A"}], [{"text": "B"}, {"text": "C"}]],
        "too_many_rows": lambda: grid(501, 1),
        "too_many_columns": lambda: grid(1, 61),
        "too_many_cells": lambda: grid(167, 60),
        "long_text": lambda: [[{"text": "x" * 10001}]],
    }[case]()
    response = update_table(client, invalid)
    assert response.status_code == 422, (case, response.text)
    assert catalog(client)["revision"] == 0
    assert len(read_table(client)["table"]["rows"]) == 3
    assert client.get(f"{JOB}/review-history").json()["total"] == 0


@pytest.mark.parametrize(("rows", "columns"), [(500, 1), (1, 60), (200, 50)])
def test_inclusive_matrix_limits_accept_without_truncation(client, rows, columns):
    response = update_table(client, grid(rows, columns))
    assert response.status_code == 200, response.text[:1000]
    table = read_table(client)["table"]
    assert len(table["rows"]) == rows
    assert all(len(row) == columns for row in table["rows"])
    assert table["rows"][-1][-1]["text"] == f"R{rows - 1}C{columns - 1}"
    assert len({cell["id"] for row in table["rows"] for cell in row}) == rows * columns


def test_structure_save_invalidates_all_cells_on_page_but_not_other_pages(client):
    assert save(client, "page_verify", 0, page=1).status_code == 200
    assert save(client, "page_verify", 1, page=2).status_code == 200
    before = read_table(client)["table"]
    assert all(cell["reviewed"] for row in before["rows"] for cell in row)
    rows = editable_rows(before)
    rows[1], rows[2] = rows[2], rows[1]
    response = update_table(client, rows, revision=2)
    assert response.status_code == 200, response.text
    pages = {page["number"]: page for page in catalog(client)["pages"]}
    assert pages[1]["reviewed"] is False and pages[1]["reviewed_cells"] == 0
    assert pages[2]["reviewed"] is True and pages[2]["reviewed_cells"] == 9
    assert all(not cell["reviewed"] for row in read_table(client)["table"]["rows"] for cell in row)


def test_unavailable_page_cannot_be_repaired_or_verified(client):
    def unavailable(book):
        book["pages"][0]["status"] = "unavailable"
    replace_fixture_book(client, unavailable)
    response = update_table(client, [[{"text": "人工输入"}]])
    assert response.status_code == 422, response.text
    assert save(client, "page_verify", 0, page=1).status_code == 422
    assert catalog(client)["revision"] == 0
    result = compare_current(client)
    assert any(d["kind"] == "extraction_unavailable" and d["new_source"]
               and d["new_source"]["page"] == 1 for d in all_diffs(client, result))


def test_all_regions_must_be_structurally_confirmed_before_page_verification(client):
    extra_name = "第 1 页 · 表 2"
    def unconfirmed(book):
        extra = copy.deepcopy(book["sheets"][0])
        extra["name"] = extra_name
        book["sheets"].insert(1, extra)
        book["pages"][0]["structure_unconfirmed"] = True
        book["warnings"].append("第 1 页未能可靠恢复表格结构，仅保存单列文字块并按位置比较，须核对错列或漏行。")
        book["warnings"].append("其他范围仍未比较。")
    replace_fixture_book(client, unconfirmed)
    first = editable_rows(read_table(client)["table"])
    assert update_table(client, first).status_code == 200
    assert save(client, "page_verify", 1, page=1).status_code == 422
    second = editable_rows(read_table(client, sheet=extra_name)["table"])
    response = update_table(client, second, revision=1, sheet=extra_name)
    assert response.status_code == 200, response.text
    checked = save(client, "page_verify", 2, page=1)
    assert checked.status_code == 200, checked.text
    page = next(p for p in catalog(client)["pages"] if p["number"] == 1)
    assert page["reviewable"] is True and page["reviewed"] is True
    assert not page.get("structure_unconfirmed", False)
    job = client.get(JOB).json()
    assert "其他范围仍未比较。" in job["warnings"]
    assert not any("第 1 页未能可靠恢复表格结构" in warning for warning in job["warnings"])


def test_stale_structure_configuration_is_409_and_job_refreshes_suggestions(client):
    initial = client.get(JOB).json()
    old_config = current_config(client)
    rows = editable_rows(read_table(client)["table"])
    for index, row in enumerate(rows):
        row.insert(1, {"text": "补充说明" if index == 0 else f"说明{index}"})
    assert update_table(client, rows).status_code == 200
    for revision in (None, 0, 2):
        stale = copy.deepcopy(old_config)
        if revision is None:
            stale.pop("review_revision", None)
        else:
            stale["review_revision"] = revision
        response = client.post(f"{JOB}/runs", json=stale)
        assert response.status_code == 409, (revision, response.text)
    fresh = client.get(JOB).json()
    assert fresh["review_revision"] == fresh["structure_revision"] == 1
    physical = next(s for s in fresh["files"]["new"]["sheets"] if s["name"] == FIRST_SHEET)
    assert physical["max_column"] == 4
    assert fresh["suggested_pairs"] != initial["suggested_pairs"]
    assert "补充说明" in json.dumps(fresh["suggested_pairs"], ensure_ascii=False)
    valid_config = current_config(client)
    response = client.post(f"{JOB}/runs", json=valid_config)
    assert response.status_code == 201, response.text
    target = read_table(client)["table"]["rows"][1][2]["id"]
    assert save(client, "cell_update", 1, target_id=target, value="99").status_code == 200
    # Text-only updates after the latest structural version do not stale the mapping.
    response = client.post(f"{JOB}/runs", json=valid_config)
    assert response.status_code == 201, response.text
    assert response.json()["review_revision"] == 2


def test_header_text_correction_stales_mapping_and_refreshes_job(client):
    initial = client.get(JOB).json()
    stale = current_config(client)
    header = read_table(client)["table"]["rows"][0][1]
    response = save(client, "cell_update", 0, target_id=header["id"], value="Balance")
    assert response.status_code == 200, response.text
    assert client.post(f"{JOB}/runs", json=stale).status_code == 409
    fresh = client.get(JOB).json()
    assert fresh["review_revision"] == fresh["structure_revision"] == 1
    assert fresh["suggested_pairs"] != initial["suggested_pairs"]
    assert "Balance" in json.dumps(fresh["suggested_pairs"], ensure_ascii=False)
    assert compare_current(client)["review_revision"] == 1


def test_new_run_structural_history_export_and_old_results_are_immutable(client):
    store = client.app.state.store
    originals = {p.name: p.read_bytes() for p in (store.folder / "test-job").iterdir()}
    first = compare_current(client)
    first_report = export_rows(client, first)
    first_diffs = all_diffs(client, first)
    before = read_table(client)["table"]
    rows = editable_rows(before)
    rows.append([{"text": text} for text in ("A005", "55.00", "0005")])
    response = update_table(client, rows)
    assert response.status_code == 200, response.text
    event = response.json()["event"]
    second = compare_current(client)
    assert second["review_revision"] == 1
    assert any(d["row_key"] == "A005" and d.get("original_kind") == "row_added"
               for d in all_diffs(client, second))
    second_report = export_rows(client, second)
    records = second_report["结构修改明细"]
    headers = records[0]
    exported = [dict(zip(headers, row)) for row in records[1:]]
    before_rows = [row for row in exported if row["阶段"] == "修改前"]
    after_rows = [row for row in exported if row["阶段"] == "修改后"]
    assert len(before_rows) == 9 and len(after_rows) == 12
    assert {row["修订"] for row in exported} == {1}
    assert {row["表"] for row in exported} == {FIRST_SHEET}
    assert len({row["稳定标识"] for row in after_rows}) == 12
    added_amount = event["table_after"]["rows"][3][1]
    added_row = next(row for row in after_rows if row["稳定标识"] == added_amount["id"])
    assert added_row["当前文本"] == "55.00" and added_row["坐标"] == "B4"
    assert added_row["原始识别文本"] in (None, "")
    assert added_row["来源"]
    assert save(client, "cell_update", 1, target_id=added_amount["id"], value="77.00").status_code == 200
    assert client.get(f"/api/runs/{first['id']}").json() == first
    assert all_diffs(client, first) == first_diffs
    assert export_rows(client, first) == first_report
    assert export_rows(client, second) == second_report
    history = client.get(f"{JOB}/review-history").json()
    persisted = next(e for e in history["items"] if e["action"] == "table_update")
    assert persisted["table_before"] == event["table_before"]
    assert persisted["table_after"] == event["table_after"]
    assert all(p.read_bytes() == originals[p.name] for p in (store.folder / "test-job").iterdir())


def test_deleted_cells_do_not_survive_in_alias_or_accept_later_corrections(client):
    before = read_table(client)["table"]
    removed_id = before["rows"][1][1]["id"]
    rows = editable_rows(before)
    del rows[1]
    assert update_table(client, rows).status_code == 200
    assert removed_id not in {item["id"] for item in catalog(client)["items"]}
    assert save(client, "cell_update", 1, target_id=removed_id, value="999").status_code == 422
    result = compare_current(client)
    relevant = [d for d in all_diffs(client, result) if d["row_key"] == "A001"]
    assert len(relevant) == 1 and relevant[0]["original_kind"] == "row_removed"
    assert relevant[0]["new_source"] is None
    history = client.get(f"{JOB}/review-history").json()
    assert history["total"] == 1
    assert any(cell["id"] == removed_id for row in history["items"][0]["table_before"]["rows"] for cell in row)


def test_concurrent_structure_saves_commit_exactly_one_complete_event(client):
    rows = editable_rows(read_table(client)["table"])
    rows.append([{"text": text} for text in ("A005", "55", "0005")])
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(update_table, client, copy.deepcopy(rows)) for _ in range(2)]
        responses = [future.result() for future in futures]
    assert sorted(response.status_code for response in responses) == [200, 409]
    history = client.get(f"{JOB}/review-history").json()
    assert history["revision"] == history["total"] == 1
    assert len(history["items"][0]["table_before"]["rows"]) == 3
    assert len(history["items"][0]["table_after"]["rows"]) == 4
    assert len(read_table(client)["table"]["rows"]) == 4


def test_comparison_captures_structure_snapshot_during_concurrent_correction(client, monkeypatch):
    rows = editable_rows(read_table(client)["table"])
    target = rows[1][1]["id"]
    rows[1][1]["text"] = "99.00"
    assert update_table(client, rows).status_code == 200
    settings = current_config(client)
    captured, proceed = Event(), Event()
    original_comparator = app_module.compare_pdfs

    def blocked(old, new, config):
        captured.set()
        assert proceed.wait(10)
        return original_comparator(old, new, config)

    monkeypatch.setattr(app_module, "compare_pdfs", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.post, f"{JOB}/runs", json=settings)
        try:
            assert captured.wait(10)
            response = save(client, "cell_update", 1, target_id=target, value="100.00")
            assert response.status_code == 200, response.text
        finally:
            proceed.set()
        compared = future.result()
    assert compared.status_code == 201, compared.text
    run = compared.json()
    assert run["review_revision"] == 1
    changed = next(d for d in all_diffs(client, run) if d["row_key"] == "A001")
    assert changed["new_value"] == "99.00" and changed["delta"] == "89"
    report = export_rows(client, run)
    assert len(report["OCR修改记录"]) == 2
    assert "100.00" not in {value for row in report["结构修改明细"][1:] for value in row}
    assert catalog(client)["revision"] == 2
