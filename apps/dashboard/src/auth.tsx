import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import {
  loadSession,
  login as apiLogin,
  loginAsGuest,
  saveSession,
  setUnauthorizedHandler,
} from "./api/client";
import type { Session } from "./api/types";

interface AuthState {
  session: Session | null;
  notice: string | null;
  login: (apiKey: string) => Promise<void>;
  loginGuest: () => Promise<void>;
  logout: (notice?: string) => void;
  can: (permission: string) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children, initial }: { children: ReactNode; initial?: Session | null }) {
  const [session, setSession] = useState<Session | null>(() => initial ?? loadSession());

  const [notice, setNotice] = useState<string | null>(null);

  const logout = useCallback((reason?: string) => {
    saveSession(null);
    setSession(null);
    setNotice(reason ?? null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(logout);
  }, [logout]);

  // Sign out at the moment the session token expires, rather than on the next failed request.
  useEffect(() => {
    if (!session) return;
    const ms = session.expires_at * 1000 - Date.now();
    const timer = setTimeout(() => logout("Your session expired. Please sign in again."), Math.max(ms, 0));
    return () => clearTimeout(timer);
  }, [session, logout]);

  const value = useMemo<AuthState>(
    () => ({
      session,
      notice,
      login: async (apiKey: string) => {
        setSession(await apiLogin(apiKey));
        setNotice(null);
      },
      loginGuest: async () => {
        setSession(await loginAsGuest());
        setNotice(null);
      },
      logout,
      // UI hint only: the API enforces every permission server-side.
      can: (p: string) => session?.permissions.includes(p) ?? false,
    }),
    [session, notice, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
