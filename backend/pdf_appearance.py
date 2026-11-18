from __future__ import annotations

import base64
import copy
import hashlib
import io
import math
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from statistics import median

import cv2
import numpy as np
from PIL import Image, ImageDraw
from pdfminer.pdftypes import resolve1
from openpyxl.utils import get_column_letter

from backend.pdf_paragraphs import compare_paragraphs


VISUAL_DPI = 96
_CJK = re.compile(r'[\u3400-\u9fff]')
_BAD = re.compile(r'\(cid:\d+\)|\ufffd|\x00')
_CATEGORIES = ('content', 'structure', 'format', 'layout')


def _union(boxes):
    boxes = [box for box in boxes if box]
    return [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else [0, 0, 1, 1]


def _inside(box, region):
    return region[0] <= (box[0] + box[2]) / 2 <= region[2] and region[1] <= (box[1] + box[3]) / 2 <= region[3]


def _color(value, space=None):
    values = list(value) if isinstance(value, (list, tuple)) else [value] if isinstance(value, (int, float)) else None
    if values is None:
        return None
    values = [round(float(v), 4) for v in values]
    if len(values) == 1:
        return [values[0]] * 3
    if len(values) == 3:
        return values
    if len(values) == 4 and (not space or 'CMYK' in str(space)):
        c, m, y, k = values
        return [round(1 - min(1, component + k), 4) for component in (c, m, y)]
    return {'space': str(space), 'values': values}


def _font_name(name):
    return re.sub(r'^[A-Z]{6}\+', '', str(name).strip('/\''))


def _lines(chars):
    groups = []
    rotated = [c for c in chars if abs(c.get('angle', 0)) > .5]
    regular = [c for c in chars if abs(c.get('angle', 0)) <= .5]
    for char in sorted(regular, key=lambda c: (c['bbox'][1], c['bbox'][0], c.get('order', 0))):
        box = char['bbox']
        height = box[3] - box[1]
        centre = (box[1] + box[3]) / 2
        candidates = [g for g in groups[-4:] if abs(centre - g['centre']) <= .6 * max(height, g['height'])]
        if candidates:
            group = min(candidates, key=lambda g: abs(centre - g['centre']))
            group['chars'].append(char)
        else:
            groups.append({'centre': centre, 'height': height, 'chars': [char]})
    result = []
    for group in sorted(groups, key=lambda g: g['centre']):
        row = sorted(group['chars'], key=lambda c: (c['bbox'][0], c.get('order', 0)))
        text = ''.join(c['text'] for c in row)
        result.append({'text': text, 'bbox': _union(c['bbox'] for c in row), 'chars': row})
    rotated_groups = []
    for char in rotated:
        angle = math.radians(char['angle'])
        box = char['bbox']
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        axis = cx * math.sin(angle) + cy * math.cos(angle)
        group = next((g for g in rotated_groups if abs(g['angle'] - char['angle']) < .5 and abs(g['axis'] - axis) < .012), None)
        if group:
            group['chars'].append(char)
        else:
            rotated_groups.append({'angle': char['angle'], 'axis': axis, 'chars': [char]})
    for group in rotated_groups:
        # PDF drawing order preserves the glyph sequence for rotated runs.
        row = sorted(group['chars'], key=lambda c: c.get('order', 0))
        result.append({'text': ''.join(c['text'] for c in row), 'bbox': _union(c['bbox'] for c in row), 'chars': row})
    result.sort(key=lambda line: (line['bbox'][1], line['bbox'][0]))
    return result


def _strokes(vectors, width, height):
    pieces = []
    for vector in vectors:
        if not vector.get('stroke_enabled', True):
            continue
        b = vector['bbox_pt']
        segments = ([b[0], b[1], b[2], b[1]], [b[2], b[1], b[2], b[3]], [b[0], b[3], b[2], b[3]], [b[0], b[1], b[0], b[3]]) if vector['kind'] == 'rect' else [b] if vector['kind'] == 'line' else []
        for x0, y0, x1, y1 in segments:
            if abs(y1 - y0) <= .4:
                orientation, axis, start, end = 'horizontal', (y0 + y1) / 2, min(x0, x1), max(x0, x1)
            elif abs(x1 - x0) <= .4:
                orientation, axis, start, end = 'vertical', (x0 + x1) / 2, min(y0, y1), max(y0, y1)
            else:
                orientation, axis, start, end = 'diagonal', 0, 0, 0
            if orientation != 'diagonal' and end - start < .1:
                continue
            pieces.append({'orientation': orientation, 'axis': axis, 'start': start, 'end': end,
                           'bbox_pt': [x0, y0, x1, y1], 'styles': [{'linewidth_pt': vector['linewidth_pt'], 'dash': vector['dash'], 'stroke': vector['stroke']}]})
    merged = []
    for piece in sorted(pieces, key=lambda p: (p['orientation'], p['axis'], p['start'])):
        existing = next((p for p in reversed(merged) if p['orientation'] == piece['orientation'] != 'diagonal' and abs(p['axis'] - piece['axis']) <= .5 and piece['start'] <= p['end'] + 1), None)
        if existing:
            existing['start'] = min(existing['start'], piece['start'])
            existing['end'] = max(existing['end'], piece['end'])
            existing['styles'].extend(s for s in piece['styles'] if s not in existing['styles'])
        else:
            merged.append(copy.deepcopy(piece))
    for stroke in merged:
        if stroke['orientation'] == 'horizontal':
            stroke['bbox_pt'] = [stroke['start'], stroke['axis'], stroke['end'], stroke['axis']]
        elif stroke['orientation'] == 'vertical':
            stroke['bbox_pt'] = [stroke['axis'], stroke['start'], stroke['axis'], stroke['end']]
        b = stroke['bbox_pt']
        stroke['bbox'] = [b[0] / width, b[1] / height, b[2] / width, b[3] / height]
    return merged


def _frames(strokes, width, height):
    horizontal = [s for s in strokes if s['orientation'] == 'horizontal']
    vertical = [s for s in strokes if s['orientation'] == 'vertical']
    frames = []
    for i, top in enumerate(horizontal):
        for bottom in horizontal[i + 1:]:
            if bottom['axis'] - top['axis'] < 4:
                continue
            tolerance=max(1,max(style['linewidth_pt'] for line in (top,bottom) for style in line['styles'])/2+.5)
            sides=[]
            for end in ('start','end'):
                candidates=[v for v in vertical if abs(v['axis']-top[end])<=tolerance and abs(v['axis']-bottom[end])<=tolerance and v['start']<=top['axis']+tolerance and v['end']>=bottom['axis']-tolerance]
                if len(candidates)!=1:break
                sides.append(candidates[0])
            if len(sides)==2 and sides[0]['axis']<sides[1]['axis']:
                frames.append([sides[0]['axis']/width,top['axis']/height,sides[1]['axis']/width,bottom['axis']/height])
    return [b for i, b in enumerate(frames) if not any(i != j and o[0] <= b[0] + .0001 and o[1] <= b[1] + .0001 and o[2] >= b[2] - .0001 and o[3] >= b[3] - .0001 and (o[2] - o[0]) * (o[3] - o[1]) > (b[2] - b[0]) * (b[3] - b[1]) + .0001 for j, o in enumerate(frames))]


def _extract_page_tables(page) -> list[dict]:
    """每页仅调用一次原生表格识别与文本提取，保留原始 PDF 坐标及单元格合并跨度。"""
    extracted_tables = []
    for table in page.find_tables():
        rows = [list(row.cells) for row in table.rows]
        boxes = [b for row in rows for b in row if b is not None]
        xs = sorted({round(v, 4) for b in boxes for v in (b[0], b[2])})
        ys = sorted({round(v, 4) for b in boxes for v in (b[1], b[3])})
        spans = []
        for r, row in enumerate(rows, 1):
            for c, b in enumerate(row, 1):
                if b is None:
                    continue
                c1, c2 = xs.index(round(b[0], 4)) + 1, xs.index(round(b[2], 4))
                r1, r2 = ys.index(round(b[1], 4)) + 1, ys.index(round(b[3], 4))
                spans.append({
                    'row': r,
                    'column': c,
                    'bbox': b,
                    'row_start': r1,
                    'row_end': r2,
                    'column_start': c1,
                    'column_end': c2,
                })
        extracted_tables.append({
            'bbox': table.bbox,
            'rows': rows,
            'row_count': len(rows),
            'column_count': max((len(row) for row in rows), default=0),
            'extracted': table.extract(),
            'spans': spans,
        })
    return extracted_tables


def extract_page_appearance(page, page_image, *, tables=None) -> dict:
    unit = float(resolve1(page.page_obj.attrs.get('UserUnit', 1)))
    width, height = float(page.width) * unit, float(page.height) * unit
    origin_x, origin_y = page.bbox[:2]

    def box(item):
        return [(float(item['x0']) - origin_x) / page.width, (float(item['top']) - origin_y) / page.height,
                (float(item['x1']) - origin_x) / page.width, (float(item['bottom']) - origin_y) / page.height]

    font_info = {}
    for value in resolve1(page.page_obj.resources.get('Font', {})).values():
        font = resolve1(value)
        descendants = resolve1(font.get('DescendantFonts', []))
        face = resolve1(descendants[0]) if descendants else font
        descriptor = resolve1(face.get('FontDescriptor', {}))
        name = _font_name(font.get('BaseFont', ''))
        font_info[name] = {'weight': descriptor.get('FontWeight'), 'flags': descriptor.get('Flags'), 'italic_angle': descriptor.get('ItalicAngle'),
                           'embedded': any(k in descriptor for k in ('FontFile', 'FontFile2', 'FontFile3'))}
    chars = []
    for index, char in enumerate(page.chars):
        name = _font_name(char.get('fontname', ''))
        info = font_info.get(name, {})
        weight = info.get('weight')
        bold = True if re.search(r'bold|heavy|black', name, re.I) or int(info.get('flags') or 0) & 262144 else bool(weight >= 600) if isinstance(weight, (int, float)) else False if re.search(r'regular|normal|roman', name, re.I) else None
        matrix = list(char.get('matrix', (1, 0, 0, 1, 0, 0)))
        angle = (math.degrees(math.atan2(matrix[1], matrix[0])) + 180) % 360 - 180
        horizontal = abs(math.sin(math.radians(angle))) < .01
        chars.append({'id': f'c{index}', 'order': index, 'text': char.get('text', ''), 'bbox': box(char), 'font': name,
                      'raw_font': char.get('fontname', ''), 'size_pt': round(float(char.get('size', 0)) * unit, 4) if horizontal else None,
                      'raw_bbox_size_pt': round(float(char.get('size', 0)) * unit, 4), 'matrix': matrix, 'angle': round(angle, 4),
                      'color': _color(char.get('non_stroking_color'), char.get('ncs')) or [0, 0, 0], 'bold': bold,
                      'italic_angle': info.get('italic_angle'), 'bad_unicode': bool(_BAD.search(char.get('text', ''))),
                      'method': 'text', 'reliable': not bool(_BAD.search(char.get('text', '')))})
    lines = _lines(chars)
    runs = []
    for number, line in enumerate(lines):
        groups = []
        for char in line['chars']:
            signature = (char['font'], char['size_pt'], str(char['color']), char['bold'])
            if groups and groups[-1]['signature'] == signature:
                groups[-1]['chars'].append(char)
            else:
                groups.append({'signature': signature, 'chars': [char]})
        for group in groups:
            first = group['chars'][0]
            runs.append({'line': number, 'text': ''.join(c['text'] for c in group['chars']), 'bbox': _union(c['bbox'] for c in group['chars']),
                         'font': first['font'], 'size_pt': first['size_pt'], 'color': first['color'], 'bold': first['bold']})
    vectors = []
    for kind, items in (('line', page.lines), ('rect', page.rects), ('curve', page.curves)):
        for item in items:
            normalized = box(item)
            dash = item.get('dash')
            vectors.append({'kind': kind, 'bbox': normalized, 'bbox_pt': [v * (width if i % 2 == 0 else height) for i, v in enumerate(normalized)],
                            'linewidth_pt': round(float(item.get('linewidth') or 0) * unit, 4), 'dash': dash,
                            'stroke': _color(item.get('stroking_color')) or [0, 0, 0], 'fill': (_color(item.get('non_stroking_color')) or [0, 0, 0]) if item.get('fill') else None,
                            'stroke_enabled': item.get('stroke', True)})
    strokes = _strokes(vectors, width, height)
    # 仅在未传入预提取表格时才提取，避免传入空列表 [] 时被误判重新调用 find_tables()
    if tables is None:
        tables = _extract_page_tables(page)
    rendered_tables, cells = [], []
    for index, table in enumerate(tables):
        table_cells, merged = [], set()
        for span in table['spans']:
            b = span['bbox']
            c1, c2 = span['column_start'], span['column_end']
            r1, r2 = span['row_start'], span['row_end']
            record = {
                'table_index': index,
                'row': span['row'],
                'column': span['column'],
                'row_span': r2 - r1 + 1,
                'column_span': c2 - c1 + 1,
                'bbox': [
                    (b[0] - origin_x) / page.width,
                    (b[1] - origin_y) / page.height,
                    (b[2] - origin_x) / page.width,
                    (b[3] - origin_y) / page.height,
                ],
            }
            if record['row_span'] > 1 or record['column_span'] > 1:
                record['merged_range'] = f'{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}'
                merged.add(record['merged_range'])
            table_cells.append(record)
        tb = table['bbox']
        rendered_tables.append({
            'bbox': [
                (tb[0] - origin_x) / page.width,
                (tb[1] - origin_y) / page.height,
                (tb[2] - origin_x) / page.width,
                (tb[3] - origin_y) / page.height,
            ],
            'rows': table['row_count'],
            'columns': table['column_count'],
            'cells': table_cells,
            'merged_ranges': sorted(merged),
            'text': table['extracted'],
        })
        cells.extend(table_cells)
    rgb = page_image.original.convert('RGB').resize((max(1, round(width * VISUAL_DPI / 72)), max(1, round(height * VISUAL_DPI / 72))), Image.Resampling.LANCZOS)
    color_buffer = io.BytesIO()
    rgb.save(color_buffer, format='PNG')
    images = [{'bbox': box(item), 'source_size': list(item.get('srcsize', ())), 'bits': item.get('bits')} for item in page.images]
    image_area = sum(max(0, i['bbox'][2] - i['bbox'][0]) * max(0, i['bbox'][3] - i['bbox'][1]) for i in images)
    unsupported = []
    for annotation in page.annots or []:
        subtype = str(resolve1(annotation.get('data', {})).get('Subtype', 'annotation'))
        unsupported.append({'kind': 'annotation', 'subtype': subtype})
    if page.pdf.doc.catalog.get('OCProperties'):
        unsupported.append({'kind': 'optional_content', 'subtype': '可选内容层'})
    return {'version': 1, 'page_number': page.page_number, 'width_pt': width, 'height_pt': height, 'user_unit': unit,
            'rotation': int(page.rotation), 'media_box': list(page.mediabox), 'crop_box': list(page.cropbox),
            'chars': chars, 'runs': runs, 'lines': [{'text': line['text'], 'bbox': line['bbox'], 'char_ids': [c['id'] for c in line['chars']]} for line in lines],
            'vectors': vectors, 'strokes': strokes, 'frames': _frames(strokes, width, height), 'tables': rendered_tables, 'cells': cells, 'images': images,
            'raster_dominant': image_area > .15 and len(chars) < 12 or image_area > .8 and not page.lines and not page.rects,
            'unsupported_objects': unsupported,
            'render': {'dpi': VISUAL_DPI, 'width': rgb.width, 'height': rgb.height,
                       'rgb_png_base64': base64.b64encode(color_buffer.getvalue()).decode(),
                       'sha256': hashlib.sha256(color_buffer.getvalue()).hexdigest()},
            'limitations': ['字体指 PDF 有效字体资源和字号，不代表原编辑器的字体设置或加粗开关。',
                            '字距、基线距、换行与对齐仅描述实际几何；不反推段落样式。',
                            '复杂透明混合、ICC 专色和任意曲线路径未作逐属性语义解析；光栅文字不推断字体。']}


def _source(view, boxes=None, precision=None):
    result = {'kind': 'pdf', 'page': view['number'], 'bbox': _union(boxes) if boxes else _union(view['scopes']), 'method': 'text'}
    box = result['bbox']
    for start, end in ((0, 2), (1, 3)):
        lo, hi = sorted((max(0, min(1, box[start])), max(0, min(1, box[end]))))
        if hi - lo < .002:
            lo, hi = max(0, lo - .001), min(1, hi + .001)
        box[start], box[end] = lo, hi
    if precision:
        result['location_precision'] = precision
    return result


def _text_source(view, atoms):
    chars=list({atom['char']['id']:atom['char'] for atom in atoms if not atom.get('line_separator')}.values())
    boxes=[]
    for line in _lines(chars):
        segment=[]
        for char in line['chars']:
            if segment and abs(char.get('angle',0))<=.5:
                gap=(char['bbox'][0]-segment[-1]['bbox'][2])*view['appearance']['width_pt']
                if gap>max(12,1.5*max(char.get('size_pt') or 10,segment[-1].get('size_pt') or 10)):
                    boxes.append(_union(c.get('glyph_bbox',c['bbox']) for c in segment));segment=[]
            segment.append(char)
        if segment:
            boxes.append(_union(c.get('glyph_bbox',c['bbox']) for c in segment))
    source=_source(view,boxes,precision='text')
    if len(boxes)>1:
        source['highlight_boxes']=[_source(view,[box])['bbox'] for box in boxes]
        source['bbox']=_union(source['highlight_boxes'])
    corrected=[char for char in chars if char.get('glyph_bbox') and char['glyph_bbox']!=char['bbox']]
    if corrected:
        source['layout_bbox']=_union(char['bbox'] for char in chars)
        source['geometry_basis']='原度量框未充分覆盖字形，采用本侧唯一匹配的PDFium字形紧框'
        source['glyph_geometry_corrected_count']=len(corrected)
    return source


def index_page_overlays(book):
    overlays = defaultdict(list)
    for sheet in book.get('sheets', []):
        if sheet.get('member_sheets'):
            continue
        for cell in sheet.get('cells', {}).values():
            src = cell.get('pdf_source', {})
            page = src.get('page')
            if page is None or (sheet.get('structure') == 'text_blocks' and cell.get('row') == 1):
                continue
            if src.get('method') == 'ocr' or cell.get('correction_revision') is not None or cell.get('manual'):
                overlays[page].append((sheet, cell))
    return overlays


def _page_views(book, report, overlays_by_page):
    result, unavailable = [], []
    explicit = report.get('comparison_regions')
    for number in report.get('pages', []):
        page = next((p for p in book.get('pages', []) if p.get('number', p.get('page')) == number), None)
        if not page or not page.get('appearance'):
            unavailable.append(number)
            continue
        if explicit is not None:
            scopes = [region['bbox'] for region in explicit if region['page'] == number]
        else:
            peers = [r for r in book.get('reports', []) if number in r.get('pages', [])]
            if len(peers) > 1:
                unavailable.append(number)
                continue
            scopes = [[0, 0, 1, 1]]
        if not scopes:
            unavailable.append(number)
            continue
        appearance = page['appearance']
        chars = [copy.deepcopy(c) for c in appearance['chars'] if any(_inside(c['bbox'], region) for region in scopes)]
        overlays = []
        seen = set()
        for sheet, cell in overlays_by_page.get(number, ()):
            src = cell['pdf_source']
            if not any(_inside(src.get('bbox', [0, 0, 1, 1]), region) for region in scopes):
                continue
            revised = cell.get('correction_revision') is not None or cell.get('manual')
            identity = cell.get('review_id') or (sheet['name'], cell['address'])
            if identity in seen:
                continue
            seen.add(identity)
            text = cell.get('current_text', cell.get('display', ''))
            manual = bool(cell.get('manual') and src.get('location_precision', src.get('precision')) == 'page')
            if not manual:
                chars = [c for c in chars if not _inside(c['bbox'], src['bbox'])]
            reliable = bool(cell.get('reviewed') and page.get('reviewed'))
            virtual = [{'id': f'review-{identity}-{i}', 'order': i, 'text': ch, 'bbox': src['bbox'], 'font': None, 'size_pt': None,
                        'color': None, 'bold': None, 'method': src.get('method', 'ocr'), 'reliable': reliable, 'review_id': cell.get('review_id'),
                        'manual': manual, 'corrected': revised, 'bad_unicode': bool(_BAD.search(text))} for i, ch in enumerate(text)]
            if virtual:
                overlays.append({'text': text, 'bbox': src['bbox'], 'chars': virtual, 'manual': manual})
        lines = _lines(chars) + overlays
        lines.sort(key=lambda line: (bool(line.get('manual')), line['bbox'][1], line['bbox'][0]))
        selected = lambda records: [item for item in records if any(_inside(item['bbox'], scope) for scope in scopes)]
        view = {'number': number, 'metadata': page, 'appearance': appearance, 'scopes': scopes, 'lines': lines,
                'original_lines': _lines([copy.deepcopy(c) for c in appearance['chars'] if any(_inside(c['bbox'], r) for r in scopes)]),
                'strokes': selected(appearance.get('strokes', [])), 'tables': selected(appearance.get('tables', [])),
                'frames': [b for b in appearance.get('frames', []) if any(_inside(b, r) for r in scopes)],
                'images': selected(appearance.get('images', [])), 'revised': bool(overlays)}
        result.append(view)
    return result, unavailable


def _atoms(view, exclusions=None, original=False):
    atoms = []
    lines = view['original_lines'] if original else view['lines']
    previous = None
    for line_index, line in enumerate(lines):
        chars = [char for char in line['chars'] if not any(_inside(char['bbox'], box) for box in exclusions or [])]
        visible = [c for c in chars if c['text'].strip()]
        if not visible:
            continue
        if previous and re.match(r'[A-Za-z0-9]', previous['text'][-1:]) and re.match(r'[A-Za-z0-9]', visible[0]['text'][:1]):
            atoms.append({'text': ' ', 'char': visible[0], 'line': line_index, 'line_separator': True})
        for index, char in enumerate(chars):
            for value in char['text']:
                if value.isspace():
                    before = next((c['text'][-1:] for c in reversed(chars[:index]) if c['text'].strip()), '')
                    after = next((c['text'][:1] for c in chars[index + 1:] if c['text'].strip()), '')
                    if _CJK.search(before) or _CJK.search(after):
                        continue
                    value = ' '
                    if not atoms or atoms[-1]['text'] == ' ':
                        continue
                atoms.append({'text': value, 'char': char, 'line': line_index})
        previous = visible[-1]
    return [atom for index, atom in enumerate(atoms) if atom.get('line_separator') or atom['text'] != ' ' or (index > 0 and index + 1 < len(atoms) and atoms[index - 1]['line'] == atom['line'] == atoms[index + 1]['line'])]


def _fingerprint(view):
    text = ''.join(atom['text'] for atom in _atoms(view))
    text = re.sub(r'第\s*\d+\s*页\s*共\s*\d+\s*页', '', text)
    return text


def _pair_pages(old, new):
    pairs, remaining_old, remaining_new = [], list(old), list(new)
    if len(old) == len(new) == 1:
        return [(old[0], new[0], '已确认业务报表的唯一页面', 'confirmed')]
    for attribute in ('text', 'render'):
        left = {id(v): _fingerprint(v) if attribute == 'text' else v['appearance']['render']['sha256'] for v in remaining_old}
        right = {id(v): _fingerprint(v) if attribute == 'text' else v['appearance']['render']['sha256'] for v in remaining_new}
        for a in list(remaining_old):
            key = left[id(a)]
            candidates = [b for b in remaining_new if right[id(b)] == key]
            if key and len(candidates) == 1 and sum(value == key for value in left.values()) == 1:
                b = candidates[0]
                pairs.append((a, b, '唯一全文特征一致，允许页面重排' if attribute == 'text' else '唯一统一渲染图像一致', 'confirmed'))
                remaining_old.remove(a)
                remaining_new.remove(b)
    for a in list(remaining_old):
        scores = sorted([(SequenceMatcher(None, _fingerprint(a), _fingerprint(b), autojunk=False).ratio(), b) for b in remaining_new], key=lambda item: item[0], reverse=True)
        if not scores or not _fingerprint(a):
            continue
        score, b = scores[0]
        reverse = sorted(SequenceMatcher(None, _fingerprint(other), _fingerprint(b), autojunk=False).ratio() for other in remaining_old)
        margin = score - (scores[1][0] if len(scores) > 1 else 0)
        reverse_margin = score - (reverse[-2] if len(reverse) > 1 else 0)
        if score >= .65 and margin >= .12 and reverse_margin >= .12:
            pairs.append((a, b, f'双向唯一全文匹配，相似度 {score:.3f}', 'confirmed'))
            remaining_old.remove(a)
            remaining_new.remove(b)
    # Explicit printed page sequence is evidence; array position alone is not.
    for a in list(remaining_old):
        match = re.search(r'第\s*(\d+)\s*页\s*共\s*(\d+)\s*页', ''.join(l['text'] for l in a['lines']))
        if not match:
            continue
        candidates = [b for b in remaining_new if re.search(rf'第\s*{match[1]}\s*页\s*共\s*{match[2]}\s*页', ''.join(l['text'] for l in b['lines']))]
        peers = [v for v in remaining_old if re.search(rf'第\s*{match[1]}\s*页\s*共\s*{match[2]}\s*页', ''.join(l['text'] for l in v['lines']))]
        if len(candidates) == len(peers) == 1:
            b = candidates[0]
            pairs.append((a, b, '报表内明确且唯一的印刷页序', 'confirmed'))
            remaining_old.remove(a)
            remaining_new.remove(b)
    single_side = bool(pairs) and not (remaining_old and remaining_new)
    pairs.extend((a, None, '其余页面已唯一匹配，本页仅旧版存在' if single_side else '缺少唯一页面对应依据', 'confirmed' if single_side else 'pending') for a in remaining_old)
    pairs.extend((None, b, '其余页面已唯一匹配，本页仅新版存在' if single_side else '缺少唯一页面对应依据', 'confirmed' if single_side else 'pending') for b in remaining_new)
    return sorted(pairs, key=lambda pair: (pair[0]['number'] if pair[0] else math.inf, pair[1]['number'] if pair[1] else math.inf))


def _match_text(text):
    import unicodedata
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or ''))


def _content_fragments(view, exclusions):
    atoms = _atoms(view, exclusions)
    cells = view['appearance'].get('cells', [])
    buckets = {}
    previous = None
    serial = 0
    for atom in atoms:
        if atom.get('line_separator'):
            continue
        char = atom['char']
        containing = [c for c in cells if _inside(char['bbox'], c['bbox']) and
                      min(char['bbox'][3],c['bbox'][3])-max(char['bbox'][1],c['bbox'][1]) >= .85*(char['bbox'][3]-char['bbox'][1]) and
                      min(char['bbox'][2],c['bbox'][2])-max(char['bbox'][0],c['bbox'][0]) >= .85*(char['bbox'][2]-char['bbox'][0])]
        cell = min(containing, key=lambda c: (c['bbox'][2]-c['bbox'][0])*(c['bbox'][3]-c['bbox'][1])) if containing else None
        if cell:
            key = ('cell', tuple(cell['bbox']))
        else:
            gap = (char['bbox'][0]-previous['char']['bbox'][2])*view['appearance']['width_pt'] if previous else 0
            if previous is None or atom['line'] != previous['line'] or gap > max(20, 3*(char.get('size_pt') or 0)) or previous.get('cell'):
                serial += 1
            key = ('text', serial)
        atom['cell'] = cell
        buckets.setdefault(key, {'atoms': [], 'cell': cell})['atoms'].append(atom)
        previous = atom
    fragments = []
    for bucket in buckets.values():
        values = bucket['atoms']
        fragment_view = view
        if bucket['cell']:
            chars=list({atom['char']['id']: atom['char'] for atom in values}.values())
            chars.extend(char for line in view['lines'] for char in line['chars'] if char['text'].isspace() and _inside(char['bbox'],bucket['cell']['bbox']) and not any(_inside(char['bbox'],box) for box in exclusions))
            chars=list({char['id']:char for char in chars}.values())
            fragment_view={**view, 'lines': _lines(chars)}
            values=_atoms(fragment_view)
        text = _fragment(fragment_view, values)
        if not text.strip():
            continue
        fragment = {**bucket, 'atoms': values, 'view': fragment_view, 'text': text, 'key': _match_text(text),
                    'bbox': _union(x['char']['bbox'] for x in values), 'context': None, 'column': None, 'row_key': None}
        containing_frames = [box for box in view['frames'] if _inside(fragment['bbox'], box)]
        fragment['frame'] = min(containing_frames, key=lambda box: (box[2]-box[0])*(box[3]-box[1])) if containing_frames else None
        cell = bucket['cell']
        if cell:
            table = view['appearance']['tables'][cell['table_index']]
            rows = table.get('text', [])
            col = cell['column']-1
            if rows and col < len(rows[0]):
                header = next((h for h in table['cells'] if h['row'] == 1 and h['bbox'][0] <= (cell['bbox'][0]+cell['bbox'][2])/2 <= h['bbox'][2]), None)
                label = rows[0][header['column']-1] if header else rows[0][col]
                fragment['column'] = re.split(r'[A-Za-z]=', _match_text(label), maxsplit=1)[0] or None
                if header and header['column_span'] > 1 and cell['row'] > 2 and len(rows) > 1 and col < len(rows[1]) and rows[1][col]:
                    fragment['column'] = (fragment['column'] or '') + '/' + _match_text(rows[1][col])
            if cell['row'] > 1 and cell['column'] > 1 and cell['row'] <= len(rows):
                row = rows[cell['row']-1]
                fragment['row_key'] = next((_match_text(t) for t in row[:col] if t and len(_match_text(t)) <= 40), None)
        fragments.append(fragment)
    for fragment in fragments:
        cell=fragment['cell']
        if not cell:
            continue
        if cell['column_span']>1 and cell['row']>1:
            fragment['column']=None
            continue
        peers=[f for f in fragments if f['cell'] and f['cell']['table_index']==cell['table_index'] and f['cell']['row']==1 and
               f['cell']['bbox'][0] <= (cell['bbox'][0]+cell['bbox'][2])/2 <= f['cell']['bbox'][2]]
        if peers:
            heading=min(peers,key=lambda f:f['cell']['column_span'])
            fragment['column']=re.split(r'[A-Za-z]=',heading['key'],maxsplit=1)[0] or None
            if heading['cell']['column_span']>1 and cell['row']>2:
                child=next((f for f in fragments if f['cell'] and f['cell']['table_index']==cell['table_index'] and f['cell']['row']==2 and f['cell']['column']==cell['column']),None)
                if child:fragment['column']=(fragment['column'] or '')+'/'+child['key']
    fragments.sort(key=lambda f: (f['bbox'][1],f['bbox'][0]))
    for fragment in fragments:
        if fragment['cell']:
            continue
        box = fragment['bbox']
        neighbours = [f for f in fragments if f is not fragment and not f['cell'] and f['bbox'][2] < box[0] and
                      abs((f['bbox'][1]+f['bbox'][3]-box[1]-box[3])/2) < max(box[3]-box[1],f['bbox'][3]-f['bbox'][1])*.8]
        if neighbours:
            context = max(neighbours,key=lambda f:f['bbox'][2])['key']
            fragment['context'] = re.split(r'[:：]', context, maxsplit=1)[0]
    consumed=set()
    for fragment in fragments:
        if fragment['cell'] or not re.match(r'^第[0-9０-９]*页(?:共[0-9０-９]*)?$',fragment['key']):
            continue
        size=max(x['char'].get('size_pt') or 10 for x in fragment['atoms'])
        candidates=[f for f in fragments if f is not fragment and not f['cell'] and f['key'] in ('共','共页','页') and
                    0 < (f['bbox'][1]-fragment['bbox'][1])*view['appearance']['height_pt'] <= 3*size and min(abs(f['bbox'][edge]-fragment['bbox'][edge]) for edge in (0,2))*view['appearance']['width_pt'] < 2*size]
        if len(candidates)==1:
            continuation=candidates[0]
            fragment['atoms'].extend(continuation['atoms']);fragment['text']=_fragment(view,fragment['atoms']);fragment['key']=_match_text(fragment['text'])
            fragment['bbox']=_union(x['char']['bbox'] for x in fragment['atoms']);consumed.add(id(continuation))
    for fragment in fragments:
        if id(fragment) in consumed or fragment['cell'] or not fragment['context'] or not fragment['key'].startswith('(') or fragment['key'].count('(')<=fragment['key'].count(')'):
            continue
        size=max(x['char'].get('size_pt') or 10 for x in fragment['atoms'])
        candidates=[f for f in fragments if f is not fragment and not f['cell'] and id(f) not in consumed and
                    0 < (f['bbox'][1]-fragment['bbox'][1])*view['appearance']['height_pt'] <= 3*size and
                    re.match(r'^[^()（）]{1,12}[)）]',f['text'])]
        if len(candidates)!=1:
            continue
        continuation=candidates[0]
        end=next(i+1 for i,x in enumerate(continuation['atoms']) if x['text'] in ')）')
        fragment['atoms'].extend(continuation['atoms'][:end])
        fragment['text']=_fragment(view,fragment['atoms']);fragment['key']=_match_text(fragment['text'])
        fragment['bbox']=_union(x['char']['bbox'] for x in fragment['atoms'])
        continuation['atoms']=continuation['atoms'][end:]
        if continuation['atoms']:
            continuation['text']=_fragment(view,continuation['atoms']);continuation['key']=_match_text(continuation['text'])
            continuation['bbox']=_union(x['char']['bbox'] for x in continuation['atoms'])
        else:
            consumed.add(id(continuation))
    fragments=[f for f in fragments if id(f) not in consumed]
    merged = []
    for fragment in fragments:
        previous = merged[-1] if merged else None
        if previous and not previous['cell'] and not fragment['cell'] and (not previous['context'] and not fragment['context'] or previous['key'].startswith('第') and fragment['key'] in ('页','共','共页')):
            box, prev_box = fragment['bbox'], previous['bbox']
            size = max(x['char'].get('size_pt') or 10 for x in fragment['atoms']+previous['atoms'])
            gap = (box[1]-prev_box[3])*view['appearance']['height_pt']
            begins_field = re.match(r'^(?:表)?[A-Z]\.\d+(?:\.\d+)*', fragment['key']) or re.fullmatch(r'[（(][^）)]+[）)]', fragment['text'].strip()) or re.fullmatch(r'[0-9０-９]+', fragment['text'].strip()) or re.match(r'^[0-9０-９]{1,3}[\u3400-\u9fff]', fragment['key']) or re.match(r'^(?:[^。.!?！？:：]{1,24}[:：]|[0-9０-９]{1,3}[.．、)](?!\d)|[（(][0-9一二三四五六七八九十]+[）)])', fragment['text'].strip())
            peers = any(f is not fragment and f is not previous and abs(f['bbox'][1]-box[1]) < .008 for f in fragments)
            if (len(previous['key']) >= 6 or previous['key'].startswith('第') and fragment['key'] in ('页','共','共页')) and not begins_field and not peers and not (re.match(r'[A-Za-z]{3,}', previous['text']) and re.match(r'[A-Za-z]{3,}', fragment['text']) and previous['text'].split()[0] == fragment['text'].split()[0]) and -.2*size <= gap <= 2*size and (previous['frame'] and previous['frame'] == fragment['frame'] or abs(box[0]-prev_box[0])*view['appearance']['width_pt'] <= max(24,3*size)):
                previous['atoms'].extend(fragment['atoms'])
                previous['text'] = _fragment(view,previous['atoms'])
                previous['key'] = _match_text(previous['text'])
                previous['bbox'] = _union([prev_box,box])
                continue
        merged.append(fragment)
    return merged


def _complete_cell_edges(fragments, opposite):
    consumed=set()
    references=defaultdict(list)
    free_text=defaultdict(list)
    for reference in opposite:
        if reference['cell']:
            references[(reference['column'],reference['cell']['column'],reference['cell']['row'])].append(reference)
    for fragment in fragments:
        if not fragment['cell']:
            free_text[(fragment['view']['number'],fragment['key'])].append(fragment)
    for fragment in fragments:
        if not fragment['cell'] or len(fragment['key'])<2:
            continue
        candidates=[]
        for reference in references[(fragment['column'],fragment['cell']['column'],fragment['cell']['row'])]:
            if reference['key'].count(fragment['key'])!=1 or reference['key']==fragment['key']:
                continue
            prefix,suffix=reference['key'].split(fragment['key'])
            if len(prefix)+len(suffix)>20:
                continue
            box=fragment['cell']['bbox'];parts=[]
            for value in (prefix,suffix):
                if not value:
                    parts.append(None);continue
                nearby=[f for f in free_text[(fragment['view']['number'],value)] if id(f) not in consumed and
                        box[0]-.005 <= f['bbox'][0] and f['bbox'][2] <= box[2]+.005 and box[1]-.025 <= f['bbox'][1] <= box[3]+.025]
                if len(nearby)!=1:break
                parts.append(nearby[0])
            if len(parts)==2:candidates.append(parts)
        if len(candidates)!=1:
            continue
        prefix,suffix=candidates[0]
        atoms=(prefix['atoms'] if prefix else [])+fragment['atoms']+(suffix['atoms'] if suffix else [])
        chars=list({atom['char']['id']:atom['char'] for atom in atoms}.values())
        fragment['view']={**fragment['view'],'lines':_lines(chars)}
        fragment['atoms']=_atoms(fragment['view']);fragment['text']=_fragment(fragment['view'],fragment['atoms']);fragment['key']=_match_text(fragment['text'])
        fragment['bbox']=_union(x['char']['bbox'] for x in fragment['atoms'])
        fragment['boundary_basis']='跨单元格边线的相邻字形与另一版同列同位置完整文字唯一吻合'
        consumed.update(id(part) for part in (prefix,suffix) if part)
    fragments[:]=[f for f in fragments if id(f) not in consumed]


def _continue_title_fields(fragments, opposite):
    labels={m[1] for f in opposite if not f['cell'] and (m:=re.match(r'^([^:：]{1,24})[:：]',f['key']))}
    titles={re.sub(r'^表','',f['key']) for f in opposite if not f['cell'] and re.match(r'^(?:表)?[A-Z]\.\d',f['key'])}
    for field in fragments:
        if field['cell'] or not (match:=re.match(r'^([^:：]{1,24})[:：]',field['key'])) or match[1] in labels:
            continue
        candidates=[]
        for label in labels:
            if not match[1].endswith(label):
                continue
            prefix=match[1][:-len(label)]
            if not 1<=len(prefix)<=3:
                continue
            for title in fragments:
                if title['cell'] or title['view'] is not field['view'] or title['bbox'][1]>=field['bbox'][1] or field['bbox'][1]-title['bbox'][3]>.06:
                    continue
                if re.sub(r'^表','',title['key'])+prefix in titles:
                    candidates.append((title,prefix))
        if len(candidates)!=1:
            continue
        title,prefix=candidates[0]
        for peer in fragments:
            if peer['view'] is field['view'] and peer['context']==match[1]:
                peer['context']=match[1][len(prefix):]
        consumed='';end=0
        for index,atom in enumerate(field['atoms']):
            consumed+=_match_text(atom['text'])
            if consumed==prefix:
                end=index+1;break
            if not prefix.startswith(consumed):break
        if not end:
            continue
        title['atoms'].extend(field['atoms'][:end]);field['atoms']=field['atoms'][end:]
        for fragment in (title,field):
            fragment['text']=_fragment(fragment['view'],fragment['atoms'])
            fragment['key']=_match_text(fragment['text'])
            fragment['bbox']=_union(x['char']['bbox'] for x in fragment['atoms'])
            fragment['boundary_basis']='相邻标题尾字与另一版完整标题唯一吻合，字段前缀与另一版字段标签一致'


def _anchor_warnings(fragment):
    return [name+'含未恢复乱码，文字对应依赖尚未可靠' for name,key in [('行键','row_key'),('列标题','column'),('字段标签','context')] if _BAD.search(fragment.get(key) or '')]


def _content_pairs(left, right, page_links):
    mapping, reverse, bases = {}, {}, {}
    row_links=set()
    context_links=set()
    by_page,by_key,by_label,by_context,by_slot,by_row={},{},{},{},{},{}
    for side,fragments in (('old',left),('new',right)):
        by_page[side],by_key[side],by_label[side]=defaultdict(list),defaultdict(list),defaultdict(list)
        by_context[side],by_slot[side],by_row[side]=defaultdict(list),defaultdict(list),defaultdict(list)
        for index,fragment in enumerate(fragments):
            by_page[side][fragment['view']['number']].append(index)
            by_key[side][fragment['key']].append(index)
            by_label[side][(fragment['key'],fragment['column'])].append(index)
            if fragment['context']:
                by_context[side][fragment['context']].append(index)
            if fragment['column'] and fragment['row_key']:
                by_slot[side][(fragment['column'],fragment['row_key'])].append(index)
            if fragment['cell']:
                cell=fragment['cell']
                by_row[side][(fragment['view']['number'],cell['table_index'],cell['row'],fragment['column'])].append(index)
    linked_pages={'old':defaultdict(set),'new':defaultdict(set)}
    for old_page,new_page in page_links:
        linked_pages['old'][new_page].add(old_page)
        linked_pages['new'][old_page].add(new_page)
    linked_indices={side:{page:sorted(index for target in targets for index in by_page[side][target])
                         for page,targets in links.items()} for side,links in linked_pages.items()}
    linked_rows={'old':defaultdict(set),'new':defaultdict(set)}
    def row_link(a,b):
        return (a['view']['number'],a['cell']['table_index'],a['cell']['row'],b['view']['number'],b['cell']['table_index'],b['cell']['row']) if a['cell'] and b['cell'] else None
    def compatible(a,b,ignore_row=False):
        if bool(a['cell']) != bool(b['cell']):
            return False
        if a['column'] and b['column'] and a['column'] != b['column']:
            # A changed heading can still match its counterpart, but body fields
            # under different named columns cannot borrow each other's text.
            if a['cell']['row'] > 1 or b['cell']['row'] > 1:
                return False
        if not ignore_row and a['row_key'] and b['row_key'] and a['row_key'] != b['row_key'] and row_link(a,b) not in row_links:
            return False
        if a['context'] and b['context'] and a['context'] != b['context'] and (a['context'],b['context']) not in context_links:
            return False
        return True
    def slot(a,b):
        if not compatible(a,b):
            return False
        if a['context'] and a['context'] == b['context']:
            return True
        if row_link(a,b) in row_links and a['column'] and a['column']==b['column']:
            return True
        return bool(a['column'] and a['column'] == b['column'] and a['row_key'] and a['row_key'] == b['row_key'])
    def bind(i,j,basis):
        mapping[i]=j;reverse[j]=i;bases[i]=basis
        before=re.match(r'^([^:：]{1,24})[:：]',left[i]['key']);after=re.match(r'^([^:：]{1,24})[:：]',right[j]['key'])
        if before and after:context_links.add((before[1],after[1]))
    # A stable project/row label can establish a row whose printed sequence
    # number changed. Numeric values cannot create this row correspondence.
    for i,a in enumerate(left):
        if not a['cell'] or not a['column'] or re.fullmatch(r'[\d.,()+−—-]+',a['key']) or len(a['key'])<2:
            continue
        candidates=[j for j in by_label['new'][(a['key'],a['column'])] if j not in reverse and (b:=right[j])['cell'] and
                    (a['view']['number'],b['view']['number']) in page_links and compatible(a,b,ignore_row=True)]
        if len(candidates)==1:
            j=candidates[0]
            peers=[k for k in by_label['old'][(right[j]['key'],right[j]['column'])] if k not in mapping and (other:=left[k])['cell'] and
                   (other['view']['number'],right[j]['view']['number']) in page_links and compatible(other,right[j],ignore_row=True)]
            if len(peers)==1:
                bind(i,j,'已确认原页对内，同列唯一非数字名称作为行锚点')
                link=row_link(a,right[j])
                row_links.add(link)
                linked_rows['new'][link[:3]].add(link[3:])
                linked_rows['old'][link[3:]].add(link[:3])
    # Identical text in the identical source region of a confirmed page pair
    # is direct visual evidence, even when merged headers repeat a column name.
    # Keep row/context compatibility so this cannot override a known row move.
    exact_regions={'old':defaultdict(list),'new':defaultdict(list)}
    def region_key(fragment,page=None):
        return (fragment['view']['number'] if page is None else page,fragment['key'],
                tuple(round(v,9) for v in fragment['bbox']),bool(fragment['cell']))
    for side,fragments in (('old',left),('new',right)):
        for index,fragment in enumerate(fragments):
            exact_regions[side][region_key(fragment)].append(index)
    for i,a in enumerate(left):
        if i in mapping:
            continue
        candidates=[j for page in linked_pages['new'].get(a['view']['number'],())
                    for j in exact_regions['new'].get(region_key(a,page),())
                    if j not in reverse and compatible(a,right[j])]
        if len(candidates)!=1:
            continue
        j=candidates[0]
        peers=[k for page in linked_pages['old'].get(right[j]['view']['number'],())
               for k in exact_regions['old'].get(region_key(right[j],page),())
               if k not in mapping and compatible(left[k],right[j])]
        if len(peers)==1:
            bind(i,j,'已确认页对内，文字与原文区域双向唯一且完全一致')
    # Confirmed page correspondence disambiguates repeated headers before a
    # report-wide search for content that genuinely moved to another page.
    for i,a in enumerate(left):
        if i in mapping:
            continue
        candidates=[j for j in linked_indices['new'].get(a['view']['number'],[]) if j not in reverse and
                    compatible(a,b:=right[j]) and (slot(a,b) or a['key']==b['key'])]
        if len(candidates)==1:
            j=candidates[0]
            peers=[k for k in linked_indices['old'].get(right[j]['view']['number'],[]) if k not in mapping and
                   compatible(other:=left[k],right[j]) and (slot(other,right[j]) or other['key']==right[j]['key'])]
            if len(peers)==1:
                bind(i,j,'已确认原页对内的行列标签或唯一完整片段文本')
    def slot_indices(fragment,side):
        candidates=set(by_context[side].get(fragment['context'],[])) if fragment['context'] else set()
        if fragment['column'] and fragment['row_key']:
            candidates.update(by_slot[side].get((fragment['column'],fragment['row_key']),[]))
        if fragment['cell'] and fragment['column']:
            cell=fragment['cell']
            identity=(fragment['view']['number'],cell['table_index'],cell['row'])
            for target in linked_rows[side].get(identity,()):
                candidates.update(by_row[side].get((*target,fragment['column']),[]))
        return sorted(candidates)
    # Named row/column evidence precedes string equality, so swapping two
    # amounts under fixed row labels remains two value changes.
    for i,a in enumerate(left):
        if i in mapping:
            continue
        candidates=[j for j in slot_indices(a,'new') if j not in reverse and slot(a,right[j])]
        if len(candidates)==1:
            j=candidates[0]
            if sum(slot(left[k],right[j]) for k in slot_indices(right[j],'old') if k not in mapping)==1:
                bind(i,j,'相同列标题与行键，或相同同行字段标签')
    for i,a in enumerate(left):
        if i in mapping:
            continue
        candidates=[j for j in by_key['new'][a['key']] if j not in reverse and compatible(a,right[j])]
        peers=[k for k in by_key['old'][a['key']] if k not in mapping]
        if len(candidates)==len(peers)==1:
            bind(i,candidates[0],'双向唯一的完整片段文本，且行列上下文不冲突')
    # Repeated signature/field labels are bounded by already matched nearby
    # labels; they must not jump across a different signer's section.
    for i,a in enumerate(left):
        if i in mapping or a['cell']:
            continue
        anchors=[(k,j) for k,j in mapping.items() if not left[k]['cell'] and not right[j]['cell'] and left[k]['view'] is a['view']]
        preceding=[(k,j) for k,j in anchors if left[k]['bbox'][1]<a['bbox'][1]-.003]
        following=[(k,j) for k,j in anchors if left[k]['bbox'][1]>a['bbox'][1]+.003]
        lower=max(preceding,key=lambda pair:left[pair[0]]['bbox'][1]) if preceding else None
        upper=min(following,key=lambda pair:left[pair[0]]['bbox'][1]) if following else None
        if not lower or not upper or right[lower[1]]['view'] is not right[upper[1]]['view']:
            continue
        candidates=[j for j in by_key['new'][a['key']] if j not in reverse and not (b:=right[j])['cell'] and b['view'] is right[lower[1]]['view'] and compatible(a,b) and
                    right[lower[1]]['bbox'][1]-.003 <= b['bbox'][1] <= right[upper[1]]['bbox'][1]+.003]
        if len(candidates)==1:
            bind(i,candidates[0],'已配对的前后字段共同限定同一段内的唯一重复标签')
    # Repeated labels/symbols only pair in a shared column or nearby free-text
    # slot. Reading-order occurrence alone is not a business-row key.
    for i,a in enumerate(left):
        if i in mapping:
            continue
        candidates=[j for j in by_key['new'][a['key']] if j not in reverse and compatible(a,b:=right[j]) and
                    ((a['column'] and a['column']==b['column']) or (not a['cell'] and abs(a['bbox'][0]-b['bbox'][0])<.03))]
        if candidates:
            distances=sorted((abs(a['bbox'][1]-right[j]['bbox'][1]),j) for j in candidates)
            if len(distances)==1 or distances[1][0]-distances[0][0]>.015:
                bind(i,distances[0][1],'同列或同一横向区域中的唯一邻近重复文本')
    def score(a,b):
        if not compatible(a,b):
            return 0
        x,y=a['key'],b['key']
        if not x or not y:
            return 0
        if re.fullmatch(r'[-‐‑‒–—−]+',x) and re.fullmatch(r'[-‐‑‒–—−]+',y):
            return .95 if abs(a['bbox'][1]-b['bbox'][1])<.12 else 0
        px=re.match(r'^([^:：]{1,24})[:：]',x);py=re.match(r'^([^:：]{1,24})[:：]',y)
        if px and py and px[1]==py[1]:
            return .98
        if re.fullmatch(r'[\d.,]+',x) or re.fullmatch(r'[\d.,]+',y):
            if not (re.fullmatch(r'[\d.,]+',x) and re.fullmatch(r'[\d.,]+',y)):
                return 0
            if not a['cell'] and not b['cell'] and abs(a['bbox'][0]-b['bbox'][0])<.02 and abs(a['bbox'][1]-b['bbox'][1])<.02:
                return .8
            return 0
        return SequenceMatcher(None,x,y,autojunk=False).ratio() if min(len(x),len(y))>=3 else 0
    scores={}
    scores_by_left=defaultdict(list)
    scores_by_right=defaultdict(list)
    for i,a in enumerate(left):
        if i in mapping:
            continue
        for j,b in enumerate(right):
            if j in reverse:
                continue
            value=score(a,b)
            if value > 0:
                scores[(i,j)]=value
                scores_by_left[i].append((j,value))
                scores_by_right[j].append((i,value))
    for (i,j),value in sorted(scores.items(),key=lambda p:p[1],reverse=True):
        if value < .62 or i in mapping or j in reverse:
            continue
        alternatives=[v for jj,v in scores_by_left[i] if jj!=j and jj not in reverse]
        alternatives.extend(v for ii,v in scores_by_right[j] if ii!=i and ii not in mapping)
        if value-max(alternatives,default=0) >= .12:
            bind(i,j,f'双向唯一片段相似度 {value:.3f}，且行列上下文不冲突')
    for i,a in enumerate(left):
        if i in mapping:
            continue
        candidates=[j for j in by_key['new'][a['key']] if j not in reverse and compatible(a,b:=right[j]) and
                    (a['view']['number'],b['view']['number']) in page_links]
        if len(candidates)==1:
            j=candidates[0]
            if sum(compatible(left[k],right[j]) for k in by_key['old'][right[j]['key']] if k not in mapping)==1:
                bind(i,j,'已确认原页与字段对应中的唯一相同文字')
    pairs=[(a,right[mapping[i]],False,bases[i]) for i,a in enumerate(left) if i in mapping]
    for i,a in enumerate(left):
        if i not in mapping:
            ambiguous=any(value>=.45 and j not in reverse for j,value in scores_by_left[i])
            pairs.append((a,None,ambiguous,'未建立可靠文字对应'))
    for j,b in enumerate(right):
        if j not in reverse:
            ambiguous=any(value>=.45 and i not in mapping for i,value in scores_by_right[j])
            pairs.append((None,b,ambiguous,'未建立可靠文字对应'))
    return pairs


def _content_changes(old_views, new_views, entry, page_links):
    fragments={}
    for side,views in [('old',old_views),('new',new_views)]:
        fragments[side]=[fragment for view in views for fragment in _content_fragments(view,[r['bbox'] for r in entry.get('content_exclusions',{}).get(side,[]) if r['page']==view['number']])]
    _complete_cell_edges(fragments['old'],fragments['new'])
    _complete_cell_edges(fragments['new'],fragments['old'])
    _continue_title_fields(fragments['old'],fragments['new'])
    _continue_title_fields(fragments['new'],fragments['old'])
    reliability={}
    for side,views in (('old',old_views),('new',new_views)):
        failed={id(view):_failed_regions(view) for view in views}
        bad_pages=any(view['metadata'].get('status') == 'unavailable' or
                      view['appearance']['chars'] and sum(c.get('bad_unicode',False) for c in view['appearance']['chars']) > len(view['appearance']['chars'])/2
                      for view in views)
        bad_fragments=[candidate for candidate in fragments[side] if _anchor_warnings(candidate) or
                       any(not atom['char'].get('reliable',True) or atom['char'].get('bad_unicode') for atom in candidate['atoms']) or
                       any(_intersects(candidate['bbox'],box) for box in failed.get(id(candidate['view']),_failed_regions(candidate['view'])))]
        reliability[side]=(bad_pages,[(view,failed[id(view)]) for view in views if failed[id(view)]],bad_fragments)
    def unreliable_opposite(fragment, side, views):
        band=lambda box: 0 if box[1]<.35 else 2 if box[1]>.75 else 1
        bad_pages,failed_views,bad_fragments=reliability[side]
        if bad_pages:
            return True
        for view,regions in failed_views:
            for region in regions:
                if not fragment['cell'] and band(region)==band(fragment['bbox']):
                    return True
                if fragment['cell']:
                    for cell in view['appearance'].get('cells',[]):
                        if not _intersects(cell['bbox'],region):
                            continue
                        table=view['appearance']['tables'][cell['table_index']]
                        rows=table.get('text',[]);column=cell['column']-1
                        label=_match_text(rows[0][column]) if rows and column<len(rows[0]) else ''
                        row_key=next((_match_text(t) for t in rows[cell['row']-1][:column] if t),None) if cell['row']<=len(rows) else None
                        if (not label or not fragment['column'] or label==fragment['column']) and (not row_key or not fragment['row_key'] or row_key==fragment['row_key']):
                            return True
                    if view['appearance'].get('raster_dominant'):
                        return True
        for candidate in bad_fragments:
            if bool(candidate['cell']) != bool(fragment['cell']):
                continue
            if fragment['cell']:
                if candidate['column'] and fragment['column'] and candidate['column'] != fragment['column']:
                    continue
                if candidate['row_key'] and fragment['row_key'] and candidate['row_key'] != fragment['row_key'] and not _BAD.search(candidate['row_key']):
                    continue
            else:
                if band(candidate['bbox']) != band(fragment['bbox']):
                    continue
            return True
        return False
    results=[]
    for left,right,ambiguous,basis in _content_pairs(fragments['old'],fragments['new'],page_links):
        cross_page = bool(left and right and (left['view']['number'],right['view']['number']) not in page_links)
        evidence = {'match_basis': basis, 'cross_page_content_match': cross_page, 'old': None, 'new': None}
        for side,fragment in [('old',left),('new',right)]:
            if fragment:
                evidence[side] = {'page': fragment['view']['number'], 'fragment_text': fragment['text'], 'text_bbox': fragment['bbox'],
                                  'cell': fragment['cell'], 'column_label': fragment['column'], 'row_key': fragment['row_key'], 'field_context': fragment['context'], 'boundary_basis': fragment.get('boundary_basis'), 'anchor_warnings': _anchor_warnings(fragment)}
        def record(*values):
            results.append((*values, None, evidence))
        before=left['text'] if left else None;after=right['text'] if right else None
        if before==after:
            continue
        a=left['view'] if left else (old_views[0] if len(old_views)==1 else None)
        b=right['view'] if right else (new_views[0] if len(new_views)==1 else None)
        involved=(left['atoms'] if left else [])+(right['atoms'] if right else [])
        doubtful=ambiguous or any(_anchor_warnings(f) for f in (left,right) if f) or any(not x['char'].get('reliable',True) or x['char'].get('bad_unicode') for x in involved)
        source_a=_text_source(a,left['atoms']) if left else _source(a,precision='page') if a else None
        source_b=_text_source(b,right['atoms']) if right else _source(b,precision='page') if b else None
        doubtful |= bool(not left and unreliable_opposite(right,'old',old_views))
        doubtful |= bool(not right and unreliable_opposite(left,'new',new_views))
        if left and right and re.sub(r'\s+','',before)==re.sub(r'\s+','',after):
            continue
        message='按网格单元格、行列标签或双向唯一正文片段对应；保留标点，换行不拼接相邻列。'
        if cross_page:
            message+=' 此项为报表范围内的跨页文字对应，不改变整页配对。'
        if not left or not right:
            message+=' 未找到可靠的对应文字区域，缺失一侧只定位原页，不将另一页坐标冒充精确空白位置。'
        if ambiguous:
            message+=' 存在多个相似文字候选，增删或对应关系仍待确认。'
        if doubtful:
            message+=' 含不可靠提取或未能排除的对应文字，须核对后确认。'
        # For reflowed paragraphs compare compact characters locally; never use
        # whole-page matching ranges to decide what this paragraph added/lost.
        if left and right and ('\n' in before or '\n' in after) and re.sub(r'\s+','',before)!=re.sub(r'\s+','',after):
            la=[x for x in left['atoms'] if not x['text'].isspace()];ra=[x for x in right['atoms'] if not x['text'].isspace()]
            ops=SequenceMatcher(None,[x['text'] for x in la],[x['text'] for x in ra],autojunk=False).get_opcodes()
            for tag,i1,i2,j1,j2 in ops:
                if tag=='equal':continue
                va=_fragment(a,la[i1:i2]) if i2>i1 else None;vb=_fragment(b,ra[j1:j2]) if j2>j1 else None
                if va==vb:continue
                sa=_text_source(a,la[i1:i2]) if i2>i1 else _source(a,[left['bbox']],precision='corresponding_region')
                sb=_text_source(b,ra[j1:j2]) if j2>j1 else _source(b,[right['bbox']],precision='corresponding_region')
                record('content','text_changed','文字内容',va,vb,message,a,b,sa,sb,doubtful)
        else:
            record('content','text_changed','文字内容',before,after,message,a,b,source_a,source_b,doubtful)
    return results


def _fragment(view, atoms):
    if not atoms:
        return ''
    parts = []
    for line in dict.fromkeys(atom['line'] for atom in atoms):
        selected = {atom['char']['id'] for atom in atoms if atom['line'] == line}
        chars = view['lines'][line]['chars']
        positions = [i for i, char in enumerate(chars) if char['id'] in selected]
        if positions:
            parts.append(''.join(char['text'] for index,char in enumerate(chars) if char['id'] in selected or char['text'].isspace() and positions[0]<index<positions[-1]))
    return '\n'.join(parts)


def _failed_regions(view):
    regions = [r['bbox'] for r in view['metadata'].get('ocr_regions', []) if r.get('affects_content') and r.get('status') != 'ok']
    if view['metadata'].get('status') == 'unavailable':
        regions += view['scopes']
    return regions


def _intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _geometry_pairs(left, right):
    candidates = []
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            if a.get('orientation') != b.get('orientation') or a.get('kind') != b.get('kind'):
                continue
            score = sum(abs(x - y) for x, y in zip(a['bbox'], b['bbox']))
            candidates.append((score, i, j))
    used_a, used_b, pairs = set(), set(), []
    for _, i, j in sorted(candidates):
        if i not in used_a and j not in used_b:
            pairs.append((left[i], right[j])); used_a.add(i); used_b.add(j)
    return pairs


def _free_lines(view):
    return [s for s in view['strokes'] if s['orientation'] == 'horizontal' and not any(
        f[0] - .002 <= s['bbox'][0] and f[2] + .002 >= s['bbox'][2] and f[1] - .002 <= s['bbox'][1] <= f[3] + .002 for f in view['frames'])]


def _geometry_text(item, appearance):
    box = item['bbox']
    return f"位置 ({box[0] * appearance['width_pt']:.2f}, {box[1] * appearance['height_pt']:.2f}) pt；宽 {(box[2]-box[0]) * appearance['width_pt']:.2f}、高 {(box[3]-box[1]) * appearance['height_pt']:.2f} pt"


def _style_text(styles):
    return '；'.join(f"线宽 {style['linewidth_pt']:g} pt，{'虚线 ' + str(style['dash']) if style['dash'] else '实线'}，颜色 {style['stroke']}" for style in styles)


def _border_correspondence(old, new, text_pairs):
    def edge(box, role):
        x0,y0,x1,y1=box
        return {'top':[x0,y0,x1,y0],'bottom':[x0,y1,x1,y1],'left':[x0,y0,x0,y1],'right':[x1,y0,x1,y1]}[role]
    def contains(line, box, tolerance=.002):
        horizontal=abs(box[3]-box[1])<1e-8
        axis,start,end=(1,0,2) if horizontal else (0,1,3)
        return abs(line[axis]-box[axis])<=tolerance and line[start]-tolerance<=box[start] and line[end]+tolerance>=box[end]
    pieces={}
    for side,view in [('old',old),('new',new)]:
        appearance=view['appearance']
        pieces[side]=[piece for vector in appearance['vectors'] for piece in _strokes([vector],appearance['width_pt'],appearance['height_pt'])]
    def local_line(side,box):
        horizontal=abs(box[3]-box[1])<1e-8
        orientation='horizontal' if horizontal else 'vertical'
        axis,start,end=(1,0,2) if horizontal else (0,1,3)
        candidates=[piece for piece in pieces[side] if piece['orientation']==orientation and abs(piece['bbox'][axis]-box[axis])<.002 and min(piece['bbox'][end],box[end])-max(piece['bbox'][start],box[start])>1e-5]
        intervals=sorted((max(piece['bbox'][start],box[start]),min(piece['bbox'][end],box[end])) for piece in candidates)
        cursor=box[start]
        for lo,hi in intervals:
            if lo>cursor+.002:return None
            cursor=max(cursor,hi)
        if cursor<box[end]-.002:return None
        styles=[]
        for piece in candidates:
            for style in piece['styles']:
                if style not in styles:styles.append(style)
        return {'bbox':box,'orientation':orientation,'styles':styles} if styles else None
    matches=[];covered={'old':[],'new':[]};seen=set()
    def add_match(a,b,basis,role,context=None):
        key=(tuple(round(v,6) for v in a),tuple(round(v,6) for v in b))
        if key in seen:return
        x,y=local_line('old',a),local_line('new',b)
        if x is None or y is None:return
        seen.add(key);covered['old'].append(a);covered['new'].append(b)
        matches.append((x,y,{'match_basis':basis,'boundary_role':role,'old_bbox':a,'new_bbox':b,'context':context}))
    if len(old['frames'])==len(new['frames'])==1:
        for role in ('top','bottom','left','right'):
            add_match(edge(old['frames'][0],role),edge(new['frames'][0],role),'已确认页面内双侧唯一闭合外框的同名边',role)
    def named_cell(fragment):
        cell=fragment['cell']
        if not cell or not re.search(r'[A-Za-z\u3400-\u9fff]',fragment['key']):return False
        rows=fragment['view']['appearance']['tables'][cell['table_index']].get('text',[])
        column=cell['column']-1
        return sum(column<len(row) and _match_text(row[column])==fragment['key'] for row in rows)==1
    cells=set()
    for left,right,_,basis in text_pairs:
        if left['key']!=right['key'] or not named_cell(left) or not named_cell(right):continue
        key=(tuple(left['cell']['bbox']),tuple(right['cell']['bbox']))
        if key in cells:continue
        cells.add(key)
        for role in ('top','bottom','left','right'):
            a,b=edge(key[0],role),edge(key[1],role)
            if any(contains(match[0]['bbox'],a) and contains(match[1]['bbox'],b) for match in matches):continue
            add_match(a,b,'可靠同文单元格对应的同名边：'+basis,role,{'old_text':left['text'],'new_text':right['text'],'old_cell':left['cell'],'new_cell':right['cell']})
    def filling_anchor(view,fragment):
        text=_match_text(fragment['text'])
        label=text.split(':',1)[0]
        if ':' not in text and label!='工程':return None
        box=fragment['bbox'];size=max(atom['char'].get('size_pt') or 10 for atom in fragment['atoms'])
        candidates=[]
        for line in _free_lines(view):
            b=line['bbox']
            if not -1<=(b[1]-box[3])*view['appearance']['height_pt']<=size:continue
            if label=='工程' and 0<=(box[0]-b[2])*view['appearance']['width_pt']<=size:
                candidates.append((line,'before_label'))
            elif ':' in text and b[0]>=box[0] and b[2]>box[2]:
                candidates.append((line,'after_label'))
        return (candidates[0],label) if len(candidates)==1 else None
    for left,right,_,basis in text_pairs:
        if left['cell'] or right['cell']:continue
        la,ra=filling_anchor(old,left),filling_anchor(new,right)
        if la and ra and la[1]==ra[1] and la[0][1]==ra[0][1]:
            add_match(la[0][0]['bbox'],ra[0][0]['bbox'],'可靠相同字段标签旁的唯一填写线：'+basis,la[0][1],{'field_label':la[1]})
    if not old['tables'] and not new['tables'] and not old['frames'] and not new['frames']:
        if len(old['strokes'])==len(new['strokes'])==1 and old['strokes'][0]['orientation']==new['strokes'][0]['orientation']:
            add_match(old['strokes'][0]['bbox'],new['strokes'][0]['bbox'],'已确认页面内双侧各自唯一的孤立线段','isolated_line')
        for x in old['strokes']:
            if any(contains(box,x['bbox']) for box in covered['old']):continue
            candidates=[y for y in new['strokes'] if not any(contains(box,y['bbox']) for box in covered['new']) and x['orientation']==y['orientation'] and max(abs(a-b) for a,b in zip(x['bbox'],y['bbox']))<.0001]
            if len(candidates)!=1:continue
            y=candidates[0]
            if sum(z['orientation']==y['orientation'] and max(abs(a-b) for a,b in zip(z['bbox'],y['bbox']))<.0001 for z in old['strokes'])==1:
                add_match(x['bbox'],y['bbox'],'无表格及外框的孤立线段，两侧端点原位一致且双向唯一','isolated_line')
    unresolved={}
    for side,view in [('old',old),('new',new)]:
        unresolved[side]=[]
        for stroke in view['strokes']:
            box=stroke['bbox'];horizontal=stroke['orientation']=='horizontal'
            if stroke['orientation'] not in ('horizontal','vertical'):
                unresolved[side].append(stroke);continue
            axis,start,end=(1,0,2) if horizontal else (0,1,3)
            ranges=sorted((max(b[start],box[start]),min(b[end],box[end])) for b in covered[side] if abs(b[axis]-box[axis])<.002 and min(b[end],box[end])>max(b[start],box[start]))
            cursor=box[start]
            for lo,hi in ranges:
                if lo>cursor+.002:break
                cursor=max(cursor,hi)
            if cursor<box[end]-.002:unresolved[side].append(stroke)
    return matches,unresolved


def _visual_regions(old, new, residual=False):
    def bitmap(view):
        render = view['appearance']['render']
        # 优先使用彩色渲染快照，惰性回退兼容仅含灰度图的旧版本历史快照
        payload = render.get('rgb_png_base64') or render.get('gray_png_base64')
        image = Image.open(io.BytesIO(base64.b64decode(payload))).convert('RGB')
        mask = Image.new('L', image.size, 255)
        draw = ImageDraw.Draw(mask)
        regions = view['scopes']
        if residual:
            regions = [item['bbox'] for v in (old, new) for item in v['images']]
            regions += [item['bbox'] for v in (old, new) for item in v['appearance']['vectors'] if item['kind'] == 'curve' or item['fill'] is not None]
        for box in regions:
            draw.rectangle([box[0] * image.width, box[1] * image.height, box[2] * image.width, box[3] * image.height], fill=0)
        image.paste((255, 255, 255), (0, 0), mask)
        if residual:
            painter = ImageDraw.Draw(image)
            for v in (old, new):
                for line in v['original_lines']:
                    for char in line['chars']:
                        b = char['bbox']
                        painter.rectangle((b[0] * image.width - 1, b[1] * image.height - 1, b[2] * image.width + 1, b[3] * image.height + 1), fill='white')
        return image
    a, b = bitmap(old), bitmap(new)
    if a.size != b.size:
        return [{'old': _union(old['scopes']), 'new': _union(new['scopes']), 'pixels': None, 'reason': '页面物理画布不同；视觉差异按整页呈现，不据此推断字形属性'}]
    x_rgb, y_rgb = np.asarray(a), np.asarray(b)
    x, y = cv2.cvtColor(x_rgb, cv2.COLOR_RGB2GRAY), cv2.cvtColor(y_rgb, cv2.COLOR_RGB2GRAY)
    x_blur, y_blur = cv2.GaussianBlur(x, (3, 3), .6), cv2.GaussianBlur(y, (3, 3), .6)
    x_ink, y_ink = x_blur < 205, y_blur < 205
    dx = cv2.distanceTransform((~x_ink).astype(np.uint8), cv2.DIST_L2, 3)
    dy = cv2.distanceTransform((~y_ink).astype(np.uint8), cv2.DIST_L2, 3)
    changed = (x_ink & (dy > 1.2)) | (y_ink & (dx > 1.2)) | (x_ink & y_ink & (np.abs(x_blur.astype(int) - y_blur.astype(int)) > 45))
    color_delta = np.max(np.abs(cv2.GaussianBlur(x_rgb, (3, 3), .6).astype(int) - cv2.GaussianBlur(y_rgb, (3, 3), .6).astype(int)), axis=2)
    changed |= (color_delta > 35) & x_ink & y_ink
    if int(changed.sum()) < 9:
        return []
    connected = cv2.dilate(changed.astype(np.uint8), np.ones((7, 7), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(connected, 8)
    regions = []
    for index in range(1, count):
        left, top, width, height, _ = stats[index]
        pixels = int(changed[labels == index].sum())
        if pixels < 9:
            continue
        box = [left / a.width, top / a.height, (left + width) / a.width, (top + height) / a.height]
        regions.append({'old': box, 'new': box, 'pixels': pixels, 'reason': '统一 96 dpi、轻度模糊和 1.2 像素边缘容差后仍有可见差异'})
    if len(regions) > 12:
        regions = regions[:11] + [{'old': _union(r['old'] for r in regions[11:]), 'new': _union(r['new'] for r in regions[11:]),
                                  'pixels': sum(r['pixels'] for r in regions[11:]), 'reason': '其余相邻视觉变化区域合并展示'}]
    return regions


def _filled_vectors(view):
    """提取当前页比较范围内的带填充矢量对象。"""
    return [x for x in view['appearance']['vectors'] if x['fill'] is not None and any(_inside(x['bbox'], r) for r in view['scopes'])]


def _validate_report_pairing(old, new, missing_old, missing_new, entry, pairs, add, unavailable_dimensions) -> list:
    """校验报表级快照完整性、人工确认状态、页数变化与页面顺序。"""
    if missing_old or missing_new:
        add('layout', 'appearance_unavailable', '完整报表范围', missing_old or None, missing_new or None,
            '页面缺少外观快照，或同页多报表未划定完整范围；不能把未比较视为一致。', issue=True)
        unavailable_dimensions.update(_CATEGORIES)
    if entry.get('confirmation', 'confirmed') != 'confirmed':
        add('layout', 'page_pairing_unverified', '报表对应关系', None, None, '报表对应关系尚未确认，未执行完整报表差异判断。', issue=True)
        pairs = []
        unavailable_dimensions.update(_CATEGORIES)
    if len(old) != len(new):
        add('layout', 'page_count_changed', '报表页数', len(old), len(new), '已确认报表范围内的页面数量发生变化；单页内容仍按唯一证据配对。', old[0] if old else None, new[0] if new else None)
    matched = [(a, b) for a, b, _, state in pairs if a and b and state == 'confirmed']
    new_order = [new.index(b) for a, b in matched]
    if new_order != sorted(new_order):
        add('layout', 'page_order_changed', '报表页面顺序', [old.index(a) + 1 for a, b in matched], [new.index(b) + 1 for a, b in matched], '按唯一内容特征匹配到相同页面，其在报表内的顺序发生变化。', matched[0][0], matched[0][1])
    return pairs


def _compare_page_pairing_and_canvas(a, b, basis, state, add, limits, unavailable_dimensions) -> bool | None:
    """比较单页配对状态与画布物理属性；返回页面尺寸/方向是否变化，未配对或单侧页返回 None 以跳过后续阶段。"""
    if state != 'confirmed':
        add('layout', 'page_pairing_unverified', '页面对应关系', a['number'] if a else None, b['number'] if b else None, basis + '；没有按页数组位置强行对齐。', a, b, issue=True)
        unavailable_dimensions.update(_CATEGORIES)
        return None
    if not a or not b:
        add('structure', 'report_page_added' if b else 'report_page_removed', '报表页面', a['number'] if a else None, b['number'] if b else None, basis, a, b)
        return None
    ap, bp = a['appearance'], b['appearance']
    crop = lambda p: [(p['crop_box'][i] - p['media_box'][i]) * (1 if i < 2 else -1) * p['user_unit'] for i in range(4)]
    if any(abs(x - y) > .25 for x, y in zip(crop(ap), crop(bp))):
        add('layout', 'page_crop_changed', '页面裁切边距', crop(ap), crop(bp), '比较裁切框相对于原始纸张四边的物理内缩量（pt）；纸张尺寸改变或原点平移不重复算作裁切变化。', a, b)
    for view in (a, b):
        if view['appearance'].get('unsupported_objects'):
            unavailable_dimensions.update(_CATEGORIES)
            for category in _CATEGORIES:
                limits[category].append('本页实际含注释、Widget 或可选内容层；其交互状态/隐藏内容未完整解析，不能据可见原生对象判定完全一致。')
        for category in ('format', 'structure'):
            limits[category].extend(view['appearance'].get('limitations', []))
    orientation_a, orientation_b = ('横向' if ap['width_pt'] > ap['height_pt'] else '纵向'), ('横向' if bp['width_pt'] > bp['height_pt'] else '纵向')
    size_changed = abs(ap['width_pt'] - bp['width_pt']) > .5 or abs(ap['height_pt'] - bp['height_pt']) > .5
    rotation_changed = ap['rotation'] != bp['rotation']
    if size_changed or rotation_changed:
        kind = 'page_orientation_changed' if orientation_a != orientation_b else 'page_rotation_changed' if rotation_changed else 'page_size_changed'
        add('layout', kind, '页面物理尺寸与方向', f"{ap['width_pt']:.2f} × {ap['height_pt']:.2f} pt；{orientation_a}；Rotate={ap['rotation']}",
            f"{bp['width_pt']:.2f} × {bp['height_pt']:.2f} pt；{orientation_b}；Rotate={bp['rotation']}",
            '使用 PDF 物理尺寸及 UserUnit；未将两页任意拉伸至相同尺寸，后续位置变化按整块汇总。', a, b)
    return size_changed or rotation_changed


def _compare_page_text_and_paragraphs(a, b, entry, page_changed, add, limits, unavailable_dimensions, paragraph_checks) -> list:
    """比较单页字符属性、空白换行、字距、位置偏移与段落几何属性，并返回 format_groups 供后续边框对应使用。"""
    ap, bp = a['appearance'], b['appearance']
    exclusions = {side: [r['bbox'] for r in entry.get('content_exclusions', {}).get(side, []) if r['page'] == view['number']] for side, view in (('old', a), ('new', b))}
    aa, ba = _atoms(a, exclusions['old']), _atoms(b, exclusions['new'])
    if _failed_regions(a) or _failed_regions(b) or any(x['char'].get('bad_unicode') or not x['char'].get('reliable', True) for x in aa + ba):
        add('content', 'text_extraction_unverified', '文字可靠性', None, None, '范围内含乱码或未完成核验的 OCR/校正文字，不能将当前字符串相同视为内容已验证。', a, b, issue=True)
    if a['revised'] or b['revised']:
        limits['format'].append('人工校正仅覆盖内容比较；字体及矢量格式仍描述原 PDF，不将新文字伪装成原件排版。')
    native_a = {**a, 'lines': a['original_lines']}
    native_b = {**b, 'lines': b['original_lines']}
    fa, fb = _content_fragments(native_a, []), _content_fragments(native_b, [])
    _complete_cell_edges(fa, fb)
    _complete_cell_edges(fb, fa)
    _continue_title_fields(fa, fb)
    _continue_title_fields(fb, fa)
    format_groups = []
    unverified_groups = []
    for left, right, ambiguous, basis in _content_pairs(fa, fb, {(a['number'], b['number'])}):
        if not left or not right:
            continue
        atoms = left['atoms'] + right['atoms']
        if ambiguous or _anchor_warnings(left) or _anchor_warnings(right) or any(not x['char'].get('reliable', True) or x['char'].get('bad_unicode') for x in atoms):
            unverified_groups.append((left, right))
            continue
        la = [x for x in left['atoms'] if x['text'].strip()]
        ra = [x for x in right['atoms'] if x['text'].strip()]
        blocks = SequenceMatcher(None, [x['text'] for x in la], [x['text'] for x in ra], autojunk=False).get_matching_blocks()
        glyphs = [(la[i + k], ra[j + k]) for i, j, n in blocks for k in range(n)]
        format_groups.append((left, right, glyphs, basis))
    linked = [pair for _, _, glyphs, _ in format_groups for pair in glyphs]
    if unverified_groups:
        left, right = unverified_groups[0]
        add('format', 'attribute_unverified', '字符格式对应待核验', None, None,
            f'{len(unverified_groups)} 个文字片段的原生文字或行列锚点不可靠，未对其字体、空白、换行及字距作确定判断。', a, b,
            _text_source(a, left['atoms']), _text_source(b, right['atoms']), issue=True)
    rotated = [(x, y) for x, y in linked if abs((x['char'].get('angle', 0) - y['char'].get('angle', 0) + 180) % 360 - 180) > .5]
    if rotated:
        add('layout', 'content_rotation_changed', '文字内容旋转', sorted({x['char'].get('angle', 0) for x, y in rotated}), sorted({y['char'].get('angle', 0) for x, y in rotated}), '由原生字符基线矩阵判断内容旋转，与纸张横竖及 Rotate 分开。', a, b, _text_source(a, [rotated[0][0]]), _text_source(b, [rotated[0][1]]))
    if any(c.get('size_pt') is None for v in (a, b) for line in v['original_lines'] for c in line['chars']):
        unavailable_dimensions.add('format')
        limits['format'].append('旋转字形包围框高度不能可靠反推字号，相关字号不作确定判断。')
    for attr, kind, label in (('font', 'font_changed', 'PDF 字体资源'), ('size_pt', 'font_size_changed', 'PDF 有效字号'), ('color', 'text_color_changed', '文字颜色'), ('bold', 'font_weight_changed', '可确认字体粗细')):
        changed = [(x, y) for x, y in linked if x['char'].get(attr) is not None and y['char'].get(attr) is not None and
                   (abs(x['char'][attr] - y['char'][attr]) > .15 if attr == 'size_pt' else x['char'][attr] != y['char'][attr])]
        if changed:
            examples = list(dict.fromkeys((str(x['char'][attr]), str(y['char'][attr])) for x, y in changed))
            first = max(changed, key=lambda pair: pair[0]['char'].get('size_pt') or 0)
            add('format', kind, label, '；'.join(v[0] for v in examples[:8]), '；'.join(v[1] for v in examples[:8]),
                f'相同可匹配文字中有 {len(changed)} 个字符的{label}证据变化；已去除字体随机子集前缀，不推断编辑器设置。', a, b,
                _text_source(a, [first[0]]), _text_source(b, [first[1]]))
    moves, gaps = [], []
    for left, right, glyphs, basis in format_groups:
        evidence = {'match_basis': basis, 'old': {'fragment_text': left['text'], 'cell': left['cell'], 'text_bbox': left['bbox'], 'row_key': left['row_key'], 'column_label': left['column']},
                    'new': {'fragment_text': right['text'], 'cell': right['cell'], 'text_bbox': right['bbox'], 'row_key': right['row_key'], 'column_label': right['column']}}
        compact = lambda value: re.sub(r'\s+', '', value)
        if compact(left['text']) == compact(right['text']):
            if re.findall(r'\s+', left['text']) != re.findall(r'\s+', right['text']):
                add('format', 'text_whitespace_changed', '原文空白与换行', left['text'], right['text'],
                    '同一可靠单元格或段落的非空白文字相同，仅空格或换行改变；范围不跨相邻字段。', a, b,
                    _text_source(a, left['atoms']), _text_source(b, right['atoms']), measurements=evidence)
            old_lines = {x['line'] for x in left['atoms'] if x['text'].strip()}
            new_lines = {x['line'] for x in right['atoms'] if x['text'].strip()}
            if len(old_lines) != len(new_lines):
                add('format', 'line_break_changed', '同文换行', len(old_lines), len(new_lines),
                    '同一可靠单元格或段落的相同文字跨越的物理行数改变，不包含邻列或相邻字段。', a, b,
                    _text_source(a, left['atoms']), _text_source(b, right['atoms']), measurements=evidence)
        if len(glyphs) < 3:
            continue
        old_box, new_box = _union(x['char']['bbox'] for x, y in glyphs), _union(y['char']['bbox'] for x, y in glyphs)
        if max(abs(old_box[0] - new_box[0]), abs(old_box[1] - new_box[1])) > .008:
            moves.append((_text_source(a, [x for x, y in glyphs]), _text_source(b, [y for x, y in glyphs]), evidence))
        line_pairs = defaultdict(list)
        for x, y in glyphs:
            line_pairs[(x['line'], y['line'])].append((x, y))
        for items in line_pairs.values():
            if len(items) < 3:
                continue
            positions = []
            for fragment, item in ((left, items[0][0]), (right, items[0][1])):
                positions.append({char['id']: index for index, char in enumerate(fragment['view']['lines'][item['line']]['chars'])})
            adjacent = [(p, q) for p, q in zip(items, items[1:]) if all(positions[side][q[side]['char']['id']] == positions[side][p[side]['char']['id']] + 1 for side in (0, 1))]
            if len(adjacent) < 2:
                continue
            def spacing(side, width):
                return median((q[side]['char']['bbox'][0] - p[side]['char']['bbox'][2]) * width / max(p[side]['char'].get('size_pt') or 1, 1) for p, q in adjacent)
            xgap, ygap = spacing(0, ap['width_pt']), spacing(1, bp['width_pt'])
            if abs(xgap - ygap) > .08:
                evidence = {**evidence, 'adjacent_gap_count': len(adjacent), 'gap_pairs': [
                    {side: {'text': [p[index]['text'], q[index]['text']], 'char_ids': [p[index]['char']['id'], q[index]['char']['id']],
                            'bboxes': [p[index]['char']['bbox'], q[index]['char']['bbox']],
                            'gap_em': (q[index]['char']['bbox'][0] - p[index]['char']['bbox'][2]) * width / max(p[index]['char'].get('size_pt') or 1, 1)}
                     for index, side, width in ((0, 'old', ap['width_pt']), (1, 'new', bp['width_pt']))} for p, q in adjacent]}
                runs = []
                for p, q in adjacent:
                    if runs and all(runs[-1][-1][side]['char']['id'] == p[side]['char']['id'] for side in (0, 1)):
                        runs[-1].append(q)
                    else:
                        runs.append([p, q])
                sources = []
                for side, view in ((0, a), (1, b)):
                    source = _text_source(view, [item[side] for run in runs for item in run])
                    parts = [_text_source(view, [item[side] for item in run]) for run in runs]
                    boxes = [box for part in parts for box in part.get('highlight_boxes', [part['bbox']])]
                    if len(boxes) > 1:
                        source.update(highlight_boxes=boxes, bbox=_union(boxes))
                    sources.append(source)
                gaps.append((xgap, ygap, *sources, evidence))
    if moves:
        add('layout', 'text_layout_changed', '文字块位置', f'{len(moves)} 个匹配文字区域', f'{len(moves)} 个匹配文字区域',
            '只在已可靠对应的单元格或段落内测量文字位置；下方定位第一处测量样例。' + (' 页面尺寸/方向也变化。' if page_changed else ''), a, b,
            moves[0][0], moves[0][1], measurements=moves[0][2])
    if gaps:
        add('format', 'character_spacing_changed', '实际字距', f'{gaps[0][0]:.3f} em', f'{gaps[0][1]:.3f} em',
            f'{len(gaps)} 个可靠单元格或段落内的同行字距测量改变；仅比较两侧原始字符序列中均真实相邻的同一字符对，至少两条共同间隙。下方定位第一处测量样例。', a, b,
            gaps[0][2], gaps[0][3], measurements=gaps[0][4])
    page_checks = compare_paragraphs(a, b)
    paragraph_checks.extend(page_checks)
    uncertain_paragraphs = []
    for check in page_checks:
        if check['status'] == 'changed':
            attr = check['attribute']
            add('format', 'paragraph_line_spacing_changed' if attr == 'line_spacing' else 'paragraph_alignment_changed',
                '有效段落行距（基线）' if attr == 'line_spacing' else '段落几何对齐', check['old_value'], check['new_value'],
                check['message'], a, b, check['old_source'], check['new_source'], measurements=check['measurements'])
        elif check['status'] == 'unverified':
            measurements = check['measurements']
            left, right = measurements.get('old'), measurements.get('new')
            field = 'baseline_gaps_pt' if check['attribute'] == 'line_spacing' else 'line_edges_pt'
            if left and right and field in left and field in right and left[field] != right[field]:
                uncertain_paragraphs.append(check)
            elif left is None or right is None:
                uncertain_paragraphs.append(check)
            elif measurements.get('native_baselines_unavailable') or '文字提取或 OCR' in check['message'] or '缺少可比较' in check['message']:
                uncertain_paragraphs.append(check)
    if uncertain_paragraphs:
        check = uncertain_paragraphs[0]
        add('format', 'paragraph_measurement_unverified', '段落属性待核验', check['old_value'], check['new_value'],
            f'{len(uncertain_paragraphs)} 项段落属性不能确定；详情见逐段检查。' + check['message'], a, b,
            check['old_source'], check['new_source'], issue=True, measurements=check['measurements'])
    return format_groups


def _compare_page_vectors_and_geometry(a, b, format_groups, add) -> None:
    """比较单页表格与表单结构、边框线段样式、填充区域、图像位置及网格行列比例。"""
    ap, bp = a['appearance'], b['appearance']
    topology = lambda v: sorted((t['rows'], t['columns'], tuple(t['merged_ranges'])) for t in v['tables'])
    if topology(a) != topology(b):
        add('structure', 'table_structure_changed', '表格行列与合并结构', topology(a), topology(b), '比较矢量网格恢复的行数、列数和合并范围，包含空白行与空白表单；不依赖业务数值。', a, b)
    def form_shape(view):
        free = _free_lines(view)
        return (len(view['frames']), len(free), len(view['images']))
    if form_shape(a) != form_shape(b):
        add('structure', 'form_structure_changed', '空框、填写线及图像结构', form_shape(a), form_shape(b), '依次为闭合外框、框外填写横线、图像数量；已合并相接共线段，不以 PDF 绘图对象数量冒充可见结构数量。', a, b)
    frame_changes = []
    for x, y in _geometry_pairs([{'bbox': f} for f in a['frames']], [{'bbox': f} for f in b['frames']]):
        fa, fb = x['bbox'], y['bbox']
        before = [round((fa[2] - fa[0]) * ap['width_pt'], 2), round((fa[3] - fa[1]) * ap['height_pt'], 2)]
        after = [round((fb[2] - fb[0]) * bp['width_pt'], 2), round((fb[3] - fb[1]) * bp['height_pt'], 2)]
        if max(abs(u - v) for u, v in zip(before, after)) > .5 or max(abs(u - v) for u, v in zip(fa, fb)) > .008:
            frame_changes.append((fa, fb, before, after))
    if frame_changes:
        fa, fb, before, after = frame_changes[0]
        add('layout', 'form_geometry_changed', '空框尺寸与位置', f'{before[0]} × {before[1]} pt', f'{after[0]} × {after[1]} pt', f'{len(frame_changes)} 个对应闭合表单框的中心线宽高或页边位置变化；宽高取左右纵线、上下横线的中心轴距离，线宽及端点伸缩另计。', a, b, _source(a, [fa]), _source(b, [fb]), measurements={'geometry_basis': '闭合框四边中心轴距离', 'old_bbox': fa, 'new_bbox': fb, 'old_size_pt': before, 'new_size_pt': after})
    border_pairs, unresolved_borders = _border_correspondence(a, b, format_groups)
    for x, y, evidence in border_pairs:
        if evidence['boundary_role'] not in ('before_label', 'after_label', 'isolated_line') or x['orientation'] != 'horizontal':
            continue
        if any(abs(u - v) > .001 for u, v in zip(x['bbox'], y['bbox'])) or _geometry_text(x, ap) != _geometry_text(y, bp):
            add('layout', 'filling_line_geometry_changed', '填写线长度与位置', _geometry_text(x, ap), _geometry_text(y, bp), '填写线按可靠相同字段或唯一孤立线锚比较物理长度与位置，不按全页最近坐标建立对应。', a, b, _source(a, [x['bbox']]), _source(b, [y['bbox']]), measurements=evidence)
    signature = lambda styles: sorted(_style_text([style]) for style in styles)
    changed_styles = [(x, y, evidence) for x, y, evidence in border_pairs if signature(x['styles']) != signature(y['styles'])]
    if changed_styles:
        x, y, evidence = changed_styles[0]
        confirmed = [{**basis, 'old': {'bbox': p['bbox'], 'styles': p['styles']}, 'new': {'bbox': q['bbox'], 'styles': q['styles']}, 'style_changed': signature(p['styles']) != signature(q['styles'])} for p, q, basis in border_pairs]
        add('format', 'border_style_changed', '矢量线宽、线型与颜色', _style_text(x['styles']), _style_text(y['styles']), f'{len(changed_styles)} 个具有可靠边界对应的局部线段样式改变；下方数值与定位仅展示第一处样例，全部已确认对应保留在测量证据中。', a, b, _source(a, [x['bbox']]), _source(b, [y['bbox']]), measurements={**evidence, 'confirmed_changed_segments': len(changed_styles), 'confirmed_pairs': confirmed})
    remaining_signature = lambda side: sorted((stroke['orientation'], tuple(round(v, 5) for v in stroke['bbox']), tuple(signature(stroke['styles']))) for stroke in unresolved_borders[side])
    if remaining_signature('old') != remaining_signature('new'):
        old_remaining, new_remaining = unresolved_borders['old'], unresolved_borders['new']
        add('format', 'border_correspondence_unverified', '其余线段样式对应待核验', f'{len(old_remaining)} 条未完整对应线段', f'{len(new_remaining)} 条未完整对应线段',
            '这些线段缺少唯一外框或可靠同文单元格边界锚；两侧定位各自的一处参考线段，不代表两者已对应，不能按最近坐标判定其局部线型变化。', a, b,
            _source(a, [old_remaining[0]['bbox']], precision='corresponding_region') if old_remaining else _source(a, precision='page'),
            _source(b, [new_remaining[0]['bbox']], precision='corresponding_region') if new_remaining else _source(b, precision='page'), issue=True,
            measurements={'unresolved_old_segments': len(old_remaining), 'unresolved_new_segments': len(new_remaining), 'match_basis': '缺少可靠边界锚，未建立完整对应',
                          'unresolved_sources': {side: [{'bbox': stroke['bbox'], 'orientation': stroke['orientation'], 'styles': stroke['styles'], 'reason': '未被可靠锚覆盖完整线段'} for stroke in unresolved_borders[side]] for side in ('old', 'new')}})
    for x, y in _geometry_pairs(_filled_vectors(a), _filled_vectors(b)):
        if x['fill'] != y['fill']:
            add('format', 'fill_changed', '区域填充颜色', x['fill'], y['fill'], '比较同一几何区域的填充色，区域颜色互换不会被全局集合去重隐藏。', a, b, _source(a, [x['bbox']]), _source(b, [y['bbox']]))
        elif any(abs(u - v) > .001 for u, v in zip(x['bbox'], y['bbox'])):
            add('layout', 'fill_geometry_changed', '填充区域位置', _geometry_text(x, ap), _geometry_text(y, bp), '填充区域几何变化。', a, b, _source(a, [x['bbox']]), _source(b, [y['bbox']]))
    for x, y in _geometry_pairs(a['images'], b['images']):
        if any(abs(u - v) > .001 for u, v in zip(x['bbox'], y['bbox'])):
            add('layout', 'image_position_changed', '图片区域位置', _geometry_text(x, ap), _geometry_text(y, bp), '同数量图像仍比较各自位置和物理尺寸。', a, b, _source(a, [x['bbox']]), _source(b, [y['bbox']]))
    def grid_profile(table):
        box = table['bbox']
        return ([round((v - box[0]) / max(box[2] - box[0], 1e-9), 4) for v in sorted({c['bbox'][i] for c in table['cells'] for i in (0, 2)})],
                [round((v - box[1]) / max(box[3] - box[1], 1e-9), 4) for v in sorted({c['bbox'][i] for c in table['cells'] for i in (1, 3)})])
    for x, y in _geometry_pairs(a['tables'], b['tables']):
        if x['rows'] == y['rows'] and x['columns'] == y['columns'] and grid_profile(x) != grid_profile(y):
            add('layout', 'grid_geometry_changed', '表内行高与列宽', grid_profile(x), grid_profile(y), '行列数量和外框相同也比较内部网格分割比例；未将不同列宽归一化消除。', a, b, _source(a, [x['bbox']]), _source(b, [y['bbox']]))


def _compare_page_visual_residual(a, b, add, limits, unavailable_dimensions) -> None:
    """检查扫描图像占比与非文本/残差区域的视觉变化。"""
    ap, bp = a['appearance'], b['appearance']
    raster = ap.get('raster_dominant') or bp.get('raster_dominant')
    if raster:
        unavailable_dimensions.update(('content', 'format', 'structure'))
        limits['format'].append('本报表含扫描图像，无法从像素可靠确定字体名称、字号或加粗设置。')
        limits['structure'].append('扫描结构仅凭视觉区域提示；未将像素线条推断为可靠行列/合并关系。')
    visual_needed = raster or a['images'] or b['images'] or any(v['kind'] == 'curve' for p in (ap, bp) for v in p['vectors']) or len(_filled_vectors(a)) != len(_filled_vectors(b))
    if visual_needed:
        for region in _visual_regions(a, b, residual=not raster):
            add('layout', 'visual_region_changed', '扫描区域视觉变化', None, None, region['reason'] + '；仅确认视觉证据，文字与字体属性待核验。', a, b,
                _source(a, [region['old']]), _source(b, [region['new']]), issue=True)


def compare_report_appearance(old_book, new_book, entry, *, overlays_by_side=None):
    diffs = []
    paragraph_checks = []
    limits = {category: [] for category in _CATEGORIES}
    unavailable_dimensions = set()
    if overlays_by_side is None:
        overlays_by_side = {'old': index_page_overlays(old_book), 'new': index_page_overlays(new_book)}
    old, missing_old = _page_views(old_book, entry.get('old') or {}, overlays_by_side['old'])
    new, missing_new = _page_views(new_book, entry.get('new') or {}, overlays_by_side['new'])
    pairs = _pair_pages(old, new)

    def add(category, kind, field, before, after, message, a=None, b=None, old_source=None, new_source=None, issue=False, delta=None, measurements=None):
        source_a = old_source or (_source(a) if a else None)
        source_b = new_source or (_source(b) if b else None)
        diffs.append({'id': f'appearance-{len(diffs) + 1:05d}', 'kind': kind, 'table_id': entry['id'], 'table_name': entry.get('title', '完整报表'),
                      'row_key': f"旧第 {a['number']} 页 / 新第 {b['number']} 页" if a and b else '完整报表', 'field': field,
                      'old_value': None if before is None else str(before), 'new_value': None if after is None else str(after), 'delta': delta,
                      'message': message, 'severity': 'issue' if issue else 'change', 'old_source': source_a, 'new_source': source_b,
                      'category': category, 'layer': 'uncertainty' if issue else category, 'report_id': entry['id']})
        if measurements is not None:
            diffs[-1]['measurements'] = measurements

    pairs = _validate_report_pairing(old, new, missing_old, missing_new, entry, pairs, add, unavailable_dimensions)
    # 必须按 pairs 逐页顺次执行四阶段比较，保证 diffs 编号、顺序与 limitations 首次插入序严格一致
    for a, b, basis, state in pairs:
        page_changed = _compare_page_pairing_and_canvas(a, b, basis, state, add, limits, unavailable_dimensions)
        if page_changed is None:
            continue
        format_groups = _compare_page_text_and_paragraphs(a, b, entry, page_changed, add, limits, unavailable_dimensions, paragraph_checks)
        _compare_page_vectors_and_geometry(a, b, format_groups, add)
        _compare_page_visual_residual(a, b, add, limits, unavailable_dimensions)
    content_old = list({v['number']: v for a,b,_,state in pairs if state == 'confirmed' for v in [a] if v}.values())
    content_new = list({v['number']: v for a,b,_,state in pairs if state == 'confirmed' for v in [b] if v}.values())
    for arguments in _content_changes(content_old, content_new, entry, {(a['number'],b['number']) for a,b,_,state in pairs if a and b and state=='confirmed'}):
        add(*arguments)
    dimensions = {}
    for category in _CATEGORIES:
        changes = sum(d['category'] == category and d['severity'] == 'change' for d in diffs)
        issues = sum(d['category'] == category and d['severity'] == 'issue' for d in diffs)
        unsupported = category in unavailable_dimensions
        status = 'partial' if (issues or unsupported) and changes else 'unverified' if issues or unsupported else 'changed' if changes else 'unchanged'
        if category == 'format' and not changes and not issues and not any(v['appearance']['chars'] or v['appearance']['vectors'] for v in old + new) and not unsupported:
            status = 'not_applicable'
        if category == 'layout':
            limits[category].append('签章区域未作语义识别；以文字、外框和图片的实际几何变化提示。')
        if category == 'format':
            limits[category].append('字距只比较两侧原始同行中均真实相邻的对应字符对；少于两条共同间隙时不作确定字距判断，不跨过未匹配字符或原文空格。')
            limits[category].append('段落行距指对应原生文字行的物理基线距离，对齐指唯一可证的几何边缘/中心；不反推编辑器段落参数。单行、等宽行、OCR、对应不完整、页面缩放及字号混杂等范围以逐段检查标明待核验。')
            limits[category].extend(['不反推原编辑器的段落样式、加粗开关或字体授权；字体证据是 PDF 资源和有效字号。'])
        dimensions[category] = {'status': status, 'changes': changes, 'issues': issues, 'summary': f'{changes} 项变化，{issues} 项待核验' if changes or issues else '已比较的原件证据未发现变化' if status == 'unchanged' else '该维度存在未验证范围',
                                'limitations': list(dict.fromkeys(limits[category]))}
    return {'diffs': diffs, 'dimensions': dimensions, 'paragraph_checks': paragraph_checks,
            'paragraph_coverage': {state: sum(check['status'] == state for check in paragraph_checks) for state in ('changed', 'unchanged', 'unverified')}, 'page_pairs': [{'old_page': a['number'] if a else None, 'new_page': b['number'] if b else None, 'basis': basis, 'status': state} for a,b,basis,state in pairs]}
