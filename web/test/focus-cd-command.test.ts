import { describe, expect, it, vi } from 'vitest';
import { executeCdCommand, parseCdCommand } from '../src/focus/cdCommand';

describe('Focus /cd command', () => {
  it('recognizes only a literal leading /cd command and preserves paths with spaces', () => {
    expect(parseCdCommand('/cd /work/project')).toEqual({ workspace: '/work/project' });
    expect(parseCdCommand('/cd /work/my project')).toEqual({ workspace: '/work/my project' });
    expect(parseCdCommand('/cd\tC:\\Users\\me\\project')).toEqual({ workspace: 'C:\\Users\\me\\project' });
  });

  it('keeps a directory query distinguishable from a normal prompt', () => {
    expect(parseCdCommand('/cd')).toEqual({ workspace: '' });
    expect(parseCdCommand('/cd   ')).toEqual({ workspace: '' });
  });

  it('leaves normal prompts and multiline input for Codex', () => {
    expect(parseCdCommand('/cd-not-a-command')).toBeNull();
    expect(parseCdCommand('please /cd /work')).toBeNull();
    expect(parseCdCommand('/cd /work\nthen explain this directory')).toBeNull();
  });

  it('shows the current directory for bare /cd and preserves attachments', async () => {
    const restoreDraft = vi.fn(async () => true);
    const showCurrentDirectory = vi.fn();
    const openWorkspaceDraft = vi.fn();

    const handled = await executeCdCommand('/cd', ['attachment-1'], {
      currentDirectory: () => '/work/current',
      openWorkspaceDraft,
      restoreDraft,
      showCurrentDirectory,
      showAttachmentsInvalidated: vi.fn(),
    });

    expect(handled).toBe(true);
    expect(showCurrentDirectory).toHaveBeenCalledWith('/work/current');
    expect(restoreDraft).toHaveBeenCalledWith('', ['attachment-1']);
    expect(openWorkspaceDraft).not.toHaveBeenCalled();
  });

  it('admits a literal workspace before consuming attachments from the old scope', async () => {
    const restoreDraft = vi.fn(async () => true);
    const showAttachmentsInvalidated = vi.fn();
    const openWorkspaceDraft = vi.fn(async () => ({
      status: 'committed' as const,
      committed: true as const,
      workspace: '/work/new',
      scopeChanged: true,
      previousComposerScopeId: 'tab-1:generation:1:draft:/work/old',
      currentComposerScopeId: 'tab-1:generation:2:draft:/work/new',
      attachmentDisposition: 'invalidated' as const,
      composerScopeEffect: 'apply' as const,
      invalidatedAttachmentCount: 1,
      reboundAttachmentCount: 0,
    }));

    const handled = await executeCdCommand('/cd /work/new', ['attachment-1'], {
      currentDirectory: () => '/work/old',
      openWorkspaceDraft,
      restoreDraft,
      showCurrentDirectory: vi.fn(),
      showAttachmentsInvalidated,
    });

    expect(handled).toBe(true);
    expect(openWorkspaceDraft).toHaveBeenCalledWith('/work/new');
    expect(showAttachmentsInvalidated).toHaveBeenCalledWith(1);
    expect(restoreDraft).not.toHaveBeenCalled();
  });

  it('restores the exact command draft when server-side cwd admission fails', async () => {
    const restoreDraft = vi.fn(async () => true);

    const handled = await executeCdCommand('/cd /missing', ['attachment-1'], {
      currentDirectory: () => '/work/old',
      openWorkspaceDraft: vi.fn(async () => ({
        status: 'failed' as const,
        committed: false as const,
        workspace: '' as const,
        scopeChanged: false as const,
        previousComposerScopeId: '' as const,
        currentComposerScopeId: '' as const,
        attachmentDisposition: 'unchanged' as const,
        composerScopeEffect: 'none' as const,
        invalidatedAttachmentCount: 0 as const,
        reboundAttachmentCount: 0 as const,
      })),
      restoreDraft,
      showCurrentDirectory: vi.fn(),
      showAttachmentsInvalidated: vi.fn(),
    });

    expect(handled).toBe(true);
    expect(restoreDraft).toHaveBeenCalledWith('/cd /missing', ['attachment-1']);
  });

  it('preserves attachments when the server confirms there was no scope change', async () => {
    const restoreDraft = vi.fn(async () => true);

    await executeCdCommand('/cd /work/current', ['attachment-1'], {
      currentDirectory: () => '/work/current',
      openWorkspaceDraft: vi.fn(async () => ({
        status: 'committed' as const,
        committed: true as const,
        workspace: '/work/current',
        scopeChanged: false,
        previousComposerScopeId: '',
        currentComposerScopeId: 'tab-1:generation:1:draft:/work/current',
        attachmentDisposition: 'unchanged' as const,
        composerScopeEffect: 'none' as const,
        invalidatedAttachmentCount: 0,
        reboundAttachmentCount: 0,
      })),
      restoreDraft,
      showCurrentDirectory: vi.fn(),
      showAttachmentsInvalidated: vi.fn(),
    });

    expect(restoreDraft).toHaveBeenCalledWith('', ['attachment-1']);
  });

  it('restores same-cwd rebound payload into the authoritative target composer', async () => {
    let targetComposerAttachments: string[] = [];
    const restoreDraft = vi.fn(async (_text: string, attachments: ReadonlyArray<string>) => {
      targetComposerAttachments = [...attachments];
      return true;
    });
    const showAttachmentsInvalidated = vi.fn();

    await executeCdCommand('/cd /work/current', ['attachment-1'], {
      currentDirectory: () => '/work/current',
      openWorkspaceDraft: vi.fn(async () => ({
        status: 'committed' as const,
        committed: true as const,
        workspace: '/work/current',
        scopeChanged: true,
        previousComposerScopeId: 'tab-1:generation:1:thread:thread-1',
        currentComposerScopeId: 'tab-1:generation:2:draft:/work/current',
        attachmentDisposition: 'rebound' as const,
        composerScopeEffect: 'apply' as const,
        invalidatedAttachmentCount: 0,
        reboundAttachmentCount: 1,
      })),
      restoreDraft,
      showCurrentDirectory: vi.fn(),
      showAttachmentsInvalidated,
    });

    expect(restoreDraft).toHaveBeenCalledWith('', ['attachment-1']);
    expect(targetComposerAttachments).toEqual(['attachment-1']);
    // A following ordinary submit reads the same target-composer chip.
    expect(targetComposerAttachments.map((fileId) => ({ fileId }))).toEqual([
      { fileId: 'attachment-1' },
    ]);
    expect(showAttachmentsInvalidated).not.toHaveBeenCalled();
  });

  it('does not restore an old command into a newer superseding composer intent', async () => {
    const restoreDraft = vi.fn(async () => true);

    await executeCdCommand('/cd /work/old-intent', ['attachment-1'], {
      currentDirectory: () => '/work/current',
      openWorkspaceDraft: vi.fn(async () => ({
        status: 'superseded' as const,
        committed: false,
        workspace: '',
        scopeChanged: false,
        previousComposerScopeId: '',
        currentComposerScopeId: '',
        attachmentDisposition: 'unchanged' as const,
        composerScopeEffect: 'none' as const,
        invalidatedAttachmentCount: 0,
        reboundAttachmentCount: 0,
      })),
      restoreDraft,
      showCurrentDirectory: vi.fn(),
      showAttachmentsInvalidated: vi.fn(),
    });

    expect(restoreDraft).not.toHaveBeenCalled();
  });
});
