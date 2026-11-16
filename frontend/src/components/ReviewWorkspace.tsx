import { useEffect, useMemo, useState } from 'react';
import { ApiError, jsonRequest, messageOf, request } from '../api';
import type { Job, ReviewAction, ReviewEvent, ReviewHistory, ReviewResponse, Run, Side } from '../types';
import PdfViewer from './PdfViewer';
import StructureEditor from './StructureEditor';
import StructureGridPreview from './StructureGridPreview';

const LIMIT = 100;
const actionLabels: Record<ReviewAction, string> = { cell_update: '文字校正', cell_verify: '核验识别格', cell_unverify: '撤销识别格核验', page_verify: '整页核验', page_unverify: '撤销整页核验', table_update: '调整表格结构' };
type Draft = { value: string; reason: string };

export default function ReviewWorkspace({ job, run, comparing, onBack, onCompare, onConfigure }: { job: Job; run: Run; comparing: boolean; onBack: () => void; onCompare: () => void; onConfigure: () => void }) {
  const fullReportMode = job.report_inventory_version === 2;
  const [structureMode, setStructureMode] = useState(false);
  const [structureRevision, setStructureRevision] = useState(job.structure_revision ?? 0);
  const [side, setSide] = useState<Side>('old');
  const [pageNumber, setPageNumber] = useState(job.files.old.pages?.[0]?.number ?? 1);
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<ReviewResponse | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [selectionRevision, setSelectionRevision] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, Draft>>({});
  const [pageConfirmed, setPageConfirmed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState(false);
  const [reload, setReload] = useState(0);
  const [notice, setNotice] = useState('');
  const [history, setHistory] = useState<ReviewHistory | null>(null);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyError, setHistoryError] = useState('');
  const [historyReload, setHistoryReload] = useState(0);
  const draftKey = (id: string) => `${side}:${id}`;
  const hasDrafts = Object.keys(drafts).length > 0;

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(''); setPageConfirmed(false);
    const query = new URLSearchParams({ side, page: String(pageNumber), offset: String(offset), limit: String(LIMIT) });
    request<ReviewResponse>(`/api/jobs/${job.id}/review?${query}`, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      setData(result); setStructureRevision(result.structure_revision ?? 0); setConflict(false);
      setSelectedId(id => result.items.some(item => item.id === id) ? id : result.items[0]?.id ?? '');
    }).catch(e => { if (!controller.signal.aborted) setError(messageOf(e)); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [job.id, side, pageNumber, offset, reload]);

  useEffect(() => {
    const controller = new AbortController();
    setHistoryError(''); setHistory(null);
    request<ReviewHistory>(`/api/jobs/${job.id}/review-history?offset=${historyOffset}&limit=30`, { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setHistory(result); }).catch(e => { if (!controller.signal.aborted) setHistoryError(messageOf(e)); });
    return () => controller.abort();
  }, [job.id, historyOffset, historyReload]);

  useEffect(() => {
    if (!hasDrafts) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [hasDrafts]);

  const selected = data?.items.find(item => item.id === selectedId);
  const currentPage = data?.pages.find(page => page.number === pageNumber);
  const draft = selected ? drafts[draftKey(selected.id)] ?? { value: selected.current_text, reason: '' } : null;
  const changed = !!selected && draft?.value !== selected.current_text;
  const available = !loading && !saving && !comparing && !!data && !conflict && !error;
  const currentJob = useMemo(() => ({ ...job, files: { ...job.files, [side]: { ...job.files[side], pages: (job.files[side].pages ?? []).map(page => ({ ...page, ...data?.pages.find(p => p.number === page.number) })) } } }), [job, side, data]);
  const source = selected ? { ...selected.source, kind: 'pdf' as const, page: selected.page, bbox: selected.bbox ?? selected.source?.bbox } : { kind: 'pdf' as const, page: pageNumber };

  function edit(field: keyof Draft, value: string) {
    if (!selected || !draft) return;
    setDrafts(all => ({ ...all, [draftKey(selected.id)]: { ...draft, [field]: value } }));
  }
  function discardCurrent() {
    if (!selected) return;
    setDrafts(all => { const next = { ...all }; delete next[draftKey(selected.id)]; return next; });
  }
  async function act(action: ReviewAction) {
    if (!available || !data) return;
    const cellAction = action.startsWith('cell_');
    if (cellAction && !selected) return;
    if (action === 'cell_update' && (!draft?.reason.trim() || !changed)) return;
    if (action === 'page_verify' && !pageConfirmed) return;
    setSaving(true); setError(''); setNotice('');
    try {
      const result = await request<{ revision: number; event: ReviewEvent }>(`/api/jobs/${job.id}/review`, jsonRequest({
        expected_revision: data.revision, side, action,
        ...(cellAction ? { target_id: selected!.id } : { page: pageNumber }),
        ...(action === 'cell_update' ? { value: draft!.value } : {}),
        reason: action === 'cell_update' ? draft!.reason.trim() : action === 'page_verify' ? fullReportMode ? '已对照原图核对识别文字与漏识别内容；结构、格式和版式分别判定' : '已逐项核对原图，确认无漏字、漏行、错列' : actionLabels[action],
      }));
      if (action === 'cell_update') discardCurrent();
      setNotice(`修订 ${result.revision} 已保存。${action === 'cell_update' ? '文字保存不等于核验，请对照原图单独核验。' : ''}当前比较结果未更新，请按原配置重新比较。`);
      setData(previous => previous ? { ...previous, revision: result.revision } : previous);
      setReload(n => n + 1); setHistoryReload(n => n + 1); setPageConfirmed(false);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) { setConflict(true); setError('其他操作已更新此任务。你的草稿已保留；请加载最新状态，核对当前文本后再保存。'); }
      else setError(messageOf(e));
    } finally { setSaving(false); }
  }

  const staleStructure = structureRevision > (run.config.review_revision ?? 0);
  if (structureMode) return <StructureEditor job={job} onBack={() => { setStructureMode(false); setReload(n => n + 1); setHistoryReload(n => n + 1); }} onSaved={revision => setStructureRevision(revision)} onConfigure={onConfigure} />;

  return <section aria-label="OCR 校正与核验工作区">
    <div className="toolbar"><div><h2>校正与核验 OCR</h2><p className="muted small">原始识别结果与原页保留不变；校正和核验分别记录。</p></div><div className="actions"><button disabled={saving || hasDrafts || comparing} onClick={onBack}>返回比较结果</button><button disabled={!available || hasDrafts} onClick={() => setStructureMode(true)}>补行与调整结构</button><button className="primary" disabled={!available || hasDrafts} onClick={staleStructure ? onConfigure : onCompare}>{comparing ? '正在处理…' : staleStructure ? '确认结构与比较规则' : '按原配置重新比较'}</button></div></div>
    <div className="notice"><p>当前比较使用修订 {run.review_revision ?? 0}；当前校正修订 {data?.revision ?? '…'}。核验整页前，请确认原图没有漏识别内容。</p>{staleStructure && <p>结构已改变，原比较范围与列映射已过时。请重新确认最新结构与比较规则。</p>}{hasDrafts && <p>有 {Object.keys(drafts).length} 个未保存草稿，切换页或版本会保留。保存或放弃草稿后可返回、重新比较。<button disabled={saving} onClick={() => setDrafts({})}>放弃全部未保存草稿</button></p>}</div>
    {notice && <div className="notice" role="status">{notice}</div>}
    {error && <div className="error-banner" role="alert">{error}<button disabled={loading || saving} onClick={() => setReload(n => n + 1)}>{conflict ? '加载最新状态（保留草稿）' : '重试读取'}</button></div>}
    <div className="toolbar compact"><label className="inline-label">版本<select disabled={saving || comparing} value={side} onChange={e => { const next = e.target.value as Side; if (next === side) return; setSide(next); setPageNumber(job.files[next].pages?.[0]?.number ?? 1); setOffset(0); setData(null); setSelectedId(''); }}><option value="old">旧版</option><option value="new">新版</option></select></label><label className="inline-label">页面<select disabled={saving || comparing} value={pageNumber} onChange={e => { const next = Number(e.target.value); if (next === pageNumber) return; setPageNumber(next); setOffset(0); setData(null); setSelectedId(''); }}>{(job.files[side].pages ?? []).map(page => <option key={page.number} value={page.number}>第 {page.number} 页 · {page.method === 'ocr' ? '扫描识别' : '文本提取'}</option>)}</select></label><span className="small muted">全部物理识别格，包含未发生差异的值</span></div>
    <div className="review-layout"><div className="review-editor"><div className="review-cell-list" aria-label="全部识别格">{loading ? <p role="status">读取识别格…</p> : data?.items.length ? data.items.map(item => <button key={item.id} className={`diff-item ${selectedId === item.id ? 'active' : ''}`} aria-pressed={selectedId === item.id} onClick={() => { setSelectedId(item.id); setSelectionRevision(n => n + 1); }} disabled={saving}><span className="label">{item.sheet}!{item.cell} · {item.reviewed ? '已核验' : '待核验'}{drafts[draftKey(item.id)] ? ' · 有草稿' : ''}</span><span>{item.current_text || '（空文本）'}</span></button>) : <p className="empty">本页没有可校正的识别格。请对照原图检查提取状态。</p>}</div>
      <div className="list-pagination"><span className="label">{data?.total ? `${offset + 1}–${Math.min(offset + LIMIT, data.total)} / ${data.total}` : '0 格'}</span><div className="actions"><button disabled={!available || offset === 0} onClick={() => setOffset(n => Math.max(0, n - LIMIT))}>上一批</button><button disabled={!available || !data || offset + LIMIT >= data.total} onClick={() => setOffset(n => n + LIMIT)}>下一批</button></div></div>
      {selected && draft && !loading && <div className="review-form"><h3>{selected.sheet}!{selected.cell}</h3><dl><dt>原始文本</dt><dd>{selected.original_text || '（空文本）'}</dd><dt>已保存文本</dt><dd>{selected.current_text || '（空文本）'}</dd><dt>当前状态</dt><dd>{selected.reviewed ? '识别格已核验' : '识别格待核验'} · {selected.structure === 'table' ? '表格单元格' : '文字区域'}{selected.correction_revision != null ? ` · 最近校正修订 ${selected.correction_revision}` : ''}</dd></dl><label>校正文本（可清空）<textarea aria-label="校正文本" value={draft.value} disabled={!available} onChange={e => edit('value', e.target.value)} /></label><label>修改原因（保存时必填）<textarea aria-label="修改原因" rows={2} value={draft.reason} disabled={!available} onChange={e => edit('reason', e.target.value)} /></label><div className="actions"><button className="primary" disabled={!available || !changed || !draft.reason.trim()} onClick={() => void act('cell_update')}>保存文字校正</button><button disabled={!available || !drafts[draftKey(selected.id)]} onClick={discardCurrent}>放弃此格草稿</button><button disabled={!available || changed || selected.reviewed} onClick={() => void act('cell_verify')}>确认此格与原图一致</button><button disabled={!available || !selected.reviewed} onClick={() => void act('cell_unverify')}>撤销此格核验</button></div><p className="small muted">保存会撤销此格和本页的核验。单格核验仅确认当前值，不表示本页无漏识别。</p></div>}
      <div className="review-page-state"><h3>第 {pageNumber} 页{fullReportMode ? '文字核验' : '完整性'}</h3><p>{currentPage ? `${currentPage.reviewed ? '整页已核验' : '整页待核验'} · 已核验 ${currentPage.reviewed_cells} / ${currentPage.total_cells} 格` : '正在读取页面状态…'}</p>{currentPage && !currentPage.reviewable && <p className="warning-text">{fullReportMode ? '此页当前提取状态不支持文字核验，请先查看提取问题。' : '此页提取失败或结构不完整，不能通过整页核验消除问题。'}</p>}<label className="check-label"><input type="checkbox" checked={pageConfirmed} disabled={!available || !currentPage?.reviewable || hasDrafts} onChange={e => setPageConfirmed(e.target.checked)} />{fullReportMode ? '我已对照原图核对识别文字与漏识别内容；结构、格式和版式另行判定' : '我已逐项核对原图，确认无漏字、漏行、错列'}</label><div className="actions"><button disabled={!available || !currentPage?.reviewable || currentPage.reviewed || !pageConfirmed || hasDrafts} onClick={() => void act('page_verify')}>确认整页及全部识别格</button><button disabled={!available || !currentPage || (!currentPage.reviewed && !currentPage.reviewed_cells)} onClick={() => void act('page_unverify')}>撤销整页及全部识别格核验</button></div></div>
    </div><PdfViewer key={side} job={currentJob} side={side} source={source} selectionId={`${side}:${pageNumber}:${selectedId}:${selectionRevision}`} isIssue={!selected?.reviewed} pageNavigation={false} /></div>
    <details className="review-history" open><summary>校正与核验历史</summary>{historyError ? <div className="error-banner" role="alert">{historyError}<button onClick={() => setHistoryReload(n => n + 1)}>重试历史</button></div> : !history ? <p role="status">读取历史…</p> : <><div className="table-scroll"><table><thead><tr><th>修订 / 时间</th><th>位置 / 操作</th><th>修改前</th><th>修改后</th><th>原因</th></tr></thead><tbody>{history.items.map(event => <tr key={event.revision}><td>{event.revision}<br />{event.created_at}</td><td>{event.side === 'old' ? '旧版' : '新版'} 第 {event.page} 页 {event.sheet && `${event.sheet}${event.cell ? `!${event.cell}` : ''}`}<br />{actionLabels[event.action]}</td><td>{event.before_text ?? '—'}</td><td>{event.after_text ?? '—'}</td><td>{event.reason}{event.table_before && event.table_after && <details><summary>展开结构修改前后网格</summary><StructureGridPreview table={event.table_before} label="修改前" /><StructureGridPreview table={event.table_after} label="修改后" /></details>}</td></tr>)}</tbody></table>{!history.items.length && <p className="empty">尚无人工校正或核验记录。</p>}</div><div className="list-pagination"><span>{history.total} 条事件</span><div className="actions"><button disabled={historyOffset === 0} onClick={() => setHistoryOffset(n => Math.max(0, n - 30))}>较新记录</button><button disabled={historyOffset + 30 >= history.total} onClick={() => setHistoryOffset(n => n + 30)}>较早记录</button></div></div></>}</details>
  </section>;
}
