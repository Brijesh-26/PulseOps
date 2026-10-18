import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { post } from "../api/client";
import type { Evidence, Impact, ImpactTask, Investigation, Proposal, TimelineEntry } from "../api/types";
import { useAuth } from "../auth";
import { Badge, Card, ConfidenceBar, Empty, ErrorState, EvidenceChips, fmtTime } from "./ui";

type Focus = (code: string) => void;

export function InvestigationPanel({ run, onFocus }: { run: Investigation | null; onFocus: Focus }) {
  if (!run) return <Card title="Investigation"><Empty>No investigation has run yet.</Empty></Card>;
  const r = run.result;
  return (
    <Card
      title="Investigation"
      actions={
        <span className="row small muted">
          <Badge value={run.status} /> {run.provider}
          {run.model ? ` · ${run.model}` : ""} · schema v{run.schema_version}
        </span>
      }
    >
      {run.status === "PENDING" || run.status === "RUNNING" ? (
        <div className="notice" role="status">Investigation {run.status.toLowerCase()}…</div>
      ) : null}
      {run.status === "FAILED" && (
        <div className="notice bad" role="alert">
          Investigation failed: <span className="mono">{run.error_code}</span> — {run.error_message}
        </div>
      )}
      {r && (
        <>
          <p>{r.investigation_summary}</p>
          <dl className="kv">
            <dt>Confidence</dt>
            <dd>
              <ConfidenceBar value={r.confidence.overall} />{" "}
              <span className="muted small">({r.confidence.calibration}) {r.confidence.rationale}</span>
            </dd>
            <dt>Uncertainty</dt>
            <dd>{r.uncertainty}</dd>
          </dl>

          <h3>Observed facts</h3>
          {r.observed_facts.length ? (
            <ul className="plain">
              {r.observed_facts.map((f, i) => (
                <li key={i}>
                  {f.statement} <EvidenceChips codes={f.evidence_ids} onSelect={onFocus} />
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">None recorded.</p>
          )}

          <h3>Root-cause hypotheses</h3>
          {run.hypotheses.map((h) => (
            <div key={h.rank} className={`hyp ${h.is_supported ? "" : "unsupported"}`}>
              <div className="hyp-head">
                <strong>
                  #{h.rank} {h.title}
                </strong>
                <span className="row">
                  <Badge value={h.category} kind="plain" />
                  <ConfidenceBar value={h.confidence} />
                </span>
              </div>
              <p className="small">{h.description}</p>
              <div className="small">
                Supports: <EvidenceChips codes={h.supporting_evidence} onSelect={onFocus} />
              </div>
              {h.contradicting_evidence.length > 0 && (
                <div className="small">
                  Contradicts: <EvidenceChips codes={h.contradicting_evidence} onSelect={onFocus} />
                </div>
              )}
              {!h.is_supported && <div className="small notice warn">Not supported by valid evidence.</div>}
              {h.validation_notes && <div className="small muted">Validator: {h.validation_notes}</div>}
            </div>
          ))}

          {r.missing_information.length > 0 && (
            <>
              <h3>Missing information</h3>
              <ul className="plain">{r.missing_information.map((m) => <li key={m}>{m}</li>)}</ul>
            </>
          )}
          {r.recommended_diagnostics.length > 0 && (
            <>
              <h3>Recommended next diagnostics</h3>
              <ul className="plain">{r.recommended_diagnostics.map((m) => <li key={m}>{m}</li>)}</ul>
            </>
          )}
          {r.suspicious_evidence_ids.length > 0 && (
            <div className="notice warn">
              Suspicious, instruction-like content was ignored as evidence:{" "}
              <EvidenceChips codes={r.suspicious_evidence_ids} onSelect={onFocus} />
            </div>
          )}
          {run.validation_issues.length > 0 && (
            <>
              <h3>Backend validation ({run.validation_issues.length})</h3>
              <ul className="plain small">
                {run.validation_issues.map((v, i) => (
                  <li key={i}>
                    <span className="mono">{v.type}</span> {v.path && <span className="muted">[{v.path}]</span>} {v.message}
                  </li>
                ))}
              </ul>
            </>
          )}
          {run.tool_calls.length > 0 && (
            <p className="muted small">
              Tools used: {run.tool_calls.map((t) => t.tool).join(", ")} · evidence presented: {run.evidence_codes.length}
            </p>
          )}
        </>
      )}
    </Card>
  );
}

export function ProposalPanel({ proposal, incidentId, onFocus }: { proposal: Proposal | undefined; incidentId: string; onFocus: Focus }) {
  const { session, can } = useAuth();
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const decide = useMutation({
    mutationFn: (kind: "approve" | "reject") =>
      post<Proposal>(`/api/v1/proposals/${proposal!.id}/${kind}`, { reason: reason || undefined }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["incident", incidentId] }),
  });

  if (!proposal) return <Card title="Recovery proposal"><Empty>No recovery action proposed.</Empty></Card>;
  const pending = proposal.status === "PENDING_APPROVAL";
  const own = proposal.proposed_by === session?.principal;
  const ex = proposal.execution;
  return (
    <Card title="Recovery proposal" actions={<Badge value={proposal.status} />}>
      <dl className="kv">
        <dt>Action</dt>
        <dd className="mono">{proposal.action_type}</dd>
        <dt>Parameters</dt>
        <dd className="mono small">{JSON.stringify(proposal.parameters)}</dd>
        <dt>Proposed by</dt>
        <dd>
          {proposal.proposed_by} <span className="muted small">({proposal.proposer_type})</span>
        </dd>
        <dt>Evidence</dt>
        <dd><EvidenceChips codes={proposal.evidence_codes} onSelect={onFocus} /></dd>
        <dt>Expires</dt>
        <dd className="small">{fmtTime(proposal.expires_at)}</dd>
      </dl>
      <p className="small">{proposal.rationale}</p>
      {proposal.status_reason && <div className="notice warn small">{proposal.status_reason}</div>}

      {pending && can("proposals:decide") && !own && (
        <div>
          <label htmlFor="reason" className="small muted">Reason (required to reject)</label>
          <textarea id="reason" value={reason} onChange={(e) => setReason(e.target.value)} />
          <div className="row" style={{ marginTop: 8 }}>
            <button className="btn primary" disabled={decide.isPending} onClick={() => decide.mutate("approve")}>
              Approve &amp; run in simulator
            </button>
            <button className="btn danger" disabled={decide.isPending || reason.trim().length < 3} onClick={() => decide.mutate("reject")}>
              Reject
            </button>
          </div>
        </div>
      )}
      {pending && own && <p className="muted small">You proposed this action; another approver must decide.</p>}
      {pending && !can("proposals:decide") && <p className="muted small">Waiting for an approver.</p>}
      {decide.isError && <ErrorState error={decide.error} />}
      {decide.isSuccess && <div className="notice ok" role="status">Decision recorded.</div>}

      {proposal.decision && (
        <p className="small">
          <strong>{proposal.decision.decision}</strong> by {proposal.decision.decided_by} at {fmtTime(proposal.decision.decided_at)}
          {proposal.decision.reason ? ` — “${proposal.decision.reason}”` : ""}
        </p>
      )}
      {ex && (
        <>
          <h3>Simulated execution</h3>
          <dl className="kv small">
            <dt>Status</dt>
            <dd><Badge value={ex.status} /></dd>
            <dt>Postcondition</dt>
            <dd>{ex.postcondition_ok === null ? "—" : ex.postcondition_ok ? "verified" : "NOT met"}</dd>
            <dt>Preconditions</dt>
            <dd>
              {ex.preconditions.map((c) => (
                <div key={c.name}>
                  {c.ok ? "✓" : "✗"} <span className="mono">{c.name}</span> <span className="muted">{c.detail}</span>
                </div>
              ))}
            </dd>
            <dt>Output</dt>
            <dd className="mono">{JSON.stringify(ex.output)}</dd>
            {ex.error && (
              <>
                <dt>Error</dt>
                <dd className="mono">{ex.error}</dd>
              </>
            )}
          </dl>
        </>
      )}
    </Card>
  );
}

interface TreeNode {
  name: string;
  info?: ImpactTask;
  children: Map<string, TreeNode>;
}

function buildTree(origin: string, tasks: ImpactTask[]): TreeNode {
  const root: TreeNode = { name: origin, children: new Map() };
  for (const t of tasks) {
    let node = root;
    for (const step of t.path.slice(1)) {
      if (!node.children.has(step)) node.children.set(step, { name: step, children: new Map() });
      node = node.children.get(step)!;
    }
    node.info = t;
  }
  return root;
}

function Tree({ node }: { node: TreeNode }) {
  return (
    <li>
      <span className="node">{node.name}</span>{" "}
      {node.info?.latest_status && <Badge value={node.info.latest_status} />}{" "}
      {node.info?.datasets.length ? <span className="muted small">→ {node.info.datasets.join(", ")}</span> : null}
      {node.children.size > 0 && (
        <ul className="tree" style={{ paddingLeft: 18 }}>
          {[...node.children.values()].map((c) => <Tree key={c.name} node={c} />)}
        </ul>
      )}
    </li>
  );
}

export function ImpactPanel({ impact }: { impact: Impact | undefined }) {
  if (!impact) return <Card title="Dependency impact"><Empty>Loading impact…</Empty></Card>;
  if (!impact.origin) return <Card title="Dependency impact"><Empty>No task associated.</Empty></Card>;
  return (
    <Card title="Dependency impact">
      <p className="small muted">Computed from the dependency graph, not by the AI.</p>
      {impact.upstream.length > 0 && (
        <>
          <h3>Upstream</h3>
          <ul className="tree">
            {impact.upstream.map((u) => (
              <li key={u.task}>
                <span className="node">{u.task}</span> {u.latest_status && <Badge value={u.latest_status} />}
              </li>
            ))}
          </ul>
        </>
      )}
      <h3>Downstream ({impact.downstream.length} affected)</h3>
      <ul className="tree">
        <Tree node={buildTree(impact.origin, impact.downstream)} />
      </ul>
      {impact.datasets.length > 0 && (
        <>
          <h3>Datasets</h3>
          <ul className="plain small">
            {impact.datasets.map((d) => (
              <li key={d.key}>
                <span className="mono">{d.key}</span> {d.is_stale ? <Badge value="STALE" /> : <span className="muted">fresh</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  );
}

export function EvidencePanel({ evidence, focus }: { evidence: Evidence[] | undefined; focus: string | null }) {
  const [filter, setFilter] = useState("");
  if (!evidence) return <Card title="Evidence"><Empty>Loading evidence…</Empty></Card>;
  const shown = evidence.filter(
    (e) => !filter || `${e.code} ${e.kind} ${e.title} ${e.content}`.toLowerCase().includes(filter.toLowerCase()),
  );
  return (
    <Card title={`Evidence (${evidence.length})`} actions={<input aria-label="Filter evidence" placeholder="filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />}>
      <p className="small muted">Logs and payloads are untrusted data and are shown as plain text.</p>
      {shown.length === 0 ? (
        <Empty>No evidence.</Empty>
      ) : (
        shown.map((e) => (
          <div key={e.id} id={`ev-${e.code}`} className={`evidence ${focus === e.code ? "focus" : ""}`}>
            <div className="row">
              <span className="chip">{e.code}</span>
              <Badge value={e.kind} kind="plain" />
              <strong className="small">{e.title}</strong>
              <span className="muted small">
                {e.collected_by} · {fmtTime(e.observed_at ?? e.collected_at)}
              </span>
            </div>
            <pre>{e.content}</pre>
          </div>
        ))
      )}
    </Card>
  );
}

export function TimelinePanel({ entries }: { entries: TimelineEntry[] | undefined }) {
  return (
    <Card title="Timeline">
      {!entries ? (
        <Empty>Loading…</Empty>
      ) : entries.length === 0 ? (
        <Empty>No activity.</Empty>
      ) : (
        <ul className="timeline">
          {entries.map((t, i) => (
            <li key={i}>
              <span className="when">{fmtTime(t.at)}</span>
              <span>
                <span className="mono small">{t.title}</span>
                {t.actor && <span className="muted small"> · {t.actor}</span>}
                {"reason" in t.details && t.details.reason ? <div className="small muted">{String(t.details.reason)}</div> : null}
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

export function HistoryPanel({ runs, proposals }: { runs: Investigation[] | undefined; proposals: Proposal[] }) {
  return (
    <Card title="Investigation & proposal history">
      <h3>Investigations</h3>
      {!runs?.length ? (
        <p className="muted small">None.</p>
      ) : (
        <table>
          <tbody>
            {runs.map((r) => (
              <tr key={r.id}>
                <td className="small">{fmtTime(r.created_at)}</td>
                <td><Badge value={r.status} /></td>
                <td className="small">{r.trigger} by {r.requested_by}</td>
                <td><ConfidenceBar value={r.overall_confidence} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h3>Proposals</h3>
      {proposals.length === 0 ? (
        <p className="muted small">None.</p>
      ) : (
        <table>
          <tbody>
            {proposals.map((p) => (
              <tr key={p.id}>
                <td className="small">{fmtTime(p.created_at)}</td>
                <td className="mono small">{p.action_type}</td>
                <td><Badge value={p.status} /></td>
                <td className="small muted">{p.decision ? `${p.decision.decision} by ${p.decision.decided_by}` : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}
