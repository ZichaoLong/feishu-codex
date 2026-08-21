import { describe, expect, it } from 'vitest';
import { projectFocusContextUsage } from '../../src/focus/contextUsage';

describe('projectFocusContextUsage', () => {
  it('uses last usage and matches Codex status baseline semantics', () => {
    expect(projectFocusContextUsage({
      total: { totalTokens: 769_096_416 },
      last: { totalTokens: 143_558 },
      modelContextWindow: 258_400,
    }, true)).toEqual({
      usedTokens: 143_558,
      windowTokens: 258_400,
      remainingPercent: 47,
    });
  });

  it('allows context usage to fall after compaction while cumulative usage rises', () => {
    const before = projectFocusContextUsage({
      total: { totalTokens: 773_739_543 },
      last: { totalTokens: 223_211 },
      modelContextWindow: 258_400,
    }, true);
    const after = projectFocusContextUsage({
      total: { totalTokens: 773_968_359 },
      last: { totalTokens: 19_303 },
      modelContextWindow: 258_400,
    }, true);

    expect(before?.remainingPercent).toBe(14);
    expect(after?.remainingPercent).toBe(97);
    expect(after?.usedTokens).toBeLessThan(before?.usedTokens ?? 0);
  });

  it('clamps baseline and exhausted contexts like Codex status', () => {
    expect(projectFocusContextUsage({
      last: { totalTokens: 12_000 },
      modelContextWindow: 258_400,
    }, true)?.remainingPercent).toBe(100);
    expect(projectFocusContextUsage({
      last: { totalTokens: 300_000 },
      modelContextWindow: 258_400,
    }, true)?.remainingPercent).toBe(0);
    expect(projectFocusContextUsage({
      last: { totalTokens: 8_000 },
      modelContextWindow: 8_000,
    }, true)?.remainingPercent).toBe(0);
  });

  it('fails closed without an available last usage and context window', () => {
    expect(projectFocusContextUsage({
      total: { totalTokens: 769_096_416 },
      modelContextWindow: 258_400,
    }, true)).toBeNull();
    expect(projectFocusContextUsage({
      last: { totalTokens: 143_558 },
      modelContextWindow: null,
    }, true)).toBeNull();
    expect(projectFocusContextUsage({
      last: { totalTokens: 143_558 },
      modelContextWindow: 258_400,
    }, false)).toBeNull();
  });
});
