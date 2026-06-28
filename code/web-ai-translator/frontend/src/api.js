/**
 * API base URL.
 * - Development (Vite dev server on :5173): point to backend :8000
 * - Production (served by FastAPI on :8000): use same origin
 */
const API_URL = import.meta.env.DEV
  ? 'http://localhost:8000'
  : window.location.origin;

export default API_URL;

/** Return Authorization header object if a session token exists. */
export function authHeaders() {
  const token = localStorage.getItem('auth_token');
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** fetch() wrapper that automatically injects the auth token. */
export function apiFetch(url, options = {}) {
  const headers = { ...(options.headers || {}), ...authHeaders() };
  return fetch(url, { ...options, headers });
}
