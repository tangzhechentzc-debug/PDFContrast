import { useEffect, useRef, useState } from 'react';
import { ApiError, jsonRequest, messageOf, request } from './api';
import type { CompareConfig, ImportStatus, Job, Run } from './types';
import UploadPanel from './components/UploadPanel';
import ConfigurationEditor from './components/ConfigurationEditor';
import ComparisonWorkspace from './components/ComparisonWorkspace';
import ReviewWorkspace from './components/ReviewWorkspace';

function updateUrl(jobId?: string, runId?: string) {
  const url = new URL(window.location.href);
  url.searchParams.delete('import');
  url.searchParams.delete('report');
  if (jobId) url.searchParams.set('job', jobId); else url.searchParams.delete('job');
  if (runId) url.searchParams.set('run', runId); else url.searchParams.delete('run');
  window.history.replaceState(null, '', url);
}

export default function App() {
  const titleRef = useRef<HTMLHeadingElement>(null);
  const [importId, setImportId] = useState<string | null>(() => new URLSearchParams(window.location.search).get('import'));
  const [job, setJob] = useState<Job | null>(null);
  const [run, setRun] = useState<Run | null>(null);
  const [configure, setConfigure] = useState(false);
  const [reviewing, setReviewing] = useState(false);
  const [useSuggestions, setUseSuggestions] = useState(false);
  const [configRevision, setConfigRevision] = useState(0);
  const [staleConfig, setStaleConfig] = useState(false);
  const [restoring, setRestoring] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams(window.location.search);
    const jobId = params.get('job');
    const runId = params.get('run');
    async function restore() {
      try {
        if (params.get('import') || (!jobId && !runId)) return;
        const savedRun = runId ? await request<Run>(`/api/runs/${encodeURIComponent(runId)}`, { signal: controller.signal }) : null;
        const id = savedRun?.job_id || jobId;
        const savedJob = id ? await request<Job>(`/api/jobs/${encodeURIComponent(id)}`, { signal: controller.signal }) : null;
        if (!controller.signal.aborted) { setJob(savedJob); setRun(savedRun); setConfigure(!savedRun); }
      } catch (e) { if (!controller.signal.aborted) setError(`无法恢复上次比较：${messageOf(e)}`); }
      finally { if (!controller.signal.aborted) setRestoring(false); }
    }
    void restore();
    return () => controller.abort();
  }, []);

  function focusTitle() {
    requestAnimationFrame(() => titleRef.current?.focus());
  }

  function uploaded(value: Job) { setImportId(null); setUseSuggestions(false); setStaleConfig(false); setJob(value); setRun(null); setConfigure(true); setReviewing(false); setError(''); updateUrl(value.id); focusTitle(); }
  function startImport(id: string) {
    setImportId(id); setJob(null); setRun(null); setError('');
    const url = new URL(window.location.href); url.searchParams.delete('job'); url.searchParams.delete('run'); url.searchParams.delete('report'); url.searchParams.set('import', id); window.history.replaceState(null, '', url);
  }
  function reset() { setImportId(null); setJob(null); setRun(null); setConfigure(false); setReviewing(false); setError(''); updateUrl(); focusTitle(); }
  async function openConfiguration(fresh = false) {
    if (!job || busy) return;
    setBusy(true); setError('');
    try {
      const latest = job.kind === 'pdf' ? await request<Job>(`/api/jobs/${job.id}`) : job;
      setJob(latest); setUseSuggestions(fresh || (latest.structure_revision ?? 0) > (run?.config.review_revision ?? 0));
      setConfigRevision(n => n + 1); setConfigure(true); setReviewing(false); setStaleConfig(false); focusTitle();
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  }
  async function reprocess() {
    if (!job || busy) return;
    setBusy(true); setError('');
    try {
      const status = await request<ImportStatus>(`/api/jobs/${job.id}/reprocess`, { method: 'POST' });
      setConfigure(false); setReviewing(false); startImport(status.id);
    } catch (e) { setError(messageOf(e)); }
    finally { setBusy(false); }
  }
  async function compare(config: CompareConfig) {
    if (!job || busy) return;
    setBusy(true); setError(''); setStaleConfig(false);
    try {
      const result = await request<Run>(`/api/jobs/${job.id}/runs`, jsonRequest(config));
      setRun(result); setConfigure(false); setReviewing(false); updateUrl(job.id, result.id); focusTitle();
    } catch (e) { setError(messageOf(e)); if (e instanceof ApiError && e.status === 409 && job.kind === 'pdf') setStaleConfig(true); }
    finally { setBusy(false); }
  }

  function backToResult(actionId: string) {
    setConfigure(false); setReviewing(false);
    requestAnimationFrame(() => document.getElementById(actionId)?.focus());
  }

  const fullResult = job?.kind === 'pdf' && run?.report_inventory_version === 2 && !configure && !reviewing;
  return <main className={fullResult ? 'result-page' : undefined}>
    <header className={`app-header${fullResult ? ' compact-app-header' : ''}`}>
      <div className="topline"><strong>报表对比 / Report Contrast</strong><div className="actions"><span className="chip">{job ? job.kind === 'pdf' ? 'PDF · 本地版' : 'Excel · 本地版' : '本地版'}</span>{job && <button onClick={reset} disabled={busy || reviewing}>新建比较</button>}</div></div>
      <h1 ref={titleRef} tabIndex={-1}>{reviewing ? '校正与核验识别内容' : configure ? '配置比较' : run ? '比较结果' : '比较两份报表'}</h1>
    </header>
    {!fullResult && job?.parent_job_id && <p className="notice small">此任务由原件重新处理生成；<a href={`?job=${encodeURIComponent(job.parent_job_id)}`}>查看原任务</a>，其校正与历史结果保持独立。</p>}
    {error && <div className="error-banner app-error" role="alert"><span>{error}</span>{staleConfig && <button disabled={busy} onClick={() => void openConfiguration(true)}>重新加载结构与比较规则</button>}<button aria-label="关闭错误提示" onClick={() => setError('')}>关闭</button></div>}
    {restoring ? <div className="empty restore-state" role="status">正在恢复比较…</div> : !job ? <UploadPanel onUploaded={uploaded} importId={importId} onImportStarted={startImport} onImportCleared={reset} /> : configure || !run ? <ConfigurationEditor key={`${job.id}-${run?.id ?? 'new'}-${configRevision}`} job={job} initialConfig={useSuggestions ? undefined : run?.config} busy={busy} onCompare={compare} onReprocess={() => void reprocess()} onCancel={run ? () => backToResult("configure-result") : undefined} /> : reviewing ? <ReviewWorkspace key={job.id} job={job} run={run} comparing={busy} onBack={() => backToResult("review-result")} onCompare={() => void compare(run.config)} onConfigure={() => void openConfiguration(true)} /> : null}
    {job && run && <div hidden={configure || reviewing}>
      <ComparisonWorkspace key={run.id} job={job} run={run} onConfigure={() => void openConfiguration()} onReview={() => { setReviewing(true); focusTitle(); }} onReprocess={() => void reprocess()} actionBusy={busy} />
    </div>}
    <footer className="app-footer"><span>原文件只读 · 文件在本机处理</span><span>支持两份 .xlsx 或两份 PDF · 扫描内容在本机识别并待核验。</span></footer>
  </main>;
}
