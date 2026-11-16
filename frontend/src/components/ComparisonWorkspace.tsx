import { useEffect, useRef, useState } from 'react';
import { dimensionLabels, dimensionStatusLabels, kindLabels, messageOf, request, sourceLabel } from '../api';
import type { ComparisonDimension, Diff, DiffPage, Job, ResultLocation, Run } from '../types';
import SheetViewer from './SheetViewer';
import PdfViewer from './PdfViewer';
import PdfReportOverview from './PdfReportOverview';
import PdfIssueGroups from './PdfIssueGroups';
import PdfFullReportOverview from './PdfFullReportOverview';
import PdfComparisonViewer from './PdfComparisonViewer';
import DiffText from './DiffText';

const PAGE_SIZE = 100;
const paragraphAttributeLabels = { line_spacing: '段落行距', alignment: '段落左 / 中 / 右对齐' };
const paragraphStatusLabels = { changed: '有变化', unchanged: '未发现变化', unverified: '未验证' };

export default function ComparisonWorkspace({ job, run, onConfigure, onReview, onReprocess, actionBusy }: { job: Job; run: Run; onConfigure: () => void; onReview: () => void; onReprocess: () => void; actionBusy: boolean }) {
  const pdf = job.kind === 'pdf';
  const hasReportInventory = pdf && !!run.reports;
  const fullReportMode = pdf && run.report_inventory_version === 2;
  const [layer, setLayer] = useState(hasReportInventory && !fullReportMode ? 'content' : 'all');
  const [category, setCategory] = useState('all');
  const [reportId, setReportId] = useState(() => {
    if (!fullReportMode) return '';
    const requested = new URLSearchParams(window.location.search).get('report');
    if (requested === 'all') return '';
    return run.reports?.find(report => report.id === requested)?.id ?? run.reports?.find(report => report.old && report.new)?.id ?? run.reports?.[0]?.id ?? '';
  });
  const [location, setLocation] = useState<ResultLocation | null>(null);
  const originalsRef = useRef<HTMLDivElement>(null);
  const evidenceHeading = useRef<HTMLHeadingElement>(null);
  const overview = useRef<HTMLDetailsElement>(null);
  const snapshotJob = run.review_pages ? { ...job, files: { old: { ...job.files.old, pages: run.review_pages.old }, new: { ...job.files.new, pages: run.review_pages.new } } } : job;
  const hasOcr = pdf && [...(snapshotJob.files.old.pages ?? []), ...(snapshotJob.files.new.pages ?? [])].some(page => page.method === 'ocr' && !page.reviewed);
  const Viewer = pdf ? PdfViewer : SheetViewer;
  const isOcrDiff = (diff: Diff) => pdf && diff.severity === 'issue' && (diff.old_source?.method === 'ocr' || diff.new_source?.method === 'ocr' || diff.kind.startsWith('ocr_'));
  const labelFor = (diff: Diff) => isOcrDiff(diff) && (diff.kind.endsWith('added') || diff.kind.endsWith('removed')) ? `疑似${kindLabels[diff.kind] ?? '增删变化'}` : kindLabels[diff.kind] ?? (fullReportMode && diff.category && diff.category !== 'completeness' ? `${dimensionLabels[diff.category]}${diff.severity === 'issue' ? '待核验' : '变化'}` : diff.kind);
  function displayValue(diff: Diff, side: 'old' | 'new'): string {
    const value = diff[`${side}_value`];
    if (value !== null) return value;
    if (fullReportMode && diff.kind === 'text_changed' && diff.category === 'content' && diff.severity === 'change' &&
        (diff[`${side}_source`] as { location_precision?: string } | null)?.location_precision === 'corresponding_region') return '空白';
    return pdf ? '无对应提取值' : '不存在';
  }

  const [query, setQuery] = useState('');
  const [search, setSearch] = useState('');
  const [kind, setKind] = useState('all');
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<DiffPage | null>(null);
  const [selected, setSelected] = useState<Diff | null>(null);
  const [selectionRevision, setSelectionRevision] = useState(0);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [overviewOpened, setOverviewOpened] = useState(false);
  const [historyOpened, setHistoryOpened] = useState(false);
  const [runs, setRuns] = useState<Run[]>([]);
  const [runsLoading, setRunsLoading] = useState(true);
  const [runsError, setRunsError] = useState('');
  const [runsReload, setRunsReload] = useState(0);

  useEffect(() => {
    if (!historyOpened) return;
    const controller = new AbortController();
    setRunsError(''); setRunsLoading(true);
    request<{ items: Run[] }>(`/api/jobs/${job.id}/runs`, { signal: controller.signal }).then(result => { if (!controller.signal.aborted) setRuns(result.items); }).catch(e => { if (!controller.signal.aborted) setRunsError(messageOf(e)); }).finally(() => { if (!controller.signal.aborted) setRunsLoading(false); });
    return () => controller.abort();
  }, [job.id, historyOpened, runsReload]);
  const nextSelection = useRef<'first' | 'last'>('first');
  const loadedPosition = useRef('');
  const positions = useRef(new Map<string, { offset: number; id: string }>());
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (query.trim() === search) return;
    const timer = setTimeout(() => {
      const nextSearch = query.trim();
      setSearch(nextSearch);
      const key = JSON.stringify([reportId, layer, category, kind, nextSearch]);
      setOffset(positions.current.get(key)?.offset ?? 0);
    }, 250);
    return () => clearTimeout(timer);
  }, [query, search, reportId, layer, category, kind]);
  const positionKey = JSON.stringify([reportId, layer, category, kind, search]);
  useEffect(() => {
    if (fullReportMode && selected && page && loadedPosition.current === positionKey) positions.current.set(positionKey, { offset: page.offset, id: selected.id });
  }, [positionKey, selected, page, fullReportMode]);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setError(''); setSelected(null); setPage(null);
    const direction = nextSelection.current;
    nextSelection.current = 'first';
    const params = new URLSearchParams({ offset: String(offset), limit: String(PAGE_SIZE), kind, q: search });
    if (hasReportInventory) { params.set('layer', layer); if (reportId) params.set('report_id', reportId); }
    if (fullReportMode) params.set('category', category);
    request<DiffPage>(`/api/runs/${run.id}/diffs?${params}`, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return;
      const saved = positions.current.get(positionKey);
      const restored = saved?.offset === offset ? result.items.find(item => item.id === saved.id) : undefined;
      loadedPosition.current = positionKey;
      setPage(result);
      setSelected(direction === 'last' ? result.items.at(-1) ?? null : restored ?? result.items[0] ?? null);
    }).catch(e => { if (!controller.signal.aborted) setError(messageOf(e)); }).finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
  }, [run.id, offset, kind, search, reload, layer, reportId, hasReportInventory, fullReportMode, category, positionKey]);

  useEffect(() => {
    const active = listRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]');
    if (active && listRef.current) {
      const list = listRef.current;
      const itemTop = active.offsetTop;
      if (itemTop < list.scrollTop || itemTop + active.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = itemTop - 4;
    }
  }, [selected?.id]);

  const index = page?.items.findIndex(x => x.id === selected?.id) ?? -1;
  const absoluteIndex = index >= 0 ? offset + index : -1;
  function move(direction: number) {
    if (!page || busy) return;
    setLocation(null);
    const next = page.items[index + direction];
    if (next) setSelected(next);
    else if (direction > 0 && offset + page.items.length < page.total) { nextSelection.current = 'first'; setOffset(offset + PAGE_SIZE); }
    else if (direction < 0 && offset > 0) { nextSelection.current = 'last'; setOffset(Math.max(0, offset - PAGE_SIZE)); }
  }

  function focusEvidence() {
    requestAnimationFrame(() => evidenceHeading.current?.focus());
  }

  function locate(value: ResultLocation) {
    setLocation(value);
    if (fullReportMode) {
      if (overview.current) overview.current.open = false;
      focusEvidence();
    } else {
      setSelected(null);
      requestAnimationFrame(() => originalsRef.current?.scrollIntoView({ block: 'start' }));
    }
  }

  function changeFilters(nextLayer: string, nextReport: string, nextCategory: string, nextKind = kind, nextSearch = search) {
    setLocation(null);
    setLayer(nextLayer); setReportId(nextReport); setCategory(nextCategory); setKind(nextKind);
    const key = JSON.stringify([nextReport, nextLayer, nextCategory, nextKind, nextSearch]);
    setOffset(positions.current.get(key)?.offset ?? 0);
  }

  function filterLayer(nextLayer: string, nextReport = '', nextCategory = 'all') {
    if (fullReportMode) {
      changeFilters(nextLayer, nextReport, nextCategory);
      if (overview.current) overview.current.open = false;
      requestAnimationFrame(() => document.getElementById('current-report')?.focus());
    } else {
      setSelected(null); setLayer(nextLayer); setReportId(nextReport); setKind('all'); setOffset(0);
      setLocation(null); setQuery(''); setSearch('');
      requestAnimationFrame(() => originalsRef.current?.scrollIntoView({ block: 'start' }));
    }
  }

  function clearFilters() {
    setQuery(''); setSearch('');
    changeFilters('all', reportId, 'all', 'all', '');
  }

  useEffect(() => {
    if (!fullReportMode) return;
    const url = new URL(window.location.href);
    url.searchParams.set('report', reportId || 'all');
    window.history.replaceState(window.history.state, '', url);
  }, [fullReportMode, reportId]);

  const selectedReport = run.reports?.find(report => report.id === reportId) ?? null;
  const evidenceReport = location
    ? run.reports?.find(report => report.id === location.report_id) ?? null
    : selectedReport ?? run.reports?.find(report => report.id === selected?.report_id) ?? null;
  const dimensionChanges = (dimension: ComparisonDimension) => (run.reports ?? []).reduce((sum, report) => sum + (report.dimensions?.[dimension]?.changes ?? 0), 0);
  const reliableReports = run.reports?.filter(report => report.presence === 'shared' && report.content_status === 'reliable') ?? [];
  const contentChanges = reliableReports.reduce((total, report) => total + report.change_count, 0);
  const oldSource = location ? location.old_source : selected?.old_source ?? null;
  const newSource = location ? location.new_source : selected?.new_source ?? null;
  const sourceSelection = location?.id ?? (selected ? `${selected.id}:${selectionRevision}` : null);
  const excluded = [...run.coverage.excluded_sheets.old.map(s => `旧版「${s}」`), ...run.coverage.excluded_sheets.new.map(s => `新版「${s}」`)];
  const hasIssues = hasOcr || run.status === 'completed_with_issues' || run.summary.issues > 0 || excluded.length > 0 || run.coverage.warnings.length > 0;
  const hasSuspectedDifferences = (run.summary.by_kind.ocr_suspected ?? 0) > 0;
  const resultTitle = hasSuspectedDifferences ? (run.summary.changes > 0 ? '发现内容变化与疑似差异' : '发现疑似差异，请核对扫描原图') : run.summary.changes > 0 ? (hasOcr ? '发现报表差异，扫描内容仍待核验' : '发现报表差异') : (run.summary.by_kind.ocr_review ?? 0) > 0 && run.summary.issues === run.summary.by_kind.ocr_review ? '按识别结果未发现差异，扫描内容待核验' : hasIssues ? '已比较部分未发现确定差异，仍有内容待核对' : '所选范围未发现差异';

  const hasFilters = !!(query || search || kind !== 'all' || category !== 'all' || layer !== 'all');
  const dimensions = selectedReport?.dimensions;
  const statuses = Object.values(dimensions ?? {}).map(item => item.status);
  const noChanges = statuses.includes('unchanged') && statuses.every(status => status === 'unchanged' || status === 'not_applicable');
  const notCompared = !!selectedReport && statuses.every(status => status === 'not_applicable');
  const coveragePanel = <details className="coverage-details" open={!pdf && hasIssues}><summary>查看比较范围与规则</summary><div className="coverage-content"><p><strong>已比较：</strong>旧版 {run.coverage.compared_sheets.old.join('、') || '无'}；新版 {run.coverage.compared_sheets.new.join('、') || '无'}。</p>{excluded.length > 0 && <p className="warning-text"><strong>未比较：</strong>{excluded.join('、')}。</p>}<p>数值绝对容差 {run.config.absolute_tolerance}；{run.config.ignore_outer_whitespace ? '忽略' : '保留'}文本首尾空白{!pdf && <>；{run.config.compare_formulas ? '包含' : '不比较'}公式；{run.config.compare_formatting ? '包含' : '不比较'}格式</>}。{hasOcr && ' 扫描内容尚未人工核验；可直接浏览两侧原页。'}</p>{!pdf && run.coverage.warnings.length > 0 && <ul>{run.coverage.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul>}</div></details>;
  const historyPanel = <details className="coverage-details" onToggle={event => { if (event.currentTarget.open) setHistoryOpened(true); }}><summary>历史比较结果 · 修订 {run.review_revision ?? 0}</summary>
    {fullReportMode && job.parent_job_id && <p className="small">本任务由原件重新处理生成。<a href={`?job=${encodeURIComponent(job.parent_job_id)}`}>查看原任务</a>及其独立保存的校正与历史结果。</p>}{runsError ? <p className="error-banner">{runsError}<button onClick={event => {
      event.currentTarget.closest('details')?.querySelector('summary')?.focus({ preventScroll: true });
      setRunsReload(n => n + 1);
    }}>重试读取</button></p> : runsLoading ? <p role="status">读取历史结果…</p> : <ul>{runs.map(item => <li key={item.id}><a aria-current={item.id === run.id ? "page" : undefined} href={`?job=${encodeURIComponent(job.id)}&run=${encodeURIComponent(item.id)}`}>比较 {item.id} · 修订 {item.review_revision ?? 0}</a>{item.id === run.id ? "（当前）" : ""} · 变化 {item.summary.changes} / 待核对 {item.summary.issues}</li>)}</ul>}<p>每次比较固定使用当时的校正与核验状态；之后的修改不会改变历史结果或其导出。</p></details>;

  return <section className={fullReportMode ? 'full-result' : undefined} aria-label="比较结果">
    {fullReportMode && <dl className="version-strip">
      <div><dt className="evidence-side old">旧版</dt><dd>{job.files.old.name}</dd></div>
      <div><dt className="evidence-side new">新版</dt><dd>{job.files.new.name}</dd></div>
    </dl>}
    <div className="toolbar result-toolbar"><div className="actions"><button id="configure-result" disabled={actionBusy} onClick={onConfigure}>调整范围与规则</button>{pdf && <button id="review-result" disabled={actionBusy} onClick={onReview}>校正与核验 OCR</button>}<a className="button primary" href={`/api/runs/${run.id}/export`} download>{fullReportMode ? '导出完整报告' : pdf ? '导出完整比较报告' : '导出完整差异'}</a></div>{!fullReportMode && <span className="muted small">{pdf ? hasReportInventory ? '导出包含当次完整性、内容差异与待处理问题。' : '导出保留当次差异与范围提示；历史结果尚无业务报表完整性判定。' : '公式结果来自文件缓存，未重新计算。'}</span>}</div>
    {pdf && !fullReportMode && <div className="notice"><p>此历史结果未计算完整报表的四维对比，仍按当时的比较与核验状态展示。</p><button disabled={actionBusy} onClick={onReprocess}>重新处理原件，生成四维比较（新任务）</button></div>}
    {pdf && !fullReportMode && <PdfReportOverview run={run} busy={actionBusy} legacy={job.report_inventory_version !== 1} onLocate={locate} onContent={id => filterLayer('content', id)} onConfigure={onConfigure} onReprocess={onReprocess} />}
    {!fullReportMode && <div className={`result-summary ${hasIssues ? 'has-issues' : ''}`}><div><h2>{hasReportInventory ? '可靠共有报表的内容差异' : resultTitle}</h2>{hasReportInventory ? <div className="result-counts"><span>可靠共有实例 <strong>{reliableReports.length}</strong></span><span>内容变化 <strong>{contentChanges}</strong></span><span>待处理问题 <strong>{run.issues?.length ?? run.summary.issues}</strong></span></div> : <div className="result-counts"><span>变化 <strong>{run.summary.changes}</strong></span><span>待核对 <strong>{run.summary.issues}</strong></span><span>已匹配项目 <strong>{run.summary.matched_rows}</strong></span><span>{pdf ? '比较字段' : '比较单元格'} <strong>{run.summary.compared_cells}</strong></span></div>}</div>{hasIssues && <span className="chip warn">◇ 存在未完成核对或未比较范围</span>}</div>}
    {!fullReportMode && coveragePanel}
    {hasReportInventory && !fullReportMode && <div className="toolbar compact result-filters"><label className="inline-label">结果层级<select value={layer} onChange={e => filterLayer(e.target.value)}><option value="content">可靠共有报表内容</option><option value="completeness">报表完整性事件</option><option value="uncertainty">待确认区域与疑似差异</option><option value="all">全部事件（保留原始证据）</option></select></label><label className="inline-label">报表实例<select value={reportId} onChange={e => filterLayer(layer, e.target.value)}><option value="">本层全部实例</option>{(layer === 'content' ? reliableReports : run.reports ?? []).map(report => <option key={report.id} value={report.id}>{report.title} · {report.object || '业务对象待确认'}</option>)}</select></label></div>}
    {fullReportMode && <section className="selected-report-panel" aria-label="单报表比较">
      <div className="report-context">
        <label className="report-picker">当前报表
          <select id="current-report" aria-label="当前完整报表" value={reportId} onChange={event => changeFilters(layer, event.target.value, category)}>
            <option value="">全部报表事件 / 独立原页浏览</option>
            {run.reports?.map(report => <option key={report.id} value={report.id}>{report.type_code ? `${report.type_code} · ` : ''}{report.title} · 旧 {report.old?.pages.join('、') || '无'} / 新 {report.new?.pages.join('、') || '无'}</option>)}
          </select>
        </label>
        <div className="report-identity">
          <h2>{selectedReport ? `${selectedReport.type_code ? `${selectedReport.type_code} · ` : ''}${selectedReport.title}` : '全部报表事件'}</h2>
          {selectedReport && <p className="small muted">旧版页码 {selectedReport.old?.pages.join('、') || '无对应页'} / 新版页码 {selectedReport.new?.pages.join('、') || '无对应页'}{selectedReport.confirmation === 'pending' ? ' · 对应关系待确认' : ''}</p>}
        </div>
      </div>
      {selectedReport && <div className="selected-dimensions" aria-label="当前报表四维摘要">
        {(Object.keys(dimensionLabels) as ComparisonDimension[]).map(dimension => {
          const result = selectedReport.dimensions?.[dimension];
          return <button key={dimension} aria-pressed={category === dimension} onClick={() => changeFilters(layer, reportId, category === dimension ? 'all' : dimension)}>
            <span className="dimension-title">{dimensionLabels[dimension]} <span>{result ? dimensionStatusLabels[result.status] : '尚未计算'}</span></span>
            <span className={result?.issues ? 'warning-text' : 'muted'}>{result ? `${result.changes} 项变化 · ${result.issues} 项待核对` : '无当次结果'}</span>
          </button>;
        })}
      </div>}
    </section>}
    {fullReportMode && <div className="result-disclosures">
      <details className="coverage-details" ref={overview} onToggle={event => { if (event.currentTarget.open) setOverviewOpened(true); }}><summary>全部报表总览 · {run.reports?.length ?? 0} 张</summary>
        <div className="result-counts">{(Object.keys(dimensionLabels) as ComparisonDimension[]).map(dimension => <span key={dimension}>{dimensionLabels[dimension]}变化 <strong>{dimensionChanges(dimension)}</strong></span>)}</div>
        {overviewOpened && <PdfFullReportOverview run={run} busy={actionBusy} onOpen={(id, dimension) => filterLayer('all', id, dimension ?? 'all')} onLocate={locate} onConfigure={onConfigure} />}
      </details>
      {historyPanel}
      <details className="coverage-details"><summary>比较范围与判定依据{hasIssues && <span className="warning-text"> · 有待核对或未比较范围</span>}</summary>
        {selectedReport && <div className="report-basis"><h3>{selectedReport.title}</h3><p>{selectedReport.basis}</p>
          {!!selectedReport.blocking_reasons.length && <ul>{selectedReport.blocking_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
          <div className="report-dimension-basis">{(Object.keys(dimensionLabels) as ComparisonDimension[]).map(dimension => {
            const result = selectedReport.dimensions?.[dimension];
            return <details key={dimension}><summary>{dimensionLabels[dimension]} · {result ? dimensionStatusLabels[result.status] : '尚未计算'}</summary>
              <p>{result?.summary || '该维度没有当次结果。'}</p>
              {!!result?.limitations.length && <ul>{result.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
            </details>;
          })}</div>
          <details className="coverage-details" data-testid="paragraph-checks"><summary>段落行距与对齐检查{selectedReport.paragraph_checks ? ` · 变化 ${selectedReport.paragraph_checks.filter(check => check.status === 'changed').length} 项 · 未发现变化 ${selectedReport.paragraph_checks.filter(check => check.status === 'unchanged').length} 项 · 未验证 ${selectedReport.paragraph_checks.filter(check => check.status === 'unverified').length} 项` : ' · 本次未记录'}</summary>{selectedReport.paragraph_checks?.length ? <div className="table-scroll"><table style={{ minWidth: '44rem' }}><thead><tr><th>检查属性</th><th>判定</th><th>旧版结果 / 页码</th><th>新版结果 / 页码</th><th>依据与验证范围</th></tr></thead><tbody>{selectedReport.paragraph_checks.map((check, index) => <tr key={`${check.attribute}:${index}`}><td>{paragraphAttributeLabels[check.attribute]}</td><td className={check.status === 'unverified' ? 'warning-text' : undefined}>{paragraphStatusLabels[check.status]}</td>{(['old', 'new'] as const).map(side => <td key={side}><p>{check[`${side}_value`] ?? '未提供测量结果'}</p>{check[`${side}_source`] ? <button onClick={() => locate({ id: `paragraph:${selectedReport.id}:${index}`, report_id: selectedReport.id, title: `${selectedReport.title} / ${paragraphAttributeLabels[check.attribute]}`, message: check.message, old_source: check.old_source, new_source: check.new_source })}>查看{side === 'old' ? '旧版' : '新版'}{sourceLabel(check[`${side}_source`], true)}</button> : <span className="small muted">未提供原页定位</span>}</td>)}<td>{check.message}</td></tr>)}</tbody></table></div> : <p className="muted">此结果没有段落行距与对齐的检查记录，不能据此认定这些属性相同。</p>}</details>
        </div>}
        {coveragePanel}
      </details>
    </div>}
    {pdf && !fullReportMode && historyPanel}
    <div className={`workspace ${fullReportMode ? 'full-report-workspace' : ''}`} ref={originalsRef}><aside className="sidebar"><h2>{fullReportMode ? '差异与待核对记录' : hasReportInventory ? layer === 'content' ? '可靠内容差异' : layer === 'uncertainty' ? '待确认记录' : layer === 'completeness' ? '完整性事件' : '全部事件' : '差异明细'}</h2> {fullReportMode && <div className="record-filters">
        <label>维度<select aria-label="比较维度" value={category} onChange={event => changeFilters(layer, reportId, event.target.value)}><option value="all">全部维度</option>{Object.entries(dimensionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <label>范围<select aria-label="完整报表事件范围" value={layer} onChange={event => changeFilters(event.target.value, reportId, category)}><option value="all">全部事件</option><option value="completeness">完整性事件</option><option value="uncertainty">待确认 / 未验证</option></select></label>
      </div>}<label className="label" htmlFor="diff-search">按项目、字段或内容查找</label><input id="diff-search" type="search" className="search" placeholder={fullReportMode ? "搜索字段、原值或新值" : "例如 A001、金额"} value={query} onChange={e => { setLocation(null); setQuery(e.target.value); }} /><label className="sr-only" htmlFor="kind-filter">差异类型</label><select id="kind-filter" className="kind-filter" value={kind} onChange={e => { if (fullReportMode) changeFilters(layer, reportId, category, e.target.value); else { setLocation(null); setKind(e.target.value); setOffset(0); } }}><option value="all">全部类型</option>{Object.entries(kindLabels).filter(([key]) => (run.summary.by_kind[key] ?? 0) > 0).map(([key, label]) => <option key={key} value={key}>{label}{!fullReportMode && `（${run.summary.by_kind[key]}）`}</option>)}</select>
      {error && <div className="error-banner" role="alert">{fullReportMode ? `差异记录加载失败：${error}` : error}<button onClick={() => { const filter = document.getElementById('kind-filter'); if (fullReportMode && filter) filter.focus({ preventScroll: true }); setReload(n => n + 1); }}>重试读取</button></div>}
      {fullReportMode && <button type="button" className="skip-record-list" onClick={focusEvidence}>查看当前记录与原文</button>}
      <div className="diff-list" ref={listRef} aria-label="选择差异" aria-busy={busy}>{busy ? <div className="empty" role="status">正在读取差异记录…</div> : error ? null : page?.items.length ? page.items.map(diff => <button key={diff.id} className={`diff-item ${!location && diff.id === selected?.id ? 'active' : ''}`} aria-pressed={!location && diff.id === selected?.id} onClick={() => { setLocation(null); setSelected(diff); setSelectionRevision(n => n + 1);
        if (fullReportMode && window.matchMedia('(max-width: 1000px)').matches) focusEvidence(); }}><span className={`kind ${diff.severity === 'issue' ? 'warning-text' : ''}`}>{fullReportMode ? <>{diff.category ? diff.category === 'completeness' ? '完整性 · ' : `${dimensionLabels[diff.category]} · ` : ''}{diff.severity === 'issue' ? '待核对' : '有变化'}</> : labelFor(diff)}</span>
        <strong>{fullReportMode ? labelFor(diff) : diff.row_key || diff.table_name}</strong>
        <span className="diff-field">{fullReportMode ? diff.field || diff.row_key : diff.field || diff.table_name}</span>
        {fullReportMode ? <>{diff.old_value !== diff.new_value ? <span className="diff-card-values">{(['old', 'new'] as const).map(side => <small key={side}><b>{side === 'old' ? '旧' : '新'}</b><span><DiffText diff={diff} side={side} fallback={displayValue(diff, side)} preview /></span></small>)}</span> : <small>{diff.row_key || diff.message}</small>}</> : <small>{displayValue(diff, 'old')} → {displayValue(diff, 'new')}</small>}</button>) : <div className="empty">{fullReportMode ? hasFilters ? '当前筛选没有匹配记录。' : notCompared ? '当前报表没有可用的比较结果，请查看判定依据。' : noChanges ? '本报表已比较范围内未发现变化。' : '本范围没有差异记录，请查看比较范围与待核对项。' : hasReportInventory && layer === 'content' ? '本层没有符合条件的可靠内容差异；请同时核对报表完整性和下方待处理区域。' : run.summary.total ? '没有符合筛选条件的差异。' : hasIssues ? '没有确定差异，请查看上方范围和待核对提示。' : '所选范围内没有差异。'}{(fullReportMode ? hasFilters : query || kind !== 'all') && <button onClick={() => {
        if (fullReportMode) { document.getElementById('diff-search')?.focus({ preventScroll: true }); clearFilters(); }
        else { setQuery(''); setSearch(''); setKind('all'); setOffset(0); }
      }}>清空筛选</button>}</div>}</div>
      <div className="list-pagination"><span className="label">{page?.total ? `${offset + 1}–${Math.min(offset + PAGE_SIZE, page.total)} / ${page.total}` : busy || error ? '—' : '0 条'}</span><div className="actions"><button disabled={busy || offset === 0} aria-label="上一页差异" onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>上一页</button><button disabled={busy || !page || offset + PAGE_SIZE >= page.total} aria-label="下一页差异" onClick={() => setOffset(offset + PAGE_SIZE)}>下一页</button></div></div>
    </aside>{fullReportMode ? <div className="comparison-evidence"><div className="evidence-heading">
        <div className="evidence-heading-text">
          <h3 ref={evidenceHeading} tabIndex={-1} aria-live="polite">{location ? location.title : selected ? labelFor(selected) : '原文核对'}</h3>
          {evidenceReport && !location && <p className="small muted evidence-report-title" title={evidenceReport.title}>{evidenceReport.title}</p>}
          {selected && !location && <p className="small muted">{[selected.field, selected.row_key].filter(Boolean).join(' · ')}</p>}
        </div>
        <div className="evidence-nav">
          <button type="button" className="return-to-list" onClick={() => (listRef.current?.querySelector<HTMLButtonElement>('[aria-pressed="true"]') ?? document.getElementById('diff-search'))?.focus()}>返回列表</button>
          {location ? <button onClick={() => { setLocation(null); focusEvidence(); }}>返回上次差异</button> : <>
            <button aria-disabled={busy || absoluteIndex <= 0} onClick={() => { if (!busy && absoluteIndex > 0) move(-1); }}>上一条</button>
            <span role="status"><span className="sr-only">当前筛选中的记录位置 </span>{absoluteIndex >= 0 && page ? `${absoluteIndex + 1} / ${page.total}` : '—'}</span>
            <button aria-disabled={busy || absoluteIndex < 0 || !page || absoluteIndex >= page.total - 1} onClick={() => { if (!busy && absoluteIndex >= 0 && page && absoluteIndex < page.total - 1) move(1); }}>下一条</button>
          </>}
        </div>
      </div>
      <section className="selected-diff-summary" aria-label="当前差异说明">
      {location ? <p className="small">{location.message}</p> : selected ? <>
        <div className="selected-diff-values">{(['old', 'new'] as const).map(side => <div key={side}>
          <span className={`evidence-side ${side}`}>{side === 'old' ? '旧版' : '新版'}</span>
          <div key={selected.id} className="evidence-value" tabIndex={0} role="region" aria-label={side === 'old' ? '旧版取值' : '新版取值'}><DiffText diff={selected} side={side} fallback={displayValue(selected, side)} /></div>
        </div>)}</div>
        <div className="evidence-facts"><strong className={selected.severity === 'issue' ? 'warning-text' : 'muted'}>{selected.severity === 'issue' ? '待核对 · 尚不能确认变化' : '已检测 · 尚未人工复核'}</strong>
          {selected.delta !== null && <span>差额（新版 − 旧版） {selected.delta}{isOcrDiff(selected) && '（识别值，待核验）'}</span>}
        </div>
        <p className="small evidence-reason">{selected.message}</p>
        {['text_whitespace_changed', 'line_break_changed'].includes(selected.kind) && <p className="small muted">␠ 空格 · □ 全角空格 · ⇥ 制表符 · ↵ 换行</p>}
      </> : <p className="small muted">{busy ? '正在读取差异记录。' : '可通过页面对应或手动选页查看原件。'}</p>}
    </section>
    <PdfComparisonViewer job={snapshotJob} report={evidenceReport} oldSource={oldSource} newSource={newSource} selectionId={sourceSelection} />
    </div> : <div className="viewers"><Viewer job={snapshotJob} side="old" source={oldSource} selectionId={sourceSelection} isIssue={!!location || selected?.severity === 'issue'} /><Viewer job={snapshotJob} side="new" source={newSource} selectionId={sourceSelection} isIssue={!!location || selected?.severity === 'issue'} /></div>}
    {!fullReportMode && <section className="diff-detail" aria-live="polite"><div className="section-heading"><h2>{location ? location.title : selected ? `${selected.row_key || selected.table_name}${selected.field ? ` / ${selected.field}` : ''}` : '差异详情'}</h2><div className="actions"><button disabled={busy || absoluteIndex <= 0} onClick={() => move(-1)}>← 上一条</button><button disabled={busy || absoluteIndex < 0 || !page || absoluteIndex >= page.total - 1} onClick={() => move(1)}>下一条 →</button></div></div>
      {location ? <><p className="explanation">{location.message}</p><div className="source-addresses"><span>旧版：{sourceLabel(location.old_source, true)}</span><span>新版：{sourceLabel(location.new_source, true)}</span></div><div className="actions"><button disabled={actionBusy} onClick={onConfigure}>确认配对与规则</button><button disabled={actionBusy} onClick={onReview}>校正与核验 OCR</button></div></> : selected ? <><div className="detail-grid"><div><span className="label">旧值</span><div className="value"><DiffText diff={selected} side="old" fallback={displayValue(selected, 'old')} /></div></div><div><span className="label">新值</span><div className="value"><DiffText diff={selected} side="new" fallback={displayValue(selected, 'new')} /></div></div><div><span className="label">差额（新版 − 旧版）</span><div className="value">{isOcrDiff(selected) ? selected.delta !== null ? `${selected.delta}（识别值差额，待核验）` : '待核验' : selected.delta ?? '不适用'}</div></div></div><span className={`chip ${selected.severity === 'issue' ? 'warn' : ''}`}>{selected.severity === 'issue' ? '◇ 待核对' : '△ 已检测，尚未人工复核'}</span><p className="explanation">{selected.message}</p><div className="source-addresses"><span>旧版：{sourceLabel(selected.old_source, pdf)}</span><span>新版：{sourceLabel(selected.new_source, pdf)}</span></div></> : <p className="muted">{busy ? '读取后自动定位第一条差异。' : '选择一条差异查看原始位置与判定依据。'}</p>}
    </section>}</div>
    {pdf && <PdfIssueGroups run={run} busy={actionBusy} onLocate={locate} onConfigure={onConfigure} onReview={onReview} onReprocess={onReprocess} onShowIssues={id => {
      if (fullReportMode) {
        setQuery(''); setSearch('');
        changeFilters('uncertainty', id ?? '', 'all', 'all', '');
        requestAnimationFrame(() => document.getElementById('current-report')?.focus());
      } else filterLayer(hasReportInventory ? 'uncertainty' : 'all', id);
    }} collapsed={fullReportMode} />}
  </section>;
}
