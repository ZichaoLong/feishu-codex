import {
  clearKaTeXWorker,
  clearMermaidWorker,
  disableKatex,
  disableMermaid,
  enableKatex,
  enableMermaid,
  setKaTeXWorker,
  setMermaidWorker,
} from 'markstream-vue';
import { containsFocusMarkdownMath } from './markdownMath';

export interface MarkdownRuntimeFeatures {
  katex: boolean;
  mermaid: boolean;
}

type CapabilityState = 'idle' | 'loading' | 'ready' | 'failed';

let katexState: CapabilityState = 'idle';
let mermaidState: CapabilityState = 'idle';
let katexLoad: Promise<boolean> | null = null;
let mermaidLoad: Promise<boolean> | null = null;

// markstream ships optional peer loaders enabled by default. Disable them at
// the Focus integration boundary so ordinary prose cannot pull KaTeX or
// Mermaid onto the main thread before an explicit feature request has prepared
// its worker. A failed preparation remains disabled and renders source text.
disableKatex();
disableMermaid();
clearKaTeXWorker();
clearMermaidWorker();

/** Detect only the heavy syntax that Focus intentionally supports. */
export function detectMarkdownRuntimeFeatures(text: string): MarkdownRuntimeFeatures {
  const features: MarkdownRuntimeFeatures = {
    katex: containsFocusMarkdownMath(text),
    mermaid: false,
  };
  let fence: { marker: '`' | '~'; length: number } | null = null;

  for (const line of String(text ?? '').split(/\r?\n/)) {
    if (fence) {
      const trimmed = line.replace(/^ {0,3}/, '');
      let markerLength = 0;
      while (trimmed[markerLength] === fence.marker) markerLength += 1;
      if (markerLength >= fence.length && trimmed.slice(markerLength).trim() === '') {
        fence = null;
      }
      continue;
    }

    const opening = /^(?: {0,3})(`{3,}|~{3,})[ \t]*([^ \t]*)/.exec(line);
    if (opening) {
      const markerRun = opening[1] ?? '';
      fence = {
        marker: markerRun.startsWith('`') ? '`' : '~',
        length: markerRun.length,
      };
      if ((opening[2] ?? '').toLowerCase() === 'mermaid') features.mermaid = true;
    }

    if (features.katex && features.mermaid) break;
  }
  return features;
}

function prepareKatex(): Promise<boolean> {
  if (katexState === 'ready' || katexState === 'failed') return Promise.resolve(false);
  if (katexLoad) return katexLoad;
  katexState = 'loading';
  katexLoad = (async () => {
    try {
      const [workerModule] = await Promise.all([
        import('markstream-vue/workers/katexRenderer.worker?worker&type=module'),
        import('katex/dist/katex.min.css'),
      ]);
      const worker = new workerModule.default();
      clearKaTeXWorker();
      setKaTeXWorker(worker);
      enableKatex();
      katexState = 'ready';
      return true;
    } catch {
      disableKatex();
      clearKaTeXWorker();
      katexState = 'failed';
      return false;
    } finally {
      katexLoad = null;
    }
  })();
  return katexLoad;
}

function prepareMermaid(): Promise<boolean> {
  if (mermaidState === 'ready' || mermaidState === 'failed') return Promise.resolve(false);
  if (mermaidLoad) return mermaidLoad;
  mermaidState = 'loading';
  mermaidLoad = (async () => {
    try {
      const workerModule = await import(
        'markstream-vue/workers/mermaidParser.worker?worker&type=module'
      );
      const worker = new workerModule.default();
      clearMermaidWorker();
      setMermaidWorker(worker);
      enableMermaid();
      mermaidState = 'ready';
      return true;
    } catch {
      disableMermaid();
      clearMermaidWorker();
      mermaidState = 'failed';
      return false;
    } finally {
      mermaidLoad = null;
    }
  })();
  return mermaidLoad;
}

/**
 * Prepare only the capabilities referenced by this Markdown source. The
 * boolean is true when global renderer state changed and mounted renderers
 * should remount once to upgrade their source fallback.
 */
export async function prepareMarkdownRuntime(text: string): Promise<boolean> {
  const features = detectMarkdownRuntimeFeatures(text);
  const changes = await Promise.all([
    features.katex ? prepareKatex() : Promise.resolve(false),
    features.mermaid ? prepareMermaid() : Promise.resolve(false),
  ]);
  return changes.some(Boolean);
}
