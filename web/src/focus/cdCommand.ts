import type { WorkspaceDraftOpenOutcome } from './types';

/**
 * Focus Web's intentionally small literal slash-command surface.
 *
 * `/cd` is a browser navigation command, not a prompt for Codex.  The caller
 * still asks the server to admit the resulting cwd; this parser only keeps
 * command-shaped text from accidentally becoming agent input.
 */
export function parseCdCommand(text: string): { workspace: string } | null {
  const normalized = text.trim();
  if (normalized === '/cd') return { workspace: '' };
  const match = /^\/cd[ \t]+([^\r\n]+)$/u.exec(normalized);
  if (!match) return null;
  return { workspace: match[1]!.trim() };
}

export interface CdCommandExecution<TAttachment> {
  currentDirectory: () => string;
  openWorkspaceDraft: (workspace: string) => Promise<WorkspaceDraftOpenOutcome>;
  restoreDraft: (
    text: string,
    attachments: ReadonlyArray<TAttachment>,
  ) => Promise<boolean>;
  showCurrentDirectory: (directory: string) => void;
  showAttachmentsInvalidated: (count: number) => void;
}

/** Execute the complete composer-level `/cd` contract when text is a command. */
export async function executeCdCommand<TAttachment>(
  text: string,
  attachments: ReadonlyArray<TAttachment>,
  execution: CdCommandExecution<TAttachment>,
): Promise<boolean> {
  const command = parseCdCommand(text);
  if (!command) return false;
  if (!command.workspace) {
    execution.showCurrentDirectory(execution.currentDirectory() || '-');
    if (attachments.length > 0) await execution.restoreDraft('', attachments);
    return true;
  }

  const outcome = await execution.openWorkspaceDraft(command.workspace);
  if (outcome.status === 'superseded') return true;
  if (outcome.status === 'failed') {
    await execution.restoreDraft(text, attachments);
    return true;
  }
  if (attachments.length > 0) {
    if (outcome.attachmentDisposition === 'invalidated') {
      if (outcome.invalidatedAttachmentCount > 0) {
        execution.showAttachmentsInvalidated(outcome.invalidatedAttachmentCount);
      }
    } else if (
      outcome.attachmentDisposition === 'unchanged'
      || outcome.attachmentDisposition === 'rebound'
    ) {
      await execution.restoreDraft('', attachments);
    }
    // Composer clears its source session before emitting submit.  A committed
    // rebound therefore restores the payload into the now-authoritative draft
    // explicitly; a superseded result returned above can never do so.
  }
  return true;
}
