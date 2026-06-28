import { useEffect, useState, useCallback } from 'react';

import API_URL, { apiFetch } from '../api.js';

const STRATEGY_LABELS = {
  round_robin:    { name: 'Round-Robin',    desc: 'Baseline — luân chuyển theo thứ tự cố định, không quan tâm history.' },
  cooldown_aware: { name: 'Cooldown-aware', desc: 'Ưu tiên account chưa bị cooldown gần đây (last_cooldown_ts cũ nhất).' },
  lru:            { name: 'LRU',            desc: 'Chọn account đã idle lâu nhất — cho thời gian phục hồi tự nhiên.' },
  adaptive:       { name: 'Adaptive',       desc: 'Score-based: kết hợp success rate, latency, cooldown frequency, idle time.' },
};

/**
 * Admin panel for selecting the account-pool scheduling strategy.
 * Visible only when the logged-in user has is_admin=true.
 */
export default function SchedulerPanel({ onError }) {
  const [loading, setLoading]   = useState(true);
  const [saving, setSaving]     = useState(false);
  const [current, setCurrent]   = useState(null);
  const [strategies, setStrategies] = useState([]);
  const [accounts, setAccounts] = useState([]);
  const [errMsg, setErrMsg]     = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    setErrMsg(null);
    try {
      const res = await apiFetch(`${API_URL}/api/settings/scheduler`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCurrent(data.current);
      setStrategies(data.strategies || []);
      setAccounts(data.accounts || []);
    } catch (err) {
      const m = err.message || String(err);
      setErrMsg(m);
      onError?.('Không tải được scheduler status', m);
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => { load(); }, [load]);

  // Auto-refresh per-account stats every 10s so the operator can watch
  // history accumulate during a live job.
  useEffect(() => {
    const id = setInterval(load, 10_000);
    return () => clearInterval(id);
  }, [load]);

  async function selectStrategy(name) {
    if (name === current) return;
    setSaving(true);
    try {
      const res = await apiFetch(`${API_URL}/api/settings/scheduler`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy: name }),
      });
      if (!res.ok) {
        const detail = await res.text();
        throw new Error(detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setCurrent(data.current);
    } catch (err) {
      const m = err.message || String(err);
      setErrMsg(m);
      onError?.('Đổi strategy thất bại', m);
    } finally {
      setSaving(false);
    }
  }

  if (loading && !current) return <div className="panel">Đang tải...</div>;

  return (
    <div className="panel scheduler-panel">
      <h2>Multi-account Scheduling</h2>
      <p className="muted">
        Chọn chiến lược phân phối tác vụ qua các tài khoản Gemini. Thay đổi áp dụng ngay,
        không cần restart backend.
      </p>

      {errMsg && <div className="error-banner">{errMsg}</div>}

      <h3>Strategies</h3>
      <div className="strategy-grid">
        {strategies.map(name => {
          const meta = STRATEGY_LABELS[name] || { name, desc: '' };
          const active = name === current;
          return (
            <div
              key={name}
              className={`strategy-card ${active ? 'active' : ''}`}
              onClick={() => !saving && selectStrategy(name)}
              role="button"
              tabIndex={0}
            >
              <div className="strategy-head">
                <strong>{meta.name}</strong>
                {active && <span className="badge-active">đang dùng</span>}
              </div>
              <div className="strategy-desc">{meta.desc}</div>
            </div>
          );
        })}
      </div>

      <h3>Per-account history</h3>
      <table className="accounts-table">
        <thead>
          <tr>
            <th>Email</th>
            <th>Trạng thái</th>
            <th>OK</th>
            <th>Fail</th>
            <th>Cooldowns</th>
            <th>Avg latency (s)</th>
            <th>Recent success</th>
            <th>Last used</th>
          </tr>
        </thead>
        <tbody>
          {accounts.length === 0 && (
            <tr><td colSpan={8} className="muted">Chưa có account nào trong pool.</td></tr>
          )}
          {accounts.map(a => (
            <tr key={a.email}>
              <td>{a.email}</td>
              <td><span className={`state-pill state-${a.state}`}>{a.state}</span></td>
              <td>{a.success}</td>
              <td>{a.fail}</td>
              <td>{a.cooldowns}</td>
              <td>{a.avg_latency || '-'}</td>
              <td>{Math.round(a.recent_success_rate * 100)}%</td>
              <td>{a.last_used_ts ? new Date(a.last_used_ts * 1000).toLocaleTimeString() : '-'}</td>
            </tr>
          ))}
        </tbody>
      </table>

      <div className="benchmark-hint">
        <strong>So sánh các strategies?</strong> Chạy benchmark simulator:
        <pre>cd backend &amp;&amp; python -m benchmarks.scheduler_simulator --workload medium</pre>
        Output: <code>backend/benchmarks/results/scheduler_benchmark.csv</code>
      </div>
    </div>
  );
}
