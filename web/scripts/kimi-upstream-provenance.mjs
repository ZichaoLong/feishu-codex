/*
 * Verify the Kimi-derived portion of Focus Web against the exact Git commit
 * recorded in provenance/kimi-web-files.json.
 *
 * This is intentionally separate from `npm run build`: release builds use
 * checked-in license evidence and must not require a developer's local Kimi
 * checkout.  A source comparison, however, should use Kimi's Git object
 * database so it is insensitive to an uncommitted upstream worktree.
 */

import crypto from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const WEB_ROOT = path.resolve(path.dirname(SCRIPT_PATH), '..');
const MANIFEST_PATH = path.join(WEB_ROOT, 'provenance', 'kimi-web-files.json');
const UPSTREAM_DOC_PATH = path.join(WEB_ROOT, 'UPSTREAM.md');
const HEX_SHA256 = /^[0-9a-f]{64}$/;
const GIT_COMMIT = /^[0-9a-f]{40,64}$/;

function fail(message) {
  throw new Error(message);
}

function sha256(bytes) {
  return crypto.createHash('sha256').update(bytes).digest('hex');
}

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf8'));
  } catch (error) {
    fail(`Cannot read ${path.relative(WEB_ROOT, filePath)}: ${error instanceof Error ? error.message : String(error)}`);
  }
}

function assertRelativePath(value, label) {
  if (typeof value !== 'string' || !value) fail(`${label} must be a non-empty POSIX-relative path.`);
  if (value.includes('\\') || path.posix.normalize(value) !== value || value === '.' || value.startsWith('../') || path.posix.isAbsolute(value)) {
    fail(`${label} is not a safe POSIX-relative path: ${JSON.stringify(value)}.`);
  }
  return value;
}

function pathIn(root, relative, label) {
  const safe = assertRelativePath(relative, label);
  const absolute = path.resolve(root, safe);
  if (absolute !== root && !absolute.startsWith(`${root}${path.sep}`)) {
    fail(`${label} escapes its declared root.`);
  }
  return absolute;
}

function readRegularFile(root, relative, label) {
  const filePath = pathIn(root, relative, label);
  const stat = fs.statSync(filePath, { throwIfNoEntry: false });
  if (!stat?.isFile()) fail(`${label} is missing or is not a regular file: ${relative}.`);
  return fs.readFileSync(filePath);
}

function listRegularFiles(root, relative, label) {
  const entryPath = pathIn(root, relative, label);
  const stat = fs.lstatSync(entryPath, { throwIfNoEntry: false });
  if (!stat) fail(`${label} is missing: ${relative}.`);
  if (stat.isSymbolicLink()) fail(`${label} must not be a symbolic link: ${relative}.`);
  if (stat.isFile()) return [relative];
  if (!stat.isDirectory()) fail(`${label} is neither a regular file nor a directory: ${relative}.`);

  const files = [];
  for (const entry of fs.readdirSync(entryPath, { withFileTypes: true }).sort((left, right) => left.name.localeCompare(right.name))) {
    const child = path.posix.join(relative, entry.name);
    if (entry.isSymbolicLink()) fail(`${label} contains a symbolic link: ${child}.`);
    if (entry.isFile()) files.push(child);
    else if (entry.isDirectory()) files.push(...listRegularFiles(root, child, label));
    else fail(`${label} contains an unsupported filesystem entry: ${child}.`);
  }
  return files;
}

function assertSortedUnique(values, label) {
  let previous = '';
  for (const value of values) {
    if (typeof value !== 'string') fail(`${label} contains a non-string value.`);
    if (previous && previous >= value) fail(`${label} must be strictly sorted and unique.`);
    previous = value;
  }
}

function assertOnlyKeys(value, allowed, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail(`${label} must be an object.`);
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) fail(`${label} contains an unsupported field: ${key}.`);
  }
}

function loadManifest() {
  const manifest = readJson(MANIFEST_PATH);
  assertOnlyKeys(manifest, new Set(['format', 'format_version', 'upstream', 'scope', 'files', 'focus_owned_files']), 'Provenance manifest');
  if (manifest.format !== 'focus-kimi-web-provenance' || manifest.format_version !== 1) {
    fail('Unsupported Kimi provenance manifest format. Update the guard together with the manifest format.');
  }

  const upstream = manifest.upstream;
  assertOnlyKeys(upstream, new Set([
    'name', 'source_url', 'source_root', 'imported_commit', 'imported_on',
    'package_version', 'license', 'license_evidence', 'license_sha256',
  ]), 'Manifest upstream');
  for (const field of ['name', 'source_url', 'source_root', 'imported_commit', 'imported_on', 'package_version', 'license', 'license_evidence', 'license_sha256']) {
    if (typeof upstream[field] !== 'string' || !upstream[field]) fail(`Manifest upstream.${field} must be a non-empty string.`);
  }
  assertRelativePath(upstream.source_root, 'Manifest upstream.source_root');
  assertRelativePath(upstream.license_evidence, 'Manifest upstream.license_evidence');
  if (!GIT_COMMIT.test(upstream.imported_commit)) fail('Manifest upstream.imported_commit must be a Git object id.');
  if (!HEX_SHA256.test(upstream.license_sha256)) fail('Manifest upstream.license_sha256 must be a SHA-256 digest.');
  if (upstream.license !== 'MIT') fail(`Unexpected Kimi source license: ${upstream.license}.`);

  const scope = manifest.scope;
  assertOnlyKeys(scope, new Set(['local_roots']), 'Manifest scope');
  if (!Array.isArray(scope.local_roots) || scope.local_roots.length === 0) fail('Manifest scope.local_roots must be a non-empty array.');
  for (const localRoot of scope.local_roots) assertRelativePath(localRoot, 'Manifest scope.local_roots entry');
  assertSortedUnique(scope.local_roots, 'Manifest scope.local_roots');

  if (!manifest.files || typeof manifest.files !== 'object' || Array.isArray(manifest.files)) {
    fail('Manifest files must be a non-empty object mapping a local path to null or a Focus SHA-256 digest.');
  }
  const files = Object.entries(manifest.files).map(([recordPath, focusSha256]) => {
    const safePath = assertRelativePath(recordPath, 'Manifest files path');
    if (focusSha256 !== null && (typeof focusSha256 !== 'string' || !HEX_SHA256.test(focusSha256))) {
      fail(`Manifest files[${JSON.stringify(recordPath)}] must be null or a SHA-256 digest.`);
    }
    return { path: safePath, focus_sha256: focusSha256 || undefined };
  });
  if (files.length === 0) fail('Manifest files must not be empty.');
  assertSortedUnique(files.map((record) => record.path), 'Manifest files paths');

  if (!Array.isArray(manifest.focus_owned_files)) fail('Manifest focus_owned_files must be an array.');
  const focusOwnedFiles = manifest.focus_owned_files.map((entry, index) => assertRelativePath(entry, `Manifest focus_owned_files[${index}]`));
  assertSortedUnique(focusOwnedFiles, 'Manifest focus_owned_files');
  const overlap = files.map((record) => record.path).filter((entry) => focusOwnedFiles.includes(entry));
  if (overlap.length > 0) fail(`A file cannot be both Kimi-derived and Focus-owned: ${overlap.join(', ')}.`);

  const localLicense = readRegularFile(WEB_ROOT, upstream.license_evidence, 'Kimi license evidence');
  if (sha256(localLicense) !== upstream.license_sha256) {
    fail(`Kimi MIT evidence digest does not match manifest upstream.license_sha256: ${upstream.license_evidence}.`);
  }

  const sourceFiles = scope.local_roots.flatMap((localRoot) => listRegularFiles(WEB_ROOT, localRoot, 'Manifest source scope')).sort();
  const inventory = [...files.map((record) => record.path), ...focusOwnedFiles].sort();
  if (sourceFiles.join('\n') !== inventory.join('\n')) {
    const missingFromManifest = sourceFiles.filter((entry) => !inventory.includes(entry));
    const missingFromScope = inventory.filter((entry) => !sourceFiles.includes(entry));
    const details = [
      missingFromManifest.length > 0 ? `unclassified local files: ${missingFromManifest.join(', ')}` : '',
      missingFromScope.length > 0 ? `missing local files: ${missingFromScope.join(', ')}` : '',
    ].filter(Boolean).join('; ');
    fail(`Kimi provenance inventory does not exactly cover its source scope (${details}).`);
  }

  return { manifest, upstream, files, focusOwnedFiles };
}

function git(repository, args, label) {
  const repositoryPath = path.resolve(repository);
  if (!fs.statSync(repositoryPath, { throwIfNoEntry: false })?.isDirectory()) {
    fail(`--upstream must name a Kimi Git checkout directory: ${repository}.`);
  }
  try {
    return execFileSync('git', ['-C', repositoryPath, ...args], {
      encoding: 'buffer',
      stdio: ['ignore', 'pipe', 'pipe'],
      maxBuffer: 16 * 1024 * 1024,
    });
  } catch (error) {
    const stderr = Buffer.isBuffer(error?.stderr) ? error.stderr.toString('utf8').trim() : '';
    fail(`${label}: ${stderr || (error instanceof Error ? error.message : String(error))}`);
  }
}

function gitText(repository, args, label) {
  return git(repository, args, label).toString('utf8').trim();
}

function sourceFileSpec(upstream, relativePath) {
  return `${upstream.imported_commit}:${upstream.source_root}/${relativePath}`;
}

function assertUpstreamCheckout(upstream, repository) {
  const resolved = gitText(
    repository,
    ['rev-parse', '--verify', `${upstream.imported_commit}^{commit}`],
    `Kimi checkout does not contain imported commit ${upstream.imported_commit}`,
  );
  if (resolved !== upstream.imported_commit) {
    fail(`Kimi checkout resolved ${upstream.imported_commit} to unexpected object ${resolved}.`);
  }

  const packageBytes = git(repository, ['show', sourceFileSpec(upstream, 'package.json')], 'Cannot read Kimi kimi-web package.json');
  let upstreamPackage;
  try {
    upstreamPackage = JSON.parse(packageBytes.toString('utf8'));
  } catch (error) {
    fail(`Kimi kimi-web package.json is invalid JSON: ${error instanceof Error ? error.message : String(error)}`);
  }
  if (upstreamPackage.version !== upstream.package_version) {
    fail(`Kimi package version is ${String(upstreamPackage.version)}, expected ${upstream.package_version}.`);
  }

  const upstreamLicense = git(repository, ['show', `${upstream.imported_commit}:LICENSE`], 'Cannot read Kimi LICENSE');
  if (sha256(upstreamLicense) !== upstream.license_sha256) {
    fail('Kimi LICENSE at the recorded commit does not match checked-in MIT evidence.');
  }
}

function verify(manifestData, repository) {
  const { upstream, files, focusOwnedFiles } = manifestData;
  assertUpstreamCheckout(upstream, repository);

  let modifiedCount = 0;
  for (const record of files) {
    const local = readRegularFile(WEB_ROOT, record.path, 'Kimi-derived Focus file');
    const source = git(repository, ['show', sourceFileSpec(upstream, record.path)], `Cannot read recorded Kimi source for ${record.path}`);
    if (record.focus_sha256) {
      modifiedCount += 1;
      if (sha256(local) !== record.focus_sha256) {
        fail(`${record.path} no longer matches its recorded Focus modification digest. Review it, then run npm run sync:kimi-provenance -- --upstream <kimi-code-checkout>.`);
      }
      if (local.equals(source)) {
        fail(`${record.path} is now identical to Kimi but remains marked as Focus-modified. Run npm run sync:kimi-provenance -- --upstream <kimi-code-checkout>.`);
      }
    } else if (!local.equals(source)) {
      fail(`${record.path} diverges from Kimi without a recorded Focus modification digest. Review it, then run npm run sync:kimi-provenance -- --upstream <kimi-code-checkout>.`);
    }
  }

  const upstreamText = fs.readFileSync(UPSTREAM_DOC_PATH, 'utf8');
  for (const evidence of [upstream.source_url, upstream.imported_commit, upstream.imported_on, upstream.license_evidence]) {
    if (!upstreamText.includes(evidence)) fail(`UPSTREAM.md is missing provenance evidence: ${evidence}.`);
  }

  return { kimiFiles: files.length, modifiedFiles: modifiedCount, focusFiles: focusOwnedFiles.length };
}

function canonicalManifest(manifestData, repository) {
  const { manifest, upstream, files, focusOwnedFiles } = manifestData;
  assertUpstreamCheckout(upstream, repository);
  const refreshedFiles = files.map((record) => {
    const local = readRegularFile(WEB_ROOT, record.path, 'Kimi-derived Focus file');
    const source = git(repository, ['show', sourceFileSpec(upstream, record.path)], `Cannot read recorded Kimi source for ${record.path}`);
    return [record.path, local.equals(source) ? null : sha256(local)];
  });
  return {
    format: manifest.format,
    format_version: manifest.format_version,
    upstream: manifest.upstream,
    scope: manifest.scope,
    files: Object.fromEntries(refreshedFiles),
    focus_owned_files: focusOwnedFiles,
  };
}

function usage() {
  return [
    'Usage:',
    '  node scripts/kimi-upstream-provenance.mjs --verify --upstream /path/to/kimi-code',
    '  node scripts/kimi-upstream-provenance.mjs --refresh --upstream /path/to/kimi-code',
    '',
    '--verify requires the recorded Git commit and compares every listed source file.',
    '--refresh updates only recorded Focus-modification digests; it never adds paths or changes the import commit.',
  ].join('\n');
}

function parseArgs(argv) {
  let action = '';
  let upstream = '';
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--verify' || argument === '--refresh') {
      if (action) fail(`Choose only one action.\n${usage()}`);
      action = argument.slice(2);
      continue;
    }
    if (argument === '--upstream') {
      upstream = argv[index + 1] || '';
      index += 1;
      continue;
    }
    if (argument.startsWith('--upstream=')) {
      upstream = argument.slice('--upstream='.length);
      continue;
    }
    if (argument === '--help' || argument === '-h') return { help: true };
    fail(`Unknown argument: ${argument}.\n${usage()}`);
  }
  if (!action || !upstream) fail(`${!upstream ? '--upstream is required.' : 'Choose an action.'}\n${usage()}`);
  return { action, upstream };
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  if (options.help) {
    console.log(usage());
    return;
  }
  const manifestData = loadManifest();
  if (options.action === 'verify') {
    const summary = verify(manifestData, options.upstream);
    console.log(
      `kimi-upstream-provenance: verified ${summary.kimiFiles} Kimi-derived files `
      + `(${summary.modifiedFiles} Focus-modified) and ${summary.focusFiles} Focus-owned files.`,
    );
    return;
  }

  const refreshed = `${JSON.stringify(canonicalManifest(manifestData, options.upstream), null, 2)}\n`;
  const current = fs.readFileSync(MANIFEST_PATH, 'utf8');
  if (current === refreshed) {
    console.log('kimi-upstream-provenance: manifest already synchronized.');
    return;
  }
  fs.writeFileSync(MANIFEST_PATH, refreshed, 'utf8');
  console.log('kimi-upstream-provenance: refreshed manifest; review its diff before committing.');
}

if (process.argv[1] && path.resolve(process.argv[1]) === SCRIPT_PATH) {
  try {
    main();
  } catch (error) {
    console.error(`kimi-upstream-provenance: ${error instanceof Error ? error.message : String(error)}`);
    process.exitCode = 1;
  }
}
