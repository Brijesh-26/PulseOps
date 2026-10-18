import type { ReactNode } from "react";

import { ApiError } from "../api/client";

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function relTime(iso: string): string {
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.max(s, 0)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function Badge({ value, kind }: { value: string; kind?: "status" | "severity" | "plain" }) {
  const cls = kind === "plain" ? "badge" : `badge ${kind ?? "status"}-${value.toLowerCase()}`;
  return <span className={cls}>{value.replaceAll("_", " ")}</span>;
}

export function Card({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <h2>{title}</h2>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="state" role="status" aria-live="polite">
      <span className="spinner" aria-hidden /> {label}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const e = error instanceof ApiError ? error : null;
  return (
    <div className="state error" role="alert">
      <strong>{e?.status === 403 ? "Not permitted" : "Something went wrong"}</strong>
      <p>{e?.message ?? String(error)}</p>
      {e?.correlationId && <p className="muted mono">correlation id: {e.correlationId}</p>}
      {onRetry && (
        <button className="btn" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="state empty">{children}</div>;
}

export function ConfidenceBar({ value }: { value: number | null | undefined }) {
  if (value === null || value === undefined) return <span className="muted">—</span>;
  const pct = Math.round(value * 100);
  const level = value >= 0.6 ? "high" : value >= 0.35 ? "mid" : "low";
  return (
    <span className="confidence" title="Heuristic estimate, not a calibrated probability">
      <span className="bar" aria-hidden>
        <span className={`fill ${level}`} style={{ width: `${pct}%` }} />
      </span>
      <span className="mono">{pct}%</span>
    </span>
  );
}

export function EvidenceChips({ codes, onSelect }: { codes: string[]; onSelect?: (code: string) => void }) {
  if (!codes.length) return <span className="muted">no evidence</span>;
  return (
    <span className="chips">
      {codes.map((c) => (
        <button key={c} className="chip" onClick={() => onSelect?.(c)} type="button">
          {c}
        </button>
      ))}
    </span>
  );
}
