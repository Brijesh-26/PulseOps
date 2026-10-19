import { useState } from "react";
import type { FormEvent } from "react";

import { ApiError } from "../api/client";
import { useAuth } from "../auth";
import { Card } from "../components/ui";

export default function LoginPage() {
  const { login, notice } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(key.trim());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <Card title="Sign in to PulseOps">
        <form onSubmit={submit}>
          <label htmlFor="key">API key</label>
          <input
            id="key"
            type="password"
            autoComplete="off"
            value={key}
            onChange={(e) => setKey(e.target.value)}
            placeholder="po_…"
          />
          {notice && !error && (
            <div className="notice warn" role="status">
              {notice}
            </div>
          )}
          {error && (
            <div className="notice bad" role="alert">
              {error}
            </div>
          )}
          <button className="btn primary" disabled={busy || key.trim().length < 24} type="submit">
            {busy ? "Signing in…" : "Sign in"}
          </button>
          <p className="muted small">
            Demo authentication: an API key is exchanged for a short-lived session token. Local
            demo keys are listed in the README. This is not production identity management.
          </p>
        </form>
      </Card>
    </div>
  );
}
