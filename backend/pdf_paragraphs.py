from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from statistics import median


def _union(boxes):
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _inside(box, outer):
    return outer[0] <= (box[0] + box[2]) / 2 <= outer[2] and outer[1] <= (box[1] + box[3]) / 2 <= outer[3]


def _intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _text(value):
    return re.sub(r'\s+', '', value)


def _starts_block(text):
    compact = text.strip()
    return bool(re.match(r'^(?:[0-9０-９]{1,3}[.．、)](?![0-9０-９])|[（(][0-9一二三四五六七八九十]+[）)]|[一二三四五六七八九十]+[、．.]|[•●▪])', compact) or re.match(r'^[^。.!?！？:：]{1,24}[:：]', compact))


def _groups(view):
    appearance = view['appearance']
    width, height, unit = appearance['width_pt'], appearance['height_pt'], appearance['user_unit']
    cells = list({tuple(c['bbox']) for c in appearance.get('cells', [])})
    frames = list({tuple(b) for b in view['frames']})
    failed = [r['bbox'] for r in view['metadata'].get('ocr_regions', []) if r.get('affects_content') and r.get('status') != 'ok']
    buckets = defaultdict(list)
    skipped = 0
    for original_index, source in enumerate(view['original_lines']):
        assigned = defaultdict(list)
        for char in source['chars']:
            if not char['text'].strip():
                continue
            containing = [b for b in cells if _inside(char['bbox'], b)]
            kind = 'cell'
            if not containing:
                containing, kind = [b for b in frames if _inside(char['bbox'], b)], 'frame'
            if containing:
                boundary = min(containing, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
            else:
                kind = 'scope'
                boundary = tuple(next((b for b in view['scopes'] if _inside(char['bbox'], b)), view['scopes'][0]))
            assigned[(kind, tuple(boundary))].append(char)
        for (kind, boundary), chars in assigned.items():
            chars.sort(key=lambda c: c['bbox'][0])
            fragments = [[]]
            for char in chars:
                if fragments[-1] and kind == 'scope':
                    previous = fragments[-1][-1]
                    if (char['bbox'][0] - previous['bbox'][2]) * width > max(24, 2.5 * (char.get('size_pt') or 12)):
                        fragments.append([])
                fragments[-1].append(char)
            for fragment_index, fragment in enumerate(fragments):
                box = _union([c['bbox'] for c in fragment])
                sizes = [c['size_pt'] for c in fragment if c.get('size_pt')]
                baselines = [c['matrix'][5] * unit for c in fragment if c.get('matrix') and len(c['matrix']) == 6]
                reason = []
                if any(not c.get('reliable', True) or c.get('bad_unicode') or c.get('method', 'text') != 'text' for c in fragment) or any(_intersects(box, b) for b in failed):
                    reason.append('文字提取或 OCR 区域未可靠核验')
                if len(baselines) != len(fragment) or any(abs(c.get('angle', 0)) > .5 for c in fragment):
                    reason.append('缺少可比较的水平原生文字基线')
                if baselines and max(baselines) - min(baselines) > .8:
                    reason.append('同一物理行含多条基线，可能有上标或不同字段')
                if len(sizes) != len(fragment) or sizes and max(sizes) - min(sizes) > .5:
                    reason.append('同一物理行字号不一致或不可确认')
                line = {'text': ''.join(c['text'] for c in source['chars'] if c['bbox'][0] >= box[0] - .0001 and c['bbox'][2] <= box[2] + .0001), 'bbox': box,
                        'baseline_pt': median(baselines) if baselines else None,
                        'size_pt': median(sizes) if sizes else None, 'reasons': reason,
                        'original_line': original_index, 'boundary': list(boundary), 'reference_kind': kind}
                # Separate fields on an unruled physical row cannot establish a paragraph.
                key = (kind, boundary, original_index, fragment_index) if len(fragments) > 1 else (kind, boundary)
                buckets[key].append(line)
    groups = []
    for key, lines in buckets.items():
        lines.sort(key=lambda line: (line['bbox'][1], line['bbox'][0]))
        paragraphs = [[]]
        for line in lines:
            if paragraphs[-1]:
                previous = paragraphs[-1][-1]
                size = max(line['size_pt'] or 12, previous['size_pt'] or 12)
                gap = (line['bbox'][1] - previous['bbox'][1]) * height
                different_size = abs((line['size_pt'] or 0) - (previous['size_pt'] or 0)) > .5
                if gap <= .5 or gap > max(72, 5 * size) or different_size or _starts_block(line['text']):
                    paragraphs.append([])
            paragraphs[-1].append(line)
        for paragraph in paragraphs:
            if len(paragraph) == 1 and (key[0] == 'scope' or len(_text(paragraph[0]['text'])) < 5):
                skipped += 1
                continue
            groups.append({'lines': paragraph, 'bbox': _union([line['bbox'] for line in paragraph]),
                           'boundary': list(key[1]), 'reference_kind': key[0]})
    return groups, skipped


def _measurement(group, view):
    if group is None:
        return None
    lines, width = group['lines'], view['appearance']['width_pt']
    baselines = [line['baseline_pt'] for line in lines]
    gaps = [abs(y - x) for x, y in zip(baselines, baselines[1:])] if all(v is not None for v in baselines) else []
    starts = [line['bbox'][0] * width for line in lines]
    ends = [line['bbox'][2] * width for line in lines]
    centres = [(left + right) / 2 for left, right in zip(starts, ends)]
    widths = [right - left for left, right in zip(starts, ends)]
    boundary = group['boundary']
    reference = {'kind': group['reference_kind'], 'bbox': boundary,
                 'left_pt': boundary[0] * width, 'right_pt': boundary[2] * width,
                 'center_pt': (boundary[0] + boundary[2]) * width / 2}
    size = median([line['size_pt'] or 12 for line in lines])
    tolerance = max(.8, size * .06)
    spreads = {'left': max(starts) - min(starts), 'center': max(centres) - min(centres), 'right': max(ends) - min(ends)}
    alignment = None
    alignment_reason = '行宽相近或共同边缘不唯一，不能区分左、中、右对齐'
    if len(lines) >= 2 and max(widths) - min(widths) > max(3, size * .3):
        stable = [name for name, spread in spreads.items() if spread <= tolerance]
        if len(stable) == 1:
            alignment = stable[0]
            alignment_reason = '不同宽度原生行具有唯一稳定的起点、中心或终点'
            if alignment == 'center' and group['reference_kind'] != 'scope' and abs(median(centres) - reference['center_pt']) > tolerance:
                alignment, alignment_reason = None, '文字共同中心与已知边界中心不一致，内缩参考不明'
    elif len(lines) == 1 and group['reference_kind'] != 'scope':
        distances = {'left': abs(starts[0] - reference['left_pt']), 'center': abs(centres[0] - reference['center_pt']), 'right': abs(ends[0] - reference['right_pt'])}
        stable = [name for name, distance in distances.items() if distance <= tolerance]
        if len(stable) == 1:
            alignment = stable[0]
            alignment_reason = '单行与明确单元格/外框边缘或中心重合；不假定未知内缩'
        else:
            alignment_reason = '单行且框内缩未知，无法唯一判断对齐'
    if group['reference_kind'] == 'scope':
        reference['kind'] = 'observed_text_lines'
        reference['basis'] = '多行共同文字边缘/中心；报表范围仅用于区域归属，不反推正文页边距'
    else:
        reference['basis'] = '原生矢量恢复的单元格或闭合外框'
    return {'line_count': len(lines), 'baseline_gaps_pt': [round(v, 4) for v in gaps],
            'baseline_offsets_pt': [round(abs(v - baselines[0]), 4) for v in baselines] if baselines and all(v is not None for v in baselines) else [],
            'font_sizes_pt': [line['size_pt'] for line in lines], 'alignment': alignment,
            'alignment_reason': alignment_reason, 'reference': reference,
            'line_edges_pt': [{'left': round(x, 4), 'center': round(c, 4), 'right': round(y, 4)} for x, c, y in zip(starts, centres, ends)],
            'edge_spread_pt': {k: round(v, 4) for k, v in spreads.items()},
            'lines': [{'text': line['text'], 'bbox': line['bbox'], 'baseline_pt': round(line['baseline_pt'], 4) if line['baseline_pt'] is not None else None} for line in lines]}


def compare_paragraphs(old, new):
    old_groups, old_skipped = _groups(old)
    new_groups, new_skipped = _groups(new)
    candidates = []
    for i, a in enumerate(old_groups):
        av = [_text(line['text']) for line in a['lines']]
        for j, b in enumerate(new_groups):
            bv = [_text(line['text']) for line in b['lines']]
            exact = SequenceMatcher(None, av, bv, autojunk=False).ratio()
            fuzzy = SequenceMatcher(None, '\n'.join(av), '\n'.join(bv), autojunk=False).ratio()
            if fuzzy < .65 or (not exact and min(len(av), len(bv)) > 1):
                continue
            distance = sum(abs(x - y) for x, y in zip(a['boundary'], b['boundary']))
            candidates.append((exact * .7 + fuzzy * .3, -distance, i, j))
    pairs, used_a, used_b = [], set(), set()
    for score, distance, i, j in sorted(candidates, reverse=True):
        if i in used_a or j in used_b:
            continue
        contenders = [(s, d, x, y) for s, d, x, y in candidates if (x == i or y == j) and (x, y) != (i, j) and x not in used_a and y not in used_b]
        ambiguous = any(abs(score - s) < .02 and abs(distance - d) < .02 for s, d, _, _ in contenders)
        pairs.append((old_groups[i], new_groups[j], ambiguous))
        used_a.add(i); used_b.add(j)
    pairs.extend((group, None, True) for i, group in enumerate(old_groups) if i not in used_a)
    pairs.extend((None, group, True) for j, group in enumerate(new_groups) if j not in used_b)
    checks = []
    page_changed = any(abs(old['appearance'][key] - new['appearance'][key]) > .5 for key in ('width_pt', 'height_pt')) or old['appearance']['rotation'] != new['appearance']['rotation']
    for index, (a, b, ambiguous) in enumerate(pairs, 1):
        ma, mb = _measurement(a, old), _measurement(b, new)
        reasons = []
        if ambiguous or not a or not b:
            reasons.append('段落对应关系或行提取不完整，未作确定属性比较')
        for group in (a, b):
            if group:
                reasons.extend(reason for line in group['lines'] for reason in line['reasons'])
        matching = []
        if a and b:
            if len(a['lines']) != len(b['lines']):
                reasons.append('对应段落行数不同，可能换行或缺失提取，基线间距不作一一对应')
            for x, y in zip(a['lines'], b['lines']):
                ratio = SequenceMatcher(None, _text(x['text']), _text(y['text']), autojunk=False).ratio()
                matching.append({'old_text': x['text'], 'new_text': y['text'], 'similarity': round(ratio, 4)})
                if ratio < .85:
                    reasons.append('行序或对应文字不够一致')
        sizes_changed = bool(ma and mb and any(x is None or y is None or abs(x - y) > .5 for x, y in zip(ma['font_sizes_pt'], mb['font_sizes_pt'])))
        context = {'page_size_changed': any(abs(old['appearance'][key] - new['appearance'][key]) > .5 for key in ('width_pt', 'height_pt')),
                   'page_rotation_changed': old['appearance']['rotation'] != new['appearance']['rotation'],
                   'font_size_changed': sizes_changed,
                   'old_page_size_pt': [old['appearance']['width_pt'], old['appearance']['height_pt']],
                   'new_page_size_pt': [new['appearance']['width_pt'], new['appearance']['height_pt']]}
        simultaneous = (' 页面物理尺寸/方向同时变化。' if page_changed else '') + (' 对应文字字号同时变化。' if sizes_changed else '')
        measurements = {'unit': 'pt', 'old': ma, 'new': mb, 'matching': matching, 'context': context,
                        'basis': '原生字符矩阵的物理基线与可见文字边界；不是原编辑器段落设置',
                        'thresholds': {'baseline_gap_pt': .5, 'baseline_gap_relative': .02}}
        source = lambda view, group: {'kind': 'pdf', 'page': view['number'], 'bbox': group['bbox'] if group else _union(view['scopes']), 'method': 'text', 'location_precision': 'paragraph' if group else 'page'}
        for attribute in ('line_spacing', 'alignment'):
            limitations = list(dict.fromkeys(reasons))
            before = after = None
            changed = False
            if ma and mb:
                if attribute == 'line_spacing':
                    before, after = ma['baseline_gaps_pt'], mb['baseline_gaps_pt']
                    if min(ma['line_count'], mb['line_count']) < 2:
                        limitations.append('只有单行，无法测量相邻基线距离')
                    elif len(before) != ma['line_count'] - 1 or len(after) != mb['line_count'] - 1:
                        limitations.append('缺少完整基线坐标')
                    else:
                        changed = len(before) != len(after) or any(abs(x - y) > max(.5, .02 * max(x, y)) for x, y in zip(before, after))
                else:
                    before, after = ma['alignment'], mb['alignment']
                    if before is None or after is None:
                        limitations.append('至少一侧缺少唯一对齐证据：' + '；'.join(dict.fromkeys(m['alignment_reason'] for m in (ma, mb) if m['alignment'] is None)))
                    else:
                        changed = before != after
            status = 'unverified' if limitations else 'changed' if changed else 'unchanged'
            if attribute == 'line_spacing':
                description = '比较同序对应文字行的相邻原生基线距离；整块平移不改变此值。'
            else:
                description = '比较对应文字行相对于明确边界或共同文字基准的左/中/右几何对齐。'
            values = [None if value is None else (
                '；'.join(f'{gap:g} pt' for gap in value) if value else '未取得相邻基线距离')
                if attribute == 'line_spacing' else {'left': '左对齐', 'center': '居中', 'right': '右对齐'}.get(value)
                for value in (before, after)]
            checks.append({'id': f"p{old['number']}-{new['number']}-paragraph-{index}-{attribute}", 'attribute': attribute,
                           'status': status, 'old_value': values[0], 'new_value': values[1],
                           'old_source': source(old, a), 'new_source': source(new, b), 'measurements': measurements,
                           'message': description + simultaneous + (' 待核验：' + '；'.join(dict.fromkeys(limitations)) if limitations else ' 只确认有效物理几何，不将同时发生的缩放/字号变化归因为编辑器中的固定值、倍数或段落开关。')})
    if not pairs or old_skipped or new_skipped:
        raster_unverified = any(view['appearance'].get('raster_dominant') or any(c.get('method') == 'ocr' for line in view['lines'] for c in line['chars']) for view in (old, new))
        for attribute in ('line_spacing', 'alignment'):
            checks.append({'id': f"p{old['number']}-{new['number']}-paragraph-coverage-{attribute}", 'attribute': attribute,
                           'status': 'unverified', 'old_value': None, 'new_value': None,
                           'old_source': {'kind': 'pdf', 'page': old['number'], 'bbox': _union(old['scopes']), 'method': 'text', 'location_precision': 'page'},
                           'new_source': {'kind': 'pdf', 'page': new['number'], 'bbox': _union(new['scopes']), 'method': 'text', 'location_precision': 'page'},
                           'measurements': {'unit': 'pt', 'old': {'unsegmented_regions': old_skipped}, 'new': {'unsegmented_regions': new_skipped}, 'native_baselines_unavailable': raster_unverified},
                           'message': ('扫描或 OCR 文字没有可验证的原生基线，不能确定段落行距与对齐。' if raster_unverified else '') + '页面另有孤立文字、分散字段或无法可靠分段的区域；未将这些区域推断为段落属性，范围在本页汇总。'})
    return checks
