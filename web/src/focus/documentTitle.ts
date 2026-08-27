import { watch, type WatchSource, type WatchStopHandle } from 'vue';

export const DEFAULT_WEB_DISPLAY_NAME = 'Focus Web';

interface DocumentTitleTarget {
  title: string;
}

function singleLineTitle(value: string): string {
  let normalized = '';
  let pendingSpace = false;
  for (const character of value.trim()) {
    if (!character.trim()) {
      pendingSpace = normalized.length > 0;
      continue;
    }
    if (pendingSpace) normalized += ' ';
    normalized += character;
    pendingSpace = false;
  }
  return normalized;
}

export function focusDocumentTitle(
  deploymentTitle: string,
  conversationTitle: string,
): string {
  const deployment = singleLineTitle(deploymentTitle) || DEFAULT_WEB_DISPLAY_NAME;
  const conversation = singleLineTitle(conversationTitle);
  return conversation ? `${deployment} · ${conversation}` : deployment;
}

export function syncFocusDocumentTitle(
  deploymentTitle: WatchSource<string>,
  conversationTitle: WatchSource<string>,
  target: DocumentTitleTarget = document,
): WatchStopHandle {
  return watch(
    [deploymentTitle, conversationTitle],
    ([deployment, conversation]) => {
      target.title = focusDocumentTitle(deployment, conversation);
    },
    { flush: 'sync', immediate: true },
  );
}
