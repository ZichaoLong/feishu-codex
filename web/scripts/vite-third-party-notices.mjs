import path from 'node:path';
import {
  buildThirdPartyArtifacts,
  writeCanonicalNoticeCopies,
  writeThirdPartyArtifacts,
} from './third-party-notices.mjs';

/**
 * Writes stable, non-hashed legal artifacts after Vite has emitted every
 * JavaScript and worker chunk.  Their contents are derived from chunk.modules,
 * while the Gateway deliberately gives these filenames a no-store policy.
 */
export function focusThirdPartyNoticesPlugin() {
  const renderedModuleIds = new Set();
  let outputDirectory = '';

  return {
    name: 'focus-third-party-notices',
    apply: 'build',
    generateBundle(outputOptions, bundle) {
      if (outputOptions.dir) outputDirectory = path.resolve(outputOptions.dir);
      for (const output of Object.values(bundle)) {
        if (output.type !== 'chunk') continue;
        for (const moduleId of Object.keys(output.modules)) renderedModuleIds.add(moduleId);
      }
    },
    closeBundle() {
      if (renderedModuleIds.size === 0) {
        throw new Error('Focus third-party notice generation saw no rendered Rollup modules.');
      }
      if (!outputDirectory) {
        throw new Error('Focus third-party notice generation did not receive a Vite output directory.');
      }
      const artifacts = buildThirdPartyArtifacts([...renderedModuleIds]);
      writeThirdPartyArtifacts(artifacts, outputDirectory);
      writeCanonicalNoticeCopies(artifacts);
    },
  };
}
