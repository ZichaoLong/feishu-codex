/**
 * Document-global ordering for mutations initiated by this browser document.
 *
 * HTTP channels, navigation settlement, profile overlays, and writer-scope
 * receipts have their own owners. Keeping only this sequence here prevents a
 * request counter from becoming an accidental second owner for those facts.
 */
export class ClientIntentClock {
  private intentGeneration = 0;

  get currentIntent(): number {
    return this.intentGeneration;
  }

  rebase(intentGenerationFloor: number): number {
    if (!Number.isSafeInteger(intentGenerationFloor) || intentGenerationFloor < 0) {
      throw new RangeError('Client intent generation floor must be a non-negative safe integer.');
    }
    this.intentGeneration = Math.max(this.intentGeneration, intentGenerationFloor);
    return this.intentGeneration;
  }

  beginIntent(): number {
    this.intentGeneration += 1;
    return this.intentGeneration;
  }

  intentIsCurrent(generation: number): boolean {
    return generation === this.intentGeneration;
  }
}
