import { describe, expect, it } from 'vitest';
import enComposer from '../src/i18n/locales/en/composer';
import zhComposer from '../src/i18n/locales/zh/composer';

describe('Focus Web branding', () => {
  it('uses the Focus product name in an empty conversation', () => {
    expect(enComposer.emptyConversationTitle).toBe('Focus Web');
    expect(zhComposer.emptyConversationTitle).toBe('Focus Web');
  });
});
