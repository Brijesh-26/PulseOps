import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { api, post } from "../api/client";
import type { AuditRecord, Evidence, Impact, IncidentDetail, Investigation, TimelineEntry } from "../api/types";
import { useAuth } from "../auth";
import {
  EvidencePanel,
  HistoryPanel,
  ImpactPanel,
  InvestigationPanel,
  ProposalPanel,
  ProposeFixPanel,
  TimelinePanel,
} from "../components/IncidentPanels";
import { Badge, Card, ErrorState, Loading, fmtTime } from "../components/ui";

const ACTIVE = ["PENDING", "RUNNING"];

export default function IncidentDetailPage() {
  const { id = "" } = useParams();
  const { can } = useAuth();
  const qc = useQueryClient();
  const [focus, setFocus] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  const detail = useQuery({
    queryKey: ["incident", id, "detail"],
    queryFn: () => api<IncidentDetail>(`/api/v1/incidents/${id}`),
    refetchInterval: (q) =>
      q.state.data && (ACTIVE.includes(q.state.data.latest_investigation?.status ?? "") || q.state.data.status === "RECOVERING")
        ? 1500
        : 5000,
  });
  const evidence = useQuery({ queryKey: ["incident", id, "evidence"], queryFn: () => api<Evidence[]>(`/api/v1/incidents/${id}/evidence`), refetchInterval: 5000 });
  const impact = useQuery({ queryKey: ["incident", id, "impact"], queryFn: () => api<Impact>(`/api/v1/incidents/${id}/impact`), refetchInterval: 10000 });
  const timeline = useQuery({ queryKey: ["incident", id, "timeline"], queryFn: () => api<TimelineEntry[]>(`/api/v1/incidents/${id}/timeline`), refetchInterval: 5000 });
  const runs = useQuery({ queryKey: ["incident", id, "runs"], queryFn: () => api<Investigation[]>(`/api/v1/incidents/${id}/investigations`), refetchInterval: 5000 });
  const audit = useQuery({
    queryKey: ["incident", id, "audit"],
    queryFn: () => api<AuditRecord[]>(`/api/v1/incidents/${id}/audit`),
    enabled: can("audit:read"),
  });

  const refresh = () => qc.invalidateQueries({ queryKey: ["incident", id] });
  const investigate = useMutation({ mutationFn: () => post(`/api/v1/incidents/${id}/investigate`, {}), onSettled: refresh });
  const transition = useMutation({
    mutationFn: (status: string) => post(`/api/v1/incidents/${id}/transitions`, { status, reason }),
    onSettled: refresh,
  });

  if (detail.isPending) return <Loading />;
  if (detail.isError) return <ErrorState error={detail.error} onRetry={() => detail.refetch()} />;
  const inc = detail.data;

  function focusEvidence(code: string) {
    setFocus(code);
    document.getElementById(`ev-${code}`)?.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  const canInvestigate = can("investigations:run") && ["OPEN", "ACTION_REQUIRED"].includes(inc.status);
  const manual: Record<string, string[]> = { OPEN: ["RESOLVED", "CLOSED"], RESOLVED: ["CLOSED", "OPEN"] };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="small muted">
            <Link to="/incidents">Incidents</Link> / {inc.display_id}
          </div>
          <h1>{inc.title}</h1>
          <div className="row small" style={{ marginTop: 6 }}>
            <Badge value={inc.status} />
            <Badge value={inc.severity} kind="severity" />
            <Badge value={inc.category} kind="plain" />
            <span className="mono">{inc.task ?? inc.pipeline}</span>
            <span className="muted">
              opened {fmtTime(inc.opened_at)} · {inc.event_count} event(s) · v{inc.version}
            </span>
          </div>
        </div>
        <div className="row">
          {canInvestigate && (
            <button className="btn" disabled={investigate.isPending} onClick={() => investigate.mutate()}>
              Re-investigate
            </button>
          )}
        </div>
      </div>
      {investigate.isError && <ErrorState error={investigate.error} />}

      <div className="detail-grid">
        <div>
          <InvestigationPanel run={inc.latest_investigation} onFocus={focusEvidence} />
          <EvidencePanel evidence={evidence.data} focus={focus} />
        </div>
        <div>
          <ProposalPanel proposal={inc.proposals[0]} incidentId={id} onFocus={focusEvidence} />
          {can("proposals:create") && inc.status === "OPEN" && (
            <ProposeFixPanel incidentId={id} task={inc.task} evidence={evidence.data} impact={impact.data} />
          )}
          <ImpactPanel impact={impact.data} />
          {can("incidents:transition") && manual[inc.status] && (
            <Card title="Manual transition">
              <input
                aria-label="Transition reason"
                placeholder="reason"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                style={{ width: "100%", marginBottom: 8 }}
              />
              <div className="row">
                {manual[inc.status]!.map((s) => (
                  <button key={s} className="btn" disabled={reason.trim().length < 3 || transition.isPending} onClick={() => transition.mutate(s)}>
                    Mark {s.toLowerCase()}
                  </button>
                ))}
              </div>
              {transition.isError && <ErrorState error={transition.error} />}
            </Card>
          )}
          <TimelinePanel entries={timeline.data} />
          <HistoryPanel runs={runs.data} proposals={inc.proposals} />
          {can("audit:read") && (
            <Card title="Audit history">
              {audit.isPending ? (
                <Loading />
              ) : audit.isError ? (
                <ErrorState error={audit.error} />
              ) : (
                <ul className="timeline">
                  {audit.data.map((a) => (
                    <li key={a.id}>
                      <span className="when">#{a.id} {fmtTime(a.occurred_at)}</span>
                      <span className="small">
                        <span className="mono">{a.action}</span> · {a.actor}
                        <div className="mono muted" title="record hash">{a.hash.slice(0, 16)}…</div>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
