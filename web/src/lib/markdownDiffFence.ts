// A fenced `diff` in assistant Markdown keeps its complete source for copying,
// but mounts only a fixed presentation window. This is a DOM-cost boundary,
// not a mutation of the transcript or of the copy payload.

export const MARKDOWN_DIFF_HEAD_LINE_COUNT = 25;
export const MARKDOWN_DIFF_TAIL_LINE_COUNT = 25;

export type MarkdownDiffSourceRowType = 'add' | 'del' | 'hunk' | 'ctx';

export interface MarkdownDiffSourceRow {
  type: MarkdownDiffSourceRowType;
  sign: string;
  text: string;
}

export interface MarkdownDiffOmissionRow {
  type: 'omission';
  sign: '';
  text: '';
  omittedLineCount: number;
}

export type MarkdownDiffPresentationRow = MarkdownDiffSourceRow | MarkdownDiffOmissionRow;
export type MarkdownDiffCopyWriter = (source: string) => Promise<boolean>;

function sourceRow(line: string): MarkdownDiffSourceRow {
  if (line.startsWith('@@')) return { type: 'hunk', sign: '', text: line };
  if (/^\+(?!\+\+)/.test(line)) return { type: 'add', sign: '+', text: line.slice(1) };
  if (/^-(?!--)/.test(line)) return { type: 'del', sign: '-', text: line.slice(1) };
  if (line.startsWith(' ')) return { type: 'ctx', sign: '', text: line.slice(1) };
  return { type: 'ctx', sign: '', text: line };
}

export function buildMarkdownDiffPresentationRows(
  source: string,
): readonly MarkdownDiffPresentationRow[] {
  const lines = source.split('\n');
  const visibleLineCount = MARKDOWN_DIFF_HEAD_LINE_COUNT + MARKDOWN_DIFF_TAIL_LINE_COUNT;
  if (lines.length <= visibleLineCount) return lines.map(sourceRow);

  return [
    ...lines.slice(0, MARKDOWN_DIFF_HEAD_LINE_COUNT).map(sourceRow),
    {
      type: 'omission',
      sign: '',
      text: '',
      omittedLineCount: lines.length - visibleLineCount,
    } satisfies MarkdownDiffOmissionRow,
    ...lines.slice(-MARKDOWN_DIFF_TAIL_LINE_COUNT).map(sourceRow),
  ];
}

export function copyCompleteMarkdownDiffFence(
  source: string,
  write: MarkdownDiffCopyWriter,
): Promise<boolean> {
  return write(source);
}
