import type { QuestionAnswer, QuestionResponse } from '../types';
import type { FocusWebApiPort } from './api';
import type { FocusPendingRequest } from './types';

interface PendingRequestActionCapability {
  requestId: string;
  connectionGeneration: number;
  responseCapability: string;
  kind: FocusPendingRequest['kind'];
}

const ACTION_TOKEN_PREFIX = 'focus-response:';

export function pendingRequestActionToken(pending: FocusPendingRequest): string {
  return `${ACTION_TOKEN_PREFIX}${encodeURIComponent(JSON.stringify([
    pending.id, pending.connection_generation, pending.response_capability, pending.kind,
  ]))}`;
}

export function decodePendingRequestActionToken(
  token: string,
): PendingRequestActionCapability | undefined {
  try {
    if (!token.startsWith(ACTION_TOKEN_PREFIX)) return undefined;
    const value: unknown = JSON.parse(decodeURIComponent(token.slice(ACTION_TOKEN_PREFIX.length)));
    if (!Array.isArray(value) || value.length !== 4) return undefined;
    const [requestId, connectionGeneration, responseCapability, kind] = value;
    if (typeof requestId !== 'string' || !requestId) return undefined;
    if (typeof connectionGeneration !== 'number' || !Number.isInteger(connectionGeneration)
      || connectionGeneration <= 0) return undefined;
    if (typeof responseCapability !== 'string' || !responseCapability) return undefined;
    if (kind !== 'approval' && kind !== 'question' && kind !== 'elicitation') return undefined;
    return {
      requestId,
      connectionGeneration,
      responseCapability,
      kind,
    };
  } catch {
    return undefined;
  }
}

export async function respondPendingRequest(
  api: Pick<FocusWebApiPort, 'respondRequest'>,
  actionToken: string,
  action: string,
  answers: Record<string, unknown> = {},
): Promise<boolean> {
  const capability = decodePendingRequestActionToken(actionToken);
  if (!capability) return false;
  const result = await api.respondRequest(
    capability.requestId,
    capability.connectionGeneration,
    capability.responseCapability,
    action,
    answers,
  );
  return result.accepted;
}

export function answerValues(answer: QuestionAnswer): string[] {
  if (answer.kind === 'single') return [answer.optionId];
  if (answer.kind === 'multi') return answer.optionIds;
  if (answer.kind === 'other') return [answer.text];
  if (answer.kind === 'multiWithOther') {
    return [...answer.optionIds, ...(answer.otherText.trim() ? [answer.otherText.trim()] : [])];
  }
  return [];
}

export function elicitationContent(response: QuestionResponse): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(response.answers).flatMap(([fieldId, answer]) => {
      const values = answerValues(answer).filter((value) => value.trim());
      if (values.length === 0) return [];
      return [[fieldId, answer.kind === 'multi' || answer.kind === 'multiWithOther' ? values : values[0]]];
    }),
  );
}
