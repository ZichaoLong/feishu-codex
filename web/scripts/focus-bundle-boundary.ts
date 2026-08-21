import type { OutputBundle, Plugin } from 'vite';

const FORBIDDEN_FOCUS_MODULE_SEGMENT = '/src/api/daemon/';

export function focusBundleTransportViolations(bundle: OutputBundle): string[] {
  const violations = new Set<string>();
  for (const output of Object.values(bundle)) {
    if (output.type !== 'chunk') continue;
    for (const moduleId of Object.keys(output.modules)) {
      const normalized = moduleId.replaceAll('\\', '/');
      if (normalized.includes(FORBIDDEN_FOCUS_MODULE_SEGMENT)) {
        violations.add(normalized);
      }
    }
  }
  return [...violations].sort();
}

/**
 * Focus owns one browser transport: its same-origin Gateway API. Importing a
 * legacy Kimi daemon module would silently restore a second backend contract
 * and its authentication/lifecycle assumptions.
 */
export function focusBundleBoundaryPlugin(): Plugin {
  return {
    name: 'focus-bundle-transport-boundary',
    generateBundle(_options, bundle) {
      const violations = focusBundleTransportViolations(bundle);
      if (violations.length === 0) return;
      this.error(
        `Focus browser bundle imports forbidden Kimi daemon modules:\n${violations.join('\n')}`,
      );
    },
  };
}
