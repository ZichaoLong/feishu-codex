/*
 * Build the browser-delivery notice set from two auditable inputs:
 *
 *   1. the rendered Rollup module graph supplied by vite-third-party-notices;
 *   2. the exact npm resolutions in package-lock.json.
 *
 * A rendered package pulls in its declared runtime dependency closure as a
 * conservative guard for pre-bundled vendor code.  This deliberately creates
 * a small superset of modules the browser executes, rather than silently
 * losing an upstream notice because a package published pre-built code.
 *
 * This module has no network dependency.  A clean `npm ci` plus the checked-in
 * Kimi and icon-license evidence is sufficient to reproduce the release
 * artifacts.
 */

import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_VERSION = 2;
const SCRIPT_PATH = fileURLToPath(import.meta.url);
const WEB_ROOT = path.resolve(path.dirname(SCRIPT_PATH), '..');
const REPO_ROOT = path.resolve(WEB_ROOT, '..');
const LOCKFILE_PATH = path.join(WEB_ROOT, 'package-lock.json');
const NODE_MODULES_ROOT = path.join(WEB_ROOT, 'node_modules');
const DIST_ROOT = path.join(REPO_ROOT, 'bot', 'web_assets', 'dist');
const SOURCE_NOTICE_PATH = path.join(WEB_ROOT, 'THIRD_PARTY_NOTICES.md');
const PACKAGED_NOTICE_PATH = path.join(REPO_ROOT, 'bot', 'web_assets', 'THIRD_PARTY_NOTICES.md');
const KIMI_PROVENANCE_PATH = path.join(WEB_ROOT, 'provenance', 'kimi-web-files.json');

export const NOTICE_FILENAMES = Object.freeze({
  markdown: 'THIRD_PARTY_NOTICES.md',
  html: 'THIRD_PARTY_NOTICES.html',
  inventory: 'THIRD_PARTY_SBOM.json',
});

// These assets are generated or copied by Vite plugins instead of appearing
// as ordinary JS module paths.  Keep the reason explicit so a future icon or
// font replacement cannot disappear from the legal inventory unnoticed.
const EXPLICIT_ASSET_PACKAGES = Object.freeze([
  {
    name: '@fontsource-variable/inter',
    reason: 'self-hosted Inter webfont CSS and WOFF2 assets',
  },
  {
    name: '@fontsource-variable/jetbrains-mono',
    reason: 'self-hosted JetBrains Mono webfont CSS and WOFF2 assets',
  },
  {
    name: '@iconify-json/ri',
    reason: 'Remix Icon SVG data inlined by unplugin-icons',
  },
  {
    name: '@iconify-json/tabler',
    reason: 'Tabler Icon SVG data inlined by unplugin-icons',
  },
]);

// The Iconify JSON packages intentionally ship metadata and icon data rather
// than their upstream license files.  Pin the corresponding evidence here.
// Remix is Apache-2.0 according to the *shipped* @iconify-json/ri@1.2.10
// package's package.json and info.json.  Do not substitute the license of a
// newer Remix Icon checkout when auditing this locked artifact.
const PACKAGE_LICENSE_OVERRIDES = new Map([
  [
    'node_modules/@iconify-json/ri',
    {
      license: 'Apache-2.0',
      licenseEvidence: 'licenses/apache-2.0.txt',
      attribution: 'Remix Icon 4.8.0 — Remix Design (author recorded by the shipped Iconify metadata)',
      evidence: '@iconify-json/ri@1.2.10 package.json and info.json',
    },
  ],
  [
    'node_modules/@iconify-json/tabler',
    {
      license: 'MIT',
      licenseEvidence: 'licenses/tabler-icons-MIT.txt',
      attribution: 'Tabler Icons 3.45.0 — Copyright (c) 2020-2026 Paweł Kuna',
      evidence: '@iconify-json/tabler@1.2.37 package.json and info.json',
    },
  ],
]);

const KNOWN_LICENSE_EXPRESSIONS = new Set([
  'MIT',
  'Apache-2.0',
  'BSD-2-Clause',
  'BSD-3-Clause',
  'ISC',
  'OFL-1.1',
  'Unlicense',
  'MPL-2.0 OR Apache-2.0',
]);

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function normalizedText(value) {
  // License files copied from upstream occasionally contain trailing spaces.
  // They create noisy diffs in the generated notice bundle without carrying
  // legal content, so normalize them along with line endings.
  return String(value)
    .replace(/\r\n?/g, '\n')
    .replace(/[ \t]+(?=\n)/g, '')
    .replace(/\n*$/, '\n');
}

function readText(filePath) {
  return normalizedText(fs.readFileSync(filePath, 'utf8'));
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, 'utf8'));
}

function loadKimiUpstream() {
  const provenance = readJson(KIMI_PROVENANCE_PATH);
  const upstream = provenance?.upstream;
  if (provenance?.format !== 'focus-kimi-web-provenance' || provenance?.format_version !== 1 || !upstream) {
    throw new Error('web/provenance/kimi-web-files.json has an unsupported provenance format.');
  }
  for (const field of [
    'name', 'package_version', 'license', 'source_url', 'imported_commit',
    'imported_on', 'license_evidence', 'license_sha256',
  ]) {
    if (typeof upstream[field] !== 'string' || !upstream[field]) {
      throw new Error(`web/provenance/kimi-web-files.json is missing upstream.${field}.`);
    }
  }
  if (upstream.license !== 'MIT') {
    throw new Error(`Kimi source provenance must retain MIT, received ${upstream.license}.`);
  }
  return {
    name: upstream.name,
    version: upstream.package_version,
    license: upstream.license,
    sourceUrl: upstream.source_url,
    importedCommit: upstream.imported_commit,
    importedOn: upstream.imported_on,
    licenseEvidence: upstream.license_evidence,
    // SHA-256 of kimi-code/LICENSE at the imported commit.  It prevents a
    // local edit from turning the copied evidence into unverifiable attribution.
    licenseSha256: upstream.license_sha256,
  };
}

// Third-party notices and per-file provenance intentionally use one upstream
// record.  Updating an import cannot silently update one notice while leaving
// the source inventory pointing at a different Kimi commit.
const KIMI_UPSTREAM = Object.freeze(loadKimiUpstream());

function posixPath(value) {
  return value.split(path.sep).join('/');
}

function packageKeyForName(name) {
  return `node_modules/${name}`;
}

function packageNameFromLockKey(lockKey) {
  const marker = 'node_modules/';
  const index = lockKey.lastIndexOf(marker);
  if (index < 0) {
    throw new Error(`Not an npm package lock key: ${lockKey}`);
  }
  const suffix = lockKey.slice(index + marker.length);
  const parts = suffix.split('/');
  if (parts[0]?.startsWith('@')) {
    if (!parts[1]) throw new Error(`Invalid scoped npm package lock key: ${lockKey}`);
    return `${parts[0]}/${parts[1]}`;
  }
  if (!parts[0]) throw new Error(`Invalid npm package lock key: ${lockKey}`);
  return parts[0];
}

function normalizeLicense(value) {
  const normalized = String(value || '').trim().replace(/^\((.*)\)$/, '$1');
  return normalized.replace(/\s+/g, ' ');
}

function detectLicenseFromText(value) {
  if (/Mozilla Public License\s+Version 2\.0/i.test(value)) return 'MPL-2.0';
  if (/Apache License\s+Version 2\.0/i.test(value)) return 'Apache-2.0';
  if (/SIL OPEN FONT LICENSE/i.test(value)) return 'OFL-1.1';
  if (/\bThe Unlicense\b/i.test(value)) return 'Unlicense';
  if (/BSD 3-Clause License/i.test(value)) return 'BSD-3-Clause';
  if (/BSD 2-Clause License/i.test(value)) return 'BSD-2-Clause';
  if (/\bISC License\b/i.test(value)) return 'ISC';
  if (/\bMIT License\b|\bThe MIT License\b/i.test(value)) return 'MIT';
  return '';
}

function repositoryUrl(value) {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object' && typeof value.url === 'string') return value.url;
  return '';
}

function packageLicenseFiles(packageDir) {
  return fs.readdirSync(packageDir, { withFileTypes: true })
    // Do not use a broad "license-*" glob here.  Cytoscape, for example,
    // publishes license-update.mjs as a maintenance script beside LICENSE;
    // including that source code would obscure the actual legal text.
    .filter((entry) => entry.isFile() && /^(?:licen[cs]e|copying|notice)(?:[-_][a-z0-9]+(?:[-_][a-z0-9]+)*)?(?:\.(?:md|txt))?$/i.test(entry.name))
    .map((entry) => entry.name)
    .sort((left, right) => left.localeCompare(right));
}

function readPackageLicenseDocuments(packageDir, packageDisplayName) {
  return packageLicenseFiles(packageDir).map((name) => ({
    source: `${packageDisplayName}/${name}`,
    text: readText(path.join(packageDir, name)),
  }));
}

function packageDirectoryForKey(lockKey) {
  const packageDir = path.join(WEB_ROOT, lockKey);
  if (!fs.statSync(packageDir, { throwIfNoEntry: false })?.isDirectory()) {
    throw new Error(
      `${lockKey} is present in package-lock.json but absent from node_modules. `
      + 'Run npm ci before building the distributable Web assets.',
    );
  }
  return packageDir;
}

function findPackageRootForModule(moduleId, lockPackages) {
  // Vite's CommonJS transform prefixes a real absolute file path with NUL.
  // It is still third-party browser code, so strip only that virtual marker
  // before resolving its owning package; other virtual ids stay explicit.
  const cleanId = String(moduleId).replace(/^\0/, '').split('?')[0];
  if (!path.isAbsolute(cleanId)) return '';
  let candidate = cleanId;
  if (!fs.statSync(candidate, { throwIfNoEntry: false })?.isDirectory()) {
    candidate = path.dirname(candidate);
  }
  const nodeModulesPrefix = `${NODE_MODULES_ROOT}${path.sep}`;
  while (candidate.startsWith(nodeModulesPrefix)) {
    const relative = posixPath(path.relative(WEB_ROOT, candidate));
    if (lockPackages[relative] && fs.existsSync(path.join(candidate, 'package.json'))) {
      return relative;
    }
    const parent = path.dirname(candidate);
    if (parent === candidate) break;
    candidate = parent;
  }
  return '';
}

function resolveDependencyKey(lockPackages, parentKey, dependencyName) {
  let packageKey = parentKey;
  while (true) {
    const nestedCandidate = `${packageKey}/node_modules/${dependencyName}`;
    if (lockPackages[nestedCandidate]) return nestedCandidate;
    const nestedMarker = packageKey.lastIndexOf('/node_modules/');
    if (nestedMarker < 0) break;
    packageKey = packageKey.slice(0, nestedMarker);
  }
  const rootCandidate = packageKeyForName(dependencyName);
  return lockPackages[rootCandidate] ? rootCandidate : '';
}

function virtualModulePackage(moduleId) {
  const id = String(moduleId);
  if (/vite\/(?:modulepreload|preload)|\0vite\//.test(id)) return 'vite';
  if (/unplugin-icons|~icons\//.test(id)) return 'unplugin-icons';
  if (/plugin-vue/.test(id)) return '@vitejs/plugin-vue';
  if (/commonjsHelpers/.test(id)) return 'rollup';
  return '';
}

class LicenseRegistry {
  #documentsByDigest = new Map();
  #documents = [];

  add(document) {
    const text = normalizedText(document.text);
    const digest = sha256(text);
    let current = this.#documentsByDigest.get(digest);
    if (!current) {
      current = {
        id: `L${String(this.#documents.length + 1).padStart(3, '0')}`,
        sha256: digest,
        text,
        sources: new Set(),
      };
      this.#documentsByDigest.set(digest, current);
      this.#documents.push(current);
    }
    current.sources.add(document.source);
    return current.id;
  }

  toInventory() {
    return this.#documents.map((document) => ({
      id: document.id,
      sha256: document.sha256,
      sources: [...document.sources].sort(),
    }));
  }

  toRendered() {
    return this.#documents.map((document) => ({
      ...document,
      sources: [...document.sources].sort(),
    }));
  }
}

function buildCanonicalLicenseLookup(lockPackages) {
  const lookup = new Map();
  for (const [lockKey, lockRecord] of Object.entries(lockPackages).sort(([left], [right]) => left.localeCompare(right))) {
    if (!lockKey || !lockKey.startsWith('node_modules/')) continue;
    const license = normalizeLicense(lockRecord.license);
    if (!license) continue;
    const packageDir = path.join(WEB_ROOT, lockKey);
    if (!fs.statSync(packageDir, { throwIfNoEntry: false })?.isDirectory()) continue;
    const packageDisplayName = `${packageNameFromLockKey(lockKey)}@${lockRecord.version || 'unknown'}`;
    const documents = readPackageLicenseDocuments(packageDir, packageDisplayName);
    for (const document of documents) {
      const detected = detectLicenseFromText(document.text);
      const candidates = new Set([license, detected]);
      for (const part of license.split(/\s+OR\s+/)) candidates.add(part);
      for (const candidate of candidates) {
        if (!candidate || lookup.has(candidate)) continue;
        if (!detected || candidate === detected || license === candidate) {
          lookup.set(candidate, document);
        }
      }
    }
  }
  return lookup;
}

function fallbackLicenseDocuments(license, canonicalLicenseLookup) {
  const terms = license.split(/\s+OR\s+/);
  const documents = [];
  for (const term of terms) {
    const document = canonicalLicenseLookup.get(term);
    if (!document) {
      throw new Error(
        `No checked-in or installed full license text is available for ${license}. `
        + 'Add a reviewed license-evidence override before publishing.',
      );
    }
    documents.push({
      source: `${document.source} (canonical ${term} text used because this package ships no license file)`,
      text: document.text,
    });
  }
  return documents;
}

function componentLicenseDocuments({ lockKey, license, packageDir, packageDisplayName, canonicalLicenseLookup }) {
  const packagedDocuments = readPackageLicenseDocuments(packageDir, packageDisplayName);
  if (packagedDocuments.length > 0) return packagedDocuments;
  const override = PACKAGE_LICENSE_OVERRIDES.get(lockKey);
  if (override?.licenseEvidence) {
    return [{
      source: override.licenseEvidence,
      text: readText(path.join(WEB_ROOT, override.licenseEvidence)),
    }];
  }
  return fallbackLicenseDocuments(license, canonicalLicenseLookup);
}

function componentEvidence(component) {
  const evidence = [];
  if (component.bundledModuleCount > 0) {
    evidence.push(`rendered Rollup module graph (${component.bundledModuleCount})`);
  }
  evidence.push(...component.explicitReasons);
  if (component.declaredDependencyOf.size > 0) evidence.push('declared runtime dependency closure');
  return evidence.join('; ');
}

function componentAttribution(component) {
  if (component.attribution) return component.attribution;
  if (component.author) return component.author;
  return '';
}

function markdownTableCell(value) {
  return String(value)
    .replaceAll('|', '\\|')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('\n', ' ');
}

function renderMarkdown({ lockfileSha256, renderedModuleCount, components, documents }) {
  const lines = [
    '# Focus Web Third-Party Notices',
    '',
    '> Generated by `web/scripts/third-party-notices.mjs`; do not edit this file by hand.',
    '',
    '## Build provenance',
    '',
    `- Generator version: ${SCRIPT_VERSION}`,
    `- npm lockfile: \`web/package-lock.json\` (SHA-256 \`${lockfileSha256}\`)`,
    `- Rendered Rollup module identifiers inspected: ${renderedModuleCount}`,
    `- kimi-web source: ${KIMI_UPSTREAM.sourceUrl} at \`${KIMI_UPSTREAM.importedCommit}\` (package ${KIMI_UPSTREAM.version}, imported ${KIMI_UPSTREAM.importedOn})`,
    '',
    'The component table starts with the copied kimi-web source.  For npm code, it records',
    'all packages reached from the rendered module graph plus a declared runtime-dependency',
    'closure.  The closure is intentional: published browser packages can embed third-party',
    'code that no longer has a separate Rollup module identifier.  Entries explicitly marked',
    'as fonts or icons are emitted by build tooling rather than normal JavaScript imports.',
    '',
    '## Components',
    '',
    '| Component | Version | License | Attribution | Included because |',
    '| --- | --- | --- | --- | --- |',
  ];

  for (const component of components) {
    lines.push(
      `| ${markdownTableCell(component.name)} | ${markdownTableCell(component.version)} | ${markdownTableCell(component.license)} | ${markdownTableCell(componentAttribution(component))} | ${markdownTableCell(componentEvidence(component))} |`,
    );
  }

  lines.push('', '## License and notice texts', '');
  for (const document of documents) {
    lines.push(`### ${document.id}`, '');
    lines.push(`Sources: ${document.sources.map((source) => `\`${source}\``).join(', ')}`, '');
    lines.push('```text', document.text.trimEnd(), '```', '');
  }
  return `${lines.join('\n').trimEnd()}\n`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function renderHtml(markdown) {
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="robots" content="noindex" />
    <title>Focus Web Third-Party Notices</title>
  </head>
  <body>
    <main>
      <h1>Focus Web Third-Party Notices</h1>
      <p>This document is generated for the exact browser bundle. <a href="/THIRD_PARTY_SBOM.json">Machine-readable inventory</a></p>
      <pre>${escapeHtml(markdown)}</pre>
    </main>
  </body>
</html>
`;
}

function assertUpstreamEvidence() {
  const evidencePath = path.join(WEB_ROOT, KIMI_UPSTREAM.licenseEvidence);
  const evidence = readText(evidencePath);
  if (sha256(evidence) !== KIMI_UPSTREAM.licenseSha256) {
    throw new Error(
      `${KIMI_UPSTREAM.licenseEvidence} no longer matches kimi-code ${KIMI_UPSTREAM.importedCommit} LICENSE evidence.`,
    );
  }
  const upstreamDocument = readText(path.join(WEB_ROOT, 'UPSTREAM.md'));
  if (!upstreamDocument.includes(KIMI_UPSTREAM.importedCommit)) {
    throw new Error('web/UPSTREAM.md must retain the Kimi imported commit used by the notice generator.');
  }
  return evidence;
}

function loadPackageComponent({ lockKey, state, lockPackages, canonicalLicenseLookup }) {
  const lockRecord = lockPackages[lockKey];
  if (!lockRecord) throw new Error(`Package ${lockKey} is not pinned in package-lock.json.`);
  const packageDir = packageDirectoryForKey(lockKey);
  const packageManifest = readJson(path.join(packageDir, 'package.json'));
  const packageName = String(packageManifest.name || packageNameFromLockKey(lockKey));
  const packageVersion = String(packageManifest.version || '');
  if (!packageVersion || packageVersion !== String(lockRecord.version || '')) {
    throw new Error(
      `${lockKey} (${packageVersion || 'unknown'}) does not match its lockfile version `
      + `${String(lockRecord.version || 'unknown')}; run npm ci before building.`,
    );
  }
  const override = PACKAGE_LICENSE_OVERRIDES.get(lockKey);
  const lockLicense = normalizeLicense(lockRecord.license);
  const manifestLicense = normalizeLicense(packageManifest.license);
  let license = normalizeLicense(override?.license || lockLicense || manifestLicense);
  const packagedDocuments = readPackageLicenseDocuments(packageDir, `${packageName}@${packageVersion}`);
  if (!license && packagedDocuments.length > 0) {
    license = detectLicenseFromText(packagedDocuments[0].text);
  }
  if (!license || !KNOWN_LICENSE_EXPRESSIONS.has(license)) {
    throw new Error(
      `${lockKey} has an unreviewed license expression ${JSON.stringify(license || lockLicense || manifestLicense)}. `
      + 'Add reviewed support before publishing.',
    );
  }
  if (lockLicense && manifestLicense && lockLicense !== manifestLicense) {
    throw new Error(`${lockKey} has inconsistent package-lock and package.json licenses.`);
  }
  const documents = componentLicenseDocuments({
    lockKey,
    license,
    packageDir,
    packageDisplayName: `${packageName}@${packageVersion}`,
    canonicalLicenseLookup,
  });
  return {
    key: `npm:${lockKey}`,
    lockKey,
    name: packageName,
    version: packageVersion,
    license,
    integrity: String(lockRecord.integrity || ''),
    repository: repositoryUrl(packageManifest.repository),
    homepage: String(packageManifest.homepage || ''),
    author: typeof packageManifest.author === 'string' ? packageManifest.author : '',
    attribution: override?.attribution || '',
    sourceEvidence: override?.evidence || 'package-lock.json plus installed package metadata',
    documents,
    bundledModuleCount: state.bundledModuleCount,
    explicitReasons: [...state.explicitReasons].sort(),
    declaredDependencyOf: state.declaredDependencyOf,
  };
}

/**
 * Return deterministic browser notices for a set of rendered Rollup module
 * identifiers.  `moduleIds` must come from output chunks, not from a source
 * directory walk, so dead/disabled frontend code cannot become a fake runtime
 * dependency root.
 */
export function buildThirdPartyArtifacts(moduleIds) {
  const lockfileBytes = fs.readFileSync(LOCKFILE_PATH);
  const lockfileSha256 = sha256(lockfileBytes);
  const lockfile = JSON.parse(lockfileBytes.toString('utf8'));
  if (lockfile.lockfileVersion !== 3 || !lockfile.packages || !lockfile.packages['']) {
    throw new Error('web/package-lock.json must be an npm lockfile v3 with package records.');
  }
  const lockPackages = lockfile.packages;
  const canonicalLicenseLookup = buildCanonicalLicenseLookup(lockPackages);
  const states = new Map();
  const unresolvedVirtualModules = new Set();
  const renderedIds = [...new Set(moduleIds.map((moduleId) => String(moduleId)))].sort();

  const markPackage = (lockKey, {
    explicitReason = '',
    bundled = false,
    declaredDependencyOf = '',
    expandDependencies = false,
  } = {}) => {
    if (!lockPackages[lockKey]) throw new Error(`Bundle component ${lockKey} is missing from package-lock.json.`);
    let state = states.get(lockKey);
    if (!state) {
      state = {
        bundledModuleCount: 0,
        explicitReasons: new Set(),
        declaredDependencyOf: new Set(),
        expandDependencies: false,
      };
      states.set(lockKey, state);
    }
    if (bundled) state.bundledModuleCount += 1;
    if (explicitReason) state.explicitReasons.add(explicitReason);
    if (declaredDependencyOf) state.declaredDependencyOf.add(declaredDependencyOf);
    if (expandDependencies) state.expandDependencies = true;
  };

  for (const moduleId of renderedIds) {
    const packageKey = findPackageRootForModule(moduleId, lockPackages);
    if (packageKey) {
      markPackage(packageKey, { bundled: true, expandDependencies: true });
      continue;
    }
    if (moduleId.startsWith('\0') || moduleId.startsWith('virtual:') || moduleId.startsWith('~icons/')) {
      const virtualPackage = virtualModulePackage(moduleId);
      if (virtualPackage) {
        markPackage(packageKeyForName(virtualPackage), {
          explicitReason: `browser helper generated by ${virtualPackage}`,
        });
      } else {
        unresolvedVirtualModules.add(moduleId);
      }
    }
  }

  if (unresolvedVirtualModules.size > 0) {
    throw new Error(
      'Unattributed virtual browser modules were rendered: '
      + `${[...unresolvedVirtualModules].sort().join(', ')}. Add explicit license ownership before publishing.`,
    );
  }

  for (const asset of EXPLICIT_ASSET_PACKAGES) {
    markPackage(packageKeyForName(asset.name), { explicitReason: asset.reason });
  }

  // Walk declared runtime dependencies from every concrete bundle root.  Do
  // not walk optional dependencies: if one actually contributes browser code,
  // it is already a rendered-module root above.  This avoids platform-only
  // binaries leaking into a browser distribution report.
  const pending = [...states.keys()].sort();
  const expanded = new Set();
  while (pending.length > 0) {
    const parentKey = pending.shift();
    if (!parentKey || expanded.has(parentKey)) continue;
    expanded.add(parentKey);
    if (!states.get(parentKey)?.expandDependencies) continue;
    const dependencies = lockPackages[parentKey]?.dependencies || {};
    for (const dependencyName of Object.keys(dependencies).sort()) {
      const dependencyKey = resolveDependencyKey(lockPackages, parentKey, dependencyName);
      if (!dependencyKey) {
        throw new Error(`${parentKey} declares ${dependencyName}, but package-lock.json cannot resolve it.`);
      }
      const wasKnown = states.has(dependencyKey);
      markPackage(dependencyKey, { declaredDependencyOf: parentKey });
      markPackage(dependencyKey, { expandDependencies: true });
      if (!wasKnown) pending.push(dependencyKey);
    }
    pending.sort();
  }

  const registry = new LicenseRegistry();
  const kimiLicense = assertUpstreamEvidence();
  const kimiComponent = {
    key: `source:kimi-web@${KIMI_UPSTREAM.version}`,
    name: KIMI_UPSTREAM.name,
    version: `${KIMI_UPSTREAM.version} (${KIMI_UPSTREAM.importedCommit.slice(0, 12)})`,
    license: KIMI_UPSTREAM.license,
    integrity: KIMI_UPSTREAM.licenseSha256,
    repository: KIMI_UPSTREAM.sourceUrl,
    homepage: '',
    author: 'Moonshot AI',
    attribution: 'Copyright (c) 2026 Moonshot AI',
    sourceEvidence: `Imported source commit ${KIMI_UPSTREAM.importedCommit}`,
    documents: [{ source: KIMI_UPSTREAM.licenseEvidence, text: kimiLicense }],
    bundledModuleCount: 0,
    explicitReasons: new Set(['upstream-derived Focus Web source']),
    declaredDependencyOf: new Set(),
  };

  const packageComponents = [...states.entries()]
    .map(([lockKey, state]) => loadPackageComponent({
      lockKey,
      state,
      lockPackages,
      canonicalLicenseLookup,
    }))
    .sort((left, right) => left.name.localeCompare(right.name) || left.version.localeCompare(right.version));

  const components = [kimiComponent, ...packageComponents].map((component) => ({
    ...component,
    licenseDocumentIds: component.documents.map((document) => registry.add(document)),
    declaredDependencyOf: new Set(component.declaredDependencyOf),
    explicitReasons: [...component.explicitReasons].sort(),
  }));
  const documents = registry.toRendered();
  const markdown = renderMarkdown({
    lockfileSha256,
    renderedModuleCount: renderedIds.length,
    components,
    documents,
  });
  const html = renderHtml(markdown);
  const inventory = {
    format: 'focus-web-third-party-inventory',
    format_version: 1,
    generator: {
      path: 'web/scripts/third-party-notices.mjs',
      version: SCRIPT_VERSION,
    },
    build: {
      lockfile: 'web/package-lock.json',
      lockfile_sha256: lockfileSha256,
      rendered_rollup_module_count: renderedIds.length,
    },
    artifacts: {
      notice_markdown: {
        path: NOTICE_FILENAMES.markdown,
        sha256: sha256(markdown),
      },
      notice_html: {
        path: NOTICE_FILENAMES.html,
        sha256: sha256(html),
      },
    },
    upstream: {
      name: KIMI_UPSTREAM.name,
      source_url: KIMI_UPSTREAM.sourceUrl,
      imported_commit: KIMI_UPSTREAM.importedCommit,
      imported_on: KIMI_UPSTREAM.importedOn,
      source_license_sha256: KIMI_UPSTREAM.licenseSha256,
    },
    components: components.map((component) => ({
      type: component.key.startsWith('source:') ? 'source-derived' : 'npm',
      name: component.name,
      version: component.version,
      license: component.license,
      license_document_ids: component.licenseDocumentIds,
      lock_key: component.lockKey || '',
      integrity: component.integrity,
      repository: component.repository,
      homepage: component.homepage,
      author: component.author,
      attribution: component.attribution,
      source_evidence: component.sourceEvidence,
      rendered_rollup_module_count: component.bundledModuleCount,
      explicit_asset_reasons: component.explicitReasons,
      declared_dependency_of: [...component.declaredDependencyOf].sort(),
    })),
    license_documents: registry.toInventory(),
  };
  const inventoryText = `${JSON.stringify(inventory, null, 2)}\n`;

  return {
    markdown,
    html,
    inventory: inventoryText,
    files: {
      [NOTICE_FILENAMES.markdown]: markdown,
      [NOTICE_FILENAMES.html]: html,
      [NOTICE_FILENAMES.inventory]: inventoryText,
    },
  };
}

export function writeThirdPartyArtifacts(artifacts, distRoot = DIST_ROOT) {
  fs.mkdirSync(distRoot, { recursive: true });
  for (const [fileName, content] of Object.entries(artifacts.files)) {
    fs.writeFileSync(path.join(distRoot, fileName), content, 'utf8');
  }
}

export function writeCanonicalNoticeCopies(artifacts) {
  fs.writeFileSync(SOURCE_NOTICE_PATH, artifacts.markdown, 'utf8');
  fs.writeFileSync(PACKAGED_NOTICE_PATH, artifacts.markdown, 'utf8');
}

function assertArtifact(pathToArtifact) {
  if (!fs.statSync(pathToArtifact, { throwIfNoEntry: false })?.isFile()) {
    throw new Error(`Missing generated third-party artifact: ${pathToArtifact}`);
  }
}

export function verifyBuiltThirdPartyArtifacts(distRoot = DIST_ROOT) {
  const markdownPath = path.join(distRoot, NOTICE_FILENAMES.markdown);
  const htmlPath = path.join(distRoot, NOTICE_FILENAMES.html);
  const inventoryPath = path.join(distRoot, NOTICE_FILENAMES.inventory);
  for (const artifactPath of [markdownPath, htmlPath, inventoryPath]) assertArtifact(artifactPath);

  const markdown = fs.readFileSync(markdownPath, 'utf8');
  const html = fs.readFileSync(htmlPath, 'utf8');
  const inventory = readJson(inventoryPath);
  if (inventory.format !== 'focus-web-third-party-inventory' || inventory.format_version !== 1) {
    throw new Error('Unexpected third-party inventory format. Rebuild Focus Web assets.');
  }
  const lockfileSha256 = sha256(fs.readFileSync(LOCKFILE_PATH));
  if (inventory.build?.lockfile_sha256 !== lockfileSha256) {
    throw new Error('Third-party inventory does not match the current package-lock.json. Rebuild Focus Web assets.');
  }
  if (inventory.artifacts?.notice_markdown?.sha256 !== sha256(markdown)) {
    throw new Error('Third-party Markdown notice does not match its inventory digest. Rebuild Focus Web assets.');
  }
  if (inventory.artifacts?.notice_html?.sha256 !== sha256(html)) {
    throw new Error('Third-party HTML notice does not match its inventory digest. Rebuild Focus Web assets.');
  }
  if (fs.readFileSync(SOURCE_NOTICE_PATH, 'utf8') !== markdown) {
    throw new Error('web/THIRD_PARTY_NOTICES.md is not synchronized with the shipped browser notice.');
  }
  if (fs.readFileSync(PACKAGED_NOTICE_PATH, 'utf8') !== markdown) {
    throw new Error('bot/web_assets/THIRD_PARTY_NOTICES.md is not synchronized with the shipped browser notice.');
  }

  const componentLicenses = new Map(inventory.components.map((component) => [component.name, component.license]));
  for (const [name, license] of [
    ['kimi-web', 'MIT'],
    ['@fontsource-variable/inter', 'OFL-1.1'],
    ['@fontsource-variable/jetbrains-mono', 'OFL-1.1'],
    ['@iconify-json/ri', 'Apache-2.0'],
    ['@iconify-json/tabler', 'MIT'],
    ['katex', 'MIT'],
    ['mermaid', 'MIT'],
    ['vue', 'MIT'],
  ]) {
    if (componentLicenses.get(name) !== license) {
      throw new Error(`Third-party inventory is missing ${name} (${license}).`);
    }
  }
  if (!markdown.includes('Copyright (c) 2026 Moonshot AI')
      || !markdown.includes('SIL OPEN FONT LICENSE')
      || !markdown.includes('Apache License')
      || !markdown.includes('Remix Icon 4.8.0')
      || !markdown.includes('Tabler Icons 3.45.0')) {
    throw new Error('Third-party notice is missing required Kimi, OFL, or Apache license evidence.');
  }
  if (markdown.includes('license-update.mjs')) {
    throw new Error('Third-party notice included a package maintenance script instead of a license document.');
  }
  if (!html.includes('/THIRD_PARTY_SBOM.json')) {
    throw new Error('Third-party HTML notice must link to the stable machine-readable inventory.');
  }
  return inventory;
}

function main() {
  if (process.argv.includes('--verify')) {
    const inventory = verifyBuiltThirdPartyArtifacts();
    console.log(
      `third-party-notices: verified ${inventory.components.length} components and `
      + `${inventory.license_documents.length} license documents.`,
    );
    return;
  }
  throw new Error('This generator is invoked by Vite. Use `npm run check:notices` to verify built artifacts.');
}

if (process.argv[1] && path.resolve(process.argv[1]) === SCRIPT_PATH) {
  try {
    main();
  } catch (error) {
    console.error(`third-party-notices: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  }
}
