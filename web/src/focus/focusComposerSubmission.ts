import type { ComposerSubmission } from '../components/chat/composerSubmission';
import type { PromptAttachment } from '../types';

/** Settle the exact pending Composer payload from the mutation boundary result. */
export async function dispatchFocusComposerPayload(
  payload: ComposerSubmission,
  submit: (text: string, attachments: PromptAttachment[]) => Promise<boolean>,
): Promise<void> {
  try {
    if (await submit(payload.text, payload.attachments)) payload.commit();
    else payload.retain();
  } catch {
    payload.retain();
  }
}
