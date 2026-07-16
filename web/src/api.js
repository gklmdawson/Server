// Thin fetch wrapper. Same-origin in production (the coordinator serves the
// UI); the Vite dev server proxies /api. Admin actions send the admin token
// from localStorage as a bearer header — set it once via the header's ⚙.

const TOKEN_KEY = "data-intake-admin-token";

export function getAdminToken() {
  return localStorage.getItem(TOKEN_KEY) || "";
}

export function setAdminToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token);
  else localStorage.removeItem(TOKEN_KEY);
}

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
  }
}

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const token = getAdminToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const resp = await fetch(path, {
    ...options,
    headers,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
  let data = null;
  try {
    data = await resp.json();
  } catch {
    /* non-JSON error body */
  }
  if (!resp.ok) {
    const detail =
      data && data.detail
        ? typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail)
        : `HTTP ${resp.status}`;
    throw new ApiError(resp.status, detail);
  }
  return data;
}

export const api = {
  status: () => request("/api/v1/status"),
  nodes: () => request("/api/v1/nodes"),
  projects: () => request("/api/v1/projects"),
  project: (uuid) => request(`/api/v1/projects/${uuid}`),
  job: (uuid) => request(`/api/v1/jobs/${uuid}`),
  intakeOptions: () => request("/api/v1/intake/options"),
  submitIntake: (body) => request("/api/v1/intake", { method: "POST", body }),
  retryJob: (uuid) => request(`/api/v1/jobs/${uuid}/retry`, { method: "POST" }),
  cancelJob: (uuid) => request(`/api/v1/jobs/${uuid}/cancel`, { method: "POST" }),
  enableNode: (name) => request(`/api/v1/nodes/${name}/enable`, { method: "POST" }),
  disableNode: (name) => request(`/api/v1/nodes/${name}/disable`, { method: "POST" }),
  drainNode: (name) => request(`/api/v1/nodes/${name}/drain`, { method: "POST" }),
  setNodeCapabilities: (name, enabled) =>
    request(`/api/v1/nodes/${name}/capabilities`, {
      method: "POST",
      body: { enabled },
    }),
};
