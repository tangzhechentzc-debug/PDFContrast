export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); this.name = 'ApiError'; }
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try {
      const payload = await response.json();
      if (typeof payload.detail === 'string') message = payload.detail;
      else if (Array.isArray(payload.detail)) message = payload.detail.map((x: { msg: string }) => x.msg).join('；');
    } catch { /* The HTTP status remains useful when an upstream response is not JSON. */ }
    throw new ApiError(message, response.status);
  }
  return response.json() as Promise<T>;
}

export function jsonRequest(value: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) };
}

export function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : '操作失败，请重试。';
}

export const kindLabels: Record<string, string> = {
  text_changed: '文字变化',
  text_whitespace_changed: '空格变化',
  line_break_changed: '换行变化',
  font_changed: '字体变化',
  font_size_changed: '字号变化',
  font_weight_changed: '字重变化',
  text_color_changed: '文字颜色变化',
  character_spacing_changed: '字间距变化',
  border_style_changed: '边框样式变化',
  border_correspondence_unverified: '线段对应待核验',
  table_structure_changed: '表格行列与合并变化',
  form_structure_changed: '表单框结构变化',
  form_geometry_changed: '表单框尺寸与位置变化',
  filling_line_geometry_changed: '填写线尺寸与位置变化',
  page_size_changed: '纸张尺寸变化',
  page_orientation_changed: '纸张横竖变化',
  page_rotation_changed: '页面旋转变化',
  page_crop_changed: '页面裁切变化',
  content_rotation_changed: '文字旋转变化',
  text_layout_changed: '文字位置变化',
  page_count_changed: '页数变化',
  page_order_changed: '页面顺序变化',
  report_page_added: '报表新增页',
  report_page_removed: '报表删除页',
  visual_region_changed: '图像区域变化',
  text_extraction_unverified: '文字提取待核验',
  page_pairing_unverified: '页面配对待确认',
  appearance_unavailable: '页面外观未验证',
  paragraph_line_spacing_changed: '段落行距变化',
  paragraph_alignment_changed: '段落对齐变化',
  paragraph_measurement_unverified: '段落测量未验证',
  report_added: '新增业务报表',
  report_removed: '业务报表缺失',
  report_uncertain: '业务报表待确认',
  report_review: '报表对应与提取待确认',
  attribute_unverified: '属性验证未完成',
  value_changed: '内容变化',
  formula_changed: '公式变化',
  format_changed: '格式变化',
  row_added: '新增项目',
  row_removed: '删除项目',
  column_added: '新增列',
  column_removed: '删除列',
  sheet_added: '新增工作表',
  sheet_removed: '删除工作表',
  match_ambiguous: '匹配待确认',
  formula_result_unavailable: '公式结果不可用',
  ocr_suspected: '疑似差异，需核对原图',
  ocr_review: '扫描内容待核验',
  extraction_unavailable: '提取不可用',
};

export function sourceLabel(source: Source | null, pdf = false): string {
  if (!source) return '无对应位置';
  if (pdf || source.kind === 'pdf') {
    if (!source.page) return '页面位置不可用';
    const grains = { text: '文字范围', cell: '单元格范围', paragraph: '段落范围', line: '线段范围', frame: '表框范围', table: '网格范围', page: '整页范围', region: '区域范围', reference: '参考区域，非精确文字位置', unconfirmed: '仅页级定位，文字位置未确认' };
    const grain = source.location_granularity ? grains[source.location_granularity] : source.location_precision === 'page' || source.precision === 'page' ? '仅页级定位' : source.location_precision === 'corresponding_region' ? grains.reference : '定位粒度未记录';
    return [`第 ${source.page} 页`, source.method === 'ocr' ? '扫描识别' : '', grain, source.location_reason].filter(Boolean).join(' · ');
  }
  if (!source.sheet || !source.cell) return '单元格位置不可用';
  return `${source.sheet}!${source.range || source.cell}`;
}

export function columnName(index: number): string {
  let name = '';
  while (index > 0) {
    index--;
    name = String.fromCharCode(65 + index % 26) + name;
    index = Math.floor(index / 26);
  }
  return name;
}

export function parseAddress(address: string): { row: number; column: number } | null {
  const match = /^\$?([A-Z]{1,3})\$?([1-9]\d{0,6})$/i.exec(address.trim());
  if (!match) return null;
  const column = [...match[1].toUpperCase()].reduce((n, c) => n * 26 + c.charCodeAt(0) - 64, 0);
  const row = Number(match[2]);
  return column <= 16384 && row <= 1048576 ? { column, row } : null;
}
import type { Source } from './types';


export const dimensionLabels = { content: '内容', structure: '结构', format: '格式', layout: '版式' } as const;
export const dimensionStatusLabels = { changed: '有变化', unchanged: '未发现变化', partial: '部分可比', unverified: '待核验 / 未完成', not_applicable: '不可比较 / 不适用' } as const;
