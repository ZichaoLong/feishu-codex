import type { FocusTokenUsage } from './types';

// Match Codex `/status`: fixed prompts, tools, and compaction headroom are not
// counted as user-controllable context capacity. Pinned upstream evidence:
// https://github.com/openai/codex/blob/be6e8eac029b183056b7e4402879f15d2c85f61b/codex-rs/protocol/src/protocol.rs#L2226-L2271
const CODEX_CONTEXT_BASELINE_TOKENS = 12_000;

export interface FocusContextUsage {
  usedTokens: number;
  windowTokens: number;
  remainingPercent: number;
}

/** Project app-server token usage into the current-context meter. */
export function projectFocusContextUsage(
  tokenUsage: FocusTokenUsage | null | undefined,
  available: boolean,
): FocusContextUsage | null {
  const usedTokens = tokenUsage?.last?.totalTokens;
  const windowTokens = tokenUsage?.modelContextWindow;
  if (
    !available
    || typeof usedTokens !== 'number'
    || !Number.isSafeInteger(usedTokens)
    || usedTokens < 0
    || typeof windowTokens !== 'number'
    || !Number.isSafeInteger(windowTokens)
    || windowTokens <= 0
  ) {
    return null;
  }

  if (windowTokens <= CODEX_CONTEXT_BASELINE_TOKENS) {
    return { usedTokens, windowTokens, remainingPercent: 0 };
  }
  const effectiveWindow = windowTokens - CODEX_CONTEXT_BASELINE_TOKENS;
  const effectiveUsed = Math.max(usedTokens - CODEX_CONTEXT_BASELINE_TOKENS, 0);
  const remainingTokens = Math.max(effectiveWindow - effectiveUsed, 0);
  const remainingPercent = Math.round(
    Math.min(100, Math.max(0, (remainingTokens / effectiveWindow) * 100)),
  );
  return { usedTokens, windowTokens, remainingPercent };
}
