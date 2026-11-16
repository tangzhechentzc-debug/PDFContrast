import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import ReportConfigurationEditor from './ReportConfigurationEditor';
import { messageOf, request } from '../api';
import type { BusinessReport, Column, ColumnMapping, ColumnsResponse, CompareConfig, Job, PairConfig, Side } from '../types';

interface Props {
  job: Job;
  initialConfig?: CompareConfig;
  busy: boolean;
  onCompare: (config: CompareConfig) => void;
  onCancel?: () => void;
  onReprocess: () => void;
}

function autoMap(oldColumns: Column[], newColumns: Column[]): ColumnMapping[] {
  const used = new Set<string>();
  const columns: ColumnMapping[] = oldColumns.map((column, index) => {
    const matching = newColumns.find(c => !used.has(c.column) && c.label.trim() === column.label.trim());
    if (matching) used.add(matching.column);
    return { id: `column-${index}-${column.column}`, name: column.label, old_column: column.column, new_column: matching?.column ?? null, is_key: false };
  });
  newColumns.filter(c => !used.has(c.column)).forEach(c => columns.push({ id: `new-${c.column}`, name: c.label, old_column: null, new_column: c.column, is_key: false }));
  const key = columns.find(c => c.old_column && c.new_column && /编码|编号|代码|(^|\/)id$|code$/i.test(c.name)) ?? columns.find(c => c.old_column && c.new_column);
  if (key) key.is_key = true;
  return columns;
}

function newPair(job: Job): PairConfig {
  const oldSheet = job.files.old.sheets[0];
  const newSheet = job.files.new.sheets[0];
  return {
    id: crypto.randomUUID(), old_sheet: oldSheet?.name ?? null, new_sheet: newSheet?.name ?? null,
    old_header_start: oldSheet?.suggested_header_row || 1, old_header_end: oldSheet?.suggested_header_row || 1,
    new_header_start: newSheet?.suggested_header_row || 1, new_header_end: newSheet?.suggested_header_row || 1,
    old_range: null, new_range: null, mode: 'key', columns: [],
  };
}

function reportForSheet(job: Job, side: Side, name: string | null): BusinessReport | undefined {
  if (!name) return undefined;
  const members = job.files[side].sheets.find(sheet => sheet.name === name)?.member_sheets ?? [name];
  const candidates = job.files[side].reports?.filter(report => report.member_sheets.includes(name) || members.some(member => report.member_sheets.includes(member))) ?? [];
  return candidates.length === 1 ? candidates[0] : undefined;
}

function pairingConflict(left?: BusinessReport, right?: BusinessReport): string | null {
  if (!left || !right) return null;
  if (left.type_code && right.type_code && left.type_code !== right.type_code) return '两侧报表类型不同，不能确认成同一报表实例。';
  if (left.object_status === 'known' && right.object_status === 'known' && left.object && right.object && left.object !== right.object) return '两侧已识别的业务对象不同，不能确认成同一报表实例。';
  if ((left.object_status === 'template') !== (right.object_status === 'template')) return '模板与非模板对象不能强制确认成同一业务实例，请分别保留为待确认范围。';
  return null;
}

function PairEditor({ job, pair, index, onChange, onRemove, onDuplicate }: { job: Job; pair: PairConfig; index: number; onChange: (pair: PairConfig) => void; onRemove: () => void; onDuplicate: () => void }) {
  const pdf = job.kind === 'pdf';
  const sheetNoun = pdf ? '提取表 / 区域' : '工作表';
  const reports = { old: reportForSheet(job, 'old', pair.old_sheet), new: reportForSheet(job, 'new', pair.new_sheet) };
  const conflict = pdf ? pairingConflict(reports.old, reports.new) : null;
  const [expanded, setExpanded] = useState(index === 0);
  const [columns, setColumns] = useState<Record<Side, Column[]>>({ old: [], new: [] });
  const [previews, setPreviews] = useState<Record<Side, ColumnsResponse['preview']>>({ old: [], new: [] });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);

  useEffect(() => {
    if (!expanded) { setLoading(false); return; }
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setLoading(true); setError('');
      try {
        const responses = await Promise.all((['old', 'new'] as const).map(async side => {
          const sheet = pair[`${side}_sheet`];
          if (!sheet) return { columns: [], preview: [] } satisfies ColumnsResponse;
          const params = new URLSearchParams({ side, sheet, header_start: String(pair[`${side}_header_start`]), header_end: String(pair[`${side}_header_end`]) });
          const range = pair[`${side}_range`];
          if (range) params.set('range', range);
          if (job.review_revision != null) params.set('review_revision', String(job.review_revision));
          return request<ColumnsResponse>(`/api/jobs/${job.id}/columns?${params}`, { signal: controller.signal });
        }));
        if (controller.signal.aborted) return;
        setColumns({ old: responses[0].columns, new: responses[1].columns });
        setPreviews({ old: responses[0].preview, new: responses[1].preview });
        if (!pair.columns.length || reload > 0) onChange({ ...pair, columns: autoMap(responses[0].columns, responses[1].columns) });
      } catch (e) { if (!controller.signal.aborted) setError(messageOf(e)); }
      finally { if (!controller.signal.aborted) setLoading(false); }
    }, 250);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [expanded, job.id, job.review_revision, pair.old_sheet, pair.new_sheet, pair.old_header_start, pair.old_header_end, pair.new_header_start, pair.new_header_end, pair.old_range, pair.new_range, reload]);

  function updateSide(side: Side, field: 'sheet' | 'header_start' | 'header_end' | 'range', value: string | number | null) {
    let next: PairConfig = { ...pair, [`${side}_${field}`]: value, columns: [] };
    if (field === 'sheet') {
      const sheet = job.files[side].sheets.find(s => s.name === value);
      next = { ...next, ...(pdf ? { pairing_confirmed: false, confirmation_reason: '' } : {}), [`${side}_header_start`]: sheet?.suggested_header_row || 1, [`${side}_header_end`]: sheet?.suggested_header_row || 1 };
    }
    onChange(next);
  }

  function updateColumn(index: number, patch: Partial<ColumnMapping>) {
    const next = pair.columns.map((c, i) => i === index ? { ...c, ...patch } : c);
    next.forEach(c => { if (!c.old_column || !c.new_column) c.is_key = false; });
    onChange({ ...pair, columns: next });
  }

  return <details className="pair-editor" open={expanded} onToggle={event => setExpanded(event.currentTarget.open)}>
    <summary><span>比较范围 {index + 1}</span><span className="muted">{pair.old_sheet || '无旧版'} → {pair.new_sheet || '无新版'}</span></summary>
    <div className="pair-body">
      <div className="pair-sides">{(['old', 'new'] as const).map(side => <div key={side} className="pair-side">
        <label>{side === 'old' ? '旧版' : '新版'}{sheetNoun}<select value={pair[`${side}_sheet`] ?? ''} onChange={e => updateSide(side, 'sheet', e.target.value || null)}><option value="">本版本无此{sheetNoun}</option>{job.files[side].sheets.map(s => <option key={s.name} value={s.name}>{s.name}{s.hidden ? '（隐藏）' : ''}{s.candidate_merge ? '（跨页拼接候选）' : ''}</option>)}</select></label>
        <div className="range-inputs"><label>{pdf ? '抽取表头起始行' : '表头起始行'}<input type="number" min="1" max="1048576" value={pair[`${side}_header_start`]} disabled={!pair[`${side}_sheet`]} onChange={e => updateSide(side, 'header_start', Number(e.target.value) || 1)} /></label><label>{pdf ? '抽取表头结束行' : '表头结束行'}<input type="number" min="1" max="1048576" value={pair[`${side}_header_end`]} disabled={!pair[`${side}_sheet`]} onChange={e => updateSide(side, 'header_end', Number(e.target.value) || 1)} /></label></div>
        <label>{pdf ? '抽取表格范围（留空为整表）' : '区域（留空为整表）'}<input placeholder="例如 A1:F100" value={pair[`${side}_range`] ?? ''} disabled={!pair[`${side}_sheet`]} onChange={e => updateSide(side, 'range', e.target.value.toUpperCase() || null)} /></label>
      </div>)}</div>
      {pdf && <section className="pairing-identity" aria-label={`比较范围 ${index + 1} 的报表身份`}><div className="pair-sides">{(['old', 'new'] as const).map(side => <div key={side}><h3>{side === 'old' ? '旧版' : '新版'}报表身份</h3>{reports[side] ? <><p>{reports[side]!.type_code || '类型未确认'} · {reports[side]!.title}</p><p>业务对象：{reports[side]!.object || '未填写或未识别'}{reports[side]!.object_status === 'template' ? '（模板）' : reports[side]!.object_status === 'known' ? '（已识别）' : '（待确认）'}</p><small>第 {reports[side]!.pages.join('、')} 页</small></> : <p className="muted">{pair[`${side}_sheet`] ? '此区域尚未明确归属单一业务报表实例。' : '没有选择对应区域；不等于已经证实该业务报表缺失。'}</p>}</div>)}</div>{conflict && <p className="notice">{conflict}</p>}{pair.old_sheet && pair.new_sheet && <><label className="check-label"><input type="checkbox" checked={!!pair.pairing_confirmed} disabled={loading || (!!conflict && !pair.pairing_confirmed)} onChange={e => onChange({ ...pair, pairing_confirmed: e.target.checked, ...(!e.target.checked ? { confirmation_reason: '' } : {}) })} />我已对照原文确认两侧属于同一报表类型、同一业务对象</label>{pair.pairing_confirmed && <label className="pairing-reason">配对确认依据（必填）<input disabled={loading || !!conflict} value={pair.confirmation_reason ?? ''} onChange={e => onChange({ ...pair, confirmation_reason: e.target.value })} placeholder="说明原文中的对象、标题或编号依据" /></label>}<p className="small muted">未勾选时，身份未知的配对保留待确认。该确认不代表 OCR 内容已核验，也不能覆盖已知类型或对象冲突。</p></>}</section>}
      {pdf && <p className="muted small">以上行列号属于提取结果，不是 PDF 原页面坐标。请展开数据预览确认表头与字段；原文将在比较结果中按页定位。</p>}
      <div className="toolbar compact"><label className="inline-label">项目匹配方式<select value={pair.mode} onChange={e => onChange({ ...pair, mode: e.target.value as PairConfig['mode'] })}><option value="key">按业务键匹配（推荐）</option><option value="position" disabled={pdf && job.report_inventory_version === 1}>{pdf && job.report_inventory_version === 1 ? '按固定行位置（仅旧版配置兼容）' : '按固定行位置比较'}</option></select></label><button type="button" onClick={() => setReload(n => n + 1)} disabled={loading}>按表头重建映射</button></div>
      {pdf && job.report_inventory_version === 1 && pair.mode === 'position' && <p className="notice">当前继承了按位置匹配的配置。新版业务报表比较须切换为业务键，并勾选可确认的唯一标识或字段组合；不能按行位置推定对应关系。</p>}
      <p className="muted small">{pair.mode === 'key' ? '勾选可唯一识别项目的字段，例如项目编码；可组合多个字段。重复或空键会列为待确认。' : '按数据区域中的行位置匹配。插行或调序可能产生多条变化。'}</p>
      {loading && <p role="status" className="status-line">读取表头与数据预览…</p>}
      {error && <div className="error-banner" role="alert">{error}</div>}
      <div className="table-scroll mapping-table"><table><thead><tr><th>业务键</th><th>字段名称</th><th>旧版列</th><th>新版列</th><th><span className="sr-only">操作</span></th></tr></thead><tbody>{pair.columns.map((column, i) => <tr key={column.id}>
        <td><input type="checkbox" aria-label={`${column.name} 作为业务键`} checked={column.is_key} disabled={pair.mode !== 'key' || !column.old_column || !column.new_column} onChange={e => updateColumn(i, { is_key: e.target.checked })} /></td>
        <td><input aria-label={`字段 ${i + 1} 名称`} value={column.name} onChange={e => updateColumn(i, { name: e.target.value })} /></td>
        {(['old', 'new'] as const).map(side => <td key={side}><select aria-label={`${column.name} ${side === 'old' ? '旧版' : '新版'}列`} value={column[`${side}_column`] ?? ''} onChange={e => updateColumn(i, { [`${side}_column`]: e.target.value || null })}><option value="">无对应列</option>{columns[side].map(c => <option value={c.column} key={c.column}>{c.column} · {c.label}</option>)}</select></td>)}
        <td><button type="button" className="quiet-button" aria-label={`移除字段映射 ${column.name}`} onClick={() => onChange({ ...pair, columns: pair.columns.filter((_, n) => n !== i) })}>移除</button></td>
      </tr>)}</tbody></table></div>
      {!loading && !error && !pair.columns.length && <p className="muted small">此范围没有可用表头。请调整表头行和区域。</p>}
      <div className="toolbar compact"><button type="button" onClick={() => onChange({ ...pair, columns: [...pair.columns, { id: crypto.randomUUID(), name: '新字段', old_column: null, new_column: null, is_key: false }] })}>添加字段映射</button><div className="actions"><button type="button" onClick={onDuplicate}>复制此范围配置</button><button type="button" className="quiet-button danger" onClick={onRemove}>移除此比较范围</button></div></div>
      <details className="data-preview"><summary>查看表头后的实际数据</summary><div className="pair-sides">{(['old', 'new'] as const).map(side => <div key={side}><h3>{side === 'old' ? '旧版' : '新版'}数据预览</h3><div className="table-scroll"><table><thead><tr><th>行号</th>{columns[side].map(c => <th key={c.column}>{c.column} · {c.label}</th>)}</tr></thead><tbody>{previews[side].map(row => <tr key={row.row}><th>{row.row}</th>{columns[side].map(c => <td key={c.column}>{row.values[c.column] ?? ''}</td>)}</tr>)}</tbody></table></div></div>)}</div></details>
    </div>
  </details>;
}

export default function ConfigurationEditor(props: Props) {
  return props.job.kind === 'pdf' && props.job.report_inventory_version === 2 ? <ReportConfigurationEditor {...props} /> : <LegacyConfigurationEditor {...props} />;
}

function LegacyConfigurationEditor({ job, initialConfig, busy, onCompare, onCancel, onReprocess }: Props) {
  const pdf = job.kind === 'pdf';
  const [config, setConfig] = useState<CompareConfig>(() => ({ ...(initialConfig ?? { pairs: job.suggested_pairs, absolute_tolerance: '0', ignore_outer_whitespace: true, compare_formulas: true, compare_formatting: false }), ...(pdf ? { compare_formulas: false, compare_formatting: false } : {}) }));
  const [error, setError] = useState('');
  const inventoryReady = pdf && job.report_inventory_version === 1;
  const pairedSuggestions = job.suggested_pairs.filter(pair => pair.old_sheet && pair.new_sheet).length;

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!config.pairs.length && !pdf) { setError('至少保留一个比较范围。'); return; }
    for (const [index, pair] of config.pairs.entries()) {
      if (inventoryReady && pair.old_sheet && pair.new_sheet && pair.mode === 'position') { setError(`比较范围 ${index + 1} 请切换为业务键匹配，并确认唯一标识或字段组合；也可移除此范围，先生成完整性结果。`); return; }
      if (pdf && pair.pairing_confirmed) {
        const conflict = pairingConflict(reportForSheet(job, 'old', pair.old_sheet), reportForSheet(job, 'new', pair.new_sheet));
        if (conflict) { setError(`比较范围 ${index + 1}：${conflict}`); return; }
        if (!pair.old_sheet || !pair.new_sheet || !pair.confirmation_reason?.trim()) { setError(`比较范围 ${index + 1} 的人工配对确认需选择两侧区域并填写依据。`); return; }
      }
      if (!pair.old_sheet && !pair.new_sheet) { setError(`比较范围 ${index + 1} 至少选择一个${pdf ? '提取表 / 区域' : '工作表'}。`); return; }
      if (pair.old_header_start > pair.old_header_end || pair.new_header_start > pair.new_header_end) { setError(`比较范围 ${index + 1} 的表头起始行不能大于结束行。`); return; }
      if (pair.old_sheet && pair.new_sheet && !pair.columns.length) { setError(`比较范围 ${index + 1} 尚未获得字段映射，请等待表头读取或调整范围。`); return; }
      if (pair.old_sheet && pair.new_sheet && pair.mode === 'key' && !pair.columns.some(c => c.is_key && c.old_column && c.new_column)) { setError(`请为比较范围 ${index + 1} 勾选至少一个业务键，或选择按固定行位置比较。`); return; }
    }
    if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(config.absolute_tolerance.trim())) { setError('绝对容差须为非负十进制数，例如 0 或 0.01。'); return; }
    setError(''); onCompare({ ...config, ...(pdf ? { review_revision: job.review_revision ?? 0 } : {}), absolute_tolerance: config.absolute_tolerance.trim(), ...(pdf ? { compare_formulas: false, compare_formatting: false } : {}) });
  }

  return <section className="configuration" aria-labelledby="config-heading"><div className="section-heading"><div><h2 id="config-heading">确认报表范围与匹配字段</h2><p className="muted">{pdf ? '按提取表或文字区域配对；原文位置保留在各自 PDF 页面中。' : '按项目对应比较；两份表中的行号可以不同。'}</p></div><div className="actions"><a href={`/api/jobs/${job.id}/files/old`} download>旧版原件</a><a href={`/api/jobs/${job.id}/files/new`} download>新版原件</a></div></div>
    {pdf && !inventoryReady && <div className="notice"><strong>当前任务使用旧版提取数据，尚无业务报表清单</strong><p>建议先重新处理两份原件，再核对报表类型与业务实例。将创建独立后台任务，原件、原校正和历史结果保留。</p><button disabled={busy} onClick={onReprocess}>{busy ? '正在处理…' : '重新处理原件（新任务）'}</button></div>}
    {inventoryReady && <div className="configuration-inventory"><h3>当前识别出的报表候选</h3><div className="file-summary">{(['old', 'new'] as const).map(side => { const reports = job.files[side].reports ?? []; const typeCount = new Set(reports.map(report => report.type_code).filter(Boolean)).size; const pendingCount = reports.filter(report => !report.type_code || report.object_status !== 'known').length; return <div key={side}><strong>{side === 'old' ? '旧版' : '新版'}</strong><span>{typeCount} 种已识别类型 · {reports.length} 个实例候选</span><span className="warning-text small">{pendingCount} 个候选的业务身份待确认</span></div>; })}</div><p>可靠配对建议 {pairedSuggestions} 组；{job.warnings.length} 条读取提示尚需检查。</p><p className="small muted">以上为提取与配对候选，最终完整性状态以比较结果为准。相同类型、相同表头或空白表体均不代表同一业务实例或内容相同。</p></div>}
    <div className="file-summary"><div><span className="label">旧版</span><strong>{job.files.old.name}</strong><span className="muted small">{pdf ? `${job.files.old.pages?.length ?? 0} 页 · ${job.files.old.sheets.length} 个提取表 / 区域` : `${job.files.old.sheets.length} 个工作表`}</span></div><div><span className="label">新版</span><strong>{job.files.new.name}</strong><span className="muted small">{pdf ? `${job.files.new.pages?.length ?? 0} 页 · ${job.files.new.sheets.length} 个提取表 / 区域` : `${job.files.new.sheets.length} 个工作表`}</span></div></div>
    {job.warnings.length > 0 && <details className="notice"><summary>读取提示尚需检查 · {job.warnings.length} 条（展开保留的原始记录）</summary><ul>{job.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></details>}
    {pdf && <div className="notice"><strong>请确认提取与配对结果{job.review_revision != null ? ` · 修订 ${job.review_revision}` : ''}</strong><p>请分别确认报表类型和业务对象；相同表头不代表同一实例。模板或未填写的对象不得直接当作具体工程配对。点击“开始比较”表示使用当前候选和映射，不代表扫描识别内容已经核验。</p></div>}
    {pdf && !config.pairs.length && <div className="notice" role="status"><strong>{inventoryReady ? '尚无可靠实例配对，可先生成报表完整性结果。' : job.files.old.sheets.length + job.files.new.sheets.length === 0 ? '未提取出可配置表格，可查看原页与提取问题。' : '当前未配置比较范围，可查看原页与提取问题。'}</strong><p>{inventoryReady ? '本次将盘点类型覆盖、实例完整性与待处理区域；未配对内容不会强行比较，也不会据此判为一致。' : '此状态不能判定文件一致；未提取或未比较的内容将在结果中明确列出。'}</p></div>}
    <form onSubmit={submit}><fieldset disabled={busy} className="plain-fieldset">
      {config.pairs.map((pair, index) => <PairEditor key={pair.id} job={job} pair={pair} index={index} onChange={next => setConfig(c => ({ ...c, pairs: c.pairs.map(p => p.id === next.id ? next : p) }))} onRemove={() => setConfig(c => ({ ...c, pairs: c.pairs.filter(p => p.id !== pair.id) }))} onDuplicate={() => setConfig(c => ({ ...c, pairs: [...c.pairs, { ...pair, id: crypto.randomUUID(), columns: pair.columns.map(column => ({ ...column, id: crypto.randomUUID() })) }] }))} />)}
      <button type="button" className="add-range" onClick={() => setConfig(c => ({ ...c, pairs: [...c.pairs, newPair(job)] }))}>{pdf ? '添加提取表或比较区域' : '添加工作表或比较区域'}</button>
      <section className="comparison-options"><h3>比较规则</h3><div className="options-row"><label>数值绝对容差<input value={config.absolute_tolerance} inputMode="decimal" onChange={e => setConfig(c => ({ ...c, absolute_tolerance: e.target.value }))} /></label><label className="check-label"><input type="checkbox" checked={config.ignore_outer_whitespace} onChange={e => setConfig(c => ({ ...c, ignore_outer_whitespace: e.target.checked }))} />忽略文本首尾空白</label>{!pdf && <><label className="check-label"><input type="checkbox" checked={config.compare_formulas} onChange={e => setConfig(c => ({ ...c, compare_formulas: e.target.checked }))} />比较公式</label><label className="check-label"><input type="checkbox" checked={config.compare_formatting} onChange={e => setConfig(c => ({ ...c, compare_formatting: e.target.checked }))} />比较常用格式</label></>}</div><p className="muted small">{pdf ? '默认精确比较，空白与零不同。扫描内容依据 OCR 识别结果比较，仍需核对原图。' : '默认精确比较，空白与零不同。公式只读取文件保存结果，不重新计算。'}</p></section>
      {error && <div className="error-banner" role="alert">{error}</div>}
      <div className="toolbar"><div className="actions"><button type="submit" className="primary">{busy ? '正在比较…' : pdf && !config.pairs.length ? inventoryReady ? '生成报表完整性结果' : '查看原页与提取问题' : '开始比较'}</button>{onCancel && <button type="button" onClick={onCancel}>返回当前结果</button>}</div><span className="muted small">{pdf ? '未比较区域和识别失败会在结果中明确列出。' : '未纳入的工作表会在结果中明确列出。'}</span></div>
    </fieldset></form>
    {busy && <p className="status-line" role="status">正在对齐项目并计算完整差异，完成后自动打开结果。</p>}
  </section>;
}
