import { useEffect, useState } from "react";
import { Navigate, Outlet, Route, Routes } from "react-router-dom";
import { AUTH_CHANGED_EVENT, getSession, type StaffSession } from "./api/client";
import { AppShell } from "./components/AppShell";
import { AuditLogPage } from "./pages/audit-log/AuditLogPage";
import { ChangePasswordPage } from "./pages/change-password/ChangePasswordPage";
import { LoginPage } from "./pages/login/LoginPage";
import { ModerationPage } from "./pages/moderation/ModerationPage";
import { UsersPage } from "./pages/users/UsersPage";

function LoginRoute({ session }: { session: StaffSession | null }) {
  if (!session) {
    return <LoginPage />;
  }
  return (
    <Navigate
      to={session.scope === "change_password_only" ? "/change-password" : "/users"}
      replace
    />
  );
}

function ChangePasswordRoute({ session }: { session: StaffSession | null }) {
  if (!session) {
    return <Navigate to="/login" replace />;
  }
  return <ChangePasswordPage />;
}

function RequireAuth({ session }: { session: StaffSession | null }) {
  if (!session) {
    return <Navigate to="/login" replace />;
  }
  return <Outlet />;
}

function RequireFullScope({ session }: { session: StaffSession | null }) {
  if (session?.scope !== "full") {
    return <Navigate to="/change-password" replace />;
  }
  return <Outlet />;
}

export default function App() {
  const [session, setSession] = useState<StaffSession | null>(() => getSession());

  useEffect(() => {
    const syncSession = () => setSession(getSession());
    window.addEventListener(AUTH_CHANGED_EVENT, syncSession);
    return () => window.removeEventListener(AUTH_CHANGED_EVENT, syncSession);
  }, []);

  return (
    <Routes>
      <Route path="/login" element={<LoginRoute session={session} />} />
      <Route path="/change-password" element={<ChangePasswordRoute session={session} />} />

      <Route element={<RequireAuth session={session} />}>
        <Route element={<RequireFullScope session={session} />}>
          <Route element={<AppShell />}>
            <Route path="/users" element={<UsersPage />} />
            <Route path="/moderation" element={<ModerationPage />} />
            <Route path="/audit-log" element={<AuditLogPage />} />
          </Route>
        </Route>
      </Route>

      <Route path="/" element={<Navigate to="/users" replace />} />
      <Route path="*" element={<Navigate to="/users" replace />} />
    </Routes>
  );
}
