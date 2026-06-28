import { useState, useEffect, useRef } from 'react';

import API_URL, { apiFetch } from '../api.js';

const STEPS = [
  'Kết nối tới server...',
  'Tải dữ liệu heuristic...',
  'Phân tích vấn đề...',
  'Hoàn thành',
];

const MQM_CATEGORY_LABELS = {
  accuracy:    'Độ chính xác',
  fluency:     'Trôi chảy',
  terminology: 'Thuật ngữ',
  style:       'Phong cách',
  locale:      'Địa phương hóa',
};

const MQM_SEVERITY_COLOR = {
  minor:    'qs-sev-minor',
  major:    'qs-sev-major',
  critical: 'qs-sev-critical',
};

export default function QualityPanel({ jobId, jobType }) {
  const [quality, setQuality] = useState(null);
  const [loading, setLoading] = useState(true);
  const [loadStep, setLoadStep] = useState(0);   // 0-based index into STEPS
  const [loadPct, setLoadPct]   = useState(0);   // 0-100
  const stepTimerRef = useRef(null);
  const [activeTab, setActiveTab] = useState('issues');

  // Diagnostics state
  const [diagnostics, setDiagnostics] = useState(null);
  const [diagLoading, setDiagLoading] = useState(false);

  // Audit state
  const [auditSummary, setAuditSummary] = useState(null);
  const [auditEvents, setAuditEvents] = useState([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditHasMore, setAuditHasMore] = useState(false);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditError, setAuditError] = useState(null);
  const [auditFilter, setAuditFilter] = useState('');     // event_type prefix
  const [auditPhase, setAuditPhase] = useState('');       // phase filter
  const [auditLimit, setAuditLimit] = useState(200);
  const [auditEnv, setAuditEnv] = useState(null);

  // Multi-agent state
  const [maResult, setMaResult] = useState(null);
  const [maLoading, setMaLoading] = useState(false);
  const [maModel, setMaModel] = useState('qwen2.5:7b');
  const [maMaxChunks, setMaMaxChunks] = useState(15);
  const [maSynthesis, setMaSynthesis] = useState(true);
  const [maError, setMaError] = useState(null);

  // ChrF++ evaluation state
  const [chrfResult, setChrfResult] = useState(null);
  const [chrfLoading, setChrfLoading] = useState(false);
  const [chrfRefs, setChrfRefs] = useState('');
  const [chrfError, setChrfError] = useState(null);

  // LLM Judge state
  const [judgeBackend, setJudgeBackend] = useState('ollama');   // 'ollama' | 'gemini' | 'web'
  const [judgeModels, setJudgeModels] = useState(null);         // null = not loaded yet
  const [judgeModel, setJudgeModel] = useState('qwen2.5:32b');  // upgraded default
  const [judgeRunning, setJudgeRunning] = useState(false);
  const [judgeResult, setJudgeResult] = useState(null);
  const [judgeError, setJudgeError] = useState(null);
  const [judgeMaxSegs, setJudgeMaxSegs] = useState(10);
  // Cross-model web judge: '' = auto (≠ model dịch) | any supported web backend.
  const [webJudgeBackend, setWebJudgeBackend] = useState('');

  useEffect(() => {
    if (!jobId) return;
    setLoading(true);
    setQuality(null);
    setLoadStep(0);
    setLoadPct(0);

    // Animate steps while fetching
    let step = 0;
    const totalSteps = STEPS.length - 1; // last step = done
    stepTimerRef.current = setInterval(() => {
      step = Math.min(step + 1, totalSteps - 1);
      setLoadStep(step);
      setLoadPct(Math.round((step / totalSteps) * 85)); // cap at 85% until real done
    }, 400);

    const isPdf = jobType === 'pdf';
    const qualityUrl = isPdf
      ? `${API_URL}/api/pdf-translate/${jobId}/quality`
      : null;

    const qualityP = qualityUrl
      ? apiFetch(qualityUrl).then(r => r.ok ? r.json() : null).catch(() => null)
      : Promise.resolve(null);

    qualityP.then(q => {
      clearInterval(stepTimerRef.current);
      if (q) setQuality(q);
      setLoadStep(STEPS.length - 1);
      setLoadPct(100);
      setTimeout(() => setLoading(false), 350); // brief pause so 100% is visible
    });

    // Also try loading cached ChrF result (PDF only)
    if (jobType === 'pdf') {
      apiFetch(`${API_URL}/api/pdf-translate/${jobId}/evaluate`)
        .then(r => r.ok ? r.json() : null).catch(() => null)
        .then(c => { if (c) setChrfResult(c); });
      // Load cached multi-agent
      apiFetch(`${API_URL}/api/pdf-translate/${jobId}/multi-agent`)
        .then(r => r.ok ? r.json() : null).catch(() => null)
        .then(m => { if (m) setMaResult(m); });
    }

    // Also try loading cached judge result (Ollama judge by default;
    // Gemini judge cache is loaded only when user switches backend)
    const judgeUrl = jobType === 'latex'
      ? `${API_URL}/api/job/${jobId}/judge`
      : `${API_URL}/api/pdf-translate/${jobId}/judge`;
    apiFetch(judgeUrl).then(r => r.ok ? r.json() : null).catch(() => null)
      .then(j => { if (j) setJudgeResult(j); });

    // Load cached diagnostics (PDF only)
    if (jobType === 'pdf') {
      apiFetch(`${API_URL}/api/pdf-translate/${jobId}/diagnostics`)
        .then(r => r.ok ? r.json() : null).catch(() => null)
        .then(d => { if (d) setDiagnostics(d); });
    }

    return () => clearInterval(stepTimerRef.current);
  }, [jobId, jobType]);

  async function loadDiagnostics(forceRefresh = false) {
    if (!jobId || jobType !== 'pdf') return;
    setDiagLoading(true);
    try {
      const url = `${API_URL}/api/pdf-translate/${jobId}/diagnostics${forceRefresh ? '?refresh=true' : ''}`;
      const res = await apiFetch(url);
      if (res.ok) setDiagnostics(await res.json());
    } catch { /* ignore */ } finally {
      setDiagLoading(false);
    }
  }

  async function loadAudit() {
    if (!jobId || jobType !== 'pdf') return;
    setAuditLoading(true);
    setAuditError(null);
    try {
      const params = new URLSearchParams({ limit: String(auditLimit) });
      if (auditFilter) params.set('event_type', auditFilter);
      if (auditPhase) params.set('phase', auditPhase);
      const [eventsRes, summaryRes] = await Promise.all([
        apiFetch(`${API_URL}/api/pdf-translate/${jobId}/audit?${params}`),
        apiFetch(`${API_URL}/api/pdf-translate/${jobId}/audit/summary`),
      ]);
      if (!eventsRes.ok) {
        setAuditError(`Audit log fetch failed (${eventsRes.status})`);
        setAuditEvents([]);
        setAuditTotal(0);
        setAuditHasMore(false);
      } else {
        const data = await eventsRes.json();
        setAuditEvents(data.events || []);
        setAuditTotal(data.total_filtered || 0);
        setAuditHasMore(!!data.has_more);
        setAuditEnv(data.env_snapshot || null);
      }
      if (summaryRes.ok) {
        setAuditSummary(await summaryRes.json());
      } else if (summaryRes.status === 404) {
        setAuditSummary(null);
      }
    } catch (e) {
      setAuditError(String(e?.message || e));
    } finally {
      setAuditLoading(false);
    }
  }

  // Load judge models when user switches to judge tab
  function loadJudgeModels() {
    if (judgeModels !== null) return;
    const url = jobType === 'latex'
      ? `${API_URL}/api/judge/models`
      : `${API_URL}/api/pdf-translate/judge/models`;
    apiFetch(url).then(r => r.ok ? r.json() : null).catch(() => null)
      .then(data => {
        setJudgeModels(data || { ollama_running: false, models: [] });
        if (data?.models?.length) {
          const first = data.models.find(m => m.available);
          if (first) setJudgeModel(first.id);
        }
      });
  }

  async function runMultiAgent() {
    if (jobType !== 'pdf') return;
    setMaLoading(true);
    setMaError(null);
    try {
      const res = await apiFetch(`${API_URL}/api/pdf-translate/${jobId}/multi-agent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: maModel, max_chunks: maMaxChunks, run_synthesis: maSynthesis }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      setMaResult(await res.json());
    } catch (e) {
      setMaError(e.message);
    } finally {
      setMaLoading(false);
    }
  }

  async function runChrfEval() {
    if (jobType !== 'pdf') return;
    setChrfLoading(true);
    setChrfError(null);
    try {
      const refLines = chrfRefs.trim().split('\n').map(l => l.trim()).filter(Boolean);
      // User must provide hypothesis::reference per line
      const segments = refLines.map(line => {
        const sep = line.indexOf('::');
        if (sep === -1) throw new Error(`Dòng "${line.substring(0,40)}..." thiếu dấu "::" phân tách. Định dạng: "bản dịch máy :: bản dịch tham chiếu"`);
        return { hypothesis: line.slice(0, sep).trim(), reference: line.slice(sep + 2).trim() };
      });
      if (segments.length === 0) throw new Error('Cần ít nhất 1 dòng hypothesis::reference.');
      const res = await apiFetch(`${API_URL}/api/pdf-translate/${jobId}/evaluate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ segments, run_chrf: true, run_bertscore: false }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      setChrfResult(await res.json());
    } catch (e) {
      setChrfError(e.message);
    } finally {
      setChrfLoading(false);
    }
  }

  async function runJudge() {
    setJudgeRunning(true);
    setJudgeError(null);
    try {
      // Backend selection: ollama uses model id; gemini/web ignore it
      // (the backend IS the model — runs through Playwright web AI).
      const isGemini = judgeBackend === 'gemini';
      const isWeb = judgeBackend === 'web';
      // Web-based judges are PDF-only (LaTeX pipeline doesn't share the same
      // session pattern). Surface a clear error rather than 404 from BE.
      if ((isGemini || isWeb) && jobType !== 'pdf') {
        throw new Error('Judge web (Gemini/cross-model) chỉ hỗ trợ PDF jobs hiện tại');
      }
      const url = isWeb
        ? `${API_URL}/api/pdf-translate/${jobId}/judge/web`
        : (isGemini
            ? `${API_URL}/api/pdf-translate/${jobId}/judge/gemini`
            : (jobType === 'latex'
                ? `${API_URL}/api/job/${jobId}/judge`
                : `${API_URL}/api/pdf-translate/${jobId}/judge`));
      const body = isWeb
        ? { judge_backend: webJudgeBackend || null, max_segments: judgeMaxSegs, low_score_threshold: 0.70, new_session_every: 5 }
        : isGemini
          ? { max_segments: judgeMaxSegs, low_score_threshold: 0.70, new_session_every: 5 }
          : { model: judgeModel, max_segments: judgeMaxSegs, low_score_threshold: 0.70 };
      const res = await apiFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setJudgeResult(data);
    } catch (e) {
      setJudgeError(e.message);
    } finally {
      setJudgeRunning(false);
    }
  }

  // When backend switches, load that backend's cached result if available.
  async function switchJudgeBackend(backend) {
    setJudgeBackend(backend);
    setJudgeError(null);
    if (jobType !== 'pdf') return;
    const url = backend === 'web'
      ? `${API_URL}/api/pdf-translate/${jobId}/judge/web`
      : backend === 'gemini'
        ? `${API_URL}/api/pdf-translate/${jobId}/judge/gemini`
        : `${API_URL}/api/pdf-translate/${jobId}/judge`;
    try {
      const res = await apiFetch(url);
      if (res.ok) {
        setJudgeResult(await res.json());
      } else {
        setJudgeResult(null);  // no cache for this backend yet
      }
    } catch { /* ignore — keep previous result */ }
  }

  if (loading) return (
    <div className="qs-section">
      <div className="qs-progress-wrap">
        <div className="qs-progress-label">
          <span>{STEPS[loadStep]}</span>
          <span className="qs-progress-pct">{loadPct}%</span>
        </div>
        <div className="qs-progress-track">
          <div className="qs-progress-fill" style={{ width: `${loadPct}%` }} />
        </div>
        <div className="qs-progress-steps">
          {STEPS.map((s, i) => (
            <div key={i} className={`qs-step ${i < loadStep ? 'done' : i === loadStep ? 'active' : ''}`}>
              <span className="qs-step-dot" />
              <span className="qs-step-name">{s}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
  // ── helpers (defined early so usable in all branches) ──
  function scoreCls(pct) {
    if (pct == null) return 'qs-neutral';
    if (pct >= 80) return 'qs-good';
    if (pct >= 60) return 'qs-ok';
    return 'qs-low';
  }

  function judgeLabel(backend) {
    if (backend === 'gemini') return 'Gemini';
    if (backend === 'web') return 'Cross-model';
    return 'Ollama';
  }

  function scoreLabel(pct) {
    if (pct == null) return '';
    if (pct >= 80) return 'Tốt';
    if (pct >= 60) return 'Trung bình';
    return 'Thấp';
  }

  // ── scores ──
  const hScore = quality?.score ?? null;

  // ── heuristic issues ──
  const issues = quality?.issues || [];

  return (
    <div className="qs-section">
      <div className="qs-title">Đánh giá chất lượng dịch thuật</div>

      {/* ── Score overview row ── */}
      <div className="qs-overview">
        {hScore !== null && (
          <div className={`qs-score-card ${scoreCls(hScore)}`}>
            <div className="qs-score-num">{hScore}<span className="qs-score-unit">/100</span></div>
            <div className="qs-score-name">Heuristic</div>
            <div className="qs-score-desc">Rule-based check</div>
            <div className={`qs-score-tag ${scoreCls(hScore)}`}>{scoreLabel(hScore)}</div>
          </div>
        )}
        {quality && (
          <div className="qs-stats-card">
            <div className="qs-stat-row">
              <span className={`qs-stat-val ${quality.untranslated_blocks > 0 ? 'qs-low' : 'qs-good'}`}>
                {quality.untranslated_blocks ?? 0}
              </span>
              <span className="qs-stat-lbl">blocks chưa dịch</span>
            </div>
            {quality.total_blocks != null && (
              <div className="qs-stat-row">
                <span className="qs-stat-val">{quality.total_blocks}</span>
                <span className="qs-stat-lbl">blocks tổng</span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── Tabs ── */}
      <div className="qs-tabs">
        {issues.length > 0 && (
          <button
            className={`qs-tab ${activeTab === 'issues' ? 'active' : ''}`}
            onClick={() => setActiveTab('issues')}
          >
            Vấn đề heuristic ({issues.length})
          </button>
        )}
        {jobType === 'pdf' && (
          <button
            className={`qs-tab ${activeTab === 'chrf' ? 'active' : ''}`}
            onClick={() => setActiveTab('chrf')}
          >
            ChrF++ {chrfResult ? `(${chrfResult.chrf_score ?? chrfResult.score ?? '—'})` : ''}
          </button>
        )}
        {jobType === 'pdf' && (
          <button
            className={`qs-tab ${activeTab === 'multiagent' ? 'active' : ''}`}
            onClick={() => setActiveTab('multiagent')}
          >
            Multi-Agent {maResult ? `(${maResult.mean_agreement?.toFixed(0)}%)` : ''}
          </button>
        )}
        <button
          className={`qs-tab ${activeTab === 'judge' ? 'active' : ''}`}
          onClick={() => { setActiveTab('judge'); loadJudgeModels(); }}
        >
          LLM Judge {judgeResult ? `(${judgeResult.num_judged})` : ''}
        </button>
        {jobType === 'pdf' && (
          <button
            className={`qs-tab ${activeTab === 'diagnostics' ? 'active' : ''}`}
            onClick={() => { setActiveTab('diagnostics'); if (!diagnostics) loadDiagnostics(); }}
          >
            Chẩn đoán {diagnostics?.primary_cause ? '⚠' : ''}
          </button>
        )}
        {jobType === 'pdf' && (
          <button
            className={`qs-tab ${activeTab === 'audit' ? 'active' : ''}`}
            onClick={() => { setActiveTab('audit'); if (!auditSummary && !auditEvents.length) loadAudit(); }}
          >
            Audit log {auditTotal ? `(${auditTotal})` : ''}
          </button>
        )}
      </div>

      {/* ── Heuristic issues ── */}
      {activeTab === 'issues' && issues.length > 0 && (
        <div className="qs-issues-list">
          {issues.map((issue, i) => (
            <div key={i} className={`qs-issue ${issue.severity === 'error' ? 'qs-issue-error' : issue.severity === 'warning' ? 'qs-issue-warn' : 'qs-issue-info'}`}>
              <div className="qs-issue-head">
                <span className="qs-issue-sev">{issue.severity === 'error' ? '✖' : issue.severity === 'warning' ? '⚠' : 'ℹ'}</span>
                <span className="qs-issue-msg">{issue.message}</span>
                {issue.page && <span className="qs-issue-page">Trang {issue.page}</span>}
              </div>
              {(issue.original || issue.translated) && (
                <div className="qs-issue-body">
                  {issue.original && <div><span className="qs-lbl">EN:</span> {issue.original}</div>}
                  {issue.translated && <div><span className="qs-lbl">VI:</span> {issue.translated}</div>}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ── ChrF++ Evaluation ── */}
      {activeTab === 'chrf' && jobType === 'pdf' && (
        <div className="qs-chrf-panel">
          <div className="qs-chrf-desc">
            <strong>ChrF++</strong> (character n-gram F-score) — Metric đánh giá dịch thuật không phụ thuộc tokenizer,
            phù hợp cho tiếng Việt (ngôn ngữ đơn lập). Cần bản dịch tham chiếu để so sánh.
          </div>
          <div className="qs-chrf-controls">
            <label className="qs-chrf-label">
              Cặp bản dịch (mỗi dòng = "bản dịch máy :: bản dịch tham chiếu"):
              <textarea
                className="qs-chrf-textarea"
                placeholder={'Định dạng mỗi dòng:\n  bản dịch máy :: bản dịch tham chiếu (con người)\n\nVí dụ:\n  Mạng nơ-ron sâu là kiến trúc... :: Mạng nơ-ron sâu là một kiến trúc...'}
                value={chrfRefs}
                onChange={e => setChrfRefs(e.target.value)}
                rows={6}
                disabled={chrfLoading}
              />
            </label>
            <button
              className={`qs-chrf-run-btn ${chrfLoading ? 'running' : ''}`}
              onClick={runChrfEval}
              disabled={chrfLoading || !chrfRefs.trim()}
            >
              {chrfLoading ? 'Đang tính...' : chrfResult ? 'Tính lại' : 'Tính ChrF++'}
            </button>
          </div>
          {chrfError && <div className="qs-judge-error">{chrfError}</div>}
          {chrfResult && !chrfLoading && (() => {
            // API returns { chrf: {...}, bertscore: {...}, num_segments, ... }
            const chrf = chrfResult.chrf || chrfResult;
            const bs = chrfResult.bertscore;
            const chrfScore = chrf.score ?? chrf.chrf_score;
            const bsF1 = bs?.f1 ?? chrfResult.bertscore_f1;
            return (
              <div className="qs-chrf-result">
                <div className="qs-overview" style={{marginTop: '0.75rem'}}>
                  {chrfScore != null && (
                    <div className={`qs-score-card ${scoreCls(chrfScore)}`}>
                      <div className="qs-score-num">{typeof chrfScore === 'number' ? chrfScore.toFixed(1) : chrfScore}<span className="qs-score-unit">/100</span></div>
                      <div className="qs-score-name">ChrF++</div>
                      <div className="qs-score-desc">char_order=6, word_order=2</div>
                      <div className={`qs-score-tag ${scoreCls(chrfScore)}`}>{scoreLabel(chrfScore)}</div>
                    </div>
                  )}
                  {bsF1 != null && (
                    <div className={`qs-score-card ${scoreCls(bsF1 * 100)}`}>
                      <div className="qs-score-num">{(bsF1 * 100).toFixed(1)}<span className="qs-score-unit">%</span></div>
                      <div className="qs-score-name">BERTScore F1</div>
                      <div className="qs-score-desc">{bs?.model_name || 'PhoBERT'}</div>
                      <div className={`qs-score-tag ${scoreCls(bsF1 * 100)}`}>{scoreLabel(bsF1 * 100)}</div>
                    </div>
                  )}
                  <div className="qs-stats-card">
                    <div className="qs-stat-row">
                      <span className="qs-stat-val">{chrfResult.num_segments}</span>
                      <span className="qs-stat-lbl">segments đánh giá</span>
                    </div>
                    {chrf.low_quality_count != null && (
                      <div className="qs-stat-row">
                        <span className={`qs-stat-val ${chrf.low_quality_count > 0 ? 'qs-low' : 'qs-good'}`}>{chrf.low_quality_count}</span>
                        <span className="qs-stat-lbl">thấp (&lt;{chrf.low_threshold ?? 40})</span>
                      </div>
                    )}
                    {chrf.interpretation && (
                      <div className="qs-stat-row">
                        <span className="qs-stat-lbl" style={{fontStyle:'italic'}}>{chrf.interpretation}</span>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            );
          })()}
        </div>
      )}

      {/* ── Multi-Agent Analysis ── */}
      {activeTab === 'multiagent' && jobType === 'pdf' && (
        <div className="qs-chrf-panel">
          <div className="qs-chrf-desc">
            <strong>Multi-agent disagreement analysis</strong>: Dịch lại cùng đoạn EN bằng Ollama,
            so sánh với bản Gemini. ChrF++ giữa 2 bản VI đo mức đồng thuận.
            Bất đồng cao → câu/đoạn dịch không chắc chắn. Ollama có thể tổng hợp bản tốt hơn từ cả hai.
          </div>
          <div className="qs-ma-controls">
            <label className="qs-judge-label">
              Model thứ hai (Ollama):
              <input className="qs-chrf-textarea" style={{resize:'none',height:'36px',fontFamily:'Segoe UI, system-ui, -apple-system, sans-serif',padding:'6px 10px'}}
                value={maModel} onChange={e => setMaModel(e.target.value)} disabled={maLoading} />
            </label>
            <label className="qs-judge-label">
              Số chunks:
              <input type="number" min="5" max="30" value={maMaxChunks}
                onChange={e => setMaMaxChunks(Number(e.target.value))}
                className="qs-judge-num-input" disabled={maLoading} />
            </label>
            <label className="qs-judge-label" style={{flexDirection:'row', alignItems:'center', gap:'8px'}}>
              <input type="checkbox" checked={maSynthesis} onChange={e => setMaSynthesis(e.target.checked)} disabled={maLoading} />
              Tổng hợp bản tốt nhất khi bất đồng
            </label>
            <button className={`qs-chrf-run-btn ${maLoading ? 'running' : ''}`}
              onClick={runMultiAgent} disabled={maLoading}>
              {maLoading ? 'Đang chạy...' : maResult ? 'Chạy lại' : 'Phân tích Multi-Agent'}
            </button>
          </div>
          {maError && <div className="qs-judge-error">{maError}</div>}
          {maLoading && (
            <div className="qs-judge-spinner">
              <div className="qs-spinner" />
              <span>Đang dịch lại và so sánh... (mỗi chunk ~30-60s)</span>
            </div>
          )}
          {maResult && !maLoading && (
            <div className="qs-chrf-result">
              <div className="qs-overview" style={{marginTop:'0.75rem'}}>
                <div className={`qs-score-card ${scoreCls(maResult.mean_agreement)}`}>
                  <div className="qs-score-num">{(maResult.mean_agreement ?? 0).toFixed(1)}<span className="qs-score-unit">%</span></div>
                  <div className="qs-score-name">Đồng thuận TB</div>
                  <div className="qs-score-desc">ChrF++(Gemini, Ollama)</div>
                  <div className={`qs-score-tag ${scoreCls(maResult.mean_agreement)}`}>{scoreLabel(maResult.mean_agreement)}</div>
                </div>
                <div className="qs-stats-card">
                  <div className="qs-stat-row">
                    <span className="qs-stat-val qs-good">{maResult.high_agreement_count}</span>
                    <span className="qs-stat-lbl">đồng thuận (≥65%)</span>
                  </div>
                  <div className="qs-stat-row">
                    <span className="qs-stat-val qs-ok">{maResult.mild_disagreement_count}</span>
                    <span className="qs-stat-lbl">bất đồng nhẹ (40-65%)</span>
                  </div>
                  <div className="qs-stat-row">
                    <span className={`qs-stat-val ${maResult.synthesized_count > 0 ? 'qs-low' : 'qs-good'}`}>{maResult.synthesized_count}</span>
                    <span className="qs-stat-lbl">tổng hợp (&lt;40%)</span>
                  </div>
                </div>
                {/* Agreement distribution chart */}
                {maResult.agreement_distribution?.length === 10 && (
                  <div className="qs-dist-card">
                    <div className="qs-dist-title">Phân phối đồng thuận</div>
                    <div className="qs-dist-bars">
                      {maResult.agreement_distribution.map((count, i) => {
                        const maxB = Math.max(...maResult.agreement_distribution, 1);
                        return (
                          <div key={i} className="qs-dist-col">
                            <div className={`qs-dist-bar ${i >= 7 ? 'qs-good' : i >= 4 ? 'qs-ok' : 'qs-low'}`}
                              style={{height:`${Math.round((count/maxB)*48)}px`}}
                              title={`${i*10}-${i*10+10}%: ${count} chunks`} />
                            <div className="qs-dist-label">{i*10}</div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
              {maResult.interpretation && (
                <div className="qs-chrf-desc" style={{marginTop:'0.75rem'}}>{maResult.interpretation}</div>
              )}
              {/* Per-segment results */}
              {maResult.segments?.length > 0 && (
                <div className="qs-seg-table-wrap" style={{marginTop:'1rem'}}>
                  <table className="qs-seg-table">
                    <thead>
                      <tr><th>#</th><th>Đồng thuận</th><th>Trạng thái</th><th>EN gốc</th><th>Gemini</th><th>Ollama</th></tr>
                    </thead>
                    <tbody>
                      {maResult.segments.map((s, i) => {
                        const verdictLabel = {consensus:'✓ Đồng thuận', mild_disagreement:'~ Bất đồng nhẹ', synthesized:'⚡ Tổng hợp'}[s.verdict] || s.verdict;
                        const verdictCls = {consensus:'qs-row-good', mild_disagreement:'qs-row-ok', synthesized:'qs-row-low'}[s.verdict] || '';
                        return (
                          <tr key={i} className={verdictCls}>
                            <td className="qs-td-num">{s.index}</td>
                            <td className="qs-td-score"><span className={`qs-seg-score ${scoreCls(s.agreement_score)}`}>{s.agreement_score.toFixed(0)}%</span></td>
                            <td style={{fontSize:'0.78rem', whiteSpace:'nowrap'}}>{verdictLabel}</td>
                            <td className="qs-td-text">{s.en_source}</td>
                            <td className="qs-td-text">{s.vi_primary}</td>
                            <td className="qs-td-text" style={{color:'var(--text-muted)'}}>{s.vi_secondary || '—'}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* ── LLM Judge ── */}
      {activeTab === 'judge' && (
        <div className="qs-judge-panel">
          {/* Backend selector — Ollama (local) vs Gemini (web) */}
          <div className="qs-judge-controls">
            <div className="qs-judge-model-wrap">
              <label className="qs-judge-label">Backend đánh giá</label>
              <select
                className="qs-judge-select"
                value={judgeBackend}
                onChange={e => switchJudgeBackend(e.target.value)}
                disabled={judgeRunning}
              >
                <option value="ollama">Ollama (local) — Qwen / DeepSeek / Gemma</option>
                <option value="gemini" disabled={jobType !== 'pdf'}>
                  Gemini Web (Playwright) — mạnh nhất, chậm hơn{jobType !== 'pdf' ? ' [chỉ PDF]' : ''}
                </option>
                <option value="web" disabled={jobType !== 'pdf'}>
                  Cross-model Web (ChatGPT / DeepSeek) — chấm chéo, ít bias{jobType !== 'pdf' ? ' [chỉ PDF]' : ''}
                </option>
              </select>
              <div className="qs-judge-hint" style={{marginTop:6}}>
                {judgeBackend === 'web'
                  ? 'Chấm bằng web AI KHÁC model dịch (vd dịch=Gemini → chấm=ChatGPT/DeepSeek) qua Playwright — tránh self-judging bias, không tốn API key.'
                  : judgeBackend === 'gemini'
                    ? 'Reuse session Gemini Pro của bạn — không tốn API key. Có self-favoring bias ~5-10%.'
                    : 'Chạy local qua Ollama. Recommend qwen2.5:32b cho VI nếu hardware đủ.'}
              </div>
            </div>
          </div>

          {/* Ollama-only controls — model picker hidden when backend is Gemini */}
          <div className="qs-judge-controls" style={{display: judgeBackend === 'ollama' ? 'flex' : 'none'}}>
            <div className="qs-judge-model-wrap">
              <label className="qs-judge-label">Model Ollama</label>
              {judgeModels === null ? (
                <span className="qs-judge-loading">Đang tải danh sách model...</span>
              ) : !judgeModels.ollama_running ? (
                <div className="qs-judge-ollama-off">
                  <span>Ollama chưa chạy. </span>
                  <a href="https://ollama.com" target="_blank" rel="noopener">Cài đặt Ollama</a>
                  <span> rồi chạy: </span>
                  <code>ollama pull qwen2.5:7b</code>
                </div>
              ) : (
                <select
                  className="qs-judge-select"
                  value={judgeModel}
                  onChange={e => setJudgeModel(e.target.value)}
                  disabled={judgeRunning}
                >
                  {judgeModels.models
                    .filter(m => m.available)
                    .map(m => (
                      <option key={m.id} value={m.installed_id || m.id}>
                        {m.name} — {m.description}
                      </option>
                    ))
                  }
                  {judgeModels.models.filter(m => m.available).length === 0 && (
                    <option disabled>Chưa có model nào. Chạy: ollama pull qwen2.5:7b</option>
                  )}
                </select>
              )}
            </div>

            {judgeModels?.ollama_running && judgeModels.models.filter(m => !m.available && m.pull_cmd).length > 0 && (
              <details className="qs-judge-install-hint">
                <summary>Model khác có thể cài</summary>
                <ul>
                  {judgeModels.models.filter(m => !m.available).map(m => (
                    <li key={m.id}>
                      <code>{m.pull_cmd}</code> — {m.name} ({m.size_gb}GB) — {m.description}
                    </li>
                  ))}
                </ul>
              </details>
            )}
          </div>

          {/* Cross-model web judge: pick which web AI grades (must differ from translator) */}
          <div className="qs-judge-controls" style={{display: judgeBackend === 'web' ? 'flex' : 'none'}}>
            <div className="qs-judge-model-wrap">
              <label className="qs-judge-label">Model chấm (≠ model dịch)</label>
              <select
                className="qs-judge-select"
                value={webJudgeBackend}
                onChange={e => setWebJudgeBackend(e.target.value)}
                disabled={judgeRunning}
              >
                <option value="">Tự chọn (ưu tiên ChatGPT → DeepSeek)</option>
                <option value="chatgpt">ChatGPT</option>
                <option value="aistudio">AI Studio</option>
                <option value="deepseek">DeepSeek</option>
                <option value="grok">Grok</option>
                <option value="copilot">Copilot</option>
                <option value="gemini">Gemini</option>
              </select>
              <div className="qs-judge-hint" style={{marginTop:6}}>
                Nếu trùng model dịch, backend sẽ tự đổi sang model khác.
              </div>
            </div>
          </div>

          {/* Shared controls (max segments + run) — work for all backends */}
          <div className="qs-judge-controls">
            <div className="qs-judge-opts">
              <label className="qs-judge-label">
                Số segments tối đa:
                <input
                  type="number"
                  min="1" max="50"
                  value={judgeMaxSegs}
                  onChange={e => setJudgeMaxSegs(Number(e.target.value))}
                  className="qs-judge-num-input"
                  disabled={judgeRunning}
                />
              </label>
              <span className="qs-judge-hint">Ưu tiên segments có điểm thấp nhất</span>
            </div>

            <button
              className={`qs-judge-run-btn ${judgeRunning ? 'running' : ''}`}
              onClick={runJudge}
              disabled={
                judgeRunning ||
                (judgeBackend === 'ollama' && !judgeModels?.ollama_running) ||
                ((judgeBackend === 'gemini' || judgeBackend === 'web') && jobType !== 'pdf')
              }
            >
              {judgeRunning
                ? 'Đang phân tích...'
                : judgeResult ? `Chạy lại (${judgeLabel(judgeBackend)})`
                : `Phân tích chi tiết (${judgeLabel(judgeBackend)})`}
            </button>
          </div>

          {judgeError && (
            <div className="qs-judge-error">{judgeError}</div>
          )}

          {judgeRunning && (
            <div className="qs-judge-spinner">
              <div className="qs-spinner" />
              <span>LLM đang đánh giá từng segment... (có thể mất vài phút)</span>
            </div>
          )}

          {/* Results */}
          {judgeResult && !judgeRunning && (
            <div className="qs-judge-results">
              {/* Summary */}
              <div className="qs-judge-summary">
                <div className="qs-judge-sum-card">
                  <div className="qs-judge-sum-val">{judgeResult.num_judged}</div>
                  <div className="qs-judge-sum-lbl">segments phân tích</div>
                </div>
                {judgeResult.avg_score != null && (
                  <div className={`qs-judge-sum-card ${scoreCls(judgeResult.avg_score)}`}>
                    <div className="qs-judge-sum-val">{judgeResult.avg_score}<span style={{fontSize:'0.7em'}}>/100</span></div>
                    <div className="qs-judge-sum-lbl">Điểm trung bình MQM</div>
                  </div>
                )}
                {judgeResult.error_counts && Object.entries(judgeResult.error_counts).length > 0 && (
                  <div className="qs-judge-sum-card">
                    <div className="qs-judge-errcounts">
                      {Object.entries(judgeResult.error_counts)
                        .sort((a, b) => b[1] - a[1])
                        .map(([cat, cnt]) => (
                          <div key={cat} className="qs-judge-errrow">
                            <span className="qs-judge-errcat">{MQM_CATEGORY_LABELS[cat] || cat}</span>
                            <span className="qs-judge-errcnt">{cnt}</span>
                          </div>
                        ))
                      }
                    </div>
                    <div className="qs-judge-sum-lbl">Lỗi theo danh mục</div>
                  </div>
                )}
                <div className="qs-judge-sum-card qs-judge-model-badge">
                  <div className="qs-judge-sum-val" style={{fontSize:'0.85em'}}>{judgeResult.model}</div>
                  <div className="qs-judge-sum-lbl">
                    {judgeResult.judge_backend && judgeResult.translator_backend
                      ? `Chấm chéo: ${judgeResult.judge_backend} ≠ dịch (${judgeResult.translator_backend})`
                      : 'Model đánh giá'}
                  </div>
                </div>
              </div>

              {/* Per-segment results */}
              <div className="qs-judge-seg-list">
                {judgeResult.results
                  .filter(r => r.llm_result)
                  .map((r, i) => {
                    const res = r.llm_result;
                    const verdictCls = res.verdict === 'good' ? 'qs-good' : res.verdict === 'acceptable' ? 'qs-ok' : 'qs-low';
                    return (
                      <div key={i} className="qs-judge-seg">
                        <div className="qs-judge-seg-header">
                          <span className="qs-judge-seg-idx">Segment #{r.index + 1}</span>
                          <span className={`qs-judge-seg-score ${scoreCls(res.mqm_score ?? res.score)}`}>{res.mqm_score ?? res.score}/100</span>
                          <span className={`qs-judge-verdict ${verdictCls}`}>
                            {res.verdict === 'good' ? 'Tốt' : res.verdict === 'acceptable' ? 'Chấp nhận được' : 'Kém'}
                          </span>
                        </div>

                        <div className="qs-judge-texts">
                          <div className="qs-judge-src"><span className="qs-lbl">EN:</span> {r.src || res.source_span || '—'}</div>
                          <div className="qs-judge-mt"><span className="qs-lbl">VI:</span> {r.mt || res.translation_span || '—'}</div>
                        </div>

                        {res.errors?.length > 0 && (
                          <div className="qs-judge-errors">
                            {res.errors.map((e, j) => (
                              <div key={j} className={`qs-judge-err ${MQM_SEVERITY_COLOR[e.severity] || ''}`}>
                                <span className="qs-judge-err-cat">{MQM_CATEGORY_LABELS[e.category] || e.category}</span>
                                <span className={`qs-judge-err-sev`}>{e.severity}</span>
                                <span className="qs-judge-err-desc">{e.description}</span>
                                {e.translation_span && (
                                  <span className="qs-judge-err-span" title="Đoạn bị lỗi">"{e.translation_span}"</span>
                                )}
                              </div>
                            ))}
                          </div>
                        )}

                        {res.suggestion && (
                          <div className="qs-judge-suggestion">
                            <span className="qs-lbl">Gợi ý:</span> {res.suggestion}
                          </div>
                        )}
                        {res.strengths && (
                          <div className="qs-judge-strengths">
                            <span className="qs-lbl">Điểm tốt:</span> {res.strengths}
                          </div>
                        )}
                      </div>
                    );
                  })
                }
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── Diagnostics tab ── */}
      {activeTab === 'diagnostics' && jobType === 'pdf' && (
        <div className="qs-diag-panel">
          <div className="qs-diag-header">
            <span className="qs-diag-title">Chẩn đoán nguyên nhân chất lượng thấp</span>
            <button
              className="qs-diag-refresh"
              onClick={() => loadDiagnostics(true)}
              disabled={diagLoading}
              title="Phân tích lại"
            >
              {diagLoading ? '⟳ Đang phân tích...' : '⟳ Phân tích lại'}
            </button>
          </div>

          {diagLoading && !diagnostics && (
            <div className="qs-diag-loading">Đang phân tích chunk files...</div>
          )}

          {!diagLoading && !diagnostics && (
            <div className="qs-diag-empty">
              Chưa có dữ liệu chẩn đoán. Nhấn "Phân tích lại" để chạy.
            </div>
          )}

          {diagnostics && (
            <>
              {/* Summary banner */}
              <div className={`qs-diag-summary qs-diag-sev-${diagnostics.overall_severity || 'ok'}`}>
                <div className="qs-diag-sev-icon">
                  {diagnostics.overall_severity === 'critical' ? '🔴' :
                   diagnostics.overall_severity === 'warning'  ? '🟡' :
                   diagnostics.overall_severity === 'info'     ? '🔵' : '✅'}
                </div>
                <div className="qs-diag-summary-text">
                  {diagnostics.summary || 'Không phát hiện vấn đề rõ ràng.'}
                </div>
              </div>

              {/* Findings list */}
              {diagnostics.findings?.length > 0 ? (
                <div className="qs-diag-findings">
                  {diagnostics.findings.map((f, i) => (
                    <div key={i} className={`qs-diag-finding qs-diag-sev-${f.severity}`}>
                      <div className="qs-diag-finding-header">
                        <span className={`qs-diag-badge qs-diag-sev-${f.severity}`}>
                          {f.severity === 'critical' ? '● Nghiêm trọng' :
                           f.severity === 'warning'  ? '◆ Cảnh báo' : '◇ Thông tin'}
                        </span>
                        <span className="qs-diag-cause-label">{f.cause_label}</span>
                        <span className="qs-diag-confidence">
                          {Math.round(f.confidence * 100)}% tin cậy
                        </span>
                        {f.auto_fixable && (
                          <span className="qs-diag-fixable">⚡ Có thể tự sửa</span>
                        )}
                      </div>

                      <div className="qs-diag-rec">
                        <span className="qs-lbl">Khuyến nghị:</span> {f.recommendation}
                      </div>

                      {f.affected_chunks?.length > 0 && (
                        <div className="qs-diag-chunks">
                          <span className="qs-lbl">Chunks ảnh hưởng:</span>{' '}
                          {f.affected_chunks.slice(0, 12).map(c => (
                            <span key={c} className="qs-diag-chunk-tag">#{c}</span>
                          ))}
                          {f.affected_chunks.length > 12 && (
                            <span className="qs-diag-chunk-more">+{f.affected_chunks.length - 12} more</span>
                          )}
                        </div>
                      )}

                      {f.evidence?.length > 0 && (
                        <details className="qs-diag-evidence">
                          <summary>Bằng chứng ({f.evidence.length})</summary>
                          <ul>
                            {f.evidence.map((e, j) => (
                              <li key={j} className="qs-diag-evidence-item">{e}</li>
                            ))}
                          </ul>
                        </details>
                      )}
                    </div>
                  ))}
                </div>
              ) : (
                <div className="qs-diag-no-findings">
                  ✅ Không phát hiện vấn đề cụ thể. Chất lượng có thể đã đạt mức tốt.
                </div>
              )}
            </>
          )}
        </div>
      )}

      {/* ── Audit log ── */}
      {activeTab === 'audit' && jobType === 'pdf' && (
        <div className="qs-audit-panel">
          <div className="qs-audit-header">
            <span className="qs-audit-title">Audit log — dấu vết toàn bộ pipeline</span>
            <button
              className="qs-audit-refresh"
              onClick={() => loadAudit()}
              disabled={auditLoading}
              title="Tải lại"
            >
              {auditLoading ? '⟳ Đang tải...' : '⟳ Tải lại'}
            </button>
          </div>

          {auditError && (
            <div className="qs-audit-error">⚠ {auditError}</div>
          )}

          {auditLoading && auditEvents.length === 0 && !auditError && (
            <div className="qs-audit-loading">Đang đọc audit.jsonl...</div>
          )}

          {!auditLoading && auditEvents.length === 0 && !auditError && (
            <div className="qs-audit-empty">
              Chưa có audit log. File <code>workspace/jobs/{jobId}/audit.jsonl</code> có thể chưa được tạo
              (job cũ trước khi audit được tích hợp).
            </div>
          )}

          {/* Environment snapshot */}
          {auditEnv && (
            <details className="qs-audit-env">
              <summary>Môi trường chạy (Python, OS, packages)</summary>
              <div className="qs-audit-env-grid">
                <div>
                  <span className="qs-lbl">Python:</span>{' '}
                  {auditEnv.python?.version} ({auditEnv.python?.implementation})
                </div>
                <div>
                  <span className="qs-lbl">OS:</span>{' '}
                  {auditEnv.os?.system} {auditEnv.os?.release} ({auditEnv.os?.machine})
                </div>
                <div>
                  <span className="qs-lbl">Translator:</span>{' '}
                  {auditEnv.translator?.backend} / {auditEnv.translator?.mode}
                </div>
                <div>
                  <span className="qs-lbl">Snapshot lúc:</span> {auditEnv.ts}
                </div>
                <div>
                  <span className="qs-lbl">Ollama:</span>{' '}
                  {auditEnv.ollama?.available
                    ? `${auditEnv.ollama.models?.length || 0} models`
                    : 'không chạy'}
                </div>
                <div>
                  <span className="qs-lbl">Scheduler:</span>{' '}
                  {auditEnv.scheduler?.ENABLE_SCHEDULER ? 'ON' : 'OFF'}
                  {auditEnv.multi_agent?.ENABLE_MULTI_AGENT ? ' / Multi-agent ON' : ''}
                </div>
              </div>
              {auditEnv.packages && (
                <div className="qs-audit-pkgs">
                  <div className="qs-lbl">Packages:</div>
                  <pre>{Object.entries(auditEnv.packages)
                    .map(([k, v]) => `${k.padEnd(14)} ${v}`).join('\n')}</pre>
                </div>
              )}
              {auditEnv.config && Object.keys(auditEnv.config).length > 0 && (
                <div className="qs-audit-flags">
                  <div className="qs-lbl">Config:</div>
                  <pre>{JSON.stringify(auditEnv.config, null, 2)}</pre>
                </div>
              )}
              {auditEnv.ollama?.models?.length > 0 && (
                <div className="qs-audit-flags">
                  <div className="qs-lbl">Ollama models:</div>
                  <pre>{auditEnv.ollama.models.join('\n')}</pre>
                </div>
              )}
            </details>
          )}

          {/* Summary stats */}
          {auditSummary && (
            <div className="qs-audit-summary">
              <div className="qs-audit-summary-row">
                <div className="qs-audit-card">
                  <div className="qs-audit-card-title">Số events</div>
                  <div className="qs-audit-card-value">{auditTotal || '—'}</div>
                </div>
                <div className="qs-audit-card">
                  <div className="qs-audit-card-title">Thời gian chạy</div>
                  <div className="qs-audit-card-value">
                    {auditSummary.total_duration_seconds != null
                      ? `${Number(auditSummary.total_duration_seconds).toFixed(1)}s`
                      : '—'}
                  </div>
                </div>
                <div className="qs-audit-card">
                  <div className="qs-audit-card-title">Lỗi</div>
                  <div className="qs-audit-card-value">
                    {auditSummary.error_count ?? 0}
                  </div>
                </div>
                <div className="qs-audit-card">
                  <div className="qs-audit-card-title">Phases</div>
                  <div className="qs-audit-card-value">
                    {Object.keys(auditSummary.phase_durations || {}).length}
                  </div>
                </div>
              </div>

              {auditSummary.phase_durations && Object.keys(auditSummary.phase_durations).length > 0 && (
                <details className="qs-audit-section" open>
                  <summary>Thời gian từng phase</summary>
                  <table className="qs-audit-table">
                    <thead><tr><th>Phase</th><th>Thời lượng (s)</th></tr></thead>
                    <tbody>
                      {Object.entries(auditSummary.phase_durations).map(([phase, dur]) => (
                        <tr key={phase}>
                          <td><code>{phase}</code></td>
                          <td>{typeof dur === 'number' ? dur.toFixed(2) : dur}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}

              {auditSummary.event_counts_by_prefix && Object.keys(auditSummary.event_counts_by_prefix).length > 0 && (
                <details className="qs-audit-section">
                  <summary>Đếm event theo nhóm</summary>
                  <table className="qs-audit-table">
                    <thead><tr><th>Prefix</th><th>Count</th></tr></thead>
                    <tbody>
                      {Object.entries(auditSummary.event_counts_by_prefix)
                        .sort((a, b) => b[1] - a[1])
                        .map(([prefix, count]) => (
                          <tr key={prefix}>
                            <td><code>{prefix}</code></td>
                            <td>{count}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </details>
              )}

              {auditSummary.chunk_latency_summary && Object.keys(auditSummary.chunk_latency_summary).length > 0 && (
                <details className="qs-audit-section">
                  <summary>Latency dịch chunk</summary>
                  <table className="qs-audit-table">
                    <thead>
                      <tr><th>Scope</th><th>Count</th><th>Mean (s)</th><th>Min (s)</th><th>Max (s)</th></tr>
                    </thead>
                    <tbody>
                      {Object.entries(auditSummary.chunk_latency_summary).map(([scope, stats]) => (
                        <tr key={scope}>
                          <td><code>{scope}</code></td>
                          <td>{stats.count}</td>
                          <td>{stats.mean_seconds?.toFixed(2)}</td>
                          <td>{stats.min_seconds?.toFixed(2)}</td>
                          <td>{stats.max_seconds?.toFixed(2)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </details>
              )}

              {auditSummary.error_events?.length > 0 && (
                <details className="qs-audit-section">
                  <summary>Events lỗi ({auditSummary.error_count ?? auditSummary.error_events.length})</summary>
                  <ul className="qs-audit-errors">
                    {auditSummary.error_events.slice(0, 50).map((ev, i) => {
                      const msg = ev.data?.error || ev.data?.message
                               || ev.data?.exc_type || ev.data?.reason;
                      return (
                        <li key={i}>
                          <span className="qs-audit-seq">#{ev.seq}</span>
                          <code>{ev.event_type}</code>
                          {ev.phase && <span className="qs-audit-phase-tag">{ev.phase}</span>}
                          {msg && <span className="qs-audit-err-msg">— {String(msg).slice(0, 200)}</span>}
                        </li>
                      );
                    })}
                  </ul>
                </details>
              )}
            </div>
          )}

          {/* Filters + event timeline */}
          <div className="qs-audit-filters">
            <label>
              Event prefix:
              <input
                type="text"
                value={auditFilter}
                placeholder="vd: chunk. hoặc translate."
                onChange={e => setAuditFilter(e.target.value)}
              />
            </label>
            <label>
              Phase:
              <select value={auditPhase} onChange={e => setAuditPhase(e.target.value)}>
                <option value="">(tất cả)</option>
                <option value="init">init</option>
                <option value="extraction">extraction</option>
                <option value="chunking">chunking</option>
                <option value="translating">translating</option>
                <option value="rebuilding">rebuilding</option>
                <option value="validation">validation</option>
                <option value="finished">finished</option>
              </select>
            </label>
            <label>
              Limit:
              <select value={auditLimit} onChange={e => setAuditLimit(Number(e.target.value))}>
                <option value={100}>100</option>
                <option value={200}>200</option>
                <option value={500}>500</option>
                <option value={1000}>1000</option>
              </select>
            </label>
            <button className="qs-audit-apply" onClick={loadAudit} disabled={auditLoading}>
              Áp dụng
            </button>
          </div>

          {auditEvents.length > 0 && (
            <div className="qs-audit-timeline">
              <div className="qs-audit-timeline-meta">
                Hiển thị {auditEvents.length} / {auditTotal} events
                {auditHasMore && <span className="qs-audit-more"> — còn nữa, tăng Limit để xem thêm</span>}
              </div>
              <table className="qs-audit-events">
                <thead>
                  <tr>
                    <th>Seq</th>
                    <th>Time</th>
                    <th>Phase</th>
                    <th>Event</th>
                    <th>Data</th>
                  </tr>
                </thead>
                <tbody>
                  {auditEvents.map((ev) => {
                    const et = ev.event_type || '';
                    const isError = et.startsWith('error.') || et.endsWith('_failed');
                    const isWarning = et.includes('truncated') || et.includes('retry')
                                   || et.includes('fallback');
                    const cls = isError ? 'qs-audit-row-error'
                              : isWarning ? 'qs-audit-row-warn' : '';
                    const data = ev.data || {};
                    const dataKeys = Object.keys(data);
                    const dataStr = dataKeys.length > 0
                      ? JSON.stringify(data, null, 2)
                      : '';
                    return (
                      <tr key={`${ev.run_id || ''}-${ev.seq}`} className={cls}>
                        <td className="qs-audit-seq-cell">{ev.seq}</td>
                        <td className="qs-audit-ts-cell">{ev.ts?.split('T')[1]?.split('.')[0] || ev.ts}</td>
                        <td>{ev.phase && <span className="qs-audit-phase-tag">{ev.phase}</span>}</td>
                        <td><code>{et}</code></td>
                        <td>
                          {dataStr && (
                            <details>
                              <summary>{dataKeys.length} fields</summary>
                              <pre className="qs-audit-data">{dataStr}</pre>
                            </details>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
