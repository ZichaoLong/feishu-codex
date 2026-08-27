import { ref } from 'vue';
import { describe, expect, it } from 'vitest';
import {
  focusDocumentTitle,
  syncFocusDocumentTitle,
} from '../src/focus/documentTitle';

describe('Focus Web document title', () => {
  it('puts the deployment name before the resolved conversation title', () => {
    expect(focusDocumentTitle('Workstation A', 'Renamed thread')).toBe(
      'Workstation A · Renamed thread',
    );
    expect(focusDocumentTitle('Focus Web', 'First prompt preview')).toBe(
      'Focus Web · First prompt preview',
    );
    expect(focusDocumentTitle('Focus Web', '')).toBe('Focus Web');
  });

  it('keeps long conversation titles intact for native browser truncation', () => {
    const conversation = '会话'.repeat(500);

    expect(focusDocumentTitle('Focus Web', conversation)).toBe(
      `Focus Web · ${conversation}`,
    );
  });

  it('reacts to deployment, thread-switch, and rename changes', () => {
    const deployment = ref('Focus Web');
    const conversation = ref('');
    const target = { title: '' };
    const stop = syncFocusDocumentTitle(
      () => deployment.value,
      () => conversation.value,
      target,
    );

    expect(target.title).toBe('Focus Web');

    conversation.value = 'First prompt preview';
    expect(target.title).toBe('Focus Web · First prompt preview');

    deployment.value = 'Workstation A';
    expect(target.title).toBe('Workstation A · First prompt preview');

    conversation.value = 'Renamed thread';
    expect(target.title).toBe('Workstation A · Renamed thread');

    stop();
  });
});
