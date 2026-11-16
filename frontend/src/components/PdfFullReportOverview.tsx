import { useState } from 'react';
import { dimensionLabels, dimensionStatusLabels } from '../api';
import type { ComparisonDimension, ReportComparison, ResultLocation, Run, Side, Source } from '../types';

const presenceLabels = { shared: '对应报表', old_only: '仅旧版', new_only: '仅新版', uncertain: '对应关系待确认' };
const regionLabels = { blank_page: '空白页', document_section: '文档正文', continuation_note: '续页说明 / 注释', unclassified_region: '未归类区域' };

export default function PdfFullReportOverview({ run, busy, onOpen, onLocate, onConfigure }: { run: Run; busy: boolean; onOpen: (id: string, category?: ComparisonDimension) => void; onLocate: (location: ResultLocation) => void; onConfigure: () => void }) {
  const [query, setQuery] = useState('');
  const reports = run.reports ?? [];
  const shown = reports.filter(report => [report.title, report.type_code, report.old?.object, report.new?.object].join(' ').toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const changed = reports.filter(report => report.comparison_status === 'changed' || report.change_count > 0 || Object.values(report.dimensions ?? {}).some(dimension => dimension.changes > 0 || dimension.status === 'changed')).length;
  const pending = reports.filter(report => report.confirmation === 'pending' || Object.values(report.dimensions ?? {}).some(dimension => ['partial', 'unverified'].includes(dimension.status))).length;
  function locate(report: ReportComparison, side: Side, page: number) {
    const pair = report.page_pairs?.find(item => item[`${side}_page`] === page);
    const source = (target: Side): Source | null => {
      const number = target === side ? page : pair?.[`${target}_page`];
      if (!number) return null;
      return { kind: 'pdf', page: number, location_precision: 'page' };
    };
    onLocate({ id: `${report.id}:${side}:${page}`, report_id: report.id, title: report.title, message: pair?.basis ?? '此页尚无明确的对侧对应页，仅定位已知原页。', old_source: source('old'), new_source: source('new') });
  }
  return <section className="full-report-overview" aria-label="完整报表四维概览">
    <div className="result-counts"><span>存在确定变化 <strong>{changed} 张</strong></span><span>待确认或部分可比 <strong>{pending} 张</strong></span></div>
    <div className="overview-actions">
      <label className="report-search">查找报表、类型或工程名<input aria-label="查找完整报表" type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="例如表号或报表标题" /></label>
      <button disabled={busy} onClick={onConfigure}>调整报表对应关系</button>
    </div>
    <p className="small muted">比较结果反映文件间的差异。业务计算与规范符合性需另行核对。</p>
    <ul className="report-directory" aria-label="报表目录">
      {shown.map(report => <li key={report.id}>
        <div className="directory-identity">
          <button onClick={() => onOpen(report.id)}>{report.type_code ? `${report.type_code} · ` : ''}{report.title}</button>
          <span className={`chip ${report.confirmation === 'pending' ? 'warn' : ''}`}>{presenceLabels[report.presence]}</span>
          {(['old', 'new'] as const).map(side => <small key={side}>{side === 'old' ? '旧版' : '新版'} {report[side] ? report[side]?.object || (report[side]?.object_status === 'template' ? '模板' : '对象未填写') : '暂无对应'}</small>)}
        </div>
        <div className="directory-dimensions">
          {(Object.keys(dimensionLabels) as ComparisonDimension[]).map(category => {
            const result = report.dimensions?.[category];
            return <button key={category} onClick={() => onOpen(report.id, category)} aria-label={`${report.title} ${dimensionLabels[category]}摘要`}>
              <strong>{dimensionLabels[category]}</strong> <span>{result ? dimensionStatusLabels[result.status] : '尚未计算'}</span>
              <small>{result ? `${result.changes} 项变化 · ${result.issues} 项待核对` : '无当次结果'}</small>
            </button>;
          })}
        </div>
        <details className="directory-basis">
          <summary>原页、配对与验证范围</summary>
          <p>{report.basis}</p>
          {!!report.blocking_reasons.length && <ul>{report.blocking_reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
          {(['old', 'new'] as const).map(side => <div className="page-links" key={side}>
            <span>{side === 'old' ? '旧版' : '新版'}</span>
            {report[side]?.pages.map(page => <button key={page} onClick={() => locate(report, side, page)}>第 {page} 页</button>) ?? <small>暂无对应页</small>}
          </div>)}
          {(Object.keys(dimensionLabels) as ComparisonDimension[]).map(category => {
            const result = report.dimensions?.[category];
            return <div className="dimension-explanation" key={category}>
              <strong>{dimensionLabels[category]}</strong>
              <p>{result?.summary || '该维度尚未计算。'}</p>
              {!!result?.limitations.length && <ul>{result.limitations.map((item, index) => <li key={index}>{item}</li>)}</ul>}
            </div>;
          })}
        </details>
      </li>)}
    </ul>
    {!shown.length && <p className="empty">没有符合条件的报表。<button onClick={() => setQuery('')}>清空报表搜索</button></p>}
    {run.type_inventory && <details className="coverage-details"><summary>类型覆盖与原始页范围</summary><div className="table-scroll"><table><thead><tr><th>类型范围</th><th>报表类型</th><th>旧版页</th><th>新版页</th></tr></thead><tbody>{(['shared', 'old_only', 'new_only', 'uncertain'] as const).flatMap(key => run.type_inventory![key].map((type, index) => <tr key={`${key}:${index}`}><td>{presenceLabels[key]}</td><td>{type.type_code} · {type.title}</td><td>{type.old_pages.join('、') || '未识别'}</td><td>{type.new_pages.join('、') || '未识别'}</td></tr>))}</tbody></table></div></details>}
    {run.non_report_regions && <details className="non-report-regions"><summary>非业务报表文档范围 · {run.non_report_regions.length} 项</summary><div className="table-scroll"><table><thead><tr><th>归类 / 页面</th><th>依据</th><th>原文</th></tr></thead><tbody>{run.non_report_regions.map(region => <tr key={`${region.side}:${region.id}`}><td>{regionLabels[region.type]} · {region.side === 'old' ? '旧版' : '新版'}第 {region.pages.join('、')} 页</td><td>{region.basis}</td><td><button onClick={() => onLocate({ id: region.id, title: regionLabels[region.type], message: region.basis, old_source: region.side === 'old' ? region.source : null, new_source: region.side === 'new' ? region.source : null })}>定位原文</button></td></tr>)}</tbody></table></div></details>}
  </section>;
}
