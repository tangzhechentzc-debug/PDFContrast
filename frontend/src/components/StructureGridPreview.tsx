import { columnName } from '../api';
import type { StructureTable } from '../types';

export default function StructureGridPreview({ table, label }: { table: StructureTable; label: string }) {
  return <div className="structure-preview"><h3>{label} · {table.sheet} · {table.rows.length} × {table.rows[0]?.length ?? 0}</h3><div className="table-scroll"><table><thead><tr><th>行</th>{table.rows[0]?.map((_, c) => <th key={c}>{columnName(c + 1)}</th>)}</tr></thead><tbody>{table.rows.map((row, r) => <tr key={r}><th>{r + 1}</th>{row.map((cell, c) => <td key={c}><span>{cell.text || '（空）'}</span><small>{cell.id ? `ID ${cell.id}` : '新格'} · {cell.manual || cell.source?.manual ? '人工补录 · 整页定位' : '原始识别区域'}</small></td>)}</tr>)}</tbody></table></div></div>;
}
