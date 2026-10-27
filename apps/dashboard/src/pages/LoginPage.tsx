import { useEffect, useState } from "react";
import type { FormEvent } from "react";

import { ApiError, guestEnabled } from "../api/client";
import { useAuth } from "../auth";
import { Card } from "../components/ui";

export default function LoginPage() {
  const { login, loginGuest, notice } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [guest, setGuest] = useState(false);

  useEffect(() => {
    let live = true;
    void guestEnabled().then((on) => live && setGuest(on));
    return () => {
      live = false;
    };
  }, []);

  async function run(signIn: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await signIn();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    void run(() => login(key.trim()));
  }

  return (
    <div className="login">
      <Card title="Sign in to PulseOps">
        {guest && (
          <div className="guest">
            <button className="btn primary" disabled={busy} type="button" onClick={() => void run(loginGuest)}>
              Try the demo
            </button>
            <p className="muted small">
              Read-only access: browse incidents, evidence, AI hypotheses, impact and the audit trail.
            </p>
            <hr />
          </div>
        )}
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
          <button className={guest ? "btn" : "btn primary"} disabled={busy || key.trim().length < 24} type="submit">
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
