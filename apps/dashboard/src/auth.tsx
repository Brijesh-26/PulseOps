import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { loadSession, login as apiLogin, saveSession, setUnauthorizedHandler } from "./api/client";
import type { Session } from "./api/types";

interface AuthState {
  session: Session | null;
  login: (apiKey: string) => Promise<void>;
  logout: () => void;
  can: (permission: string) => boolean;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children, initial }: { children: ReactNode; initial?: Session | null }) {
  const [session, setSession] = useState<Session | null>(() => initial ?? loadSession());

  const logout = useCallback(() => {
    saveSession(null);
    setSession(null);
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(logout);
  }, [logout]);

  const value = useMemo<AuthState>(
    () => ({
      session,
      login: async (apiKey: string) => setSession(await apiLogin(apiKey)),
      logout,
      // UI hint only: the API enforces every permission server-side.
      can: (p: string) => session?.permissions.includes(p) ?? false,
    }),
    [session, logout],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}
