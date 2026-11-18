export type Side = 'old' | 'new';

export interface Source {
  sheet?: string;
  cell?: string;
  range?: string;
  kind?: 'xlsx' | 'pdf';
  page?: number;
  bbox?: [number, number, number, number];
  highlight_boxes?: [number, number, number, number][];
  content_sources?: Source[];
  location_granularity?: 'text' | 'cell' | 'paragraph' | 'line' | 'frame' | 'table' | 'page' | 'region' | 'reference' | 'unconfirmed';
  location_reason?: string;
  method?: 'text' | 'ocr';
  confidence?: number;
  manual?: boolean;
  location_precision?: 'page' | 'bbox' | 'text' | 'cell' | 'paragraph' | 'corresponding_region';
  precision?: 'page' | 'bbox';
  side?: Side;
}

export interface PdfPage {
  physical_width?: number;
  physical_height?: number;
  rotation?: number;
  media_box?: number[];
  crop_box?: number[];
  has_native_text?: boolean;
  native_lines?: unknown[];
  ocr_regions?: unknown[];
  number: number;
  width: number;
  height: number;
  image_width: number;
  image_height: number;
  method: 'text' | 'ocr';
  status?: 'ok' | 'unavailable';
  reviewed?: boolean;
  reviewable?: boolean;
  reviewed_cells?: number;
  total_cells?: number;
  structure_unconfirmed?: boolean;
}

export interface Cell {
  address: string;
  row: number;
  column: number;
  value: string | boolean | null;
  display: string;
  value_type: string;
  formula: string | null;
  cached_value: string | number | boolean | null;
  cache_status: 'not_formula' | 'available' | 'missing';
  number_format: string;
  merged_range?: string | null;
  style?: Record<string, unknown>;
  hidden_row?: boolean;
  hidden_column?: boolean;
}

export interface SheetMeta {
  name: string;
  max_row: number;
  max_column: number;
  hidden: boolean;
  suggested_header_row: number;
  nonempty_count: number;
  member_sheets?: string[];
  candidate_merge?: boolean;
}

export interface Column {
  column: string;
  label: string;
}

export interface ColumnsResponse {
  columns: Column[];
  preview: { row: number; values: Record<string, string> }[];
}

export interface ColumnMapping {
  id: string;
  name: string;
  old_column: string | null;
  new_column: string | null;
  is_key: boolean;
}

export interface PairConfig {
  old_report_id?: string | null;
  new_report_id?: string | null;
  pairing_confirmed?: boolean;
  confirmation_reason?: string;
  id: string;
  old_sheet: string | null;
  new_sheet: string | null;
  old_header_start: number;
  old_header_end: number;
  new_header_start: number;
  new_header_end: number;
  old_range: string | null;
  new_range: string | null;
  mode: 'key' | 'position';
  columns: ColumnMapping[];
}

export interface CompareConfig {
  review_revision?: number;
  pairs: PairConfig[];
  absolute_tolerance: string;
  ignore_outer_whitespace: boolean;
  compare_formulas: boolean;
  compare_formatting: boolean;
}

export interface Job {
  parent_job_id?: string;
  report_inventory_version?: number;
  review_revision?: number;
  structure_revision?: number;
  id: string;
  kind?: 'xlsx' | 'pdf';
  status: string;
  files: Record<Side, { name: string; sha256: string; sheets: SheetMeta[]; pages?: PdfPage[]; reports?: BusinessReport[] }>;
  suggested_pairs: PairConfig[];
  warnings: string[];
}

export interface Diff {
  report_id?: string;
  layer?: 'completeness' | 'content' | 'structure' | 'format' | 'layout' | 'uncertainty' | 'business';
  category?: ComparisonCategory | 'completeness';
  id: string;
  kind: string;
  table_id: string;
  table_name: string;
  row_key: string;
  field: string;
  old_value: string | null;
  new_value: string | null;
  delta: string | null;
  message: string;
  severity: 'change' | 'issue';
  old_source: Source | null;
  new_source: Source | null;
}

export interface Run {
  report_inventory_version?: number;
  non_report_regions?: NonReportRegion[];
  reports?: ReportComparison[];
  type_inventory?: TypeInventory;
  issues?: ReportIssue[];
  id: string;
  job_id: string;
  status: string;
  summary: {
    total: number;
    changes: number;
    issues: number;
    by_kind: Record<string, number>;
    by_category?: Record<string, number>;
    matched_rows: number;
    unchanged_rows: number;
    compared_cells: number;
  };
  coverage: {
    compared_sheets: Record<Side, string[]>;
    excluded_sheets: Record<Side, string[]>;
    warnings: string[];
  };
  config: CompareConfig;
  review_revision?: number;
  review_pages?: Record<Side, PdfPage[]>;
}

export interface DiffPage {
  items: Diff[];
  total: number;
  offset: number;
  limit: number;
}

export interface SheetRegion {
  sheet: string;
  start_row: number;
  start_column: number;
  end_row: number;
  end_column: number;
  max_row: number;
  max_column: number;
  cells: Cell[];
  merged_anchors?: Cell[];
  merged_ranges: string[];
  hidden: boolean;
}

export interface ReviewCell {
  id: string;
  sheet: string;
  cell: string;
  page: number;
  bbox?: Source['bbox'];
  source: Source;
  original_text: string;
  current_text: string;
  value_type: string;
  structure: 'table' | 'text_blocks';
  reviewed: boolean;
  correction_revision?: number;
}
export interface ReviewPage {
  number: number;
  method: 'text' | 'ocr';
  status: 'ok' | 'unavailable';
  reviewed: boolean;
  reviewable: boolean;
  reviewed_cells: number;
  total_cells: number;
  structure_unconfirmed?: boolean;
}
export interface ReviewResponse {
  revision: number;
  structure_revision?: number;
  items: ReviewCell[];
  total: number;
  offset: number;
  limit: number;
  pages: ReviewPage[];
}
export type ReviewAction = 'cell_update' | 'cell_verify' | 'cell_unverify' | 'page_verify' | 'page_unverify' | 'table_update';
export interface ReviewEvent {
  table_before?: StructureTable;
  table_after?: StructureTable;
  structure_revision?: number;
  revision: number;
  side: Side;
  action: ReviewAction;
  target_id?: string;
  page: number;
  sheet?: string;
  cell?: string;
  original_text?: string;
  before_text?: string;
  after_text?: string;
  reason: string;
  created_at: string;
}
export interface ReviewHistory {
  revision: number;
  items: ReviewEvent[];
  total: number;
  offset: number;
  limit: number;
}

export interface StructureCell {
  id?: string;
  text: string;
  original_text?: string;
  source?: Source;
  manual?: boolean;
  reviewed?: boolean;
}
export interface StructureTable {
  sheet: string;
  page: number;
  structure: 'table' | 'text_blocks';
  rows: StructureCell[][];
  structure_confirmed: boolean;
}
export interface StructureDirectoryItem {
  sheet: string;
  page: number;
  structure: 'table' | 'text_blocks';
  rows: number;
  columns: number;
  editable: boolean;
  edit_limit?: string;
}
export interface StructureResponse {
  revision: number;
  structure_revision: number;
  tables: StructureDirectoryItem[];
  table?: StructureTable;
}

export interface ImportStatus {
  id: string;
  status: 'queued' | 'processing' | 'ready' | 'failed';
  files: Record<Side, { name: string }>;
  progress: {
    side: Side | null;
    file_index: 0 | 1 | 2;
    file_count: number;
    stage: 'queued' | 'opening' | 'rendering' | 'text_extract' | 'ocr' | 'table_extract' | 'reading_sheet' | 'file_completed' | 'finalizing' | 'completed' | 'failed';
    unit: 'page' | 'sheet' | null;
    current: number;
    total: number | null;
    completed: number;
    item_name?: string;
  };
  job_id?: string;
  error?: string;
  created_at: string;
  updated_at: string;
}

export interface BusinessReport {
  id: string;
  title: string;
  type_code: string | null;
  object: string | null;
  object_status: string;
  pages: number[];
  member_sheets: string[];
  source: Source;
  issues?: string[];
}
export interface BusinessComparison {
  report_id: string;
  type_code: string;
  status: 'unchanged' | 'changed' | 'pending';
  compared_rows: number;
  compared_fields: number;
  issues: string[];
  changes?: number;
  issue_count?: number;
}
export interface ReportComparison {
  business?: BusinessComparison;
  paragraph_checks?: ParagraphCheck[];
  paragraph_check_summary?: ParagraphCheckSummary;
  comparison_status?: 'changed' | 'pending' | 'unchanged';
  dimensions?: Record<ComparisonDimension, DimensionSummary>;
  page_pairs?: ReportPagePair[];
  id: string;
  title: string;
  type_code: string | null;
  object: string | null;
  presence: 'shared' | 'old_only' | 'new_only' | 'uncertain';
  confirmation: 'confirmed' | 'pending';
  old: BusinessReport | null;
  new: BusinessReport | null;
  basis: string;
  blocking_reasons: string[];
  content_status: 'reliable' | 'pending' | 'not_applicable';
  change_count: number;
}
export interface ReportType {
  type_code: string | null;
  title: string;
  old_pages: number[];
  new_pages: number[];
}
export interface TypeInventory {
  shared: ReportType[];
  old_only: ReportType[];
  new_only: ReportType[];
  uncertain: ReportType[];
}
export interface ReportIssue {
  id: string;
  type: string;
  message: string;
  report_ids: string[];
  side?: Side;
  pages: number[];
  sources: Source[];
  actions: string[];
}
export interface ResultLocation {
  report_id?: string;
  id: string;
  title: string;
  message: string;
  old_source: Source | null;
  new_source: Source | null;
}

export interface NonReportRegion {
  id: string;
  type: 'blank_page' | 'document_section' | 'continuation_note' | 'unclassified_region';
  side: Side;
  pages: number[];
  source: Source;
  basis: string;
  report_id?: string;
}

export type ComparisonDimension = 'content' | 'structure' | 'format' | 'layout';
export type ComparisonCategory = ComparisonDimension | 'business';
export interface DimensionSummary {
  status: 'changed' | 'unchanged' | 'partial' | 'unverified' | 'not_applicable';
  changes: number;
  issues: number;
  summary: string;
  limitations: string[];
}
export interface ReportPagePair {
  old_page: number | null;
  new_page: number | null;
  basis: string;
  status: 'confirmed' | 'pending';
}

export interface ParagraphCheck {
  attribute: 'line_spacing' | 'alignment';
  status: 'changed' | 'unchanged' | 'unverified';
  message: string;
  old_source: Source | null;
  new_source: Source | null;
  old_value?: string | null;
  new_value?: string | null;
  measurements?: Record<string, unknown>;
}

export interface ParagraphCheckSummary {
  total: number;
  changed: number;
  unchanged: number;
  unverified: number;
}
export interface ParagraphCheckPage {
  items: ParagraphCheck[];
  total: number;
  offset: number;
  limit: number;
}
