/**
 * Safe local-image policy shared by Markdown renderers.
 *
 * A renderer may opt into resolution only when its backend can authoritatively
 * read the image.  `null` is an explicit, safe refusal (used by Focus Web);
 * an empty string is a failed best-effort resolution and deliberately keeps
 * the original source for legacy Kimi behavior.
 */

const MD_IMG_RE = /(!\[[^\]]*\]\()\s*([^)\s]+)([^)]*\))/g;
// Covers the ordinary double/single-quoted and unquoted HTML forms.  It is
// intentionally not an HTML parser; markstream sanitizes the eventual HTML,
// and this pre-pass only prevents a local path from being handed to <img>.
const HTML_IMG_RE = /(<img\b[^>]*?\bsrc\s*=\s*)(["']?)([^\s>"']+)\2([^>]*>)/gi;

export type LocalImageResolution = string | null;

export function isLocalImageSrc(src: string): boolean {
  return !/^(https?:|data:|blob:)/i.test(src);
}

export function collectLocalImageSources(text: string): string[] {
  const sources: string[] = [];
  for (const re of [MD_IMG_RE, HTML_IMG_RE]) {
    re.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = re.exec(text)) !== null) {
      const source = re === MD_IMG_RE ? (match[2] ?? '') : (match[3] ?? '');
      if (source && isLocalImageSrc(source)) sources.push(source);
    }
  }
  return sources;
}

export function rewriteLocalImageSources(
  text: string,
  options: {
    enabled: boolean;
    resolvedImages: ReadonlyMap<string, LocalImageResolution>;
    unavailableText: string;
  },
): string {
  if (!options.enabled) return text;

  // undefined = resolver has not finished, null = backend explicitly refuses
  // access, '' = legacy resolver failed and asks us to keep the original src.
  const substitute = (source: string): string | null | undefined => {
    if (!isLocalImageSrc(source)) return undefined;
    const resolved = options.resolvedImages.get(source);
    if (resolved === undefined) return IMG_PLACEHOLDER;
    if (resolved === null) return null;
    return resolved === '' ? undefined : resolved;
  };
  const unavailable = options.unavailableText;
  return text
    .replace(MD_IMG_RE, (full, prefix: string, source: string, suffix: string) => {
      const next = substitute(source);
      if (next === undefined) return full;
      return next === null ? unavailable : `${prefix}${next}${suffix}`;
    })
    .replace(HTML_IMG_RE, (full, prefix: string, quote: string, source: string, suffix: string) => {
      const next = substitute(source);
      if (next === undefined) return full;
      return next === null ? unavailable : `${prefix}${quote}${next}${quote}${suffix}`;
    });
}

// 1×1 transparent bitmap accepted by Markstream's image sanitizer.  It is
// used only while an authorized resolver is still in flight, never as a
// substitute for denied server-file access.
export const IMG_PLACEHOLDER = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7';
