import { describe, expect, it } from 'vitest';
import { ClientIntentClock } from '../../../src/focus/clientIntentClock';

describe('ClientIntentClock', () => {
  it('owns only the document-global intent sequence', () => {
    const clock = new ClientIntentClock();

    expect(clock.currentIntent).toBe(0);
    const first = clock.beginIntent();
    const second = clock.beginIntent();

    expect(clock.intentIsCurrent(first)).toBe(false);
    expect(clock.intentIsCurrent(second)).toBe(true);
    expect(clock.currentIntent).toBe(2);
  });

  it('rebases monotonically above a retained server floor', () => {
    const clock = new ClientIntentClock();
    clock.beginIntent();

    expect(clock.rebase(7)).toBe(7);
    expect(clock.rebase(3)).toBe(7);
    expect(clock.beginIntent()).toBe(8);
    expect(() => clock.rebase(-1)).toThrow(RangeError);
  });
});
