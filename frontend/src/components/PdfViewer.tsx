import { useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import type { Job, Side, Source } from '../types';

export default function PdfViewer({ job, side, source, selectionId, isIssue, pageNavigation = true }: { job: Job; side: Side; source: Source | null; selectionId: string | null; isIssue?: boolean; pageNavigation?: boolean }) {
  const pages = job.files[side].pages ?? [];
  const firstPage = pages[0]?.number ?? 1;
  const [pageNumber, setPageNumber] = useState(source?.page ?? firstPage);
  const [pageInput, setPageInput] = useState(String(source?.page ?? firstPage));
  const [zoom, setZoom] = useState(100);
  const [browseMissing, setBrowseMissing] = useState(false);
  const [error, setError] = useState('');
  const [loadedImage, setLoadedImage] = useState('');
  const [failedImage, setFailedImage] = useState('');
  const [attempt, setAttempt] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const highlightRef = useRef<HTMLDivElement>(null);
  const sideLabel = side === 'old' ? '旧版' : '新版';
  const page = pages.find(p => p.number === pageNumber);
  const pageIndex = pages.findIndex(p => p.number === pageNumber);
  const imageUrl = `/api/jobs/${job.id}/pages/${side}/${pageNumber}.png?attempt=${attempt}`;
  const bboxKey = source?.bbox?.join(',') ?? '';
  const missingSource = !!selectionId && !source && !browseMissing;

  useEffect(() => {
    setBrowseMissing(false); setError('');
    if (source?.page) { setPageNumber(source.page); setPageInput(String(source.page)); }
  }, [selectionId, source?.page, bboxKey]);

  const highlight = useMemo(() => {
    if (!source?.bbox || source.manual || source.location_precision === 'page' || source.precision === 'page' || source.page !== pageNumber) return null;
    const [x0, y0, x1, y1] = source.bbox;
    if (![x0, y0, x1, y1].every(Number.isFinite) || x1 <= x0 || y1 <= y0) return null;
    const left = Math.max(0, Math.min(1, x0));
    const top = Math.max(0, Math.min(1, y0));
    return { left: `${left * 100}%`, top: `${top * 100}%`, width: `${(Math.max(left, Math.min(1, x1)) - left) * 100}%`, height: `${(Math.max(top, Math.min(1, y1)) - top) * 100}%` };
  }, [bboxKey, source?.page, source?.manual, source?.location_precision, source?.precision, pageNumber]);

  useEffect(() => {
    if (loadedImage !== imageUrl || missingSource) return;
    const frame = requestAnimationFrame(() => {
      const scroll = scrollRef.current;
      const target = highlightRef.current;
      if (!scroll) return;
      if (target && highlight) {
        const sr = scroll.getBoundingClientRect();
        const tr = target.getBoundingClientRect();
        scroll.scrollTop = Math.max(0, scroll.scrollTop + tr.top - sr.top - (scroll.clientHeight - tr.height) / 2);
        scroll.scrollLeft = Math.max(0, scroll.scrollLeft + tr.left - sr.left - (scroll.clientWidth - tr.width) / 2);
      } else { scroll.scrollTop = 0; scroll.scrollLeft = 0; }
    });
    return () => cancelAnimationFrame(frame);
  }, [loadedImage, imageUrl, zoom, bboxKey, selectionId, highlight, missingSource]);

  function showPage(number: number) {
    setPageNumber(number); setPageInput(String(number)); setError(''); setBrowseMissing(true);
  }

  function jump(event: FormEvent) {
    event.preventDefault();
    const number = Number(pageInput);
    if (!Number.isInteger(number) || !pages.some(p => p.number === number)) { setError(`请输入此文件的有效页码（共 ${pages.length} 页）。`); return; }
    showPage(number);
  }

  const mixed = page?.method === 'ocr' && (page.native_lines?.length ?? 0) > 0;
  const scanned = page?.method === 'ocr' || (source?.page === pageNumber && source.method === 'ocr');
  return <section className="viewer pdf-viewer" aria-label={`${sideLabel} PDF 原文`}>
    <div className="viewer-heading"><span className="label">{sideLabel}</span><a href={`/api/jobs/${job.id}/files/${side}`} download className="small">下载原件</a></div>
    <div className="file-name" title={job.files[side].name}>{job.files[side].name}</div>
    <div className="location" data-testid={`${side}-location`}>{missingSource ? '本版本无对应原文位置' : `第 ${pageNumber} 页${source?.page === pageNumber && source.manual ? ' · 人工补录 · 整页定位' : source?.page === pageNumber && (source.location_precision === 'page' || source.precision === 'page') ? ' · 整页定位' : source?.page === pageNumber && highlight ? ' · 已定位差异区域' : ' · 浏览原件'}`}</div>
    {missingSource ? <div className="absent"><strong>本版本没有可定位的对应原文</strong><span className="muted">请结合另一侧原文和差异说明核对。</span>{pages.length > 0 && <button onClick={() => showPage(firstPage)}>浏览本侧原件</button>}</div> : !pages.length ? <div className="absent"><strong>原页面信息不可用</strong><span className="muted">请下载原文件查看，并检查提取失败提示。</span></div> : <>
      {pageNavigation && <div className="pdf-controls"><div className="actions"><button disabled={pageIndex <= 0} aria-label={`${sideLabel}上一页`} onClick={() => showPage(pages[pageIndex - 1].number)}>上一页</button><button disabled={pageIndex < 0 || pageIndex >= pages.length - 1} aria-label={`${sideLabel}下一页`} onClick={() => showPage(pages[pageIndex + 1].number)}>下一页</button></div><form className="address-form" onSubmit={jump}><label className="sr-only" htmlFor={`${side}-page`}>{sideLabel} PDF 页码</label><input id={`${side}-page`} type="number" min="1" max={pages.at(-1)?.number} value={pageInput} onChange={e => setPageInput(e.target.value)} /><button type="submit">跳页</button></form></div>}
      <div className="pdf-controls"><label className="inline-label">缩放<select aria-label={`${sideLabel} PDF 缩放`} value={zoom} onChange={e => setZoom(Number(e.target.value))}>{[50, 75, 100, 125, 150, 200, 300].map(value => <option key={value} value={value}>{value}%</option>)}</select></label><span className="label">{pageIndex + 1} / {pages.length} 页</span></div>
      {scanned && <div className="pdf-ocr-note"><span className={`chip ${page?.reviewed ? '' : 'warn'}`}>{mixed ? page?.reviewed ? '含局部 OCR · 已核验' : '含局部 OCR · 待核验' : page?.reviewed ? '扫描来源 · 本页已核验' : '◇ 扫描内容待核验'}</span><p>{mixed ? '原生文字来源保留，局部 OCR 识别区域需对照原图核验；原生文字不会改标为 OCR。' : page?.reviewed ? job.report_inventory_version === 2 ? '本页识别文字已核验；结构、字体和版式依各维度结果分别判断。' : '已确认本页识别值与完整性；原始来源仍为扫描识别。' : '识别值相同也需核对原图，不能据此认定内容一致。'}</p></div>}
      {error && <div className="error-banner" role="alert">{error}</div>}
      {failedImage === imageUrl ? <div className="absent"><strong>原页图像加载失败</strong><button onClick={() => setAttempt(n => n + 1)}>重试加载原页</button></div> : <div className="pdf-scroll" ref={scrollRef} tabIndex={0} aria-label={`${sideLabel} PDF 原页，可滚动查看`}>
        {loadedImage !== imageUrl && <p className="pdf-loading" role="status">读取原页图像…</p>}
        <div className="pdf-canvas" style={{ width: `${zoom}%` }}>
          <img key={imageUrl} src={imageUrl} alt={`${sideLabel}原文件第 ${pageNumber} 页`} width={page?.image_width} height={page?.image_height} onLoad={() => { setLoadedImage(imageUrl); setFailedImage(''); }} onError={() => setFailedImage(imageUrl)} />
          {highlight && <div ref={highlightRef} className={`pdf-highlight ${isIssue || (scanned && !page?.reviewed) ? 'uncertain' : ''}`} style={highlight} aria-label={`${sideLabel}当前差异所在区域`} />}
        </div>
      </div>}
      <p className="viewer-note">原文件页面的只读图像；高亮为覆盖层，不改写 PDF。{source?.page === pageNumber && source.manual ? '人工补录没有原始识别框，仅定位整页，请对照原图核对。' : source?.page === pageNumber && (source.location_precision === 'page' || source.precision === 'page') ? '本记录仅定位整页，请结合原文核对。' : source?.page === pageNumber && !highlight ? '当前记录没有可用区域坐标，请核对整页。' : ''}</p>
    </>}
  </section>;
}
