import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, post } from "../api/client";
import type { ScenarioInfo } from "../api/types";
import { Card, ErrorState, Loading } from "../components/ui";

interface LoadResult {
  scenario: string;
  tag: string;
  deliveries: number;
  created_events: number;
  duplicate_deliveries: number;
}

export default function ScenariosPage() {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["scenarios"], queryFn: () => api<ScenarioInfo[]>("/api/v1/demo/scenarios") });
  const load = useMutation({
    mutationFn: (key: string) => post<LoadResult>(`/api/v1/demo/scenarios/${key}`),
    onSuccess: () => qc.invalidateQueries(),
  });

  return (
    <>
      <div className="page-head">
        <h1>Demo scenarios</h1>
        <span className="muted small">Admin only · available only when the server runs in demo mode</span>
      </div>
      {load.isSuccess && (
        <div className="notice ok" role="status">
          Loaded {load.data.scenario} (tag {load.data.tag}): {load.data.deliveries} deliveries,{" "}
          {load.data.created_events} new event(s), {load.data.duplicate_deliveries} duplicate(s). Processing happens
          asynchronously; check Incidents in a few seconds.
        </div>
      )}
      {load.isError && <ErrorState error={load.error} />}
      {list.isPending ? (
        <Loading />
      ) : list.isError ? (
        <ErrorState error={list.error} onRetry={() => list.refetch()} />
      ) : (
        list.data.map((s) => (
          <Card
            key={s.key}
            title={s.title}
            actions={
              <button className="btn primary" disabled={load.isPending} onClick={() => load.mutate(s.key)}>
                Load
              </button>
            }
          >
            <p className="muted">{s.description}</p>
          </Card>
        ))
      )}
    </>
  );
}
