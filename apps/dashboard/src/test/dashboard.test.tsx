import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { loadSession, saveSession } from "../api/client";
import type { IncidentDetail, Session } from "../api/types";
import { AuthProvider } from "../auth";
import App from "../App";
import IncidentDetailPage from "../pages/IncidentDetailPage";
import IncidentsPage from "../pages/IncidentsPage";
import LoginPage from "../pages/LoginPage";

const approver: Session = {
  token: "t",
  expires_at: Math.floor(Date.now() / 1000) + 3600,
  principal: "alice",
  roles: ["approver"],
  permissions: ["incidents:read", "audit:read", "proposals:decide"],
};
const viewer: Session = { ...approver, principal: "vic", roles: ["viewer"], permissions: ["incidents:read"] };

const INCIDENT: IncidentDetail = {
  id: "inc-1",
  display_id: "INC-7",
  title: "a_orders_etl.load_warehouse failed",
  status: "ACTION_REQUIRED",
  severity: "HIGH",
  category: "TASK_FAILURE",
  pipeline: "a_orders_etl",
  task: "a_orders_etl.load_warehouse",
  opened_at: "2026-10-18T09:00:00Z",
  updated_at: "2026-10-18T09:01:00Z",
  last_event_at: "2026-10-18T09:00:00Z",
  resolved_at: null,
  closed_at: null,
  event_count: 1,
  version: 4,
  investigation_status: "SUCCEEDED",
  latest_confidence: 0.85,
  pending_proposal_id: "prop-1",
  fingerprint: "a_orders_etl.load_warehouse:TASK_FAILURE",
  latest_investigation: {
    id: "run-1",
    status: "SUCCEEDED",
    trigger: "auto",
    requested_by: "system:incident-engine",
    provider: "mock",
    model: "mock-rules-v1",
    schema_version: "1.0",
    attempts: 1,
    created_at: "2026-10-18T09:00:01Z",
    started_at: "2026-10-18T09:00:01Z",
    finished_at: "2026-10-18T09:00:02Z",
    error_code: null,
    error_message: null,
    summary: "Database connectivity failure during task execution.",
    overall_confidence: 0.85,
    result: {
      investigation_summary: "Database connectivity failure during task execution.",
      observed_facts: [{ statement: "Logs show database connection errors", evidence_ids: ["E2"] }],
      confidence: { overall: 0.85, calibration: "uncalibrated", rationale: "heuristic" },
      uncertainty: "Based only on stored evidence.",
      missing_information: ["Whether the database is currently reachable"],
      recommended_diagnostics: [],
      suspicious_evidence_ids: [],
      downstream_impact: { affected_tasks: [], affected_datasets: [], explanation: "" },
      explanation: "",
    },
    validation_issues: [],
    tool_calls: [{ tool: "get_task_history", args: {} }],
    evidence_codes: ["E1", "E2"],
    hypotheses: [
      {
        rank: 1,
        title: "Database connectivity failure during task execution",
        description: "Connection timeouts in logs.",
        category: "DATABASE_CONNECTIVITY",
        confidence: 0.85,
        is_supported: true,
        validation_notes: null,
        supporting_evidence: ["E2"],
        contradicting_evidence: [],
      },
    ],
  },
  proposals: [
    {
      id: "prop-1",
      incident_id: "inc-1",
      investigation_run_id: "run-1",
      action_type: "CHECK_DATABASE_CONNECTIVITY",
      parameters: { resource_key: "a-warehouse-db" },
      rationale: "Confirm reachability before retrying.",
      evidence_codes: ["E2"],
      proposed_by: "system:investigator",
      proposer_type: "AI",
      status: "PENDING_APPROVAL",
      status_reason: null,
      incident_version: 4,
      created_at: "2026-10-18T09:00:02Z",
      expires_at: "2026-10-18T10:00:02Z",
      decision: null,
      execution: null,
    },
  ],
};

const EVIDENCE = [
  { id: "e1", code: "E1", kind: "EVENT", title: "TASK_FAILED event", content: "event_type=TASK_FAILED", data: {}, observed_at: null, collected_by: "system", collected_at: "2026-10-18T09:00:01Z" },
  { id: "e2", code: "E2", kind: "LOG", title: "Log excerpt", content: "<script>alert(1)</script> timeout expired", data: {}, observed_at: null, collected_by: "system", collected_at: "2026-10-18T09:00:01Z" },
];

type Handler = (url: string, init?: RequestInit) => { status?: number; body: unknown };

function mockFetch(handler: Handler) {
  return vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = String(input);
    const { status = 200, body } = handler(url, init);
    return new Response(JSON.stringify(body), {
      status,
      headers: { "Content-Type": "application/json", "x-correlation-id": "cid-123" },
    });
  });
}

function detailHandler(overrides: Partial<Record<string, unknown>> = {}): Handler {
  return (url, init) => {
    if (init?.method === "POST") return { body: { ...INCIDENT.proposals[0], status: "APPROVED" } };
    if (url.endsWith("/evidence")) return { body: EVIDENCE };
    if (url.endsWith("/impact")) return { body: { origin: INCIDENT.task, downstream: [{ task: "a_orders_etl.publish_report", depth: 1, path: [INCIDENT.task, "a_orders_etl.publish_report"], datasets: ["a.orders_report"], latest_status: null }], upstream: [], datasets: [] } };
    if (url.endsWith("/timeline")) return { body: [] };
    if (url.endsWith("/investigations")) return { body: [INCIDENT.latest_investigation] };
    if (url.endsWith("/audit")) return { body: [] };
    return { body: { ...INCIDENT, ...overrides } };
  };
}

function renderAt(path: string, route: string, element: ReactNode, session: Session | null) {
  if (session) saveSession(session);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchInterval: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider initial={session}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path={route} element={element} />
          </Routes>
        </MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

describe("incident detail", () => {
  it("shows loading, then hypotheses, evidence, impact and the proposal", async () => {
    mockFetch(detailHandler());
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, approver);
    expect(screen.getByRole("status")).toHaveTextContent(/loading/i);
    expect(await screen.findByRole("heading", { name: INCIDENT.title })).toBeInTheDocument();
    expect(screen.getByText(/#1 Database connectivity failure during task execution/)).toBeInTheDocument();
    expect(screen.getAllByText("CHECK_DATABASE_CONNECTIVITY")).toHaveLength(2); // proposal card + history
    expect(screen.getByText(/Whether the database is currently reachable/)).toBeInTheDocument();
    expect(await screen.findByText("a_orders_etl.publish_report")).toBeInTheDocument();
    // Untrusted log content is rendered as text, never as markup.
    expect(await screen.findByText(/<script>alert\(1\)<\/script> timeout expired/)).toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
  });

  it("lets an approver approve and posts to the approve endpoint", async () => {
    const spy = mockFetch(detailHandler());
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, approver);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /approve/i }));
    await waitFor(() => expect(screen.getByText("Decision recorded.")).toBeInTheDocument());
    const call = spy.mock.calls.find(([, init]) => init?.method === "POST");
    expect(String(call?.[0])).toContain("/api/v1/proposals/prop-1/approve");
    expect(new Headers(call?.[1]?.headers).get("Authorization")).toBe("Bearer t");
  });

  it("requires a reason before rejecting", async () => {
    const spy = mockFetch(detailHandler());
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, approver);
    const user = userEvent.setup();
    const reject = await screen.findByRole("button", { name: /reject/i });
    expect(reject).toBeDisabled();
    await user.type(screen.getByLabelText(/reason/i), "owner will fix");
    await user.click(reject);
    await waitFor(() => {
      const call = spy.mock.calls.find(([, init]) => init?.method === "POST");
      expect(String(call?.[0])).toContain("/reject");
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({ reason: "owner will fix" });
    });
  });

  it("shows the server's refusal for a stale proposal", async () => {
    mockFetch((url, init) =>
      init?.method === "POST"
        ? { status: 409, body: { error: { code: "stale_proposal", message: "incident changed since proposal", details: null, correlation_id: "cid-9" } } }
        : detailHandler()(url, init),
    );
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, approver);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /approve/i }));
    expect(await screen.findByText("incident changed since proposal")).toBeInTheDocument();
    expect(screen.getByText(/cid-9/)).toBeInTheDocument();
  });

  it("hides decision controls from viewers and from the proposer", async () => {
    mockFetch(detailHandler());
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, viewer);
    expect(await screen.findByText("Waiting for an approver.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
  });

  it("tells the proposer that someone else must approve", async () => {
    const own = { ...INCIDENT.proposals[0]!, proposed_by: "alice", proposer_type: "HUMAN" };
    mockFetch(detailHandler({ proposals: [own] }));
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, approver);
    expect(await screen.findByText(/another approver must decide/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /approve/i })).toBeNull();
  });

  it("renders an error state with the correlation id", async () => {
    mockFetch(() => ({ status: 500, body: { error: { code: "internal_error", message: "Internal server error", details: null, correlation_id: "cid-500" } } }));
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, approver);
    expect(await screen.findByRole("alert")).toHaveTextContent("Internal server error");
    expect(screen.getByText(/cid-500/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});

describe("propose a fix", () => {
  const operator: Session = { ...approver, principal: "olga", roles: ["operator"], permissions: ["incidents:read", "investigations:run", "proposals:create"] };

  it("lets an operator submit a proposal with evidence", async () => {
    const spy = mockFetch(detailHandler({ status: "OPEN", proposals: [], pending_proposal_id: null }));
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, operator);
    const user = userEvent.setup();
    const submit = await screen.findByRole("button", { name: /submit for approval/i });
    expect(submit).toBeDisabled();
    await user.click(await screen.findByRole("button", { name: "E2" , pressed: false }));
    await user.type(screen.getByLabelText(/rationale/i), "Timeouts were transient");
    await user.click(submit);
    await waitFor(() => {
      const call = spy.mock.calls.find(([url, init]) => init?.method === "POST" && String(url).endsWith("/proposals"));
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({
        action_type: "RETRY_TASK",
        parameters: { task: "a_orders_etl.load_warehouse" },
        rationale: "Timeouts were transient",
        evidence_codes: ["E2"],
      });
    });
  });

  it("is not offered to approvers", async () => {
    mockFetch(detailHandler({ status: "OPEN", proposals: [], pending_proposal_id: null }));
    renderAt("/incidents/inc-1", "/incidents/:id", <IncidentDetailPage />, approver);
    await screen.findByRole("heading", { name: INCIDENT.title });
    expect(screen.queryByText("Propose a fix")).toBeNull();
  });
});

describe("incident list", () => {
  it("shows an empty state", async () => {
    mockFetch(() => ({ body: { items: [], total: 0, limit: 20, offset: 0 } }));
    renderAt("/incidents", "/incidents", <IncidentsPage />, viewer);
    expect(await screen.findByText("No incidents match these filters.")).toBeInTheDocument();
  });

  it("sends filters to the API", async () => {
    const spy = mockFetch(() => ({ body: { items: [], total: 0, limit: 20, offset: 0 } }));
    renderAt("/incidents?status=OPEN&severity=HIGH", "/incidents", <IncidentsPage />, viewer);
    await screen.findByText("No incidents match these filters.");
    const url = String(spy.mock.calls[0]?.[0]);
    expect(url).toContain("status=OPEN");
    expect(url).toContain("severity=HIGH");
  });
});

describe("session expiry", () => {
  function renderApp(session: Session) {
    saveSession(session);
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, refetchInterval: false } } });
    return render(
      <QueryClientProvider client={qc}>
        <AuthProvider initial={session}>
          <MemoryRouter initialEntries={["/scenarios"]}>
            <App />
          </MemoryRouter>
        </AuthProvider>
      </QueryClientProvider>,
    );
  }

  it("returns to sign-in with a message when the token has expired", async () => {
    mockFetch(() => ({ body: [] }));
    renderApp({ ...approver, expires_at: Math.floor(Date.now() / 1000) - 5 });
    expect(await screen.findByText("Your session expired. Please sign in again.")).toBeInTheDocument();
    expect(screen.getByLabelText("API key")).toBeInTheDocument();
  });

  it("returns to sign-in when the server rejects the token", async () => {
    mockFetch(() => ({ status: 401, body: { error: { code: "unauthenticated", message: "Missing credentials", details: null, correlation_id: "c" } } }));
    renderApp(approver);
    expect(await screen.findByText(/Please sign in again/)).toBeInTheDocument();
    expect(screen.queryByText("Missing credentials")).toBeNull();
  });
});

describe("login", () => {
  it("shows the API error on failed sign-in", async () => {
    mockFetch(() => ({ status: 401, body: { error: { code: "unauthenticated", message: "Invalid API key", details: null, correlation_id: null } } }));
    renderAt("/", "/", <LoginPage />, null);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("API key"), "po_live_wrong_key_value_123456");
    await user.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid API key");
  });

  it("hides the guest button when guest access is off", async () => {
    const spy = mockFetch(() => ({ body: { guest_enabled: false } }));
    renderAt("/", "/", <LoginPage />, null);
    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: /try the demo/i })).toBeNull();
  });

  it("signs in as a read-only guest", async () => {
    const spy = mockFetch((url) =>
      url.endsWith("/auth/options") ? { body: { guest_enabled: true } } : { body: { ...viewer, principal: "guest-viewer" } },
    );
    renderAt("/", "/", <LoginPage />, null);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /try the demo/i }));
    await waitFor(() => expect(spy).toHaveBeenCalledWith(expect.stringContaining("/api/v1/auth/guest"), expect.anything()));
    expect(loadSession()?.principal).toBe("guest-viewer");
  });
});
