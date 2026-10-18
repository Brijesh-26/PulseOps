import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import type { IncidentSummary, Page } from "../api/types";
import { Badge, Card, ConfidenceBar, Empty, ErrorState, Loading, relTime } from "../components/ui";

const STATUSES = ["OPEN", "INVESTIGATING", "ACTION_REQUIRED", "RECOVERING", "RESOLVED", "CLOSED"];
const SEVERITIES = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];
const CATEGORIES = ["TASK_FAILURE", "LATENCY_ANOMALY", "DEPENDENCY_FAILURE"];
const LIMIT = 20;

export default function IncidentsPage() {
  const [params, setParams] = useSearchParams();
  const offset = Number(params.get("offset") ?? 0);
  const qs = new URLSearchParams();
  for (const k of ["status", "severity", "category", "pipeline", "q"]) {
    const v = params.get(k);
    if (v) qs.set(k, v);
  }
  qs.set("limit", String(LIMIT));
  qs.set("offset", String(offset));

  const q = useQuery({
    queryKey: ["incidents", qs.toString()],
    queryFn: () => api<Page<IncidentSummary>>(`/api/v1/incidents?${qs}`),
    refetchInterval: 5000,
    placeholderData: keepPreviousData,
  });

  function set(key: string, value: string) {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    next.delete("offset");
    setParams(next);
  }

  return (
    <>
      <div className="page-head">
        <h1>Incidents</h1>
      </div>
      <Card>
        <div className="filters" role="search">
          <input
            aria-label="Search incidents"
            placeholder="Search title or task…"
            defaultValue={params.get("q") ?? ""}
            onKeyDown={(e) => e.key === "Enter" && set("q", (e.target as HTMLInputElement).value)}
          />
          <select aria-label="Status" value={params.get("status") ?? ""} onChange={(e) => set("status", e.target.value)}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <select aria-label="Severity" value={params.get("severity") ?? ""} onChange={(e) => set("severity", e.target.value)}>
            <option value="">All severities</option>
            {SEVERITIES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <select aria-label="Category" value={params.get("category") ?? ""} onChange={(e) => set("category", e.target.value)}>
            <option value="">All categories</option>
            {CATEGORIES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <input
            aria-label="Pipeline"
            placeholder="Pipeline key"
            defaultValue={params.get("pipeline") ?? ""}
            onKeyDown={(e) => e.key === "Enter" && set("pipeline", (e.target as HTMLInputElement).value)}
          />
        </div>
        {q.isPending ? (
          <Loading />
        ) : q.isError ? (
          <ErrorState error={q.error} onRetry={() => q.refetch()} />
        ) : q.data.items.length === 0 ? (
          <Empty>No incidents match these filters.</Empty>
        ) : (
          <>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Incident</th>
                    <th>Severity</th>
                    <th>Status</th>
                    <th>Pipeline / task</th>
                    <th>Detected</th>
                    <th>Investigation</th>
                    <th className="num">Events</th>
                  </tr>
                </thead>
                <tbody>
                  {q.data.items.map((i) => (
                    <tr key={i.id}>
                      <td>
                        <Link to={`/incidents/${i.id}`}>{i.display_id}</Link>
                        <div className="small">{i.title}</div>
                      </td>
                      <td>
                        <Badge value={i.severity} kind="severity" />
                      </td>
                      <td>
                        <Badge value={i.status} />
                        {i.pending_proposal_id && <div className="small muted">approval pending</div>}
                      </td>
                      <td className="mono">{i.task ?? i.pipeline}</td>
                      <td title={i.opened_at}>{relTime(i.opened_at)}</td>
                      <td>
                        {i.investigation_status ? <Badge value={i.investigation_status} /> : "—"}
                        <div>
                          <ConfidenceBar value={i.latest_confidence} />
                        </div>
                      </td>
                      <td className="num">{i.event_count}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="row" style={{ justifyContent: "space-between", marginTop: 12 }}>
              <span className="muted small">
                {offset + 1}–{offset + q.data.items.length} of {q.data.total}
              </span>
              <span className="row">
                <button className="btn" disabled={offset === 0} onClick={() => set("offset", String(Math.max(0, offset - LIMIT)))}>
                  Previous
                </button>
                <button
                  className="btn"
                  disabled={offset + LIMIT >= q.data.total}
                  onClick={() => {
                    const next = new URLSearchParams(params);
                    next.set("offset", String(offset + LIMIT));
                    setParams(next);
                  }}
                >
                  Next
                </button>
              </span>
            </div>
          </>
        )}
      </Card>
    </>
  );
}
