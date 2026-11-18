import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Barrier, Event

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
        (folder / f'{side}.json').write_text(encode(book), encoding='utf-8')
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


def test_current_job_rebuilds_complete_saved_metadata_after_review(client, monkeypatch):
    from unittest.mock import Mock

    store = client.app.state.store
    stored = store.job('test-job')
    book = store.workbook('test-job', 'new')
    for sheet in book['sheets']:
        sheet['cell_geometry'] = []
    for side in ('old', 'new'):
        (store.folder / 'test-job' / f'{side}.json').write_text(encode(book), encoding='utf-8')
        stored['files'][side]['sheets'] = [
            {key: value for key, value in sheet.items() if key not in ('cells', 'merged_ranges')}
            for sheet in book['sheets']]
    with store.connect() as db:
        db.execute('UPDATE jobs SET metadata=? WHERE id=?', (encode(stored), 'test-job'))
    amount = next(item for item in catalog(client)['items'] if item['page'] == 1 and item['cell'] == 'B2')
    response = save(client, 'cell_update', 0, target_id=amount['id'], value='99.00')
    assert response.status_code == 200, response.text
    pairing = Mock(wraps=app_module.suggest_pdf_pairs)
    monkeypatch.setattr(app_module, 'suggest_pdf_pairs', pairing)
    monkeypatch.setattr(store, 'reviews', Mock(side_effect=AssertionError('do not reload job for reviews')))
    response = client.get('/api/jobs/test-job')
    assert response.status_code == 200, response.text
    assert response.json()['review_revision'] == 1
    assert all('cell_geometry' not in sheet for sheet in response.json()['files']['new']['sheets'])
    assert all('cell_geometry' in sheet for sheet in store.workbook('test-job', 'new')['sheets'])
    pairing.assert_called_once()
    updated_book = pairing.call_args.args[1]
    assert any(sheet['cells'].get('B2', {}).get('current_text') == '99.00' for sheet in updated_book['sheets'])


def test_review_endpoint_strips_internal_page_fields_and_skips_inventory_rebuild(client, monkeypatch):
    store = client.app.state.store
    book = store.workbook('test-job', 'new')
    book['report_inventory_version'] = 2
    book['reports'] = []
    book['pages'][0].update(
        appearance={'chars': ['private']},
        native_lines=[{'text': 'Native heading'}],
        native_text='Native heading',
        evidence_lines=[{'text': 'Evidence'}],
        ocr_regions=[{'text': 'OCR region'}],
        ocr_annotations=[{'text': 'Annotation'}],
        appearance_path='/private/appearance.json',
        image_path='/private/page.png',
        future_internal_payload={'secret': True},
    )
    (store.folder / 'test-job' / 'new.json').write_text(encode(book), encoding='utf-8')
    # 调用 /review 仅返回核验列表与脱敏页信息，不应触发全书报表清单重建
    monkeypatch.setattr(
        'backend.reports.attach_report_inventory',
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('review endpoint must not rebuild inventory')),
    )
    response = client.get('/api/jobs/test-job/review', params={'side': 'new'})
    assert response.status_code == 200, response.text
    page = response.json()['pages'][0]
    for public_key in ('number', 'method', 'status', 'reviewed', 'reviewable', 'reviewed_cells', 'total_cells', 'has_native_text'):
        assert public_key in page
    assert page['has_native_text'] is True
    internal_keys = {
        'appearance', 'native_lines', 'native_text', 'evidence_lines',
        'ocr_regions', 'ocr_annotations', 'appearance_path', 'image_path', 'future_internal_payload',
    }
    assert not (set(page) & internal_keys)


def test_save_review_lock_optimization_validation_matrix_and_concurrent_409(client, monkeypatch):
    store = client.app.state.store
    real_apply = app_module.apply_reviews
    recorded_calls = []

    def tracking_apply(book, events, *args, **kwargs):
        recorded_calls.append({'args': args, 'kwargs': dict(kwargs)})
        return real_apply(book, events, *args, **kwargs)

    monkeypatch.setattr(app_module, 'apply_reviews', tracking_apply)

    # 1. 验证 GET /review 与单次 save_review 均仅调用 1 次 apply_reviews，且显式关闭报表清单重建
    amount = next(i for i in catalog(client)['items'] if i['page'] == 1 and i['cell'] == 'B2')
    assert recorded_calls == [{'args': (), 'kwargs': {'rebuild_inventory': False}}]
    recorded_calls.clear()
    res_cell = save(client, 'cell_update', 0, target_id=amount['id'], value='66.00')
    assert res_cell.status_code == 200, res_cell.text
    assert recorded_calls == [{'args': (), 'kwargs': {'rebuild_inventory': False}}]

    recorded_calls.clear()
    res_page = save(client, 'page_verify', 1, page=1)
    assert res_page.status_code == 200, res_page.text
    assert recorded_calls == [{'args': (), 'kwargs': {'rebuild_inventory': False}}]

    # 2. 验证精确状态码矩阵（404 / 409 / 422）且不追加事件
    assert client.post('/api/jobs/missing-job/review', json={
        'expected_revision': 0, 'side': 'new', 'action': 'page_verify', 'page': 1, 'reason': '检查',
    }).status_code == 404
    assert save(client, 'page_verify', 0, page=2).status_code == 409

    with store.connect() as db:
        db.execute('INSERT INTO jobs VALUES (?,?)', ('xlsx-job', encode({'id': 'xlsx-job', 'kind': 'xlsx', 'files': {}})))
    assert client.post('/api/jobs/xlsx-job/review', json={
        'expected_revision': 0, 'side': 'new', 'action': 'page_verify', 'page': 1, 'reason': '检查',
    }).status_code == 422

    assert save(client, 'page_verify', 2, page=99).status_code == 422
    assert client.post('/api/jobs/test-job/review', json={
        'expected_revision': 2, 'side': 'new', 'action': 'page_verify', 'page': 1, 'reason': '   ',
    }).status_code == 422
    assert client.post('/api/jobs/test-job/review', json={
        'expected_revision': 2, 'side': 'new', 'action': 'invalid_action', 'page': 1, 'reason': '检查',
    }).status_code == 422

    # 构造文本页与结构未确认页，验证 page_verify 均返回 422
    text_book = workbook()
    text_book['pages'][0]['method'] = 'text'
    unconfirmed_book = workbook()
    unconfirmed_book['pages'][0]['structure_unconfirmed'] = True
    for job_id, custom_book in (('text-page-job', text_book), ('unconfirmed-job', unconfirmed_book)):
        folder = store.folder / job_id
        folder.mkdir()
        files = {}
        for side in ('old', 'new'):
            (folder / f'{side}.json').write_text(encode(custom_book), encoding='utf-8')
            files[side] = {'name': f'{side}.pdf', 'sha256': side, 'pages': custom_book['pages']}
        with store.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?,?)', (job_id, encode({'id': job_id, 'kind': 'pdf', 'files': files})))
        resp = client.post(f'/api/jobs/{job_id}/review', json={
            'expected_revision': 0, 'side': 'new', 'action': 'page_verify', 'page': 1, 'reason': '核对',
        })
        assert resp.status_code == 422
        assert store.reviews(job_id) == (0, [])

    assert store.reviews('test-job')[0] == 2

    # 3. 确定性并发屏障测试：两请求锁外同时完成 apply_reviews 与校验后并发竞争写锁，结果必为 [200, 409]
    barrier = Barrier(2)

    def barrier_apply(book, events, *args, **kwargs):
        result = real_apply(book, events, *args, **kwargs)
        if book.get('review_namespace', '').startswith('test-job:'):
            barrier.wait(5)
        return result

    monkeypatch.setattr(app_module, 'apply_reviews', barrier_apply)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(save, client, 'cell_verify', 2, target_id=amount['id']),
            pool.submit(save, client, 'page_verify', 2, page=2),
        ]
        statuses = sorted(future.result().status_code for future in futures)
    assert statuses == [200, 409]
    assert store.reviews('test-job')[0] == 3
