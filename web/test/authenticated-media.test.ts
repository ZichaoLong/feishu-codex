import { afterEach, describe, expect, it, vi } from 'vitest';

import { loadAuthenticatedMediaBlob } from '../src/lib/authenticatedMedia';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('loadAuthenticatedMediaBlob', () => {
  it('uses Focus supplied attachment loading instead of Kimi daemon files', async () => {
    const focusAttachmentBlob = new Blob(['focus attachment']);
    const focusLoader = vi.fn().mockResolvedValue(focusAttachmentBlob);

    await expect(loadAuthenticatedMediaBlob('attachment-1', focusLoader)).resolves.toBe(focusAttachmentBlob);

    expect(focusLoader).toHaveBeenCalledWith('attachment-1');
  });

  it('fails closed instead of discovering an implicit product transport', async () => {
    await expect(loadAuthenticatedMediaBlob('unowned-file')).rejects.toThrow(
      'Authenticated media requires an explicit byte loader.',
    );
  });
});
