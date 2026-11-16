import { useEffect, useRef, useState } from 'react';
import type { FormEvent, DragEvent } from 'react';
import { messageOf, request } from '../api';
import type { ImportStatus, Job } from '../types';

interface Props { onUploaded: (job: Job) => void; importId: string | null; onImportStarted: (id: string) => void; onImportCleared: () => void }

const stageLabels: Record<ImportStatus['progress']['stage'], string> = {
  queued: '等待后台处理', opening: '打开文件', rendering: '生成原页图像', text_extract: '提取页面文字',
  ocr: '扫描文字识别（OCR）', table_extract: '提取表格结构', reading_sheet: '读取工作表',
  file_completed: '本份文件处理完成', finalizing: '整理报表与比较建议', completed: '导入完成', failed: '导入失败',
};

export default function UploadPanel({ onUploaded, importId, onImportStarted, onImportCleared }: Props) {
  const [oldFile, setOldFile] = useState<File | null>(null);
  const [newFile, setNewFile] = useState<File | null>(null);
  const [uploading, setUploading] = useState(false);
  const [importStatus, setImportStatus] = useState<ImportStatus | null>(null);
  const [pollError, setPollError] = useState('');
  const [retry, setRetry] = useState(0);
  const [checking, setChecking] = useState(false);
  const uploadController = useRef<AbortController | null>(null);
  const uploadedCallback = useRef(onUploaded);
  uploadedCallback.current = onUploaded;
  const busy = uploading || !!importId;

  useEffect(() => () => uploadController.current?.abort(), []);
  useEffect(() => {
    if (!importId) { setImportStatus(null); setPollError(''); return; }
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    setPollError(''); setChecking(true);
    async function poll() {
      try {
        const status = await request<ImportStatus>(`/api/imports/${encodeURIComponent(importId!)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setImportStatus(status);
        if (status.status === 'ready') {
          const job = await request<Job>(`/api/jobs/${encodeURIComponent(status.job_id ?? status.id)}`, { signal: controller.signal });
          if (!controller.signal.aborted) uploadedCallback.current(job);
          return;
        }
        setChecking(false);
        if (status.status !== 'failed') timer = setTimeout(() => void poll(), 750);
      } catch (e) {
        if (!controller.signal.aborted) { setPollError(messageOf(e)); setChecking(false); }
      }
    }
    void poll();
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [importId, retry]);
  const [error, setError] = useState('');
  const fileKind = (file: File | null) => file?.name.toLowerCase().endsWith('.pdf') ? 'pdf' : 'xlsx';
  const mixedTypes = !!oldFile && !!newFile && fileKind(oldFile) !== fileKind(newFile);
  const pdf = fileKind(oldFile) === 'pdf' || fileKind(newFile) === 'pdf';

  function choose(file: File | undefined, side: 'old' | 'new') {
    if (!file) return;
    if (!/\.(xlsx|pdf)$/i.test(file.name)) { setError('请选择 .xlsx 或 .pdf 文件；暂不接受 .xls 或其他格式。'); return; }
    if (file.size > 50 * 1024 * 1024) { setError('每份文件不能超过 50 MB。'); return; }
    setError('');
    (side === 'old' ? setOldFile : setNewFile)(file);
  }

  function drop(event: DragEvent, side: 'old' | 'new') {
    event.preventDefault();
    if (!busy) choose(event.dataTransfer.files[0], side);
  }

  async function upload(event: FormEvent) {
    event.preventDefault();
    if (!oldFile || !newFile || busy) return;
    if (mixedTypes) { setError('两份文件必须同为 Excel，或同为 PDF，不能混合比较。'); return; }
    setUploading(true); setError('');
    const controller = new AbortController();
    uploadController.current = controller;
    const form = new FormData();
    form.append('old_file', oldFile); form.append('new_file', newFile);
    try {
      const status = await request<ImportStatus>('/api/jobs?background=true', { method: 'POST', body: form, signal: controller.signal });
      if (!controller.signal.aborted) { setImportStatus(status); onImportStarted(status.id); }
    } catch (e) { if (!controller.signal.aborted) setError(messageOf(e)); }
    finally { if (!controller.signal.aborted) setUploading(false); };
  }

  if (importId) {
    const progress = importStatus?.progress;
    const unit = progress?.unit === 'page' ? '页' : progress?.unit === 'sheet' ? '个工作表' : null;
    const activeFile = progress?.side ? importStatus?.files[progress.side] : null;
    return <section className="import-section" aria-labelledby="import-heading">
      <div className="section-heading"><div><h2 id="import-heading">{importStatus?.status === 'failed' ? '报表导入未完成' : importStatus?.status === 'ready' ? '导入完成，正在打开配置' : '正在导入报表'}</h2><p className="muted">任务已保留，刷新页面可恢复进度。</p></div><span className="chip">本机后台处理</span></div>
      {importStatus && <div className="file-summary">{(['old', 'new'] as const).map(side => <div key={side}><span className="label">{side === 'old' ? '旧版' : '新版'}{progress?.side === side && importStatus.status === 'processing' ? ' · 当前处理' : ''}</span><strong>{importStatus.files[side].name}</strong></div>)}</div>}
      {progress ? <div className="import-progress" aria-live="polite"><div className="section-heading"><div><h3>{stageLabels[progress.stage] ?? progress.stage}</h3>{activeFile && <p>{progress.side === 'old' ? '旧版' : '新版'} · {activeFile.name}</p>}</div><span className="chip">{progress.file_index > 0 ? `第 ${progress.file_index} / ${progress.file_count} 份文件` : `共 ${progress.file_count} 份文件`}</span></div>
        {unit && <><p className="import-counts">{progress.current > 0 ? progress.unit === 'page' ? `当前第 ${progress.current} 页` : `当前第 ${progress.current} 个工作表` : '尚未开始处理页面 / 工作表'}{progress.total != null ? ` · 共 ${progress.total} ${unit}` : ' · 总数尚未确定'}</p>{progress.unit === 'sheet' && progress.item_name && <p className="muted">工作表：{progress.item_name}</p>}<p>已处理 {progress.completed}{progress.total != null ? ` / ${progress.total}` : ''} {unit}</p>{progress.total != null && progress.total > 0 && <progress aria-label={progress.unit === 'page' ? '当前文件已处理页数' : '当前文件已处理工作表数'} value={progress.completed} max={progress.total} />}<p className="muted small">进度仅针对当前文件，按已处理数量显示，不代表剩余时间。已处理页可能包含识别问题，完成后请查看提取提示并核验。</p></>}
        {!unit && <p className="muted">{importStatus.status === 'queued' ? '任务正在队列中等待处理。' : '正在准备或整理导入结果。'}</p>}
      </div> : <p className="status-line" role="status">正在恢复导入任务…</p>}
      {pollError && <div className="error-banner" role="alert"><p>读取导入任务失败：{pollError}</p><p>任务标识已保留；重试只查询此任务，不会重复上传文件。</p><button disabled={checking} onClick={() => setRetry(n => n + 1)}>重试读取进度</button></div>}
      {importStatus?.status === 'failed' && <div className="error-banner" role="alert"><p>{importStatus.error || '后台导入失败，请返回检查文件后重试。'}</p><button onClick={onImportCleared}>返回选文件并重试</button></div>}
      {checking && importStatus && <p className="status-line" role="status">{importStatus.status === 'ready' ? '读取完整报表，准备比较配置…' : '正在读取最新进度…'}</p>}
      <p className="muted small import-task-id">导入任务：{importId}</p>
    </section>;
  }

  return <section className="upload-section" aria-labelledby="upload-heading">
    <div className="section-heading"><div><h2 id="upload-heading">导入两份报表</h2><p className="muted">选择两份 Excel 或两份 PDF，读取后确认比较范围和项目匹配字段。</p></div><span className="chip">Excel / PDF · 本机处理</span></div>
    <form onSubmit={upload}>
      <fieldset disabled={busy} className="plain-fieldset">
        <div className="upload-grid">
          {(['old', 'new'] as const).map(side => {
            const file = side === 'old' ? oldFile : newFile;
            return <label key={side} className={`drop-zone ${file ? 'has-file' : ''}`} onDragOver={e => e.preventDefault()} onDrop={e => drop(e, side)}>
              <span className="label">{side === 'old' ? '旧版' : '新版'}</span>
              <strong>{file ? file.name : `选择${side === 'old' ? '旧版' : '新版'}文件`}</strong>
              <span className="muted">{file ? `${(file.size / 1024).toLocaleString('zh-CN', { maximumFractionDigits: 1 })} KB · 点击替换` : '点击选择，或将文件拖到这里'}</span>
              <input type="file" accept=".xlsx,.pdf,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" aria-label={side === 'old' ? '选择旧版文件' : '选择新版文件'} onChange={e => choose(e.target.files?.[0], side)} />
            </label>;
          })}
        </div>
        <div className="toolbar"><div className="actions"><button type="submit" className="primary" disabled={!oldFile || !newFile || busy || mixedTypes}>{uploading ? '正在上传文件…' : '读取并配置比较'}</button><button type="button" disabled={!oldFile && !newFile} onClick={() => { setOldFile(newFile); setNewFile(oldFile); }}>交换旧版与新版</button></div><span className="muted small">{pdf ? '扫描页在本机识别，原始 PDF 保持只读。' : '原文件只读，不执行或重新计算公式。'}</span></div>
      </fieldset>
    </form>
    {mixedTypes && <div className="error-banner" role="alert">两份文件须同为 Excel 或同为 PDF，请替换其中一份。</div>}
    {uploading && <p className="status-line" role="status">正在上传两份文件，请保持页面打开。收到后台任务标识后才支持刷新恢复；上传期间刷新可能需要重新选择文件。</p>}
    {error && <div className="error-banner" role="alert">{error}</div>}
    <div className="sample-links"><span className="muted">Excel 示例：</span><a href="/api/examples/old" download>旧版 Excel</a><a href="/api/examples/new" download>新版 Excel</a></div>
    <div className="sample-links"><span className="muted">文本 PDF 示例：</span><a href="/api/examples/pdf/text/old" download>旧版文本 PDF</a><a href="/api/examples/pdf/text/new" download>新版文本 PDF</a></div>
    <div className="sample-links"><span className="muted">扫描 PDF 示例：</span><a href="/api/examples/pdf/scan/old" download>旧版扫描 PDF</a><a href="/api/examples/pdf/scan/new" download>新版扫描 PDF</a></div>
  </section>;
}
