// apps/kimi-web/src/lib/swarmCardRows.ts
// Build the accordion row model for the AgentSwarm inline tool card from the
// bounded parent-history tool result. Focus does not join child threads or
// maintain a live child inventory; this stays pure so the presentation window
// can be tested without mounting the component.

import type { AgentPhase } from '../types';
import type { SwarmResult, SwarmResultSubagent } from './parseSwarmResult';

export interface SwarmCardRow {
  id: string;
  name: string;
  activity: string;
  phase: AgentPhase;
  body: string;
}

export const SWARM_HEAD_ROW_COUNT = 25;
export const SWARM_TAIL_ROW_COUNT = 25;

export interface SwarmCardRowWindow {
  head: readonly SwarmCardRow[];
  tail: readonly SwarmCardRow[];
  omittedRowCount: number;
}

export function buildSwarmCardRowWindow(
  rows: readonly SwarmCardRow[],
): SwarmCardRowWindow {
  const visibleCount = SWARM_HEAD_ROW_COUNT + SWARM_TAIL_ROW_COUNT;
  if (rows.length <= visibleCount) {
    return { head: rows, tail: [], omittedRowCount: 0 };
  }
  return {
    head: rows.slice(0, SWARM_HEAD_ROW_COUNT),
    tail: rows.slice(-SWARM_TAIL_ROW_COUNT),
    omittedRowCount: rows.length - visibleCount,
  };
}

function outcomeToPhase(outcome: string): AgentPhase {
  if (outcome === 'completed') return 'completed';
  if (outcome === 'failed' || outcome === 'aborted') return 'failed';
  return 'working';
}

function resultRow(sub: SwarmResultSubagent, index: number): SwarmCardRow {
  return {
    id: sub.agentId ?? sub.item ?? `result-${index}`,
    name: sub.item ?? `subagent ${index + 1}`,
    activity: sub.body.split('\n')[0] ?? '',
    phase: outcomeToPhase(sub.outcome),
    body: sub.body,
  };
}

export function buildSwarmCardRows(result: SwarmResult | null): SwarmCardRow[] {
  return result?.subagents.map((subagent, index) => resultRow(subagent, index)) ?? [];
}
