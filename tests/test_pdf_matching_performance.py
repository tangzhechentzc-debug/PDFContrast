import time

from backend.pdf_appearance import _content_pairs


class CountedFragment(dict):
    reads = 0

    def __getitem__(self, key):
        if key == 'key':
            type(self).reads += 1
        return super().__getitem__(key)


def fragments(page_count, rows):
    output = []
    for page in range(1, page_count + 1):
        view = {'number': page}
        for row in range(rows):
            output.append(CountedFragment(key=f'Repeated field {row}', text=f'Repeated field {row}',
                          view=view, cell=None, column='', row_key='', context='',
                          bbox=[.1, .1 + row / 100, .8, .11 + row / 100]))
    return output


def test_confirmed_pages_bound_repeated_fragment_search():
    left, right = fragments(110, 12), fragments(110, 12)
    CountedFragment.reads = 0
    result = _content_pairs(left, right, {(p, p) for p in range(1, 111)})
    assert len(result) == len(left)
    assert all(a and b and not ambiguous and a['view']['number'] == b['view']['number']
               for a, b, ambiguous, _ in result)
    # Repeating an already paired page must not multiply every other page's
    # candidate scan. This budget is independent of machine speed.
    assert CountedFragment.reads < len(left) * 100


def test_ambiguous_similarity_candidates_keep_uncertainty_with_bounded_runtime():
    left, right = fragments(1, 150), fragments(1, 150)
    for index, (a, b) in enumerate(zip(left, right)):
        a['key'] = f'long shared label old {index:03d}'
        b['key'] = f'long shared label new {index:03d}'
    started = time.monotonic()
    result = _content_pairs(left, right, set())
    elapsed = time.monotonic() - started
    assert len(result) == 300
    assert all(ambiguous and (a is None or b is None) for a, b, ambiguous, _ in result)
    # A generous guard against scanning the whole score matrix for each edge.
    assert elapsed < 10


def merged_header_fragments():
    output = []
    for page in range(1, 5):
        view = {'number': page}
        for column, text in ((4, '个'), (5, '1'), (7, '125.15'), (13, '125.15')):
            output.append({'key': text, 'text': text, 'view': view,
                           'cell': {'table_index': 0, 'row': 5, 'column': column},
                           'column': '项目名称' if column < 7 else None,
                           'row_key': 'D00003', 'context': None,
                           'bbox': [column / 20, .22, column / 20 + .04, .23]})
    return output


def test_same_native_regions_under_repeated_merged_headers_do_not_invent_changes():
    left, right = merged_header_fragments(), merged_header_fragments()
    pairs = _content_pairs(left, right, {(p, p) for p in range(1, 5)})
    assert len(pairs) == len(left)
    assert all(a and b and not ambiguous and a['key'] == b['key'] and a['bbox'] == b['bbox']
               and a['view']['number'] == b['view']['number'] for a, b, ambiguous, _ in pairs)


def test_exact_region_evidence_does_not_hide_a_changed_numeric_field():
    left, right = merged_header_fragments(), merged_header_fragments()
    right[2]['key'] = right[2]['text'] = '130.00'
    pairs = _content_pairs(left, right, {(p, p) for p in range(1, 5)})
    assert any(a is left[2] and (b is None or b['key'] != a['key']) for a, b, _, _ in pairs)
    assert any(b is right[2] and (a is None or a['key'] != b['key']) for a, b, _, _ in pairs)
