import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, FormEvent } from 'react';
import { columnName, messageOf, parseAddress, request } from '../api';
import type { Cell, Job, SheetRegion, Side, Source } from '../types';

type ExcelSource = Source & { sheet: string; cell: string };

function excelSource(source: Source | null): ExcelSource | null {
  return source?.sheet && source.cell ? { ...source, sheet: source.sheet, cell: source.cell } : null;
}

function bounds(value: string) {
  const parts = value.split(':');
  const start = parseAddress(parts[0]);
  const end = parseAddress(parts[1] ?? parts[0]);
  return start && end ? { r1: start.row, c1: start.column, r2: end.row, c2: end.column } : null;
}

function rgbColor(value: unknown): string | undefined {
  if (!value || typeof value !== 'object') return;
  const color = value as Record<string, unknown>;
  if (color.type !== 'rgb' || typeof color.value !== 'string' || !/^[\da-f]{6}([\da-f]{2})?$/i.test(color.value)) return;
  return `#${color.value.slice(-6)}`;
}

function cellStyle(cell?: Cell): CSSProperties {
  const style = cell?.style;
  if (!style) return {};
  const font = (style.font ?? {}) as Record<string, unknown>;
  const fill = (style.fill ?? {}) as Record<string, unknown>;
  const alignment = (style.alignment ?? {}) as Record<string, unknown>;
  const horizontal = alignment.horizontal;
  const fillColor = fill.type === 'solid' ? rgbColor(fill.foreground) : undefined;
  return {
    fontWeight: font.bold ? 600 : undefined,
    fontStyle: font.italic ? 'italic' : undefined,
    textDecoration: font.underline && font.underline !== 'none' ? 'underline' : undefined,
    color: rgbColor(font.color),
    backgroundColor: fillColor,
    textAlign: horizontal === 'center' || horizontal === 'right' || horizontal === 'left' ? horizontal : cell?.value_type === 'number' ? 'right' : undefined,
    whiteSpace: alignment.wrap_text ? 'pre-wrap' : undefined,
  };
}

export default function SheetViewer({ job, side, source, selectionId, isIssue }: { job: Job; side: Side; source: Source | null; selectionId: string | null; isIssue: boolean }) {
  const [target, setTarget] = useState<ExcelSource | null>(() => excelSource(source));
  const [region, setRegion] = useState<SheetRegion | null>(null);
  const [address, setAddress] = useState(source?.cell ?? 'A1');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const sideLabel = side === 'old' ? '旧版' : '新版';

  useEffect(() => { setTarget(excelSource(source)); setAddress(source?.cell ?? 'A1'); setError(''); }, [source?.sheet, source?.cell, source?.range, selectionId]);

  useEffect(() => {
    if (!target) { setRegion(null); return; }
    const point = parseAddress(target.cell);
    if (!point) { setError('原单元格地址无效。'); return; }
    const controller = new AbortController();
    setBusy(true); setError(''); setRegion(null);
    const params = new URLSearchParams({ side, sheet: target.sheet, start_row: String(Math.max(1, point.row - 5)), start_column: String(Math.max(1, point.column - 3)), rows: '22', columns: '10' });
    request<SheetRegion>(`/api/jobs/${job.id}/sheet-region?${params}`, { signal: controller.signal }).then(data => { if (!controller.signal.aborted) setRegion(data); }).catch(e => { if (!controller.signal.aborted) setError(messageOf(e)); }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [job.id, side, target?.sheet, target?.cell]);

  useEffect(() => {
    if (!region || !scrollRef.current) return;
    const frame = requestAnimationFrame(() => {
      const scroll = scrollRef.current;
      const cell = scroll?.querySelector<HTMLTableCellElement>('[data-highlight="true"]');
      if (scroll && cell) {
        scroll.scrollTop = Math.max(0, cell.offsetTop - 100);
        scroll.scrollLeft = Math.max(0, cell.offsetLeft - scroll.clientWidth / 2 + cell.offsetWidth / 2);
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [region, target?.cell, target?.range]);

  const cellMap = useMemo(() => new Map([...(region?.cells ?? []), ...(region?.merged_anchors ?? [])].map(c => [c.address, c])), [region]);
  const mergeBounds = useMemo(() => (region?.merged_ranges ?? []).map(range => ({ range, box: bounds(range) })).filter(item => item.box !== null), [region]);
  const targetBounds = target ? bounds(target.range || target.cell) : null;
  const point = target ? parseAddress(target.cell) : null;
  const targetMerge = point ? mergeBounds.find(m => m.box && point.row >= m.box.r1 && point.row <= m.box.r2 && point.column >= m.box.c1 && point.column <= m.box.c2) : undefined;
  const selectedCell = target ? cellMap.get(targetMerge?.range.split(':')[0] || target.cell) : undefined;

  function jump(event: FormEvent) {
    event.preventDefault();
    if (!target) return;
    const parsed = parseAddress(address);
    const sheet = job.files[side].sheets.find(s => s.name === target.sheet);
    if (!parsed || (sheet && (parsed.row > sheet.max_row || parsed.column > sheet.max_column))) { setError('请输入该工作表范围内的单元格地址，例如 D12。'); return; }
    setTarget({ sheet: target.sheet, cell: `${columnName(parsed.column)}${parsed.row}` });
  }

  const columns = region ? Array.from({ length: Math.max(0, region.end_column - region.start_column + 1) }, (_, i) => region.start_column + i) : [];
  const rows = region ? Array.from({ length: Math.max(0, region.end_row - region.start_row + 1) }, (_, i) => region.start_row + i) : [];

  return <section className="viewer" aria-label={`${sideLabel}原工作表`}>
    <div className="viewer-heading"><span className="label">{sideLabel}</span><a href={`/api/jobs/${job.id}/files/${side}`} download className="small">下载原件</a></div>
    <div className="file-name" title={job.files[side].name}>{job.files[side].name}</div>
    <div className="location" data-testid={`${side}-location`}>{target ? `${target.sheet}!${target.range || target.cell}` : selectionId ? '本版本无对应位置' : '请选择差异查看原位置'}</div>
    {!target ? <div className="absent"><strong>{selectionId ? '本版本无对应项目或位置' : '尚未选择差异'}</strong><span className="muted">{selectionId ? '存在的原文保留在另一侧；具体原因见差异详情。' : '差异列表中的每一项都保留原文件地址。'}</span></div> : <>
      <div className="sheet-controls"><label className="sr-only" htmlFor={`${side}-sheet`}>{sideLabel}工作表</label><select id={`${side}-sheet`} value={target.sheet} onChange={e => { setTarget({ sheet: e.target.value, cell: 'A1' }); setAddress('A1'); }}>{job.files[side].sheets.map(sheet => <option key={sheet.name} value={sheet.name}>{sheet.name}{sheet.hidden ? '（隐藏）' : ''}</option>)}</select><form onSubmit={jump} className="address-form"><label className="sr-only" htmlFor={`${side}-address`}>{sideLabel}跳转地址</label><input id={`${side}-address`} value={address} onChange={e => setAddress(e.target.value.toUpperCase())} placeholder="D12" autoComplete="off" /><button type="submit">定位</button></form></div>
      <div className="sheetbar"><strong>{target.cell}</strong><span className="formula-label">ƒx</span><span>{selectedCell?.formula || selectedCell?.display || (busy ? '读取中…' : '（空白）')}</span></div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      {busy && <div className="grid-loading" role="status">正在读取原工作表区域…</div>}
      {region && <><div className="grid-scroll" ref={scrollRef} tabIndex={0} aria-label={`${sideLabel}工作表单元格，支持滚动查看`}><table className="sheet-grid"><thead><tr><th className="corner" scope="col"></th>{columns.map(col => <th key={col} scope="col">{columnName(col)}</th>)}</tr></thead><tbody>{rows.map(row => <tr key={row}><th scope="row">{row}</th>{columns.map(col => {
        const merged = mergeBounds.find(m => m.box && row >= m.box.r1 && row <= m.box.r2 && col >= m.box.c1 && col <= m.box.c2);
        const box = merged?.box;
        const startRow = box ? Math.max(box.r1, region.start_row) : row;
        const startColumn = box ? Math.max(box.c1, region.start_column) : col;
        if (row !== startRow || col !== startColumn) return null;
        const endRow = box ? Math.min(box.r2, region.end_row) : row;
        const endColumn = box ? Math.min(box.c2, region.end_column) : col;
        const address = `${columnName(col)}${row}`;
        const cell = cellMap.get(merged?.range.split(':')[0] || address) ?? cellMap.get(address);
        const highlight = !!targetBounds && startRow <= targetBounds.r2 && endRow >= targetBounds.r1 && startColumn <= targetBounds.c2 && endColumn >= targetBounds.c1;
        const hidden = cell?.hidden_row || cell?.hidden_column;
        return <td key={col} rowSpan={endRow - startRow + 1} colSpan={endColumn - startColumn + 1} style={cellStyle(cell)} className={`${highlight ? 'highlight' : ''} ${isIssue && highlight ? 'uncertain' : ''} ${hidden ? 'was-hidden' : ''}`} data-highlight={highlight ? 'true' : undefined} data-address={address} title={`${address}${hidden ? '（原文件中隐藏，已展开）' : ''}${cell?.formula ? ` · ${cell.formula}` : ''}`} onClick={() => { setTarget({ sheet: target.sheet, cell: merged?.range.split(':')[0] || address, ...(merged ? { range: merged.range } : {}) }); setAddress(merged?.range.split(':')[0] || address); }}>{cell?.display ?? ''}</td>;
      })}</tr>)}</tbody></table></div><div className="grid-caption"><span>{columnName(region.start_column)}{region.start_row}:{columnName(region.end_column)}{region.end_row} · 共 {region.max_row} 行 × {region.max_column} 列</span>{region.hidden && <span className="chip warn">原隐藏工作表，已展开</span>}</div></>}
      {selectedCell && <details className="cell-properties"><summary>原单元格读值与公式</summary><dl><dt>原始值</dt><dd>{selectedCell.value === null ? '（空白）' : String(selectedCell.value)}</dd><dt>数据类型</dt><dd>{selectedCell.value_type}</dd><dt>数字格式</dt><dd>{selectedCell.number_format || 'General'}</dd>{selectedCell.formula && <><dt>公式</dt><dd>{selectedCell.formula}</dd><dt>文件保存结果</dt><dd>{selectedCell.cache_status === 'missing' ? '不可用（未保存缓存）' : String(selectedCell.cached_value ?? '')} · 未重算</dd></>}{selectedCell.merged_range && <><dt>合并区域</dt><dd>{selectedCell.merged_range}</dd></>}</dl></details>}
      <p className="viewer-note">原地址只读视图；隐藏行列会展开。数字显示与 Excel 渲染可能不同。</p>
    </>}
  </section>;
}
