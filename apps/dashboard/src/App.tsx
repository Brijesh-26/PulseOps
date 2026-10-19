import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "./auth";
import AuditPage from "./pages/AuditPage";
import IncidentDetailPage from "./pages/IncidentDetailPage";
import IncidentsPage from "./pages/IncidentsPage";
import LoginPage from "./pages/LoginPage";
import OverviewPage from "./pages/OverviewPage";
import ScenariosPage from "./pages/ScenariosPage";

export default function App() {
  const { session, logout, can } = useAuth();
  if (!session) return <LoginPage />;
  return (
    <>
      <header className="topbar">
        <div className="brand">
          Pulse<span>Ops</span>
        </div>
        <nav className="nav" aria-label="Main">
          <NavLink to="/" end>
            Overview
          </NavLink>
          <NavLink to="/incidents">Incidents</NavLink>
          {can("audit:read") && <NavLink to="/audit">Audit</NavLink>}
          {can("demo:admin") && <NavLink to="/scenarios">Scenarios</NavLink>}
        </nav>
        <div className="who">
          <span className="muted small">
            {session.principal} · {session.roles.join(", ")}
          </span>
          <button className="btn" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/incidents" element={<IncidentsPage />} />
          <Route path="/incidents/:id" element={<IncidentDetailPage />} />
          <Route path="/audit" element={<AuditPage />} />
          <Route path="/scenarios" element={<ScenariosPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  );
}
