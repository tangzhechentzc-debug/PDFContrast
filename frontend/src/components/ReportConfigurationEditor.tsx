import { useState } from 'react';
import type { FormEvent } from 'react';
import type { CompareConfig, Job, PairConfig, Side } from '../types';

export default function ReportConfigurationEditor({ job, initialConfig, busy, onCompare, onCancel, onReprocess }: { job: Job; initialConfig?: CompareConfig; busy: boolean; onCompare: (config: CompareConfig) => void; onCancel?: () => void; onReprocess: () => void }) {
  const [config, setConfig] = useState<CompareConfig>(() => ({ ...(initialConfig ?? { pairs: job.suggested_pairs, absolute_tolerance: '0', ignore_outer_whitespace: true, compare_formulas: false, compare_formatting: true }), compare_formulas: false, compare_formatting: true, ignore_outer_whitespace: false, absolute_tolerance: '0' }));
  const [error, setError] = useState('');
  function update(index: number, patch: Partial<PairConfig>) { setConfig(value => ({ ...value, pairs: value.pairs.map((pair, i) => i === index ? { ...pair, ...patch } : pair) })); }
  function add() {
    const pair: PairConfig = { id: crypto.randomUUID(), old_report_id: null, new_report_id: null, old_sheet: null, new_sheet: null, old_header_start: 1, old_header_end: 1, new_header_start: 1, new_header_end: 1, old_range: null, new_range: null, mode: 'key', columns: [], pairing_confirmed: false, confirmation_reason: '' };
    setConfig(value => ({ ...value, pairs: [...value.pairs, pair] }));
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    const used: Record<Side, Set<string>> = { old: new Set(), new: new Set() };
    for (const [index, pair] of config.pairs.entries()) {
      if (!pair.old_report_id && !pair.new_report_id) { setError(`配对 ${index + 1} 至少选择一侧报表。`); return; }
      for (const side of ['old', 'new'] as const) {
        const id = pair[`${side}_report_id`];
        if (id && !job.files[side].reports?.some(report => report.id === id)) { setError(`配对 ${index + 1} 的${side === 'old' ? '旧版' : '新版'}报表已不在当前清单中，请重新选择。`); return; }
        if (id && used[side].has(id)) { setError(`配对 ${index + 1} 重复选择同一${side === 'old' ? '旧版' : '新版'}报表，请核对重复类型的实例。`); return; }
        if (id) used[side].add(id);
      }
      if (pair.pairing_confirmed && (!pair.old_report_id || !pair.new_report_id || !pair.confirmation_reason?.trim())) { setError(`配对 ${index + 1} 的人工确认需要两侧报表和确认依据。`); return; }
    }
    setError('');
    onCompare({ ...config, review_revision: job.review_revision ?? 0, absolute_tolerance: '0', ignore_outer_whitespace: false, compare_formatting: true, compare_formulas: false, pairs: config.pairs.map(pair => ({ ...pair, old_sheet: null, new_sheet: null, columns: [], confirmation_reason: pair.confirmation_reason?.trim() ?? '' })) });
  }
  return <section className="configuration" aria-labelledby="config-heading"><div className="section-heading"><div><h2 id="config-heading">确认完整报表的对应关系</h2><p className="muted">逐张比较内容、结构、格式与版式。模板可以与填写后的对应报表配对，工程名和填写内容的不同将在结果中保留。</p></div><button disabled={busy} onClick={onReprocess}>重新处理原件（新任务）</button></div>
    <div className="file-summary">{(['old', 'new'] as const).map(side => <div key={side}><span className="label">{side === 'old' ? '旧版' : '新版'}</span><strong>{job.files[side].name}</strong><span className="small muted">{job.files[side].reports?.length ?? 0} 张报表候选 · {job.files[side].pages?.length ?? 0} 页</span><a href={`/api/jobs/${job.id}/files/${side}`} download>下载完整原件</a></div>)}</div>
    <div className="notice"><p>建议配对 {job.suggested_pairs.length} 组。相同类型出现多次时，请按原文确认对应报表；不按页号直接配对。人工确认配对不等于确认 OCR 内容。</p><p>完整报表比较无需预先配置字段或业务键；各维度的可比范围、待核验内容和未完成项目会分别显示。</p></div>
    {job.warnings.length > 0 && <details className="notice"><summary>读取提示 · {job.warnings.length} 条（保留全部原始记录）</summary><ul>{job.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul></details>}
    <form onSubmit={submit}><fieldset className="plain-fieldset" disabled={busy}>
      {config.pairs.map((pair, index) => <details className="pair-editor full-report-pair" key={pair.id} open={index === 0}><summary>报表配对 {index + 1} · {job.files.old.reports?.find(report => report.id === pair.old_report_id)?.title ?? '无旧版选择'} → {job.files.new.reports?.find(report => report.id === pair.new_report_id)?.title ?? '无新版选择'}</summary><div className="pair-body"><div className="pair-sides">{(['old', 'new'] as const).map(side => { const selected = job.files[side].reports?.find(report => report.id === pair[`${side}_report_id`]); return <div className="pair-side" key={side}><label>{side === 'old' ? '旧版' : '新版'}完整报表<select aria-label={`配对 ${index + 1} ${side === 'old' ? '旧版' : '新版'}报表`} value={pair[`${side}_report_id`] ?? ''} onChange={event => update(index, { [`${side}_report_id`]: event.target.value || null, pairing_confirmed: false, confirmation_reason: '' })}><option value="">暂不选择对应报表</option>{job.files[side].reports?.map(report => <option value={report.id} key={report.id}>{report.type_code || '类型待确认'} · {report.title} · {report.object || (report.object_status === 'template' ? '模板' : '对象未填写')} · 第 {report.pages.join('、')} 页</option>)}</select></label>{selected && <p className="small muted">{selected.title} · {selected.object || (selected.object_status === 'template' ? '模板' : '对象未填写或未识别')} · 第 {selected.pages.join('、')} 页</p>}</div>; })}</div><label className="check-label report-confirmation"><input type="checkbox" checked={!!pair.pairing_confirmed} disabled={!pair.old_report_id || !pair.new_report_id} onChange={event => update(index, { pairing_confirmed: event.target.checked, ...(!event.target.checked ? { confirmation_reason: '' } : {}) })} />已对照原文确认这是对应报表，允许模板与填写内容不同</label>{pair.pairing_confirmed && <label className="pairing-reason">配对确认依据（必填）<input aria-label={`配对 ${index + 1} 确认依据`} value={pair.confirmation_reason ?? ''} onChange={event => update(index, { confirmation_reason: event.target.value })} placeholder="例如表号、标题与上下文；说明重复报表如何对应" /></label>}<div className="toolbar compact"><p className="small muted">手工暂不选择对侧时，该侧保留待确认，不再自动配回，也不直接判为缺失。</p><button type="button" onClick={() => setConfig(value => ({ ...value, pairs: value.pairs.filter((_, i) => i !== index) }))}>移除手工覆盖，恢复自动对应</button></div></div></details>)}
      <button type="button" onClick={add}>添加完整报表配对</button>
      {!config.pairs.length && <p className="notice">未指定手工配对，将按编号或标题自动对应所有唯一候选；歧义保留待确认。</p>}
      <div className="comparison-options"><h3>完整原文比较</h3><p className="small muted">内容、结构、格式与版式固定同时比较，保留空白、数字写法和样式差异；可靠表格中的业务键和数值差额作为补充依据。未能验证的属性单独说明。</p></div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      <div className="toolbar"><div className="actions"><button type="submit" className="primary">{busy ? '正在比较…' : '开始完整报表比较'}</button>{onCancel && <button type="button" onClick={onCancel}>返回当前结果</button>}</div><span className="small muted">修订 {job.review_revision ?? 0} · 内容 / 结构 / 格式 / 版式</span></div>
    </fieldset></form>
  </section>;
}
