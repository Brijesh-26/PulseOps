import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import type { AuditRecord, Page } from "../api/types";
import { Card, Empty, ErrorState, Loading, fmtTime } from "../components/ui";

export default function AuditPage() {
  const [action, setAction] = useState("");
  const [offset, setOffset] = useState(0);
  const qs = new URLSearchParams({ limit: "50", offset: String(offset) });
  if (action) qs.set("action", action);
  const records = useQuery({
    queryKey: ["audit", qs.toString()],
    queryFn: () => api<Page<AuditRecord>>(`/api/v1/audit?${qs}`),
  });
  const chain = useQuery({
    queryKey: ["audit-verify"],
    queryFn: () => api<{ valid: boolean; records_checked: number; first_invalid_id: number | null }>("/api/v1/audit/verify"),
  });

  return (
    <>
      <div className="page-head">
        <h1>Audit trail</h1>
        {chain.data && (
          <span className={`notice ${chain.data.valid ? "ok" : "bad"}`}>
            Hash chain {chain.data.valid ? "verified" : `BROKEN at record ${chain.data.first_invalid_id}`} ·{" "}
            {chain.data.records_checked} records
          </span>
        )}
      </div>
      <Card>
        <div className="filters">
          <input
            aria-label="Filter by action"
            placeholder="action, e.g. proposal.approved"
            value={action}
            onChange={(e) => {
              setAction(e.target.value.trim());
              setOffset(0);
            }}
          />
        </div>
        {records.isPending ? (
          <Loading />
        ) : records.isError ? (
          <ErrorState error={records.error} onRetry={() => records.refetch()} />
        ) : records.data.items.length === 0 ? (
          <Empty>No audit records.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Time</th>
                  <th>Actor</th>
                  <th>Action</th>
                  <th>Entity</th>
                  <th>Details</th>
                </tr>
              </thead>
              <tbody>
                {records.data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="mono">{r.id}</td>
                    <td className="small">{fmtTime(r.occurred_at)}</td>
                    <td>
                      {r.actor} <span className="muted small">({r.actor_type})</span>
                    </td>
                    <td className="mono">{r.action}</td>
                    <td className="small">
                      {r.incident_id ? <Link to={`/incidents/${r.incident_id}`}>{r.entity_type}</Link> : r.entity_type}
                    </td>
                    <td className="mono small">{JSON.stringify(r.details)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="row" style={{ marginTop: 12 }}>
              <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>
                Newer
              </button>
              <button className="btn" disabled={offset + 50 >= records.data.total} onClick={() => setOffset(offset + 50)}>
                Older
              </button>
            </div>
          </div>
        )}
      </Card>
    </>
  );
}
