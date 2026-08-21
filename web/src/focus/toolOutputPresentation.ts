import type { ChatTurn, ToolCall } from '../types';

export const TOOL_OUTPUT_MAX_VISIBLE_CHARS = 65_536;
const TOOL_OUTPUT_HEAD_CHARS = 16_384;
const TOOL_OUTPUT_TAIL_CHARS = 49_152;
export const TOOL_OUTPUT_PAGE_MAX_VISIBLE_CHARS = 262_144;
export const TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS = 16;

export interface BoundedToolOutput {
  lines: string[];
  omittedChars: number;
  headLineCount: number;
}

export function toolOutputCodePointLength(value: string): number {
  let length = 0;
  for (const _character of value) length += 1;
  return length;
}

function codePointSlice(value: string, start: number, end?: number): string {
  return Array.from(value).slice(start, end).join('');
}

export function admitBoundedToolOutputForPage(
  output: BoundedToolOutput,
  remainingChars: number,
  remainingOutputs: number,
): BoundedToolOutput {
  if (output.lines.length === 0) return output;
  const presentedChars = toolOutputCodePointLength(output.lines.join('\n'));
  if (remainingOutputs > 0 && presentedChars <= remainingChars) return output;
  const originalChars = output.omittedChars > 0
    ? TOOL_OUTPUT_MAX_VISIBLE_CHARS + output.omittedChars
    : presentedChars;
  return {
    lines: [],
    omittedChars: originalChars,
    headLineCount: 0,
  };
}

function omissionMarker(omittedChars: number): string {
  return `[Focus Web omitted ${omittedChars} characters of tool output; showing a bounded head and tail.]`;
}

function splitNormalizedOutput(text: string): string[] {
  return text.length === 0 ? [] : text.split('\n');
}

/**
 * Keep live tool output a presentation-sized head/tail window.
 *
 * The rollout and app-server item remain authoritative and lossless. This
 * helper only bounds the Web view that is copied on every stream update.
 */
export function appendBoundedToolOutput(
  currentLines: string[],
  addition: string,
  currentOmittedChars = 0,
  currentHeadLineCount = 0,
): BoundedToolOutput {
  if (addition.length === 0) {
    return {
      lines: currentLines,
      omittedChars: currentOmittedChars,
      headLineCount: currentHeadLineCount,
    };
  }

  if (currentOmittedChars <= 0) {
    const text = `${currentLines.join('\n')}${addition}`;
    const textLength = toolOutputCodePointLength(text);
    if (textLength <= TOOL_OUTPUT_MAX_VISIBLE_CHARS) {
      return {
        lines: splitNormalizedOutput(text),
        omittedChars: 0,
        headLineCount: 0,
      };
    }
    const omittedChars = textLength - TOOL_OUTPUT_HEAD_CHARS - TOOL_OUTPUT_TAIL_CHARS;
    const head = splitNormalizedOutput(codePointSlice(text, 0, TOOL_OUTPUT_HEAD_CHARS));
    return {
      lines: [
        ...head,
        omissionMarker(omittedChars),
        ...splitNormalizedOutput(codePointSlice(text, -TOOL_OUTPUT_TAIL_CHARS)),
      ],
      omittedChars,
      headLineCount: head.length,
    };
  }

  if (currentHeadLineCount === 0 && currentLines.length === 0) {
    return {
      lines: [],
      omittedChars: currentOmittedChars + toolOutputCodePointLength(addition),
      headLineCount: 0,
    };
  }

  const markerIndex = currentHeadLineCount;
  if (
    markerIndex <= 0
    || markerIndex >= currentLines.length
    || currentLines[markerIndex] !== omissionMarker(currentOmittedChars)
  ) {
    // A malformed or older projection must not make output unbounded. Rebound
    // its visible data without inferring an internal boundary from any
    // command-authored marker-like line.
    return appendBoundedToolOutput(currentLines, addition, 0, 0);
  }
  const headText = currentLines.slice(0, markerIndex).join('\n');
  const appendedTail = `${currentLines.slice(markerIndex + 1).join('\n')}${addition}`;
  const appendedTailLength = toolOutputCodePointLength(appendedTail);
  const newlyOmitted = Math.max(appendedTailLength - TOOL_OUTPUT_TAIL_CHARS, 0);
  const boundedTail = newlyOmitted > 0
    ? codePointSlice(appendedTail, -TOOL_OUTPUT_TAIL_CHARS)
    : appendedTail;
  const omittedChars = currentOmittedChars + newlyOmitted;
  const head = splitNormalizedOutput(headText);
  return {
    lines: [
      ...head,
      omissionMarker(omittedChars),
      ...splitNormalizedOutput(boundedTail),
    ],
    omittedChars,
    headLineCount: head.length,
  };
}

function boundedTool(
  tool: ToolCall,
  remainingChars: number,
  remainingOutputs: number,
): { tool: ToolCall; consumedChars: number; consumedOutputs: number } {
  const admitted = admitBoundedToolOutputForPage(
    {
      lines: tool.output ?? [],
      omittedChars: tool.outputOmittedChars ?? 0,
      headLineCount: tool.outputHeadLineCount ?? 0,
    },
    remainingChars,
    remainingOutputs,
  );
  const result: ToolCall = { ...tool, output: admitted.lines };
  if (admitted.omittedChars > 0) {
    result.outputTruncated = true;
    result.outputOmittedChars = admitted.omittedChars;
    result.outputHeadLineCount = admitted.headLineCount;
  } else {
    delete result.outputTruncated;
    delete result.outputOmittedChars;
    delete result.outputHeadLineCount;
  }
  if (result.diff && admitted.omittedChars > 0 && admitted.lines.length === 0) {
    result.diff = {
      ...result.diff,
      lines: [],
      omittedChars: admitted.omittedChars,
      omissionLineIndex: 0,
    };
  }
  const consumedChars = admitted.lines.length > 0
    ? toolOutputCodePointLength(admitted.lines.join('\n'))
    : 0;
  return {
    tool: result,
    consumedChars,
    consumedOutputs: admitted.lines.length > 0 ? 1 : 0,
  };
}

function orderedUniqueTools(turn: ChatTurn): ToolCall[] {
  const blockTools = (turn.blocks ?? []).flatMap((block) => (
    block.kind === 'tool' ? [block.tool] : []
  ));
  const orderedTools: ToolCall[] = [];
  const seen = new Set<string>();
  for (const tool of [...blockTools, ...(turn.tools ?? [])]) {
    if (seen.has(tool.id)) continue;
    seen.add(tool.id);
    orderedTools.push(tool);
  }
  return orderedTools;
}

function toolOutputPresentationsMatch(left: ToolCall, right: ToolCall): boolean {
  const leftOutput = left.output ?? [];
  const rightOutput = right.output ?? [];
  const leftDiff = left.diff;
  const rightDiff = right.diff;
  const diffsMatch = leftDiff === undefined && rightDiff === undefined
    ? true
    : leftDiff !== undefined
      && rightDiff !== undefined
      && leftDiff.path === rightDiff.path
      && leftDiff.omittedChars === rightDiff.omittedChars
      && leftDiff.omissionLineIndex === rightDiff.omissionLineIndex
      && leftDiff.lines.length === rightDiff.lines.length
      && leftDiff.lines.every((line, index) => {
        const mirrored = rightDiff.lines[index];
        return mirrored !== undefined
          && line.type === mirrored.type
          && line.text === mirrored.text
          && line.oldNo === mirrored.oldNo
          && line.newNo === mirrored.newNo;
      });
  return leftOutput.length === rightOutput.length
    && leftOutput.every((line, index) => line === rightOutput[index])
    && left.outputOmittedChars === right.outputOmittedChars
    && left.outputHeadLineCount === right.outputHeadLineCount
    && left.outputTruncated === right.outputTruncated
    && diffsMatch;
}

/** Reapply the page aggregate after any snapshot or merge that can change tool presentation. */
export function boundToolOutputWindow(turns: readonly ChatTurn[]): ChatTurn[] {
  let remainingChars = TOOL_OUTPUT_PAGE_MAX_VISIBLE_CHARS;
  let remainingOutputs = TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS;
  return turns.map((turn) => {
    const orderedTools = orderedUniqueTools(turn);
    if (orderedTools.length === 0) return turn;

    const boundedById = new Map<string, ToolCall>();
    for (const tool of orderedTools) {
      const admitted = boundedTool(tool, remainingChars, remainingOutputs);
      boundedById.set(tool.id, admitted.tool);
      remainingChars = Math.max(remainingChars - admitted.consumedChars, 0);
      remainingOutputs = Math.max(remainingOutputs - admitted.consumedOutputs, 0);
    }
    return {
      ...turn,
      ...(turn.tools === undefined
        ? {}
        : { tools: turn.tools.map((tool) => boundedById.get(tool.id) ?? tool) }),
      ...(turn.blocks === undefined
        ? {}
        : {
            blocks: turn.blocks.map((block) => (
              block.kind === 'tool'
                ? { kind: 'tool' as const, tool: boundedById.get(block.tool.id) ?? block.tool }
                : block
            )),
          }),
    };
  });
}

/** Validate the same logical union that rebudgeting uses, without double-counting mirrors. */
export function toolOutputWindowFitsAggregate(turns: readonly ChatTurn[]): boolean {
  let chars = 0;
  let outputs = 0;
  for (const turn of turns) {
    const byId = new Map<string, ToolCall>();
    for (const tool of [
      ...(turn.blocks ?? []).flatMap((block) => (
        block.kind === 'tool' ? [block.tool] : []
      )),
      ...(turn.tools ?? []),
    ]) {
      const mirrored = byId.get(tool.id);
      if (mirrored && !toolOutputPresentationsMatch(mirrored, tool)) return false;
      byId.set(tool.id, tool);
    }
    for (const tool of orderedUniqueTools(turn)) {
      const lines = tool.output ?? [];
      if (lines.length === 0) continue;
      chars += toolOutputCodePointLength(lines.join('\n'));
      outputs += 1;
      if (
        chars > TOOL_OUTPUT_PAGE_MAX_VISIBLE_CHARS
        || outputs > TOOL_OUTPUT_PAGE_MAX_VISIBLE_OUTPUTS
      ) return false;
    }
  }
  return true;
}
