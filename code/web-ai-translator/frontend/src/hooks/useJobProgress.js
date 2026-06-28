import { useEffect, useRef, useState } from 'react';
import API_URL from '../api';

/**
 * Subscribe to live progress events for a single job.
 *
 * Backend publishes events to Redis pub/sub on `job:{job_id}`; the FastAPI
 * `/ws/jobs/{job_id}` endpoint fans them out to whatever browser tab is
 * listening. We fall back to polling the REST status endpoint if the socket
 * can't be established (e.g. an ancient proxy stripping the Upgrade header).
 *
 * Usage:
 *   const { event, connected, error } = useJobProgress(jobId, {
 *     pollFallbackUrl: `/api/pdf-translate/${jobId}/status`,
 *   });
 */
export default function useJobProgress(jobId, { pollFallbackUrl, enabled = true } = {}) {
  const [event, setEvent] = useState(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState(null);
  const wsRef = useRef(null);
  const pollRef = useRef(null);
  const retryRef = useRef(0);

  useEffect(() => {
    if (!enabled || !jobId) return undefined;

    let cancelled = false;
    const wsUrl =
      API_URL.replace(/^http/, 'ws') + `/ws/jobs/${encodeURIComponent(jobId)}`;

    const startPolling = () => {
      if (!pollFallbackUrl || pollRef.current) return;
      const tick = async () => {
        try {
          const r = await fetch(`${API_URL}${pollFallbackUrl}`, {
            headers: {
              Authorization: `Bearer ${localStorage.getItem('auth_token') || ''}`,
            },
          });
          if (r.ok && !cancelled) setEvent(await r.json());
        } catch {
          /* keep polling */
        }
      };
      tick();
      pollRef.current = setInterval(tick, 2000);
    };

    const stopPolling = () => {
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };

    const connect = () => {
      if (cancelled) return;
      let ws;
      try {
        ws = new WebSocket(wsUrl);
      } catch (e) {
        setError(e);
        startPolling();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        if (cancelled) return;
        setConnected(true);
        setError(null);
        retryRef.current = 0;
        stopPolling();
      };

      ws.onmessage = (ev) => {
        if (cancelled) return;
        try {
          setEvent(JSON.parse(ev.data));
        } catch {
          setEvent({ raw: ev.data });
        }
      };

      ws.onerror = (e) => {
        if (cancelled) return;
        setError(e);
      };

      ws.onclose = () => {
        if (cancelled) return;
        setConnected(false);
        // Exponential backoff retry (cap at 30s). Fall back to polling while
        // we wait so the UI keeps updating.
        startPolling();
        const delay = Math.min(1000 * 2 ** retryRef.current, 30000);
        retryRef.current += 1;
        setTimeout(connect, delay);
      };
    };

    connect();

    return () => {
      cancelled = true;
      stopPolling();
      if (wsRef.current) {
        try { wsRef.current.close(); } catch { /* ignore */ }
        wsRef.current = null;
      }
    };
  }, [jobId, enabled, pollFallbackUrl]);

  return { event, connected, error };
}
