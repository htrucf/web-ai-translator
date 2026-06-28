import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'

// ── Global fetch interceptor: tự inject Bearer token cho mọi /api/* request ──
const _originalFetch = window.fetch.bind(window);
window.fetch = function (input, init = {}) {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
  const isApi = typeof url === 'string' && url.includes('/api/') && !url.includes('/api/auth/');
  if (isApi) {
    const token = localStorage.getItem('auth_token');
    if (token) {
      init = {
        ...init,
        headers: {
          ...(init.headers || {}),
          Authorization: `Bearer ${token}`,
        },
      };
    }
  }
  return _originalFetch(input, init);
};

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
