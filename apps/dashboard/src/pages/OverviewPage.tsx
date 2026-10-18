import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { Stats } from "../api/types";
import { Badge, Card, Empty, ErrorState, Loading, relTime } from "../components/ui";

export default function OverviewPage() {
  const q = useQuery({
    queryKey: ["stats"],
    queryFn: () => api<Stats>("/api/v1/stats"),
    refetchInterval: 5000,
  });
  if (q.isPending) return <Loading />;
  if (q.isError) return <ErrorState error={q.error} onRetry={() => q.refetch()} />;
  const s = q.data;
  const kpis: [string, string | number, boolean?][] = [
    ["Open incidents", s.open_incidents],
    ["Critical open", s.critical_open_incidents, s.critical_open_incidents > 0],
    ["Awaiting approval", s.pending_approvals, s.pending_approvals > 0],
    ["Failures (24h)", s.failures_24h],
    ["Resolved (7d)", s.resolved_7d],
    [
      "Mean time to resolve (30d)",
      s.mean_time_to_resolve_minutes_30d === null ? "—" : `${s.mean_time_to_resolve_minutes_30d} min`,
    ],
  ];
  return (
    <>
      <div className="page-head">
        <h1>Overview</h1>
        <span className="muted small">Computed live from stored data · updated {relTime(s.generated_at)}</span>
      </div>
      <div className="kpis">
        {kpis.map(([label, value, alert]) => (
          <div key={label} className={`kpi ${alert ? "alert" : ""}`}>
            <div className="label">{label}</div>
            <div className="value">{value}</div>
          </div>
        ))}
      </div>
      <div className="grid cols-2">
        <Card title="Incidents by status">
          <table>
            <tbody>
              {Object.entries(s.by_status).map(([status, n]) => (
                <tr key={status}>
                  <td>
                    <Link to={`/incidents?status=${status}`}>
                      <Badge value={status} />
                    </Link>
                  </td>
                  <td className="num">{n}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Recent failures">
          {s.recent_failures.length === 0 ? (
            <Empty>No failure events yet. Load a scenario to generate some.</Empty>
          ) : (
            <table>
              <tbody>
                {s.recent_failures.map((f) => (
                  <tr key={f.event_id}>
                    <td>
                      <Badge value={f.severity} kind="severity" />
                    </td>
                    <td className="mono">
                      {f.incident_id ? <Link to={`/incidents/${f.incident_id}`}>{f.task}</Link> : f.task}
                    </td>
                    <td className="muted small">{f.event_type}</td>
                    <td className="muted small">{relTime(f.occurred_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>
      <Card title="Pipeline reliability (last 7 days)">
        {s.pipelines.length === 0 ? (
          <Empty>No pipelines registered.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Pipeline</th>
                  <th className="num">Task runs</th>
                  <th className="num">Failed runs</th>
                  <th className="num">Success rate</th>
                  <th className="num">Open incidents</th>
                </tr>
              </thead>
              <tbody>
                {s.pipelines.map((p) => (
                  <tr key={p.pipeline}>
                    <td>
                      <div>{p.name}</div>
                      <div className="mono muted">{p.pipeline}</div>
                    </td>
                    <td className="num">{p.runs_7d}</td>
                    <td className="num">{p.failures_7d}</td>
                    <td className="num">
                      {p.success_rate_7d === null ? "—" : `${(p.success_rate_7d * 100).toFixed(1)}%`}
                    </td>
                    <td className="num">{p.open_incidents}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </>
  );
}
