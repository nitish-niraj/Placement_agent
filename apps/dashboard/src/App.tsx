import { useEffect, useState } from "react";
import { HashRouter, Link, NavLink, Route, Routes } from "react-router-dom";
import { getToken, setToken, useApi, clearToken } from "./api";
import { Companies, CompanyTimeline, Timeline } from "./screens/Companies";
import { Documents, Eligibility, Inbox, Notifications } from "./screens/Lists";
import { Audit, Profile, Settings } from "./screens/Profile";
import { Ask } from "./screens/Ask";
import { Overview } from "./screens/Overview";
import { useHealth } from "./ui";

function Login({ onDone }: { onDone: () => void }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setToken(value.trim());
    try {
      const res = await fetch("/api/v1/ping", {
        headers: { Authorization: `Bearer ${value.trim()}` },
      });
      if (res.ok) onDone();
      else { setError("Invalid token"); clearToken(); }
    } catch {
      setError("API unreachable");
    }
  };

  return (
    <div className="login">
      <h1>PIA</h1>
      <p>Placement Intelligence Agent — personal cockpit</p>
      <input type="password" placeholder="Dashboard token" value={value}
             onChange={(e) => setValue(e.target.value)}
             onKeyDown={(e) => e.key === "Enter" && submit()} autoFocus />
      <button onClick={submit}>Sign in</button>
      {error && <p className="error-note">{error}</p>}
    </div>
  );
}

const NAV = [
  ["/", "Overview"], ["/ask", "Ask PIA"], ["/companies", "Companies"], ["/timeline", "Timeline"],
  ["/inbox", "Inbox"], ["/documents", "Documents"], ["/notifications", "Notifications"],
  ["/eligibility", "Eligibility"], ["/profile", "Profile"], ["/settings", "Settings"],
  ["/audit", "Audit"],
] as const;

function Shell({ children }: { children: React.ReactNode }) {
  const health = useHealth();
  const stale = health === null || health.checks?.evolution_api !== "ok";
  return (
    <div className="shell">
      <nav>
        <h1>PIA</h1>
        {NAV.map(([path, label]) => (
          <NavLink key={path} to={path} end={path === "/"}>{label}</NavLink>
        ))}
        <div className="conn">
          <span className={`dot ${stale ? "dot-stale" : "dot-ok"}`} />
          WhatsApp {stale ? "stale" : "connected"}
        </div>
        <button className="secondary" onClick={() => { clearToken(); window.location.reload(); }}>
          Sign out
        </button>
      </nav>
      <main>{children}</main>
    </div>
  );
}

export default function App() {
  const [authed, setAuthed] = useState<boolean>(Boolean(getToken()));
  const { error } = useApi<{ pong: boolean }>("/ping");

  useEffect(() => {
    if (getToken() && error === "unauthorized") setAuthed(false);
  }, [error]);

  useEffect(() => {
    if (authed && !window.location.hash) window.location.hash = "#/";
  }, [authed]);

  if (!authed) return <Login onDone={() => setAuthed(true)} />;
  return (
    <HashRouter>
      <Shell>
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/ask" element={<Ask />} />
          <Route path="/companies" element={<Companies />} />
          <Route path="/companies/:id" element={<CompanyTimeline />} />
          <Route path="/timeline" element={<Timeline />} />
          <Route path="/inbox" element={<Inbox />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="/notifications" element={<Notifications />} />
          <Route path="/eligibility" element={<Eligibility />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/audit" element={<Audit />} />
          <Route path="*" element={<p className="empty">Not found — <Link to="/">Overview</Link></p>} />
        </Routes>
      </Shell>
    </HashRouter>
  );
}
