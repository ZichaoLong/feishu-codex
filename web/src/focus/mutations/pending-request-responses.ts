import type { Ref } from 'vue';
import type { ApprovalResponse, QuestionResponse } from '../../types';
import type { FocusWebApiPort } from '../api';
import {
  createPendingRequestActionState,
  type PendingRequestActionState,
} from '../client-state/pending-request-actions';
import type { FocusProjectionSync } from '../focusProjectionSync';
import {
  answerValues,
  decodePendingRequestActionToken,
  elicitationContent,
  pendingRequestActionToken,
  respondPendingRequest,
} from '../pendingRequestCapability';
import { FocusApiError } from '../types';

interface PendingRequestResponsesOptions {
  api: Pick<FocusWebApiPort, 'respondRequest'>;
  projection: Pick<FocusProjectionSync, 'snapshot' | 'refreshActiveThread'>;
  connection: Readonly<Ref<string>>;
  reportError(error: unknown): void;
  reportFatalError(error: unknown): boolean;
}

interface PendingRequestRuntimePort {
  isDisposed(): boolean;
  reportErrorIfCurrent(error: unknown, current?: boolean): boolean;
}

type PendingRequestCompositionPort = Pick<
  PendingRequestActionState,
  'approvalActions' | 'questionActions' | 'clear'
>;

interface PendingRequestResponses {
  readonly pendingRequestActions: PendingRequestCompositionPort;
  respondApproval(
    actionToken: string,
    response: ApprovalResponse & { actionId?: string },
  ): Promise<void>;
  respondQuestion(actionToken: string, response: QuestionResponse): Promise<void>;
  dismissQuestion(actionToken: string): Promise<void>;
}

function responseWasDefinitelyNotSent(error: unknown): boolean {
  return error instanceof FocusApiError && error.code === 'request_not_sent';
}

export function createPendingRequestResponses(
  options: PendingRequestResponsesOptions,
  runtime: PendingRequestRuntimePort,
): PendingRequestResponses {
  const {
    api,
    projection,
  } = options;
  const { isDisposed, reportErrorIfCurrent } = runtime;
  const pendingRequestActions = createPendingRequestActionState(projection.snapshot);

  function pendingRequestIsCurrent(actionToken: string): boolean {
    return (projection.snapshot.value?.pending_requests ?? []).some((request) => (
      request.status === 'pending' && pendingRequestActionToken(request) === actionToken
    ));
  }

  async function settleRejectedPendingResponse(
    actionToken: string,
    label: string,
  ): Promise<void> {
    let refreshError: unknown;
    try {
      await projection.refreshActiveThread();
    } catch (error) {
      refreshError = error;
    }
    if (isDisposed()) return;
    if (refreshError && options.reportFatalError(refreshError)) return;
    if (!pendingRequestIsCurrent(actionToken)) return;
    const detail = refreshError instanceof Error ? ` ${refreshError.message}` : '';
    options.reportError(new Error(`${label} was not accepted by Focus.${detail}`));
  }

  async function respondApproval(
    actionToken: string,
    response: ApprovalResponse & { actionId?: string },
  ): Promise<void> {
    const capability = decodePendingRequestActionToken(actionToken);
    if (isDisposed() || capability?.kind !== 'approval'
      || options.connection.value !== 'connected'
      || pendingRequestActions.approvalActions.value[actionToken]) return;
    const actionReceipt = pendingRequestActions.beginApproval(actionToken);
    if (!actionReceipt) return;
    let retainResponseLock = false;
    const action = response.actionId || (response.decision === 'approved'
      ? (response.scope === 'session' ? 'approve_session' : 'approve_once')
      : response.decision === 'rejected' ? 'reject' : 'cancel');
    try {
      const accepted = await respondPendingRequest(api, actionToken, action);
      if (isDisposed()) return;
      if (!accepted) {
        await settleRejectedPendingResponse(actionToken, 'The approval response');
        return;
      }
      retainResponseLock = true;
      pendingRequestActions.acceptApproval(actionReceipt);
      await projection.refreshActiveThread();
    } catch (error) {
      if (!responseWasDefinitelyNotSent(error)) {
        retainResponseLock = true;
        pendingRequestActions.acceptApproval(actionReceipt);
      }
      reportErrorIfCurrent(error, pendingRequestIsCurrent(actionToken));
    } finally {
      if (!isDisposed() && !retainResponseLock) {
        pendingRequestActions.finishApproval(actionReceipt);
      }
    }
  }

  async function respondQuestion(actionToken: string, response: QuestionResponse): Promise<void> {
    const capability = decodePendingRequestActionToken(actionToken);
    if (isDisposed() || (capability?.kind !== 'question' && capability?.kind !== 'elicitation')
      || options.connection.value !== 'connected'
      || pendingRequestActions.questionActions.value[actionToken]) return;
    const actionReceipt = pendingRequestActions.beginQuestion(actionToken, 'answer');
    if (!actionReceipt) return;
    let retainResponseLock = false;
    try {
      let accepted: boolean;
      if (capability.kind === 'elicitation') {
        accepted = await respondPendingRequest(
          api,
          actionToken,
          'accept',
          elicitationContent(response),
        );
      } else {
        const answers = Object.fromEntries(
          Object.entries(response.answers).map(([questionId, answer]) => [questionId, answerValues(answer)]),
        );
        accepted = await respondPendingRequest(api, actionToken, 'answer', answers);
      }
      if (isDisposed()) return;
      if (!accepted) {
        await settleRejectedPendingResponse(actionToken, 'The question response');
        return;
      }
      retainResponseLock = true;
      pendingRequestActions.acceptQuestion(actionReceipt);
      await projection.refreshActiveThread();
    } catch (error) {
      if (!responseWasDefinitelyNotSent(error)) {
        retainResponseLock = true;
        pendingRequestActions.acceptQuestion(actionReceipt);
      }
      reportErrorIfCurrent(error, pendingRequestIsCurrent(actionToken));
    } finally {
      if (!isDisposed() && !retainResponseLock) {
        pendingRequestActions.finishQuestion(actionReceipt);
      }
    }
  }

  async function dismissQuestion(actionToken: string): Promise<void> {
    const capability = decodePendingRequestActionToken(actionToken);
    if (isDisposed() || (capability?.kind !== 'question' && capability?.kind !== 'elicitation')
      || options.connection.value !== 'connected'
      || pendingRequestActions.questionActions.value[actionToken]) return;
    const actionReceipt = pendingRequestActions.beginQuestion(actionToken, 'dismiss');
    if (!actionReceipt) return;
    let retainResponseLock = false;
    try {
      const accepted = await respondPendingRequest(api, actionToken, 'cancel');
      if (isDisposed()) return;
      if (!accepted) {
        await settleRejectedPendingResponse(actionToken, 'The question dismissal');
        return;
      }
      retainResponseLock = true;
      pendingRequestActions.acceptQuestion(actionReceipt);
      await projection.refreshActiveThread();
    } catch (error) {
      if (!responseWasDefinitelyNotSent(error)) {
        retainResponseLock = true;
        pendingRequestActions.acceptQuestion(actionReceipt);
      }
      reportErrorIfCurrent(error, pendingRequestIsCurrent(actionToken));
    } finally {
      if (!isDisposed() && !retainResponseLock) {
        pendingRequestActions.finishQuestion(actionReceipt);
      }
    }
  }

  return {
    pendingRequestActions: {
      approvalActions: pendingRequestActions.approvalActions,
      questionActions: pendingRequestActions.questionActions,
      clear: pendingRequestActions.clear,
    },
    respondApproval,
    respondQuestion,
    dismissQuestion,
  };
}
