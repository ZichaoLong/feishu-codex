import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, describe, expect, it } from 'vitest';

import {
  ACTIVE_FOCUS_BOUNDARIES,
  analyzeFocusDependencyDirection,
} from '../scripts/check-focus-dependency-direction.mjs';

const temporaryRoots: string[] = [];
const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) fs.rmSync(root, { recursive: true });
});

function writeFixture(files: Record<string, string>): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'focus-web-direction-'));
  temporaryRoots.push(root);
  for (const [relative, source] of Object.entries(files)) {
    const target = path.join(root, relative);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, source, 'utf8');
  }
  return root;
}

function packageFixture(overrides: Record<string, string> = {}): string {
  return writeFixture({
    'focus/client-state/pending-request-actions.ts': 'export interface PendingRequestActionState {}\n',
    'focus/client-state/thread-event-revisions.ts': 'export interface ThreadEventRevisionState {}\n',
    'focus/client-state/thread-mutations.ts': 'export interface ThreadMutationState {}\n',
    'focus/mutations/actions.ts': 'export interface Action {}\n',
    'focus/useFocusWebClient.ts': 'export const client = true;\n',
    'main.ts': 'export const app = true;\n',
    'focus/View.vue': '<script setup lang="ts">const view = true;</script>\n',
    ...overrides,
  });
}

describe('Focus Web dependency direction', () => {
  it('allows presentation and composition to call mutations, then client-state', () => {
    const root = packageFixture({
      'focus/mutations/actions.ts': [
        "import type { ThreadMutationState } from '../client-state/thread-mutations';",
        'export interface Action extends ThreadMutationState {}',
      ].join('\n'),
      'focus/useFocusWebClient.ts': "export { type Action } from './mutations/actions';\n",
      'main.ts': "import './focus/mutations/actions';\n",
      'focus/View.vue': [
        '<script setup lang="ts">',
        "import type { Action } from './mutations/actions';",
        'const value: Action | null = null;',
        '</script>',
        '<template><div>{{ value }}</div></template>',
      ].join('\n'),
    });

    const result = analyzeFocusDependencyDirection(root);

    expect(result.violations).toEqual([]);
    expect(result.layerModules).toEqual({
      clientState: 3,
      mutations: 1,
      composition: 2,
      presentation: 1,
    });
    expect(result.directRelativeEdges).toBe(4);
  });

  it.each([
    [
      'static import',
      'focus/client-state/thread-event-revisions.ts',
      "import { action } from '../mutations/actions';\nvoid action;\n",
    ],
    [
      'type-only import',
      'focus/client-state/thread-mutations.ts',
      "import type { Action } from '../mutations/actions';\nexport type State = Action;\n",
    ],
    [
      'ImportTypeNode',
      'focus/client-state/pending-request-actions.ts',
      "export type State = import('../mutations/actions').Action;\n",
    ],
    [
      're-export',
      'focus/client-state/thread-event-revisions.ts',
      "export { action } from '../mutations/actions';\n",
    ],
    [
      'dynamic import',
      'focus/client-state/pending-request-actions.ts',
      "void import('../mutations/actions');\n",
    ],
  ])('rejects a client-state to mutations %s', (_name, owner, source) => {
    const root = packageFixture({
      [owner]: source,
      'focus/mutations/actions.ts': [
        'export interface Action {}',
        'export const action = 1;',
      ].join('\n'),
    });

    const result = analyzeFocusDependencyDirection(root);

    expect(result.violations).toHaveLength(1);
    expect(result.violations[0]).toMatchObject({
      source: owner,
      target: 'focus/mutations/actions.ts',
      sourceLayer: 'clientState',
      targetLayer: 'mutations',
    });
  });

  it('rejects client-state and mutations imports of composition or presentation', () => {
    const root = packageFixture({
      'focus/client-state/thread-event-revisions.ts': "import '../../main';\n",
      'focus/mutations/actions.ts': "import '../View.vue';\n",
      'main.ts': 'export const app = true;\n',
      'focus/View.vue': '<script setup lang="ts">const view = true;</script>\n',
    });

    const result = analyzeFocusDependencyDirection(root);

    expect(result.violations.map((item) => [item.sourceLayer, item.targetLayer])).toEqual([
      ['clientState', 'composition'],
      ['mutations', 'presentation'],
    ]);
  });

  it('applies directory selectors to every package child', () => {
    const root = writeFixture({
      'focus/client-state/nested/thread-mutations.ts': [
        "import type { Action } from '../../mutations/actions';",
        'export type State = Action;',
      ].join('\n'),
      'focus/mutations/actions.ts': 'export interface Action {}\n',
      'focus/useFocusWebClient.ts': 'export const client = true;\n',
      'main.ts': 'export const app = true;\n',
      'focus/View.vue': '<script setup lang="ts">const view = true;</script>\n',
    });

    const result = analyzeFocusDependencyDirection(root);

    expect(result.violations).toHaveLength(1);
    expect(result.violations[0]).toMatchObject({
      source: 'focus/client-state/nested/thread-mutations.ts',
      sourceLayer: 'clientState',
      targetLayer: 'mutations',
    });

    const staleDirectoryBoundaries = {
      ...ACTIVE_FOCUS_BOUNDARIES,
      clientState: {
        files: [],
        directories: ['focus/client-state-missing'],
      },
    };
    expect(() => analyzeFocusDependencyDirection(root, staleDirectoryBoundaries)).toThrow(
      /reviewed client-state directory selector matched no production module/,
    );
  });

  it('does not confuse a similar directory prefix with a reviewed package', () => {
    const root = writeFixture({
      'focus/client-state/pending-request-actions.ts': 'export interface PendingRequestActionState {}\n',
      'focus/client-state/thread-event-revisions.ts': 'export interface ThreadEventRevisionState {}\n',
      'focus/client-state/thread-mutations.ts': 'export interface ThreadMutationState {}\n',
      'focus/mutations/actions.ts': 'export interface Action {}\n',
      'focus/client-state-extra/thread-mutations.ts': [
        "import type { Action } from '../mutations/actions';",
        'export type Extra = Action;',
      ].join('\n'),
      'focus/useFocusWebClient.ts': 'export const client = true;\n',
      'main.ts': 'export const app = true;\n',
      'focus/View.vue': '<script setup lang="ts">const view = true;</script>\n',
    });

    const result = analyzeFocusDependencyDirection(root);

    expect(result.violations).toEqual([]);
    expect(result.layerModules).toEqual({
      clientState: 3,
      mutations: 1,
      composition: 2,
      presentation: 1,
    });
  });

  it.each([
    ['client-state', [
      'focus/client-state/pending-request-actions.ts',
      'focus/client-state/thread-event-revisions.ts',
      'focus/client-state/thread-mutations.ts',
    ]],
    ['mutations', ['focus/mutations/actions.ts']],
    ['composition', ['focus/useFocusWebClient.ts']],
    ['presentation', ['focus/View.vue']],
  ])('fails closed when a reviewed %s selector becomes stale', (label, missing) => {
    const files: Record<string, string> = {
      'focus/client-state/pending-request-actions.ts': 'export interface PendingRequestActionState {}\n',
      'focus/client-state/thread-event-revisions.ts': 'export interface ThreadEventRevisionState {}\n',
      'focus/client-state/thread-mutations.ts': 'export interface ThreadMutationState {}\n',
      'focus/mutations/actions.ts': 'export interface Action {}\n',
      'focus/useFocusWebClient.ts': 'export const client = true;\n',
      'main.ts': 'export const app = true;\n',
      'focus/View.vue': '<script setup lang="ts">const view = true;</script>\n',
    };
    for (const path of missing) delete files[path];
    const root = writeFixture(files);

    expect(() => analyzeFocusDependencyDirection(root)).toThrow(
      new RegExp(`reviewed ${label} .*selector matched no production module`),
    );
  });

  it('fails closed when one module matches multiple reviewed layers', () => {
    const root = packageFixture();
    const overlappingBoundaries = {
      ...ACTIVE_FOCUS_BOUNDARIES,
      clientState: {
        files: ['main.ts'],
        directories: ['focus/client-state'],
      },
    };

    expect(() => analyzeFocusDependencyDirection(root, overlappingBoundaries)).toThrow(
      /module matches overlapping dependency layers: main\.ts \(client-state, composition\)/,
    );
  });

  it('fails closed when a relative import has multiple live candidates', () => {
    const root = packageFixture({
      'focus/client-state/thread-event-revisions.ts': "import './target';\n",
      'focus/client-state/target.ts': 'export const target = true;\n',
      'focus/client-state/target.js': 'export const target = true;\n',
    });

    expect(() => analyzeFocusDependencyDirection(root)).toThrow(
      /ambiguous relative import: focus\/client-state\/thread-event-revisions\.ts -> \.\/target/,
    );
  });

  it('fails closed when a relative import cannot be resolved', () => {
    const root = packageFixture({
      'focus/client-state/pending-request-actions.ts': "import './missing-owner';\n",
    });

    expect(() => analyzeFocusDependencyDirection(root)).toThrow(
      /cannot resolve relative import: focus\/client-state\/pending-request-actions\.ts -> \.\/missing-owner/,
    );
  });

  it.each([
    ['TypeScript', { 'focus/client-state/thread-mutations.ts': 'export const broken = ;\n' }],
    ['Vue SFC', { 'focus/View.vue': '<script setup lang="ts">const broken = true;\n' }],
  ])('fails closed on a %s parse error', (_kind, overrides) => {
    const root = packageFixture(overrides);

    expect(() => analyzeFocusDependencyDirection(root)).toThrow(/cannot parse/);
  });

  it('fails closed on a non-literal dynamic import', () => {
    const root = packageFixture({
      'focus/client-state/pending-request-actions.ts': [
        "const target = '../mutations/actions';",
        'void import(target);',
      ].join('\n'),
    });

    expect(() => analyzeFocusDependencyDirection(root)).toThrow(
      /module specifier must be a string literal/,
    );
  });

  it('runs against nonempty current package owners and the live graph', () => {
    const result = analyzeFocusDependencyDirection(path.join(webRoot, 'src'), ACTIVE_FOCUS_BOUNDARIES);

    expect(result.layerModules.clientState).toBe(7);
    expect(result.layerModules.mutations).toBe(3);
    expect(result.layerModules.composition).toBeGreaterThan(0);
    expect(result.layerModules.presentation).toBeGreaterThan(0);
    expect(result.directRelativeEdges).toBeGreaterThan(0);
    expect(result.violations).toEqual([]);
  });
});
