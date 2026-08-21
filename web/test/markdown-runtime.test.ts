import { beforeEach, describe, expect, it, vi } from 'vitest';

const markstream = vi.hoisted(() => ({
  clearKaTeXWorker: vi.fn(),
  clearMermaidWorker: vi.fn(),
  disableKatex: vi.fn(),
  disableMermaid: vi.fn(),
  enableKatex: vi.fn(),
  enableMermaid: vi.fn(),
  normalizeStandaloneBackslashT: vi.fn((value: string) => value),
  setDefaultMathOptions: vi.fn(),
  setKaTeXWorker: vi.fn(),
  setMermaidWorker: vi.fn(),
}));
const workerConstructors = vi.hoisted(() => ({
  katex: vi.fn(),
  mermaid: vi.fn(),
}));

vi.mock('markstream-vue', async (importOriginal) => ({
  ...await importOriginal<typeof import('markstream-vue')>(),
  ...markstream,
}));
vi.mock('katex/dist/katex.min.css', () => ({}));
vi.mock('markstream-vue/workers/katexRenderer.worker?worker&type=module', () => ({
  default: workerConstructors.katex,
}));
vi.mock('markstream-vue/workers/mermaidParser.worker?worker&type=module', () => ({
  default: workerConstructors.mermaid,
}));

describe('Markdown optional runtime', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.resetModules();
  });

  it('does not construct a worker for ordinary Markdown', async () => {
    const { prepareMarkdownRuntime } = await import('../src/lib/markdownRuntime');

    await expect(prepareMarkdownRuntime('# Hello\n\nplain **prose**')).resolves.toBe(false);

    expect(workerConstructors.katex).not.toHaveBeenCalled();
    expect(workerConstructors.mermaid).not.toHaveBeenCalled();
    expect(markstream.enableKatex).not.toHaveBeenCalled();
    expect(markstream.enableMermaid).not.toHaveBeenCalled();
  });

  it('detects Mermaid fences, including an incomplete streaming fence', async () => {
    const { detectMarkdownRuntimeFeatures } = await import('../src/lib/markdownRuntime');

    expect(detectMarkdownRuntimeFeatures('Mermaid is a diagram tool.').mermaid).toBe(false);
    expect(detectMarkdownRuntimeFeatures('`mermaid`')).toEqual({ katex: false, mermaid: false });
    expect(detectMarkdownRuntimeFeatures('```ts\nconst mermaid = true\n```')).toEqual({
      katex: false,
      mermaid: false,
    });
    expect(detectMarkdownRuntimeFeatures('```mermaid\ngraph TD')).toEqual({
      katex: false,
      mermaid: true,
    });
    expect(detectMarkdownRuntimeFeatures('~~~MERMAID\ngraph TD\n~~~')).toEqual({
      katex: false,
      mermaid: true,
    });
  });

  it('detects the exact Focus math grammar outside code', async () => {
    const { detectMarkdownRuntimeFeatures } = await import('../src/lib/markdownRuntime');

    expect(detectMarkdownRuntimeFeatures('$PATH costs $5')).toEqual({ katex: false, mermaid: false });
    expect(detectMarkdownRuntimeFeatures('$x$ and before $$x$$ after')).toEqual({
      katex: false,
      mermaid: false,
    });
    expect(detectMarkdownRuntimeFeatures('`$$ not math $$`')).toEqual({ katex: false, mermaid: false });
    expect(detectMarkdownRuntimeFeatures(String.raw`\\(x\\) and \\[y\\]`)).toEqual({
      katex: false,
      mermaid: false,
    });
    expect(detectMarkdownRuntimeFeatures('```sh\necho $$\n```')).toEqual({ katex: false, mermaid: false });
    expect(detectMarkdownRuntimeFeatures('```ts\nconst value = String.raw`\\(x\\)`\n```')).toEqual({
      katex: false,
      mermaid: false,
    });
    expect(detectMarkdownRuntimeFeatures(String.raw`before \(x^2\) after`)).toEqual({
      katex: true,
      mermaid: false,
    });
    expect(detectMarkdownRuntimeFeatures(String.raw`\[x^2\]`)).toEqual({
      katex: true,
      mermaid: false,
    });
    expect(detectMarkdownRuntimeFeatures('Before\n\n$$x^2$$')).toEqual({ katex: true, mermaid: false });
    expect(detectMarkdownRuntimeFeatures('    $$indented$$')).toEqual({ katex: false, mermaid: false });
    expect(detectMarkdownRuntimeFeatures('`open\n\\(not math\\)\nclose`')).toEqual({
      katex: false,
      mermaid: false,
    });
  });

  it('initializes requested workers once and keeps capabilities independent', async () => {
    const { prepareMarkdownRuntime } = await import('../src/lib/markdownRuntime');

    const firstMermaid = prepareMarkdownRuntime('```mermaid\ngraph TD');
    const secondMermaid = prepareMarkdownRuntime('```mermaid\ngraph LR');
    await expect(Promise.all([firstMermaid, secondMermaid])).resolves.toEqual([true, true]);
    await expect(prepareMarkdownRuntime('```mermaid\ngraph BT')).resolves.toBe(false);

    expect(workerConstructors.mermaid).toHaveBeenCalledTimes(1);
    expect(markstream.setMermaidWorker).toHaveBeenCalledTimes(1);
    expect(markstream.enableMermaid).toHaveBeenCalledTimes(1);
    expect(workerConstructors.katex).not.toHaveBeenCalled();

    await expect(prepareMarkdownRuntime('$$x^2$$')).resolves.toBe(true);
    expect(workerConstructors.katex).toHaveBeenCalledTimes(1);
    expect(markstream.setKaTeXWorker).toHaveBeenCalledTimes(1);
    expect(markstream.enableKatex).toHaveBeenCalledTimes(1);

    await expect(prepareMarkdownRuntime('$x$ before $$not a block$$ after')).resolves.toBe(false);
    expect(workerConstructors.katex).toHaveBeenCalledTimes(1);
    expect(markstream.enableKatex).toHaveBeenCalledTimes(1);
  });

  it('keeps source fallback disabled after a worker fails instead of retrying forever', async () => {
    workerConstructors.mermaid.mockImplementationOnce(() => {
      throw new Error('worker unavailable');
    });
    const { prepareMarkdownRuntime } = await import('../src/lib/markdownRuntime');

    await expect(prepareMarkdownRuntime('```mermaid\ngraph TD')).resolves.toBe(false);
    await expect(prepareMarkdownRuntime('```mermaid\ngraph LR')).resolves.toBe(false);

    expect(workerConstructors.mermaid).toHaveBeenCalledTimes(1);
    expect(markstream.enableMermaid).not.toHaveBeenCalled();
    expect(markstream.disableMermaid).toHaveBeenCalledTimes(2);
  });

  it('keeps math source fallback disabled after the KaTeX worker fails', async () => {
    workerConstructors.katex.mockImplementationOnce(() => {
      throw new Error('worker unavailable');
    });
    const { prepareMarkdownRuntime } = await import('../src/lib/markdownRuntime');

    await expect(prepareMarkdownRuntime(String.raw`\(x^2\)`)).resolves.toBe(false);
    await expect(prepareMarkdownRuntime('$$y^2$$')).resolves.toBe(false);

    expect(workerConstructors.katex).toHaveBeenCalledTimes(1);
    expect(markstream.enableKatex).not.toHaveBeenCalled();
    expect(markstream.disableKatex).toHaveBeenCalledTimes(2);
  });
});
