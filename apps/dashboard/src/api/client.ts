import type { ApiErrorBody, Session } from "./types";

// Empty string = same origin (production behind a reverse proxy).
const BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? "http://localhost:8000";
const SESSION_KEY = "pulseops.session";

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public correlationId: string | null = null,
    public details: unknown = null,
  ) {
    super(message);
  }
}

// The session token lives in sessionStorage (cleared when the tab closes). It is a
// short-lived bearer token; see docs/security.md for the trade-offs of this demo auth.
export function loadSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const s = JSON.parse(raw) as Session;
    return s.expires_at * 1000 > Date.now() ? s : null;
  } catch {
    return null;
  }
}

export function saveSession(s: Session | null): void {
  try {
    if (s) sessionStorage.setItem(SESSION_KEY, JSON.stringify(s));
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    /* storage unavailable: session lasts for this page only */
  }
}

let onUnauthorized: ((reason: string) => void) | null = null;
export function setUnauthorizedHandler(fn: (reason: string) => void): void {
  onUnauthorized = fn;
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const session = loadSession();
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (session) headers.set("Authorization", `Bearer ${session.token}`);
  let resp: Response;
  try {
    resp = await fetch(`${BASE}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "network_error", "Cannot reach the PulseOps API");
  }
  // Any 401 outside the sign-in calls means the session is missing, expired or revoked:
  // send the user back to the sign-in screen instead of leaving them on an error.
  if (resp.status === 401 && !path.startsWith("/api/v1/auth/") && onUnauthorized) {
    onUnauthorized(session ? "You were signed out. Please sign in again." : "Your session expired. Please sign in again.");
  }
  if (!resp.ok) {
    let body: ApiErrorBody | null = null;
    try {
      body = (await resp.json()) as ApiErrorBody;
    } catch {
      /* non-JSON error */
    }
    throw new ApiError(
      resp.status,
      body?.error.code ?? "http_error",
      body?.error.message ?? `Request failed (${resp.status})`,
      body?.error.correlation_id ?? resp.headers.get("x-correlation-id"),
      body?.error.details ?? null,
    );
  }
  return (await resp.json()) as T;
}

export const post = <T>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export async function login(apiKey: string): Promise<Session> {
  const s = await post<Session>("/api/v1/auth/session", { api_key: apiKey });
  saveSession(s);
  return s;
}

export async function loginAsGuest(): Promise<Session> {
  const s = await post<Session>("/api/v1/auth/guest");
  saveSession(s);
  return s;
}

export async function guestEnabled(): Promise<boolean> {
  try {
    return (await api<{ guest_enabled: boolean }>("/api/v1/auth/options")).guest_enabled;
  } catch {
    return false;
  }
}
