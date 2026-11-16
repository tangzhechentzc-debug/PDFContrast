import { useEffect, useMemo, useState } from 'react';
import { ApiError, columnName, jsonRequest, messageOf, request } from '../api';
import type { Job, Side, StructureCell, StructureResponse } from '../types';
import PdfViewer from './PdfViewer';
import StructureGridPreview from './StructureGridPreview';

type Position = { row: number; column: number };
const newCell = (): StructureCell => ({ text: '', manual: true });

export default function StructureEditor({ job, onBack, onSaved, onConfigure }: { job: Job; onBack: () => void; onSaved: (revision: number) => void; onConfigure: () => void }) {
  const [side, setSide] = useState<Side>('old');
  const [pageNumber, setPageNumber] = useState(job.files.old.pages?.find(p => p.method === 'ocr')?.number ?? job.files.old.pages?.[0]?.number ?? 1);
  const [sheet, setSheet] = useState('');
  const [directory, setDirectory] = useState<StructureResponse | null>(null);
  const [current, setCurrent] = useState<StructureResponse | null>(null);
  const [rows, setRows] = useState<StructureCell[][]>([]);
  const [undo, setUndo] = useState<StructureCell[][][]>([]);
  const [dirty, setDirty] = useState(false);
  const [selected, setSelected] = useState<Position>({ row: 0, column: 0 });
  const [target, setTarget] = useState<Position>({ row: 0, column: 0 });
  const [reason, setReason] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [conflict, setConflict] = useState(false);
  const [latest, setLatest] = useState<StructureResponse | null>(null);
  const [conflictChecked, setConflictChecked] = useState(false);
  const [reload, setReload] = useState(0);
  const hasDraft = dirty || !!reason || confirmed;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setDirectory(null); setCurrent(null); setRows([]);
    request<StructureResponse>(`/api/jobs/${job.id}/review-tables?${new URLSearchParams({ side, page: String(pageNumber) })}`, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setDirectory(result); setSheet(value => result.tables.some(t => t.sheet === value) ? value : result.tables[0]?.sheet ?? '');
    }).catch(e => { if (!controller.signal.aborted) setError(messageOf(e)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [job.id, side, pageNumber, reload]);

  useEffect(() => {
    if (!directory || !sheet) return;
    const controller = new AbortController();
    setLoading(true); setError(''); setCurrent(null); setRows([]);
    request<StructureResponse>(`/api/jobs/${job.id}/review-tables?${new URLSearchParams({ side, page: String(pageNumber), sheet })}`, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setCurrent(result); setRows(result.table?.rows ?? []); setUndo([]); setSelected({ row: 0, column: 0 }); setTarget({ row: 0, column: 0 });
      setDirty(false); setReason(''); setConfirmed(false); setConflict(false); setLatest(null); setConflictChecked(false);
    }).catch(e => { if (!controller.signal.aborted) setError(messageOf(e)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [job.id, side, pageNumber, sheet, directory]);

  useEffect(() => {
    if (!hasDraft) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [hasDraft]);

  const tableInfo = directory?.tables.find(t => t.sheet === sheet);
  const width = rows[0]?.length ?? 0;
  const editable = !!tableInfo?.editable && !!current?.table && !loading && !saving;
  const selectedCell = rows[selected.row]?.[selected.column];
  const selectedWasDeleted = !!selectedCell?.id && !!latest?.table && !latest.table.rows.some(row => row.some(cell => cell.id === selectedCell.id));
  const validTarget = Number.isInteger(target.row) && Number.isInteger(target.column) && !!rows[target.row]?.[target.column] && (target.row !== selected.row || target.column !== selected.column);
  const tooLong = rows.some(row => row.some(cell => cell.text.length > 10000));
  const overLimit = rows.length > 500 || width > 60 || rows.length * width > 10000 || rows.reduce((total, row) => total + row.reduce((sum, cell) => sum + cell.text.length, 0), 0) > 2000000;
  const source = useMemo(() => selectedCell?.source && !selectedCell.manual && !selectedCell.source.manual ? { ...selectedCell.source, page: pageNumber } : { kind: 'pdf' as const, method: 'ocr' as const, page: pageNumber, bbox: [0, 0, 1, 1] as [number, number, number, number], manual: true, location_precision: 'page' as const }, [selectedCell, pageNumber]);
  const currentJob = useMemo(() => ({ ...job, files: { ...job.files, [side]: { ...job.files[side], pages: (job.files[side].pages ?? []).map(page => page.number === pageNumber ? { ...page, reviewed: false } : page) } } }), [job, side, pageNumber]);

  function change(next: StructureCell[][], selection = selected) {
    setUndo(stack => [...stack.slice(-29), rows]); setRows(next); setDirty(true); setConfirmed(false); setConflictChecked(false); setNotice('');
    setSelected({ row: Math.min(Math.max(0, selection.row), next.length - 1), column: Math.min(Math.max(0, selection.column), next[0].length - 1) });
  }
  function editText(value: string) { change(rows.map((row, r) => r === selected.row ? row.map((cell, c) => c === selected.column ? { ...cell, text: value } : cell) : row)); }
  function insertRow(after: boolean) { const at = selected.row + (after ? 1 : 0); const next = rows.slice(); next.splice(at, 0, Array.from({ length: width }, newCell)); change(next, { ...selected, row: at }); }
  function insertColumn(after: boolean) { const at = selected.column + (after ? 1 : 0); change(rows.map(row => { const next = row.slice(); next.splice(at, 0, newCell()); return next; }), { ...selected, column: at }); }
  function moveRow(delta: number) { const next = rows.slice(); const to = selected.row + delta; [next[selected.row], next[to]] = [next[to], next[selected.row]]; change(next, { ...selected, row: to }); }
  function moveColumn(delta: number) { const to = selected.column + delta; change(rows.map(row => { const next = row.slice(); [next[selected.column], next[to]] = [next[to], next[selected.column]]; return next; }), { ...selected, column: to }); }
  function moveCell(swap: boolean) {
    if (!validTarget || !selectedCell) return;
    const destination = rows[target.row][target.column];
    if (!swap && destination.text !== '') return;
    const next = rows.map(row => row.slice()); next[target.row][target.column] = selectedCell; next[selected.row][selected.column] = swap ? destination : newCell(); change(next, target);
  }
  function discard() {
    const baseline = latest ?? current;
    if (baseline) { setCurrent(baseline); setRows(baseline.table?.rows ?? []); }
    setUndo([]); setDirty(false); setReason(''); setConfirmed(false); setConflict(false); setLatest(null); setConflictChecked(false); setError(''); setSelected({ row: 0, column: 0 });
  }
  async function loadLatest() {
    setLoading(true); setError('');
    try { const result = await request<StructureResponse>(`/api/jobs/${job.id}/review-tables?${new URLSearchParams({ side, page: String(pageNumber), sheet })}`); setLatest(result); setConflictChecked(false); }
    catch (e) { setError(messageOf(e)); }
    finally { setLoading(false); }
  }
  async function save() {
    if (!editable || !reason.trim() || !confirmed || tooLong || overLimit || (conflict && (!latest || !conflictChecked))) return;
    setSaving(true); setError(''); setNotice('');
    try {
      const result = await request<{ revision: number }>(`/api/jobs/${job.id}/review`, jsonRequest({ expected_revision: (latest ?? current)!.revision, side, action: 'table_update', sheet, page: pageNumber, rows: rows.map(row => row.map(cell => ({ ...(cell.id ? { id: cell.id } : {}), text: cell.text }))), structure_confirmed: true, reason: reason.trim() }));
      onSaved(result.revision); setDirty(false); setReason(''); setConfirmed(false); setUndo([]); setConflict(false); setLatest(null); setNotice(`结构修订 ${result.revision} 已保存。本页内容核验已撤销；请核验内容，并重新确认结构与比较规则。`); setReload(n => n + 1);
    } catch (e) { if (e instanceof ApiError && e.status === 409) { setConflict(true); setLatest(null); setConflictChecked(false); setError('任务已有新的修订。草稿已保留；加载最新结构后，逐格核对删除和移动冲突，再决定保存或放弃。'); } else setError(messageOf(e)); }
    finally { setSaving(false); }
  }

  return <section aria-label="OCR 手工结构编辑器"><div className="toolbar"><div><h2>补行与调整结构</h2><p className="muted small">选择已有 OCR 区域，按原图补齐或重排。已有格保留原始身份和定位；新增格仅定位整页。</p></div><div className="actions"><button disabled={hasDraft || loading || saving} onClick={onBack}>返回文字校正与核验</button><button disabled={hasDraft || loading || saving} onClick={onConfigure}>确认结构与比较规则</button></div></div>
    {hasDraft && <div className="notice">有未提交的结构草稿。保存或放弃后才能切换版本、页、区域或离开。<button disabled={saving || loading} onClick={discard}>放弃草稿{latest ? '并采用最新结构' : ''}</button></div>}
    {notice && <div className="notice" role="status">{notice}</div>}
    {error && <div className="error-banner" role="alert">{error}{!conflict && !hasDraft && <button disabled={loading || saving} onClick={() => setReload(n => n + 1)}>重试读取</button>}</div>}
    {conflict && <div className="notice"><p>冲突草稿尚未提交。不会自动合并或覆盖最新结构。</p><button disabled={loading || saving} onClick={() => void loadLatest()}>加载最新结构（保留我的草稿）</button>{latest?.table && <details open><summary>最新结构 · 修订 {latest.revision}；上方编辑网格仍是你的草稿</summary><StructureGridPreview table={latest.table} label="服务器最新结构" /><label className="check-label"><input type="checkbox" checked={conflictChecked} onChange={e => setConflictChecked(e.target.checked)} />我已核对最新结构中的增删与移位，并将按当前草稿提交</label><p className="small">若草稿引用的格已被删除，选中该格后可将其改为人工补录，或放弃草稿采用最新结构。</p></details>}</div>}
    <div className="toolbar compact"><label className="inline-label">版本<select disabled={hasDraft || loading || saving} value={side} onChange={e => { const next = e.target.value as Side; if (next === side) return; setSide(next); setPageNumber(job.files[next].pages?.find(p => p.method === 'ocr')?.number ?? job.files[next].pages?.[0]?.number ?? 1); setSheet(''); }}><option value="old">旧版</option><option value="new">新版</option></select></label><label className="inline-label">页<select value={pageNumber} disabled={hasDraft || loading || saving} onChange={e => { const next = Number(e.target.value); if (next === pageNumber) return; setPageNumber(next); setSheet(''); }}>{(job.files[side].pages ?? []).map(page => <option key={page.number} value={page.number}>第 {page.number} 页</option>)}</select></label><label className="inline-label">物理表 / 区域<select value={sheet} disabled={hasDraft || loading || saving} onChange={e => setSheet(e.target.value)}>{directory?.tables.map(table => <option key={table.sheet} value={table.sheet}>{table.sheet} · {table.rows} × {table.columns}</option>)}</select></label></div>
    {loading && <p role="status">读取结构…</p>}
    {!loading && !directory?.tables.length && !error && <p className="empty">此页没有可编辑的 OCR 物理区域。</p>}
    {tableInfo && !tableInfo.editable && <div className="notice">此区域不能进行结构编辑：{tableInfo.edit_limit || '提取状态不支持或超过编辑上限'}。原数据不会裁剪。</div>}
    {!!rows.length && <div className="structure-layout"><div className="structure-editor"><div className="toolbar compact"><span>{rows.length} 行 × {width} 列 · 第 1 行包含表头 · 当前修订 {current?.revision}</span><button disabled={!editable || !undo.length} onClick={() => { const previous = undo.at(-1)!; setRows(previous); setUndo(stack => stack.slice(0, -1)); setSelected({ row: 0, column: 0 }); setConfirmed(false); setConflictChecked(false); setDirty(true); }}>撤销最近操作</button></div><div className="structure-grid table-scroll"><table><thead><tr><th>行</th>{rows[0].map((_, c) => <th key={c}>{columnName(c + 1)}</th>)}</tr></thead><tbody>{rows.map((row, r) => <tr key={r}><th>{r + 1}</th>{row.map((cell, c) => <td key={c} className={selected.row === r && selected.column === c ? 'selected' : ''}><button className="structure-cell" disabled={!editable} aria-pressed={selected.row === r && selected.column === c} aria-label={`选择 ${columnName(c + 1)}${r + 1}`} onClick={() => setSelected({ row: r, column: c })}><span>{cell.text || '（空）'}</span>{(!cell.id || cell.manual || cell.source?.manual) && <small>人工补录 · 整页定位</small>}</button></td>)}</tr>)}</tbody></table></div>
      <fieldset disabled={!editable} className="plain-fieldset structure-controls"><legend>选中 {columnName(selected.column + 1)}{selected.row + 1}</legend><label>单元格文字<textarea aria-label="结构单元格文字" value={selectedCell?.text ?? ''} onChange={e => editText(e.target.value)} /></label><p className="small muted">{selectedCell?.id ? `稳定 ID：${selectedCell.id}` : '新增人工格：保存时生成身份，仅定位本页。'}</p>{conflict && selectedWasDeleted && <div className="notice"><p>此格身份已在最新结构中删除。转换将保留草稿文字，并以新的人工格保存、仅定位整页。</p><button onClick={() => change(rows.map((row, r) => r === selected.row ? row.map((cell, c) => c === selected.column ? { text: cell.text, manual: true } : cell) : row))}>将此格改为人工补录</button></div>}<div className="actions"><button disabled={!editable || rows.length >= 500 || (rows.length + 1) * width > 10000} onClick={() => insertRow(false)}>上方插入行</button><button disabled={!editable || rows.length >= 500 || (rows.length + 1) * width > 10000} onClick={() => insertRow(true)}>下方插入行</button><button disabled={!editable || rows.length <= 1} onClick={() => change(rows.filter((_, r) => r !== selected.row))}>删除选中行</button><button disabled={!editable || selected.row === 0} onClick={() => moveRow(-1)}>行上移</button><button disabled={!editable || selected.row >= rows.length - 1} onClick={() => moveRow(1)}>行下移</button></div><div className="actions"><button disabled={!editable || width >= 60 || rows.length * (width + 1) > 10000} onClick={() => insertColumn(false)}>左侧插入列</button><button disabled={!editable || width >= 60 || rows.length * (width + 1) > 10000} onClick={() => insertColumn(true)}>右侧插入列</button><button disabled={!editable || width <= 1} onClick={() => change(rows.map(row => row.filter((_, c) => c !== selected.column)))}>删除选中列</button><button disabled={!editable || selected.column === 0} onClick={() => moveColumn(-1)}>列左移</button><button disabled={!editable || selected.column >= width - 1} onClick={() => moveColumn(1)}>列右移</button></div><div className="structure-target"><label>目标行<input aria-label="移动目标行" type="number" min={1} max={rows.length} value={target.row + 1} onChange={e => setTarget(t => ({ ...t, row: Number(e.target.value) - 1 }))} /></label><label>目标列<select aria-label="移动目标列" value={target.column} onChange={e => setTarget(t => ({ ...t, column: Number(e.target.value) }))}>{rows[0].map((_, c) => <option key={c} value={c}>{columnName(c + 1)}</option>)}</select></label><button disabled={!editable || !validTarget} onClick={() => moveCell(true)}>交换两格</button><button disabled={!editable || !validTarget || rows[target.row]?.[target.column]?.text !== ''} onClick={() => moveCell(false)}>移动到空格</button></div></fieldset>
      <div className="structure-submit"><label>结构修改原因（必填）<textarea aria-label="结构修改原因" disabled={!editable} value={reason} onChange={e => setReason(e.target.value)} /></label><label className="check-label"><input type="checkbox" disabled={!editable} checked={confirmed} onChange={e => setConfirmed(e.target.checked)} />我已对照原图核对该区域行列结构；该声明不代表内容已核验</label><p className="small muted">保存后该区域重建为表格，并撤销本页所有内容与完整性核验。上限 500 行、60 列、10000 格，单格最多 10000 字符，总文字最多 200 万字符。</p>{(overLimit || tooLong) && <p className="error-banner">网格或单格文本超过编辑上限，请调整后保存；不会自动裁剪。</p>}<button className="primary" disabled={!editable || !reason.trim() || !confirmed || overLimit || tooLong || (conflict && (!latest || !conflictChecked))} onClick={() => void save()}>{saving ? '正在保存结构…' : '保存结构修订'}</button></div>
    </div><PdfViewer key={side} job={currentJob} side={side} source={source} selectionId={`${side}:${pageNumber}:${sheet}:${selected.row}:${selected.column}:${selectedCell?.id ?? 'manual'}`} isIssue pageNavigation={false} /></div>}
  </section>;
}
