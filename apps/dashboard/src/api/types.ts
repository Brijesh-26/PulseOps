// Types mirror the backend response models in pulseops/api/schemas.py.

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface IncidentSummary {
  id: string;
  display_id: string;
  title: string;
  status: string;
  severity: string;
  category: string;
  pipeline: string;
  task: string | null;
  opened_at: string;
  updated_at: string;
  last_event_at: string;
  resolved_at: string | null;
  closed_at: string | null;
  event_count: number;
  version: number;
  investigation_status: string | null;
  latest_confidence: number | null;
  pending_proposal_id: string | null;
}

export interface Hypothesis {
  rank: number;
  title: string;
  description: string;
  category: string;
  confidence: number;
  is_supported: boolean;
  validation_notes: string | null;
  supporting_evidence: string[];
  contradicting_evidence: string[];
}

export interface InvestigationResult {
  investigation_summary: string;
  observed_facts: { statement: string; evidence_ids: string[] }[];
  confidence: { overall: number; calibration: string; rationale: string };
  uncertainty: string;
  missing_information: string[];
  recommended_diagnostics: string[];
  suspicious_evidence_ids: string[];
  downstream_impact: { affected_tasks: string[]; affected_datasets: string[]; explanation: string };
  explanation: string;
}

export interface Investigation {
  id: string;
  status: string;
  trigger: string;
  requested_by: string;
  provider: string;
  model: string | null;
  schema_version: string;
  attempts: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  error_code: string | null;
  error_message: string | null;
  summary: string | null;
  overall_confidence: number | null;
  result: InvestigationResult | null;
  validation_issues: { type: string; message: string; path: string }[];
  tool_calls: { tool: string; args: Record<string, unknown>; returned?: string[] }[];
  evidence_codes: string[];
  hypotheses: Hypothesis[];
}

export interface Proposal {
  id: string;
  incident_id: string;
  investigation_run_id: string | null;
  action_type: string;
  parameters: Record<string, unknown>;
  rationale: string;
  evidence_codes: string[];
  proposed_by: string;
  proposer_type: string;
  status: string;
  status_reason: string | null;
  incident_version: number;
  created_at: string;
  expires_at: string;
  decision: { decision: string; decided_by: string; decided_at: string; reason: string | null } | null;
  execution: {
    id: string;
    status: string;
    started_at: string;
    finished_at: string | null;
    preconditions: { name: string; ok: boolean; detail: string }[];
    output: Record<string, unknown>;
    postcondition_ok: boolean | null;
    error: string | null;
  } | null;
}

export interface IncidentDetail extends IncidentSummary {
  fingerprint: string;
  latest_investigation: Investigation | null;
  proposals: Proposal[];
}

export interface Evidence {
  id: string;
  code: string;
  kind: string;
  title: string;
  content: string;
  data: Record<string, unknown>;
  observed_at: string | null;
  collected_by: string;
  collected_at: string;
}

export interface TimelineEntry {
  at: string;
  kind: "event" | "audit";
  title: string;
  actor: string | null;
  details: Record<string, unknown>;
}

export interface ImpactTask {
  task: string;
  depth: number;
  path: string[];
  datasets: string[];
  latest_status: string | null;
}

export interface Impact {
  origin: string | null;
  downstream: ImpactTask[];
  upstream: ImpactTask[];
  datasets: { key: string; is_stale: boolean; stale_reason: string | null }[];
}

export interface AuditRecord {
  id: number;
  occurred_at: string;
  actor: string;
  actor_type: string;
  action: string;
  entity_type: string;
  entity_id: string;
  incident_id: string | null;
  correlation_id: string | null;
  details: Record<string, unknown>;
  hash: string;
}

export interface Stats {
  generated_at: string;
  open_incidents: number;
  critical_open_incidents: number;
  by_status: Record<string, number>;
  by_severity_open: Record<string, number>;
  failures_24h: number;
  recent_failures: {
    event_id: string;
    event_type: string;
    severity: string;
    task: string;
    occurred_at: string;
    incident_id: string | null;
  }[];
  resolved_7d: number;
  mean_time_to_resolve_minutes_30d: number | null;
  pending_approvals: number;
  pipelines: {
    pipeline: string;
    name: string;
    runs_7d: number;
    failures_7d: number;
    success_rate_7d: number | null;
    open_incidents: number;
  }[];
}

export interface Session {
  token: string;
  expires_at: number;
  principal: string;
  roles: string[];
  permissions: string[];
}

export interface ScenarioInfo {
  key: string;
  title: string;
  description: string;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details: unknown; correlation_id: string | null };
}
