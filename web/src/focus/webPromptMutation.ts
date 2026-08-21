const CANONICAL_UUID_PATTERN = new RegExp(
  '^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-'
  + '[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
);

export function isCanonicalWebMutationId(value: unknown): value is string {
  return typeof value === 'string' && CANONICAL_UUID_PATTERN.test(value);
}

export function webPromptClientUserMessageId(mutationId: string): string {
  return `focus-web:${mutationId}`;
}

export function isWebPromptClientUserMessageId(
  value: unknown,
  mutationId: string,
): value is string {
  return isCanonicalWebMutationId(mutationId)
    && value === webPromptClientUserMessageId(mutationId);
}

/** Create the browser mutation identity before yielding to the prompt POST. */
export function createWebPromptMutationId(): string | null {
  try {
    const secureCrypto = globalThis.crypto;
    if (!secureCrypto || typeof secureCrypto.randomUUID !== 'function') return null;
    const mutationId = secureCrypto.randomUUID();
    return isCanonicalWebMutationId(mutationId) ? mutationId : null;
  } catch {
    return null;
  }
}
