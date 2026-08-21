#!/usr/bin/env node
// Enforce the reviewed Focus browser owner direction without persisting a graph.

import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { parse as parseVueSfc } from '@vue/compiler-sfc';
import ts from 'typescript';

const SCRIPT_EXTENSIONS = new Set([
  '.cjs', '.cts', '.js', '.jsx', '.mjs', '.mts', '.ts', '.tsx', '.vue',
]);
const RESOLUTION_EXTENSIONS = [
  '.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.vue',
  '.json', '.css', '.svg',
];
const TEST_FILE = /\.(?:spec|test)\.[cm]?[jt]sx?$/;
const LAYERS = ['clientState', 'mutations', 'composition', 'presentation'];
const LAYER_LABELS = Object.freeze({
  clientState: 'client-state',
  mutations: 'mutations',
  composition: 'composition',
  presentation: 'presentation',
});
const FORBIDDEN_TARGETS = new Map([
  ['clientState', new Set(['mutations', 'composition', 'presentation'])],
  ['mutations', new Set(['composition', 'presentation'])],
]);

function compareCodePoint(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function boundaries(clientState, mutations) {
  return Object.freeze({
    clientState: Object.freeze(clientState),
    mutations: Object.freeze(mutations),
    composition: Object.freeze({
      files: Object.freeze(['main.ts', 'focus/useFocusWebClient.ts']),
      directories: Object.freeze([]),
    }),
  });
}

export const ACTIVE_FOCUS_BOUNDARIES = boundaries(
  { files: Object.freeze([]), directories: Object.freeze(['focus/client-state']) },
  { files: Object.freeze([]), directories: Object.freeze(['focus/mutations']) },
);

export class DependencyDirectionError extends Error {}

function posixRelative(root, candidate) {
  return path.relative(root, candidate).split(path.sep).join('/');
}

function normalizedSelector(value, kind) {
  if (
    typeof value !== 'string'
    || value.length === 0
    || value.startsWith('/')
    || value.includes('\\')
    || value.split('/').includes('..')
    || path.posix.normalize(value) !== value
    || (kind === 'directory' && value.endsWith('/'))
  ) {
    throw new DependencyDirectionError(`invalid ${kind} selector: ${JSON.stringify(value)}`);
  }
  return value;
}

function validateBoundaries(value) {
  if (!value || typeof value !== 'object') {
    throw new DependencyDirectionError('boundaries must be an object');
  }
  const keys = Object.keys(value).sort();
  if (keys.join(',') !== 'clientState,composition,mutations') {
    throw new DependencyDirectionError(`unexpected boundary layers: ${keys.join(', ')}`);
  }
  for (const layer of keys) {
    const selectors = value[layer];
    if (
      !selectors
      || !Array.isArray(selectors.files)
      || !Array.isArray(selectors.directories)
      || Object.keys(selectors).sort().join(',') !== 'directories,files'
    ) {
      throw new DependencyDirectionError(`invalid selectors for ${layer}`);
    }
    const files = selectors.files.map((item) => normalizedSelector(item, 'file'));
    const directories = selectors.directories.map(
      (item) => normalizedSelector(item, 'directory'),
    );
    if (new Set([...files, ...directories]).size !== files.length + directories.length) {
      throw new DependencyDirectionError(`duplicate selectors for ${layer}`);
    }
  }
}

function matchesSelectors(relativePath, selectors) {
  return selectors.files.includes(relativePath)
    || selectors.directories.some(
      (directory) => relativePath.startsWith(`${directory}/`),
    );
}

function assertEverySelectorMatches(relativeModules, selectedBoundaries) {
  for (const layer of ['clientState', 'mutations', 'composition']) {
    const selectors = selectedBoundaries[layer];
    for (const file of selectors.files) {
      if (!relativeModules.includes(file)) {
        throw new DependencyDirectionError(
          `reviewed ${LAYER_LABELS[layer]} file selector matched no production module: ${file}`,
        );
      }
    }
    for (const directory of selectors.directories) {
      if (!relativeModules.some((candidate) => candidate.startsWith(`${directory}/`))) {
        throw new DependencyDirectionError(
          `reviewed ${LAYER_LABELS[layer]} directory selector matched no production module: `
          + directory,
        );
      }
    }
  }
}

export function classifyFocusLayer(relativePath, selectedBoundaries = ACTIVE_FOCUS_BOUNDARIES) {
  validateBoundaries(selectedBoundaries);
  const matches = [
    matchesSelectors(relativePath, selectedBoundaries.clientState) ? 'clientState' : null,
    matchesSelectors(relativePath, selectedBoundaries.mutations) ? 'mutations' : null,
    matchesSelectors(relativePath, selectedBoundaries.composition) ? 'composition' : null,
    relativePath.endsWith('.vue') ? 'presentation' : null,
  ].filter(Boolean);
  if (matches.length > 1) {
    throw new DependencyDirectionError(
      `module matches overlapping dependency layers: ${relativePath} `
      + `(${matches.map((layer) => LAYER_LABELS[layer]).join(', ')})`,
    );
  }
  return matches[0] ?? null;
}

function walkProductionModules(root) {
  const modules = [];
  const visit = (directory) => {
    const entries = fs.readdirSync(directory, { withFileTypes: true })
      .sort((left, right) => compareCodePoint(left.name, right.name));
    for (const entry of entries) {
      const absolute = path.join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        throw new DependencyDirectionError(
          `cannot prove dependency direction through symlink: ${posixRelative(root, absolute)}`,
        );
      }
      if (entry.isDirectory()) {
        visit(absolute);
      } else if (
        entry.isFile()
        && SCRIPT_EXTENSIONS.has(path.extname(entry.name))
        && !TEST_FILE.test(entry.name)
        && !entry.name.endsWith('.d.ts')
      ) {
        modules.push(absolute);
      }
    }
  };
  visit(root);
  return modules;
}

function scriptKind(filename, lang = path.extname(filename).slice(1)) {
  const kinds = new Map([
    ['js', ts.ScriptKind.JS],
    ['jsx', ts.ScriptKind.JSX],
    ['ts', ts.ScriptKind.TS],
    ['tsx', ts.ScriptKind.TSX],
  ]);
  const normalized = ({ cjs: 'js', mjs: 'js', cts: 'ts', mts: 'ts' })[lang] ?? lang;
  const kind = kinds.get(normalized);
  if (kind === undefined) {
    throw new DependencyDirectionError(`unsupported script language ${JSON.stringify(lang)} in ${filename}`);
  }
  return kind;
}

function diagnosticText(diagnostic) {
  return ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n');
}

function stringSpecifier(node) {
  return ts.isStringLiteralLike(node) ? node.text : null;
}

function parseImports(source, filename, kind, baseLine = 0) {
  const sourceFile = ts.createSourceFile(
    filename,
    source,
    ts.ScriptTarget.Latest,
    true,
    kind,
  );
  if (sourceFile.parseDiagnostics.length > 0) {
    const diagnostic = sourceFile.parseDiagnostics[0];
    throw new DependencyDirectionError(
      `cannot parse ${filename}: ${diagnosticText(diagnostic)}`,
    );
  }
  const imports = [];
  const add = (specifier, node, form) => {
    if (!specifier.startsWith('.')) return;
    const location = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
    imports.push({ specifier, form, line: baseLine + location.line + 1 });
  };
  const requireLiteral = (node, form) => {
    const specifier = stringSpecifier(node);
    if (specifier === null) {
      throw new DependencyDirectionError(
        `cannot prove ${form} in ${filename}: module specifier must be a string literal`,
      );
    }
    add(specifier, node, form);
  };
  const visit = (node) => {
    if (ts.isImportDeclaration(node) || ts.isExportDeclaration(node)) {
      if (node.moduleSpecifier) requireLiteral(node.moduleSpecifier, ts.isImportDeclaration(node) ? 'import' : 're-export');
    } else if (
      ts.isImportEqualsDeclaration(node)
      && ts.isExternalModuleReference(node.moduleReference)
      && node.moduleReference.expression
    ) {
      requireLiteral(node.moduleReference.expression, 'import-equals');
    } else if (ts.isImportTypeNode(node)) {
      if (!ts.isLiteralTypeNode(node.argument)) {
        throw new DependencyDirectionError(`cannot prove import type in ${filename}`);
      }
      requireLiteral(node.argument.literal, 'import-type');
    } else if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) {
      if (node.arguments.length !== 1) {
        throw new DependencyDirectionError(`cannot prove dynamic import in ${filename}`);
      }
      requireLiteral(node.arguments[0], 'dynamic-import');
    } else if (
      ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === 'require'
    ) {
      if (node.arguments.length !== 1) {
        throw new DependencyDirectionError(`cannot prove require call in ${filename}`);
      }
      requireLiteral(node.arguments[0], 'require');
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return imports;
}

function importsForModule(absolutePath, relativePath) {
  const source = fs.readFileSync(absolutePath, 'utf8');
  if (!absolutePath.endsWith('.vue')) {
    return parseImports(source, relativePath, scriptKind(relativePath));
  }
  const parsed = parseVueSfc(source, { filename: relativePath, sourceMap: false });
  if (parsed.errors.length > 0) {
    const first = parsed.errors[0];
    throw new DependencyDirectionError(
      `cannot parse ${relativePath}: ${first instanceof Error ? first.message : String(first)}`,
    );
  }
  const blocks = [parsed.descriptor.script, parsed.descriptor.scriptSetup].filter(Boolean);
  const imports = [];
  for (const block of blocks) {
    if (block.src) {
      throw new DependencyDirectionError(`cannot prove external Vue script in ${relativePath}`);
    }
    imports.push(...parseImports(
      block.content,
      relativePath,
      scriptKind(relativePath, block.lang ?? 'js'),
      Math.max(0, block.loc.start.line - 1),
    ));
  }
  return imports;
}

function resolveRelativeImport(sourceRoot, importer, specifier) {
  const withoutQuery = specifier.split(/[?#]/, 1)[0];
  if (!withoutQuery) {
    throw new DependencyDirectionError(`empty relative module specifier in ${posixRelative(sourceRoot, importer)}`);
  }
  const unresolved = path.resolve(path.dirname(importer), withoutQuery);
  const relative = path.relative(sourceRoot, unresolved);
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new DependencyDirectionError(
      `relative import escapes src: ${posixRelative(sourceRoot, importer)} -> ${specifier}`,
    );
  }
  const candidates = [unresolved];
  for (const extension of RESOLUTION_EXTENSIONS) candidates.push(`${unresolved}${extension}`);
  for (const extension of RESOLUTION_EXTENSIONS) candidates.push(path.join(unresolved, `index${extension}`));
  const targets = [...new Set(candidates)].filter((candidate) => {
    try {
      return fs.statSync(candidate).isFile();
    } catch (error) {
      if (error && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) return false;
      throw error;
    }
  });
  if (targets.length === 0) {
    throw new DependencyDirectionError(
      `cannot resolve relative import: ${posixRelative(sourceRoot, importer)} -> ${specifier}`,
    );
  }
  if (targets.length > 1) {
    throw new DependencyDirectionError(
      `ambiguous relative import: ${posixRelative(sourceRoot, importer)} -> ${specifier} `
      + `(${targets.map((target) => posixRelative(sourceRoot, target)).join(', ')})`,
    );
  }
  return posixRelative(sourceRoot, targets[0]);
}

export function analyzeFocusDependencyDirection(
  sourceRoot,
  selectedBoundaries = ACTIVE_FOCUS_BOUNDARIES,
) {
  validateBoundaries(selectedBoundaries);
  const root = path.resolve(sourceRoot);
  let rootStat;
  try {
    rootStat = fs.statSync(root);
  } catch (error) {
    throw new DependencyDirectionError(`cannot read source root ${root}: ${error.message}`);
  }
  if (!rootStat.isDirectory()) {
    throw new DependencyDirectionError(`source root is not a directory: ${root}`);
  }
  const layerModules = Object.fromEntries(LAYERS.map((layer) => [layer, 0]));
  const violations = [];
  let directRelativeEdges = 0;
  const modules = walkProductionModules(root);
  assertEverySelectorMatches(
    modules.map((candidate) => posixRelative(root, candidate)),
    selectedBoundaries,
  );
  for (const importer of modules) {
    const source = posixRelative(root, importer);
    const sourceLayer = classifyFocusLayer(source, selectedBoundaries);
    if (sourceLayer !== null) {
      layerModules[sourceLayer] += 1;
    }
    for (const dependency of importsForModule(importer, source)) {
      const target = resolveRelativeImport(root, importer, dependency.specifier);
      directRelativeEdges += 1;
      const targetLayer = classifyFocusLayer(target, selectedBoundaries);
      if (FORBIDDEN_TARGETS.get(sourceLayer)?.has(targetLayer)) {
        violations.push({
          source,
          line: dependency.line,
          form: dependency.form,
          target,
          sourceLayer,
          targetLayer,
        });
      }
    }
  }
  for (const layer of LAYERS) {
    if (layerModules[layer] === 0) {
      throw new DependencyDirectionError(
        `reviewed ${LAYER_LABELS[layer]} selector matched no production module`,
      );
    }
  }
  violations.sort((left, right) => (
    compareCodePoint(left.source, right.source)
    || left.line - right.line
    || compareCodePoint(left.target, right.target)
  ));
  return {
    productionModules: modules.length,
    directRelativeEdges,
    layerModules,
    violations,
  };
}

function runCli() {
  if (process.argv.length !== 2) {
    throw new DependencyDirectionError('this guard accepts no command-line arguments');
  }
  const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const result = analyzeFocusDependencyDirection(path.join(webRoot, 'src'));
  if (result.violations.length > 0) {
    for (const violation of result.violations) {
      console.error(
        `${violation.source}:${violation.line}: ${LAYER_LABELS[violation.sourceLayer]} `
        + `cannot depend on ${LAYER_LABELS[violation.targetLayer]} `
        + `(${violation.form} ${violation.target})`,
      );
    }
    console.error(`Focus Web dependency direction: ${result.violations.length} violation(s).`);
    return 1;
  }
  console.log(
    `Focus Web dependency direction: ${result.productionModules} production modules, `
    + `${result.directRelativeEdges} direct relative edges, `
    + `${result.layerModules.clientState} client-state, `
    + `${result.layerModules.mutations} mutations, `
    + `${result.layerModules.composition} composition and `
    + `${result.layerModules.presentation} presentation module(s), 0 violations.`,
  );
  return 0;
}

const invokedPath = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href : '';
if (import.meta.url === invokedPath) {
  try {
    process.exitCode = runCli();
  } catch (error) {
    console.error(`Focus Web dependency direction could not be proven: ${error.message}`);
    process.exitCode = 2;
  }
}
