import type { FocusCoordinates } from '../types';

/**
 * Epoch-aware latest-event index for each thread.
 *
 * Coordinates from different epochs are never compared. Within one epoch the
 * record is monotonic, so a delayed HTTP response can be rejected without
 * allowing an older event to lower the thread's revision floor.
 */
export class ThreadEventRevisionIndex {
  private readonly byThread = new Map<string, FocusCoordinates>();

  clear(): void {
    this.byThread.clear();
  }

  observe(threadId: string, coordinates: FocusCoordinates): void {
    const normalizedThreadId = threadId.trim();
    if (!normalizedThreadId || !coordinates.runtime_epoch) return;
    const current = this.byThread.get(normalizedThreadId);
    if (
      current
      && current.runtime_epoch === coordinates.runtime_epoch
      && current.revision >= coordinates.revision
    ) return;
    this.byThread.set(normalizedThreadId, {
      runtime_epoch: coordinates.runtime_epoch,
      revision: coordinates.revision,
    });
  }

  hasNewerEvent(threadId: string, response: FocusCoordinates): boolean {
    const current = this.byThread.get(threadId);
    return current?.runtime_epoch === response.runtime_epoch
      && current.revision > response.revision;
  }
}
