import { useEffect, useRef, useState } from 'react';
import './PdfComparisonViewer.css';
import type { Job, ReportComparison, Side, Source } from '../types';
import { sourceLabel } from '../api';

type ViewMode = 'side' | 'switch' | 'overlay';
const sides = ['old', 'new'] as const;
const sideLabels = { old: '旧版', new: '新版' };

function regionOf(source: Source | null, page?: number) {
  if (!page || source?.page !== page || source.manual || source.location_precision === 'page' ||
      source.precision === 'page' || source.location_granularity === 'unconfirmed' || source.location_granularity === 'page') return null;
  const box = source.bbox;
  if (!box || !box.every(Number.isFinite) || box[2] <= box[0] || box[3] <= box[1]) return null;
  return {
    box,
    boxes: source.highlight_boxes?.length ? source.highlight_boxes : [box],
    reference: source.location_precision === 'corresponding_region',
  };
}

export default function PdfComparisonViewer({ job, report, oldSource, newSource, selectionId }: { job: Job; report: ReportComparison | null; oldSource: Source | null; newSource: Source | null; selectionId: string | null }) {
  const [modeChoice, setMode] = useState<ViewMode | null>(null);
  const [narrow, setNarrow] = useState(() => window.matchMedia('(max-width: 650px)').matches);
  const [activeSide, setActiveSide] = useState<Side>('new');
  const [opacity, setOpacity] = useState(50);
  const [zoom, setZoom] = useState(100);
  const [detailScale, setDetailScale] = useState(3);
  const [showOriginal, setShowOriginal] = useState(false);
  const [pairIndex, setPairIndex] = useState(0);
  const [sourceCombination, setSourceCombination] = useState(false);
  const [manualPages, setManualPages] = useState<Record<Side, number | null> | null>(null);
  const [loaded, setLoaded] = useState<Record<string, 'loaded' | 'failed'>>({});
  const [attempt, setAttempt] = useState(0);
  const [space, setSpace] = useState({ width: 800, height: 600 });
  const viewport = useRef<HTMLDivElement>(null);
  const pagePairs = report?.page_pairs ?? [];
  const sources = { old: oldSource, new: newSource };
  const mode = modeChoice ?? (narrow ? 'switch' : 'side');

  useEffect(() => {
    const media = window.matchMedia('(max-width: 650px)');
    const update = () => setNarrow(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);

  function selectSourcePages() {
    const index = pagePairs.findIndex(pair => (!oldSource?.page || pair.old_page === oldSource.page) && (!newSource?.page || pair.new_page === newSource.page));
    if (index >= 0) {
      setPairIndex(index); setManualPages(null); setSourceCombination(false);
    } else {
      setManualPages({ old: oldSource?.page ?? null, new: newSource?.page ?? null }); setSourceCombination(true);
    }
    if (!oldSource?.page || !newSource?.page) setActiveSide(oldSource?.page ? 'old' : 'new');
  }

  useEffect(() => {
    setPairIndex(0); setManualPages(null); setSourceCombination(false);
    setZoom(100); setShowOriginal(false);
  }, [report?.id]);

  useEffect(() => {
    if (selectionId && (oldSource?.page || newSource?.page)) selectSourcePages();
    else if (sourceCombination) { setManualPages(null); setSourceCombination(false); }
  }, [selectionId, oldSource?.page, newSource?.page, report?.id]);

  useEffect(() => {
    const target = viewport.current;
    if (!target) return;
    const observer = new ResizeObserver(() => {
      if (target.clientWidth > 24 && target.clientHeight > 24) {
        setSpace({ width: target.clientWidth - 24, height: target.clientHeight - 24 });
      }
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, []);

  const pair = pagePairs[pairIndex];
  const pageNumbers = manualPages ?? { old: pair?.old_page ?? null, new: pair?.new_page ?? null };
  const pages = {
    old: job.files.old.pages?.find(page => page.number === pageNumbers.old),
    new: job.files.new.pages?.find(page => page.number === pageNumbers.new),
  };
  const width = (side: Side) => pages[side]?.physical_width ?? pages[side]?.width ?? 0;
  const height = (side: Side) => pages[side]?.physical_height ?? pages[side]?.height ?? 0;
  const oldWidth = width('old'), newWidth = width('new');
  const oldHeight = height('old'), newHeight = height('new');
  const gap = mode === 'side' && pages.old && pages.new ? 16 : 0;
  const stageWidth = mode === 'side' ? oldWidth + newWidth : Math.max(oldWidth, newWidth);
  const stageHeight = Math.max(oldHeight, newHeight);
  const scale = stageWidth && stageHeight ? Math.max(.01, Math.min((space.width - gap) / stageWidth, space.height / stageHeight)) * zoom / 100 : 1;
  const visibleSide = pages[activeSide] ? activeSide : pages.old ? 'old' : 'new';
  const legacyDimensions = Object.values(pages).some(page => page && (!page.physical_width || !page.physical_height));
  const sizesDiffer = !!pages.old && !!pages.new && (Math.abs(oldWidth - newWidth) > .1 || Math.abs(oldHeight - newHeight) > .1);
  const detailBoxes = sides.map(side => {
    const source = sources[side];
    const page = job.files[side].pages?.find(item => item.number === source?.page);
    const region = regionOf(source, page?.number);
    if (!page || !region) return null;
    const { box } = region;
    if ((box[2] - box[0]) * (box[3] - box[1]) > .5) return null;
    const paperWidth = page.physical_width ?? page.width;
    const paperHeight = page.physical_height ?? page.height;
    if (!paperWidth || !paperHeight) return null;
    const left = Math.max(0, box[0] * paperWidth - 12);
    const top = Math.max(0, box[1] * paperHeight - 12);
    const right = Math.min(paperWidth, Math.max(box[2] * paperWidth + 12, left + 120));
    const bottom = Math.min(paperHeight, Math.max(box[3] * paperHeight + 12, top + 48));
    if (right <= left || bottom <= top) return null;
    return { ...region, page: page.number, left, top, width: right - left, height: bottom - top, paperWidth, paperHeight };
  });
  const hasDetails = !!selectionId && detailBoxes.some(Boolean);
  const localView = hasDetails && !showOriginal;
  const viewingOtherPages = sides.some(side => sources[side]?.page && sources[side]?.page !== pageNumbers[side]);

  useEffect(() => {
    const target = viewport.current;
    if (!target || localView) return;
    const frame = requestAnimationFrame(() => {
      const highlight = target.querySelector<HTMLElement>(mode === 'switch'
        ? `.physical-paper.${visibleSide} .full-page-highlight`
        : '.full-page-highlight');
      const canvas = target.getBoundingClientRect();
      const region = highlight?.getBoundingClientRect();
      if (zoom <= 100 || !region) target.scrollTo(0, 0);
      else target.scrollTo(
        target.scrollLeft + region.left - canvas.left - (target.clientWidth - region.width) / 2,
        target.scrollTop + region.top - canvas.top - (target.clientHeight - region.height) / 2,
      );
    });
    return () => cancelAnimationFrame(frame);
  }, [selectionId, localView, mode, visibleSide, pageNumbers.old, pageNumbers.new, zoom, scale]);

  function choosePage(side: Side, value: string) {
    setSourceCombination(false);
    setManualPages({ ...pageNumbers, [side]: value ? Number(value) : null });
    if (value) setActiveSide(side);
  }

  function imageUrl(side: Side, page: number, hd: boolean) {
    return `/api/jobs/${job.id}/pages/${side}/${page}.png?attempt=${attempt}${hd ? '&dpi=288' : ''}`;
  }

  function pageImage(url: string, alt: string, failure: string, style?: React.CSSProperties) {
    return loaded[url] === 'failed' ? <div className="paper-image-error" role="alert">{failure}<button onClick={event => {
      event.currentTarget.closest<HTMLElement>('[tabindex="0"]')?.focus({ preventScroll: true });
      setAttempt(value => value + 1);
    }}>重试图像</button></div> : <>
      <img key={url} src={url} alt={alt} draggable={false} style={style}
        onLoad={() => setLoaded(state => ({ ...state, [url]: 'loaded' }))}
        onError={() => setLoaded(state => ({ ...state, [url]: 'failed' }))} />
      {!loaded[url] && <span className="paper-loading" role="status">正在读取图像…</span>}
    </>;
  }

  return <section className="full-page-viewer" aria-label="完整报表原页比较">
    <div className="original-toolbar">
      <div className="evidence-views" role="group" aria-label="原文视图">
        <button aria-pressed={localView} aria-disabled={!hasDetails} onClick={() => { if (hasDetails) setShowOriginal(false); }}>高清局部</button>
        <button aria-pressed={!localView} onClick={() => setShowOriginal(true)}>完整原页</button>
      </div>
      {localView && <label className="inline-label">局部倍率<select aria-label="差异局部倍率" value={detailScale} onChange={event => setDetailScale(Number(event.target.value))}>
        {[2, 3, 4].map(value => <option key={value} value={value}>{value} 倍</option>)}
      </select></label>}
    </div>
    {selectionId && <div className="source-addresses small">{sides.map(side => <span className={`source-side ${side}`} key={side}><strong>{sideLabels[side]}</strong> {sourceLabel(sources[side], true)}</span>)}</div>}
    {selectionId && !hasDetails && <p className="small muted crop-unavailable">本条记录没有高清局部，可查看完整原页。</p>}
    {localView && <div className="pdf-detail-comparison">
      <div className="pdf-detail-pair">{sides.map((side, index) => {
        const detail = detailBoxes[index];
        const label = sideLabels[side];
        const url = detail ? imageUrl(side, detail.page, true) : null;
        return <section className={`pdf-detail-side ${side}`} key={side} aria-label={`${label}差异局部`}>
          <h4>{label}{detail ? ` · 第 ${detail.page} 页${detail.reference ? ' · 参考区域' : ''}` : ''}</h4>
          {detail && url ? <div key={selectionId} className="pdf-detail-scroll" tabIndex={0} aria-label={`${label}差异局部滚动画布`}>
            <div className="pdf-detail-crop" style={{ width: detail.width * detailScale, height: detail.height * detailScale }}>
              {pageImage(url, `${label}第 ${detail.page} 页所选差异原图局部`, '局部原图加载失败', {
                width: detail.paperWidth * detailScale, height: detail.paperHeight * detailScale,
                left: -detail.left * detailScale, top: -detail.top * detailScale,
              })}
              {loaded[url] === 'loaded' && detail.boxes.map((box, index) => <div key={index} className={`pdf-detail-outline ${detail.reference ? 'reference-region' : ''}`} style={{
                left: (box[0] * detail.paperWidth - detail.left) * detailScale,
                top: (box[1] * detail.paperHeight - detail.top) * detailScale,
                width: (box[2] - box[0]) * detail.paperWidth * detailScale,
                height: (box[3] - box[1]) * detail.paperHeight * detailScale,
              }} />)}
            </div>
          </div> : <p className="pdf-detail-unavailable small muted">{sources[side] ? '此侧没有可用的局部范围，请查看完整原页。' : '此侧没有对应差异区域。'}</p>}
        </section>;
      })}</div>
      <p className="viewer-note">两侧共用倍率。边框标记所选区域，可在画布内滚动查看。</p>
    </div>}
    <div hidden={localView}>
      <div className="full-page-controls">
        <label>页面对应<select aria-label="报表页面对应" value={manualPages ? 'manual' : pagePairs.length ? String(pairIndex) : 'none'} onChange={event => {
          if (event.target.value === 'manual' || event.target.value === 'none') return;
          setPairIndex(Number(event.target.value)); setManualPages(null); setSourceCombination(false);
        }}>
          <option value="none" disabled>尚无页面对应</option>
          {manualPages && <option value="manual">{sourceCombination ? '所选记录的原文来源' : '手动浏览组合'}</option>}
          {pagePairs.map((item, index) => <option key={index} value={index}>旧 {item.old_page ?? '无'} / 新 {item.new_page ?? '无'} · {item.status === 'confirmed' ? '已确认' : '待确认'}</option>)}
        </select></label>
        <label>查看方式<select aria-label="完整原页查看方式" value={mode} onChange={event => setMode(event.target.value as ViewMode)}>
          <option value="side">并排</option><option value="switch">切换旧版 / 新版</option><option value="overlay">透明叠加</option>
        </select></label>
        <label>共同缩放<select aria-label="完整原页共同缩放" value={zoom} onChange={event => setZoom(Number(event.target.value))}>
          {[50, 75, 100, 125, 150, 200, 300].map(value => <option value={value} key={value}>{value === 100 ? '适合整页' : `${value}%`}</option>)}
        </select></label>
      </div>
      <div className="page-view-options">
        {mode === 'switch' && <div className="evidence-views" role="group" aria-label="切换原页版本">
          {sides.map(side => <button key={side} disabled={!pages[side]} aria-pressed={visibleSide === side && !!pages[side]} onClick={() => setActiveSide(side)}>显示{sideLabels[side]}</button>)}
        </div>}
        {mode === 'overlay' && <label className="overlay-opacity">新版不透明度 {opacity}%<input aria-label="新版叠加不透明度" type="range" min="0" max="100" value={opacity} onChange={event => setOpacity(Number(event.target.value))} /></label>}
        {viewingOtherPages && <button aria-disabled={!viewingOtherPages} onClick={event => {
          event.currentTarget.closest('section')?.querySelector<HTMLElement>('[aria-label="报表页面对应"]')?.focus({ preventScroll: true });
          selectSourcePages();
        }}>返回所选记录页</button>}
      </div>
      <details className="manual-pages">
        <summary>手动选页与配对依据</summary>
        <div className="full-page-controls">{sides.map(side => <label key={side}>{sideLabels[side]}原页<select aria-label={`${sideLabels[side]}完整原页`} value={pageNumbers[side] ?? ''} onChange={event => choosePage(side, event.target.value)}>
          <option value="">无对应页</option>{job.files[side].pages?.map(page => <option value={page.number} key={page.number}>第 {page.number} 页</option>)}
        </select></label>)}</div>
        <p className={`small ${manualPages || pair?.status === 'pending' ? 'warning-text' : 'muted'}`}>{sourceCombination
          ? oldSource?.page && newSource?.page ? '按所选记录的两侧来源展示，整张页面配对保持原记录。' : '仅显示所选一侧原文，另一侧对应页未确认。'
          : manualPages ? '当前为手动浏览组合，原页面配对保持原记录。' : pair?.basis ?? '尚无页面对应记录。'}</p>
      </details>
      <div className="paper-size-labels">{sides.map(side => <span className={`paper-label ${side}`} key={side}>
        {sideLabels[side]} {pages[side] ? `第 ${pageNumbers[side]} 页 · ${(width(side) / 72 * 25.4).toFixed(1)} × ${(height(side) / 72 * 25.4).toFixed(1)} mm` : '无对应页'}
      </span>)}</div>
      {sizesDiffer && <p className="small muted">两侧纸张尺寸不同，按同一物理比例显示。</p>}
      {viewingOtherPages && <p className="small warning-text">正在浏览其他原页，所选差异保留在原定位页。</p>}
      {legacyDimensions && <p className="small warning-text">历史记录缺少物理尺寸属性，当前按已记录宽高显示，尺度未验证。</p>}
      <div ref={viewport} className="full-page-scroll" tabIndex={0} aria-label="共同比例完整原页画布">
        {stageWidth && stageHeight ? <div className={`full-page-stage ${mode}`} style={{ width: stageWidth * scale + gap, height: stageHeight * scale }} data-scale={scale}>
          {sides.map(side => {
            const page = pages[side];
            if (!page || (mode === 'switch' && visibleSide !== side)) return null;
            const region = regionOf(sources[side], page.number);
            const url = imageUrl(side, page.number, zoom > 100);
            return <div key={side} className={`physical-paper ${side}`} data-side={side} data-physical-width={width(side)} data-physical-height={height(side)}
              style={{ width: width(side) * scale, height: height(side) * scale, left: mode === 'side' && side === 'new' ? oldWidth * scale + gap : 0, top: 0, opacity: mode === 'overlay' && side === 'new' ? opacity / 100 : 1 }}>
              {!localView && pageImage(url, `${sideLabels[side]}完整原文件第 ${page.number} 页`, `${sideLabels[side]}原页加载失败`)}
              {loaded[url] === 'loaded' && region?.boxes.map((box, index) => <div key={index} className={`full-page-highlight ${region.reference ? 'reference-region' : ''}`} style={{
                left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%`,
              }} />)}
            </div>;
          })}
        </div> : <p className="empty">尚无可显示的对应原页。可手动选页查看原件。</p>}
      </div>
    </div>
    <div className="original-downloads">{sides.map(side => <a className="small" key={side} href={`/api/jobs/${job.id}/files/${side}`} download>下载{sideLabels[side]}原件</a>)}</div>
  </section>;
}
