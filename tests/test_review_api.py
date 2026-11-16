import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

import backend.app as app_module
from backend.app import create_app, encode
from backend.pdf import suggest_pdf_pairs
from test_review import workbook


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path / 'data')
    store = app.state.store
    book = workbook()
    folder = store.folder / 'test-job'
    folder.mkdir()
    files = {}
    for side in ('old', 'new'):
        (folder / f'{side}.json').write_text(encode(book))
        original = b'%PDF-test-original-' + side.encode()
        (folder / f'{side}.pdf').write_bytes(original)
        files[side] = {'name': f'{side}.pdf', 'sha256': hashlib.sha256(original).hexdigest(), 'pages': book['pages']}
    job = {'id': 'test-job', 'kind': 'pdf', 'files': files, 'suggested_pairs': suggest_pdf_pairs(book, book)}
    with store.connect() as db:
        db.execute('INSERT INTO jobs VALUES (?,?)', (job['id'], encode(job)))
    with TestClient(app) as client:
        yield client


def catalog(client, side='new'):
    return client.get('/api/jobs/test-job/review', params={'side': side}).json()


def save(client, action, revision, side='new', **kwargs):
    return client.post('/api/jobs/test-job/review', json={
        'expected_revision': revision, 'side': side, 'action': action, 'reason': '核对原图', **kwargs})


def compare(client):
    job = client.get('/api/jobs/test-job').json()
    result = client.post('/api/jobs/test-job/runs', json={'pairs': job['suggested_pairs']})
    assert result.status_code == 201, result.text
    return result.json()


def export_rows(client, run):
    book = load_workbook(BytesIO(client.get(f"/api/runs/{run['id']}/export").content))
    return {sheet.title: list(sheet.values) for sheet in book}


def test_review_corrections_snapshots_history_export_and_originals(client):
    store = client.app.state.store
    originals = {p.name: p.read_bytes() for p in (store.folder / 'test-job').iterdir()}
    first = compare(client)
    first_report = export_rows(client, first)
    data = catalog(client)
    assert data['revision'] == 0 and data['total'] == 18
    amount = next(i for i in data['items'] if i['page'] == 1 and i['cell'] == 'B2')
    response = save(client, 'cell_update', 0, target_id=amount['id'], value='99.00')
    assert response.status_code == 200, response.text
    assert response.json()['event']['before_text'] == '10.00'
    changed = next(i for i in catalog(client)['items'] if i['id'] == amount['id'])
    assert changed['original_text'] == '10.00' and changed['current_text'] == '99.00'
    assert changed['reviewed'] is False
    second = compare(client)
    assert second['review_revision'] == 1
    assert second['summary']['by_kind']['ocr_suspected'] == 1
    revision = 1
    for side in ('old', 'new'):
        for page in (1, 2):
            result = save(client, 'page_verify', revision, side=side, page=page)
            assert result.status_code == 200, result.text
            revision += 1
    third = compare(client)
    assert third['summary']['by_kind'] == {'value_changed': 1}
    # The continuation-table configuration warning remains even after OCR verification.
    assert third['status'] == 'completed_with_issues'
    assert third['coverage']['warnings']
    assert third['review_revision'] == 5
    assert all(p['reviewed'] for p in third['review_pages']['new'])
    history = client.get('/api/jobs/test-job/review-history').json()
    assert history['total'] == 5 and history['items'][0]['revision'] == 5
    report = export_rows(client, third)
    assert len(report['OCR修改记录']) == 6
    assert export_rows(client, first) == first_report
    assert client.get(f"/api/runs/{first['id']}").json() == first
    assert len(client.get('/api/jobs/test-job/runs').json()['items']) == 3
    # Saving the same value is still an audited correction and invalidates verification.
    assert save(client, 'cell_update', 5, target_id=amount['id'], value='99.00').status_code == 200
    assert catalog(client)['pages'][0]['reviewed'] is False
    assert all(p.read_bytes() == originals[p.name] for p in (store.folder / 'test-job').iterdir())


def test_stale_writes_and_forged_targets_do_not_append_events(client):
    target = catalog(client)['items'][0]['id']
    assert save(client, 'cell_verify', 0, side='old', target_id=target).status_code == 422
    assert save(client, 'cell_verify', 0, target_id=target, page=2).status_code == 422
    assert save(client, 'page_verify', 0, page=99).status_code == 422
    assert save(client, 'cell_update', 0, target_id=target).status_code == 422
    assert save(client, 'cell_verify', 0, target_id=target, reviewed=True).status_code == 422
    assert catalog(client)['revision'] == 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, client, 'cell_verify', 0, target_id=target) for _ in range(2)]
        assert sorted(f.result().status_code for f in futures) == [200, 409]
    assert catalog(client)['revision'] == 1
    assert client.get('/api/jobs/test-job/review-history').json()['total'] == 1


def test_comparison_captures_one_revision_during_concurrent_save(client, monkeypatch):
    captured, proceed = Event(), Event()
    original_comparator = app_module.compare_pdfs

    def blocked(old, new, config):
        captured.set()
        assert proceed.wait(10)
        return original_comparator(old, new, config)

    monkeypatch.setattr(app_module, 'compare_pdfs', blocked)
    target = next(i['id'] for i in catalog(client)['items'] if i['page'] == 1 and i['cell'] == 'B2')
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(compare, client)
        try:
            assert captured.wait(10)
            assert save(client, 'cell_update', 0, target_id=target, value='999').status_code == 200
        finally:
            proceed.set()
        run = future.result()
    assert run['review_revision'] == 0
    assert 'ocr_suspected' not in run['summary']['by_kind']
    assert len(export_rows(client, run)['OCR修改记录']) == 1
    assert catalog(client)['revision'] == 1


def test_legacy_runs_export_revision_zero(client):
    run = compare(client)
    del run['review_revision']
    del run['review_pages']
    with client.app.state.store.connect() as db:
        db.execute('UPDATE runs SET metadata=? WHERE id=?', (json.dumps(run), run['id']))
    assert save(client, 'page_verify', 0, page=1).status_code == 200
    assert len(export_rows(client, run)['OCR修改记录']) == 1
