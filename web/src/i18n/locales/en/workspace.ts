export default {
  // Switcher
  switcherTitle: 'Switch workspace',
  switchTooltip: 'Switch workspace',
  eyebrow: 'Workspace',
  branchLabel: 'branch: {branch}',
  noBranch: 'no branch',
  sessionCount: '{count} session | {count} sessions',
  allWorkspaces: 'All workspaces',
  currentWorkspace: 'Current workspace only',
  addWorkspace: 'Add workspace…',
  noWorkspace: 'No workspace',
  deleteHasSessions: 'This workspace still has sessions — archive them before deleting it',
  // Column-header scope toggle
  scopeCurrent: 'this workspace',
  scopeAll: 'all workspaces',
  // Group headers (all-workspaces scope)
  newInGroup: 'New session in this workspace',
  // Add-workspace dialog
  recentLabel: 'Recent folders',
  filterPlaceholder: 'Filter subfolders…',
  // Attention marker
  attentionTitle: '{count} item needs your attention | {count} items need your attention',
  // Per-session pending tags (sidebar)
  awaitingAnswer: 'Answer',
  awaitingAnswerTitle: 'A question is waiting for your answer',
  awaitingPermission: 'Approve',
  awaitingPermissionTitle: 'An action is waiting for your approval',
  aborted: 'Stopped',
  abortedTitle: 'This session was interrupted before finishing',
} as const;
