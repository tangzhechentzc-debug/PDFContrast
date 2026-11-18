"""Page-backed appearance snapshots for complete, bounded PDF extraction."""
from contextlib import contextmanager
import gzip
import json
from pathlib import Path


def save_appearance(folder: Path, page: dict):
    name = f"appearance-{page['number']}.json.gz"
    destination = folder / name
    temporary = destination.with_suffix('.tmp')
    with gzip.open(temporary, 'wt', encoding='utf-8', compresslevel=1) as stream:
        json.dump(page['appearance'], stream, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    temporary.replace(destination)
    page['appearance_file'] = name
    page.pop('appearance')


@contextmanager
def report_appearances(books, entry):
    loaded = []
    try:
        for side, book in books.items():
            selected = set((entry.get(side) or {}).get('pages', []))
            folder = book.get('asset_directory')
            for page in book.get('pages', []):
                if page.get('number', page.get('page')) not in selected or page.get('appearance') or not page.get('appearance_file'):
                    continue
                filename = page['appearance_file']
                if not folder or Path(filename).name != filename:
                    continue
                try:
                    with gzip.open(Path(folder) / filename, 'rt', encoding='utf-8') as stream:
                        page['appearance'] = json.load(stream)
                    loaded.append(page)
                except (OSError, ValueError, EOFError):
                    # The comparison engine reports absent snapshots as unverified.
                    page.pop('appearance', None)
        yield
    finally:
        for page in loaded:
            page.pop('appearance', None)
