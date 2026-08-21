import type {
  FocusOperatorStatus,
  FocusOperatorStatusFreshness,
  FocusOperatorWarning,
} from './types';

const MAX_PRESENTED_WARNINGS = 20;
const MAX_PRESENTED_DETAILS = 6;
const MAX_CODE_LENGTH = 96;
const MAX_SOURCE_LENGTH = 96;
const MAX_MESSAGE_LENGTH = 480;
const MAX_DETAIL_KEY_LENGTH = 80;
const MAX_DETAIL_VALUE_LENGTH = 160;

export interface OperatorWarningDetailPresentation {
  key: string;
  value: string;
}

export interface OperatorWarningPresentation {
  code: string;
  source: string;
  message: string;
  severity: FocusOperatorWarning['severity'];
  attention: 'advisory' | 'correctness';
  firstSeenAt: number;
  lastSeenAt: number;
  occurrences: number;
  details: OperatorWarningDetailPresentation[];
  detailsOmitted: boolean;
}

export interface OperatorStatusPresentation {
  warningCount: number;
  primaryWarningCount: number;
  advisoryWarningCount: number;
  errorWarningCount: number;
  omittedWarningCount: number;
  warnings: OperatorWarningPresentation[];
  degradedWithoutDetails: boolean;
  warningsAreLastKnown: boolean;
}

function boundedText(value: string, limit: number): string {
  if (value.length <= limit) return value;
  return `${value.slice(0, Math.max(0, limit - 1))}…`;
}

function scalarDetailValue(value: unknown): string | null {
  if (value === null) return 'null';
  if (typeof value === 'string') return boundedText(value, MAX_DETAIL_VALUE_LENGTH);
  if (typeof value === 'boolean') return String(value);
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
}

function projectWarning(warning: FocusOperatorWarning): OperatorWarningPresentation {
  const details: OperatorWarningDetailPresentation[] = [];
  let detailsOmitted = false;
  let inspectedDetailCount = 0;
  for (const key in warning.details) {
    // Wire admission accepts only ordinary or null-prototype records. Stop at
    // an inherited enumerable field rather than walking a mutated prototype.
    if (!Object.prototype.hasOwnProperty.call(warning.details, key)) break;
    // Entering one additional own field proves that the bounded candidate set
    // omitted something without enumerating or allocating the full key set.
    if (inspectedDetailCount >= MAX_PRESENTED_DETAILS) {
      detailsOmitted = true;
      break;
    }
    inspectedDetailCount += 1;
    const value = warning.details[key];
    const presented = scalarDetailValue(value);
    if (presented === null) {
      detailsOmitted = true;
      continue;
    }
    details.push({
      key: boundedText(key, MAX_DETAIL_KEY_LENGTH),
      value: presented,
    });
  }
  return {
    code: boundedText(warning.code, MAX_CODE_LENGTH),
    source: boundedText(warning.source, MAX_SOURCE_LENGTH),
    message: boundedText(warning.message, MAX_MESSAGE_LENGTH),
    severity: warning.severity,
    // attention becomes required in Focus wire v10. Keep the fallback
    // fail-prominent while the generated browser catalog is being updated in
    // the adjacent wire slice, and for any untyped test fixture.
    attention: warning.attention === 'advisory' ? 'advisory' : 'correctness',
    firstSeenAt: warning.first_seen_at,
    lastSeenAt: warning.last_seen_at,
    occurrences: warning.occurrences,
    details,
    detailsOmitted,
  };
}

/**
 * Convert the admitted operator projection into bounded, text-only browser
 * rows. Runtime-loop internals and nested detail objects deliberately have no
 * presentation path here; logs remain the forensic source.
 */
export function projectOperatorStatusPresentation(
  status: FocusOperatorStatus | null,
  freshness: FocusOperatorStatusFreshness,
): OperatorStatusPresentation {
  const admittedWarnings = status?.warnings ?? [];
  const warningCount = admittedWarnings.length;
  const warnings = admittedWarnings
    .slice(0, MAX_PRESENTED_WARNINGS)
    .map(projectWarning);
  const omittedWarningCount = Math.max(0, warningCount - warnings.length);
  const primaryWarningCount = admittedWarnings.filter((warning) => (
    warning.severity === 'error' || warning.attention !== 'advisory'
  )).length;
  const errorWarningCount = admittedWarnings.filter(
    (warning) => warning.severity === 'error',
  ).length;
  return {
    warningCount,
    primaryWarningCount,
    advisoryWarningCount: warningCount - primaryWarningCount,
    errorWarningCount,
    omittedWarningCount,
    warnings,
    degradedWithoutDetails: freshness === 'fresh'
      && status !== null
      && status.status !== 'ok'
      && warningCount === 0,
    warningsAreLastKnown: freshness === 'stale' && warningCount > 0,
  };
}
