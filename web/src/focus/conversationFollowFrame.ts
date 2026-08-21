export type ConversationFollowMode = 'instant' | 'smooth';

type RequestFrame = (callback: () => void) => number;
type CancelFrame = (frameId: number) => void;

export type ConversationFollowFrame = {
  request: (mode?: ConversationFollowMode) => void;
  cancel: () => void;
};

function defaultRequestFrame(callback: () => void): number {
  return (typeof requestAnimationFrame === 'function'
    ? requestAnimationFrame(callback)
    : setTimeout(callback, 16)) as unknown as number;
}

function defaultCancelFrame(frameId: number): void {
  if (typeof cancelAnimationFrame === 'function') cancelAnimationFrame(frameId);
  else clearTimeout(frameId);
}

/** Coalesce ordinary conversation-tail follow requests into one browser frame. */
export function createConversationFollowFrame(
  follow: (mode: ConversationFollowMode) => void,
  options: {
    requestFrame?: RequestFrame;
    cancelFrame?: CancelFrame;
  } = {},
): ConversationFollowFrame {
  const requestFrame = options.requestFrame ?? defaultRequestFrame;
  const cancelFrame = options.cancelFrame ?? defaultCancelFrame;
  let scheduledFrame: number | null = null;
  let scheduledGeneration = 0;
  let pendingMode: ConversationFollowMode | null = null;

  function request(mode: ConversationFollowMode = 'instant'): void {
    if (mode === 'smooth' || pendingMode === null) pendingMode = mode;
    if (scheduledFrame !== null) return;

    const generation = ++scheduledGeneration;
    scheduledFrame = requestFrame(() => {
      if (generation !== scheduledGeneration) return;
      scheduledFrame = null;
      const nextMode = pendingMode;
      pendingMode = null;
      if (nextMode !== null) follow(nextMode);
    });
  }

  function cancel(): void {
    pendingMode = null;
    scheduledGeneration += 1;
    if (scheduledFrame === null) return;
    const frameId = scheduledFrame;
    scheduledFrame = null;
    cancelFrame(frameId);
  }

  return { request, cancel };
}
