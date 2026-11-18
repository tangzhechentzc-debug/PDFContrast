import copy
import gzip
import json
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from reportlab.pdfgen import canvas

from backend.app import create_app
from backend.pdf import compare_pdfs, extract_pdf
from backend.pdf_assets import report_appearances
from test_pdf_api import text_pdf, upload_pdf, compare_pdf


def test_disk_backed_comparison_preserves_content_and_releases_snapshots(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.pdf.EAGER_PAGE_LIMIT', 0)
    books = []
    for side, amount in [('old', '12000'), ('new', '12800')]:
        path = tmp_path / f'{side}.pdf'
        path.write_bytes(text_pdf(amount))
        book = extract_pdf(path, tmp_path / side)
        books.append(book)
        assert book['asset_storage'] == 'page_files_v1'
        assert not list((tmp_path / side).glob('page-*.png'))
        assert 'appearance' not in book['pages'][0]
        with gzip.open(tmp_path / side / book['pages'][0]['appearance_file'], 'rt') as stream:
            appearance = json.load(stream)
        assert appearance['chars'] and appearance['render']['rgb_png_base64']
    result = compare_pdfs(*books, {'pairs': []})
    assert any(d['old_value'] == '12000' and d['new_value'] == '12800' for d in result['diffs'])
    assert all('appearance' not in book['pages'][0] for book in books)


def test_missing_snapshot_is_issue_not_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.pdf.EAGER_PAGE_LIMIT', 0)
    path = tmp_path / 'input.pdf'
    path.write_bytes(text_pdf())
    book = extract_pdf(path, tmp_path / 'assets')
    (tmp_path / 'assets' / book['pages'][0]['appearance_file']).unlink()
    result = compare_pdfs(book, copy.deepcopy(book), {'pairs': []})
    assert result['summary']['issues'] > 0
    assert any(d['kind'] == 'appearance_unavailable' for d in result['diffs'])


def test_page_snapshots_are_released_even_if_comparison_raises(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.pdf.EAGER_PAGE_LIMIT', 0)
    path = tmp_path / 'input.pdf'
    path.write_bytes(text_pdf())
    book = extract_pdf(path, tmp_path / 'assets')
    try:
        with report_appearances({'old': book}, {'old': {'pages': [1]}}):
            assert book['pages'][0]['appearance']['chars']
            raise RuntimeError('comparison interrupted')
    except RuntimeError:
        pass
    assert 'appearance' not in book['pages'][0]


def test_total_pixel_and_cell_counts_are_not_document_truncation_limits(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.pdf.MAX_EAGER_PIXELS', 1)
    monkeypatch.setattr('backend.pdf.MAX_CELLS', 8)
    path = tmp_path / 'many.pdf'
    writer = canvas.Canvas(str(path), pagesize=(200, 200))
    for number in range(4):
        for x in (20, 100, 180):
            writer.line(x, 60, x, 140)
        for y in (60, 100, 140):
            writer.line(20, y, 180, y)
        for x, y, text in [(30, 120, 'Code'), (110, 120, 'Amount'), (30, 80, str(number)), (110, 80, '12')]:
            writer.drawString(x, y, text)
        writer.showPage()
    writer.save()
    book = extract_pdf(path, tmp_path / 'assets')
    assert len(book['pages']) == 4
    assert sum(len(s['cells']) for s in book['sheets']) > 8
    assert len(list((tmp_path / 'assets').glob('appearance-*.json.gz'))) == 4


def test_lazy_page_endpoint_caches_both_resolutions_and_compares(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.pdf.EAGER_PAGE_LIMIT', 0)
    data_dir = tmp_path / 'data'
    with TestClient(create_app(data_dir)) as client:
        job = upload_pdf(client, text_pdf(), text_pdf('12800'))
        asset_dir = data_dir / job['id'] / 'old-pages'
        assert not list(asset_dir.glob('page-*.png'))
        run = compare_pdf(client, job)
        assert run['summary']['changes'] > 0
        url = f"/api/jobs/{job['id']}/pages/old/1.png"
        standard = client.get(url)
        high = client.get(url + '?dpi=288')
        assert standard.status_code == high.status_code == 200
        with Image.open(BytesIO(standard.content)) as a, Image.open(BytesIO(high.content)) as b:
            assert b.size == (a.width * 2, a.height * 2)
        assert (asset_dir / 'page-1.png').is_file()
        assert (asset_dir / 'page-1-288dpi.png').is_file()
        assert client.get(url).content == standard.content


def test_native_unmodified_review_fast_path_matches_full_review(tmp_path, monkeypatch):
    from backend.app import Store
    from backend.review import apply_reviews

    data_dir = tmp_path / 'data'
    with TestClient(create_app(data_dir)) as client:
        job = upload_pdf(client, text_pdf(), text_pdf())
    store = Store(data_dir)
    original = store.workbook(job['id'], 'old')
    expected = apply_reviews(original, [])

    def unexpected(*args):
        raise AssertionError('native unchanged input must not clone its entire book')

    monkeypatch.setattr('backend.app.apply_reviews', unexpected)
    assert store.reviewed_book(job['id'], 'old', []) == expected
    assert 'reviewed' not in original['pages'][0]


def test_review_fast_path_never_skips_ocr_or_review_events(tmp_path, monkeypatch):
    from backend.app import Store

    store = Store(tmp_path / 'data')
    calls = []
    base = {'report_inventory_version': 2, 'reports': [],
            'pages': [{'number': 1, 'method': 'text'}],
            'sheets': [{'name': 'p1', 'cells': {'A1': {'value': 'a', 'pdf_source': {'method': 'text'}}}}]}
    monkeypatch.setattr(store, 'workbook', lambda *args: copy.deepcopy(base))
    monkeypatch.setattr('backend.app.apply_reviews', lambda book, events: calls.append(events) or book)
    for mode in ('ocr_page', 'ocr_cell', 'requires_review', 'structure_unconfirmed', 'events'):
        candidate = copy.deepcopy(base)
        events = []
        if mode == 'ocr_page':
            candidate['pages'][0]['method'] = 'ocr'
        elif mode == 'ocr_cell':
            candidate['sheets'][0]['cells']['A1']['pdf_source']['method'] = 'ocr'
        elif mode == 'requires_review':
            candidate['sheets'][0]['cells']['A1']['requires_review'] = True
        elif mode == 'structure_unconfirmed':
            candidate['pages'][0]['structure_unconfirmed'] = True
        else:
            events = [{'side': 'old', 'action': 'cell_update', 'revision': 1}]
        monkeypatch.setattr(store, 'workbook', lambda *args, candidate=candidate: candidate)
        store.reviewed_book('job', 'old', events)
    assert len(calls) == 5
