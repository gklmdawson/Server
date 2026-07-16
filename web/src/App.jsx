import { useEffect, useRef, useState } from "react";
import { api, getAdminToken, setAdminToken } from "./api.js";
import Dashboard from "./Dashboard.jsx";
import Machines from "./Machines.jsx";
import Projects from "./Projects.jsx";
import Submit from "./Submit.jsx";

const TABS = ["Dashboard", "Projects", "Submit", "Machines"];

function SettingsDialog({ open, onClose }) {
  const ref = useRef(null);
  const [token, setToken] = useState(getAdminToken());

  useEffect(() => {
    if (open) {
      setToken(getAdminToken());
      ref.current?.showModal();
    } else {
      ref.current?.close();
    }
  }, [open]);

  const save = (e) => {
    e.preventDefault();
    setAdminToken(token.trim());
    onClose();
  };

  return (
    <dialog ref={ref} className="settings" onClose={onClose}>
      <form onSubmit={save} className="intake">
        <div className="field">
          <label>
            Admin token{" "}
            <span className="hint">
              needed for submissions and machine controls; stored only in this
              browser
            </span>
          </label>
          <input
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="DATA_INTAKE_ADMIN_TOKEN"
          />
        </div>
        <div className="actions">
          <button className="btn primary" type="submit">
            Save
          </button>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </dialog>
  );
}

export default function App() {
  const initialTab = TABS.includes(location.hash.slice(1))
    ? location.hash.slice(1)
    : "Dashboard";
  const [tab, setTab] = useState(initialTab);
  const [selectedProject, setSelectedProject] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [version, setVersion] = useState("");

  useEffect(() => {
    location.hash = tab;
  }, [tab]);

  useEffect(() => {
    api.status().then((s) => setVersion(s.version)).catch(() => {});
  }, []);

  const openProject = (uuid) => {
    setSelectedProject(uuid);
    setTab("Projects");
  };

  return (
    <>
      <header className="topbar">
        <h1>Data Intake</h1>
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t}
              className={tab === t ? "active" : ""}
              onClick={() => {
                setTab(t);
                if (t !== "Projects") setSelectedProject(null);
              }}
            >
              {t}
            </button>
          ))}
        </nav>
        <span className="spacer" />
        <span className="meta">{version && `coordinator ${version}`}</span>
        <button
          className="btn small"
          title="Admin token"
          onClick={() => setSettingsOpen(true)}
        >
          ⚙
        </button>
      </header>

      <main>
        {tab === "Dashboard" && <Dashboard onOpenProject={openProject} />}
        {tab === "Projects" && (
          <Projects selected={selectedProject} onSelect={setSelectedProject} />
        )}
        {tab === "Submit" && <Submit onSubmitted={openProject} />}
        {tab === "Machines" && <Machines />}
      </main>

      <SettingsDialog open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </>
  );
}
