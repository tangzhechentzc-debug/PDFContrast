import type { Diff } from '../types';

export default function DiffText({ diff, side, fallback, preview = false }: { diff: Diff; side: 'old' | 'new'; fallback: string; preview?: boolean }) {
  const value = diff[`${side}_value`];
  if (value === null) return <span className="muted">{fallback}</span>;
  const oldValue = diff.old_value ?? '', newValue = diff.new_value ?? '';
  const showSpaces = diff.kind === 'text_whitespace_changed' || diff.kind === 'line_break_changed';
  const visible = (text: string) => showSpaces ? text.replace(/ /g, '␠').replace(/\t/g, '⇥').replace(/\r/g, '␍').replace(/\n/g, '↵\n').replace(/\u3000/g, '□') : text;
  const snippet = (text: string) => {
    if (!preview) return visible(text);
    const chars = Array.from(visible(text));
    return chars.length > 78 ? `${chars.slice(0, 78).join('')}…` : chars.join('');
  };
  if (!['text_changed', 'text_whitespace_changed', 'line_break_changed', 'value_changed'].includes(diff.kind) || oldValue === newValue) return <>{snippet(value)}</>;
  const before = Array.from(oldValue), after = Array.from(newValue), chars = Array.from(value);
  let start = 0, end = 0;
  while (start < Math.min(before.length, after.length) && before[start] === after[start]) start++;
  while (end < Math.min(before.length, after.length) - start && before[before.length - end - 1] === after[after.length - end - 1]) end++;
  const stop = chars.length - end;
  const from = preview ? Math.max(0, start - 14) : 0;
  const until = preview ? Math.min(chars.length, from + 78) : chars.length;
  return <>{from > 0 && '…'}{visible(chars.slice(from, start).join(''))}
    {stop > start && <mark className={`diff-text-${side}`}>{visible(chars.slice(start, Math.min(stop, until)).join(''))}</mark>}
    {visible(chars.slice(stop, until).join(''))}{until < chars.length && '…'}</>;

}
