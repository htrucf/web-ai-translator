import { useState, useRef, useCallback, useEffect } from 'react';
import PdfViewer from './components/PdfViewer';
import JobHistory from './components/JobHistory';
import PdfUploadPanel from './components/PdfUploadPanel';
import QualityPanel from './components/QualityPanel';
import HistoryEditor from './components/HistoryEditor';
import LoginScreen from './components/LoginScreen';
import GlossaryEditor from './components/GlossaryEditor';
import IssueTriagePanel from './components/IssueTriagePanel';
import SchedulerPanel from './components/SchedulerPanel';
import './App.css';

import API_URL from './api.js';

const IDLE_TIMEOUT_MS = 4 * 60 * 60 * 1000; // 4 hours — must match backend SESSION_IDLE_TIMEOUT

function PipelineIcon({ name }) {
  const common = {
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 2,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
  };
  const icons = {
    extract: (
      <>
        <path {...common} d="M5 19V5h7l7 7v7H5Z" />
        <path {...common} d="M12 5v7h7" />
        <path {...common} d="M9 16h6" />
      </>
    ),
    plan: (
      <>
        <path {...common} d="M8 5h10v14H6V7a2 2 0 0 1 2-2Z" />
        <path {...common} d="M9 9h6" />
        <path {...common} d="M9 13h5" />
        <path {...common} d="M9 17h3" />
      </>
    ),
    glossary: (
      <>
        <path {...common} d="M5 5h7a3 3 0 0 1 3 3v11H8a3 3 0 0 0-3 3V5Z" />
        <path {...common} d="M15 8h4v11h-4" />
        <path {...common} d="M8 9h4" />
        <path {...common} d="M8 13h4" />
      </>
    ),
    style: (
      <>
        <path {...common} d="M4 20h16" />
        <path {...common} d="M7 16l7.5-7.5 3 3L10 19H7v-3Z" />
        <path {...common} d="M13.5 9.5l3 3" />
      </>
    ),
    translate: (
      <>
        <path {...common} d="M4 6h9" />
        <path {...common} d="M9 4v2c0 4-2 7-5 9" />
        <path {...common} d="M6 10c1 2 3 4 6 5" />
        <path {...common} d="M14 20l4-9 4 9" />
        <path {...common} d="M15.5 17h5" />
      </>
    ),
    rebuild: (
      <>
        <path {...common} d="M7 7h10v10H7z" />
        <path {...common} d="M4 12h3" />
        <path {...common} d="M17 12h3" />
        <path {...common} d="M12 4v3" />
        <path {...common} d="M12 17v3" />
      </>
    ),
    check: (
      <>
        <path {...common} d="M12 3l7 4v5c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V7l7-4Z" />
        <path {...common} d="M9 12l2 2 4-5" />
      </>
    ),
    report: (
      <>
        <path {...common} d="M5 19V5h14v14H5Z" />
        <path {...common} d="M9 15v-3" />
        <path {...common} d="M12 15V9" />
        <path {...common} d="M15 15v-5" />
      </>
    ),
    done: <path {...common} d="M5 12.5l4 4L19 6.5" />,
  };
  return (
    <svg className="job-step-icon" viewBox="0 0 24 24" aria-hidden="true">
      {icons[name] || icons.check}
    </svg>
  );
}

function App() {
  // ── Auth ──────────────────────────────────────────────────────────────────
  const [token, setToken] = useState(() => localStorage.getItem('auth_token'));
  const [userInfo, setUserInfo] = useState(null); // { username, is_admin }
  const idleTimerRef = useRef(null);
  // Pause idle logout while a translation job is running — user has no
  // mouse/keyboard activity during long Gemini polls and would otherwise
  // get kicked out mid-job. Set by the currentJob useEffect below.
  const hasActiveJobRef = useRef(false);

  function resetIdleTimer() {
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
    if (hasActiveJobRef.current) return;
    idleTimerRef.current = setTimeout(handleLogout, IDLE_TIMEOUT_MS);
  }

  function handleLogin(newToken) {
    localStorage.setItem('auth_token', newToken);
    setToken(newToken);
    resetIdleTimer();
  }

  async function handleLogout() {
    const t = localStorage.getItem('auth_token');
    localStorage.removeItem('auth_token');
    setToken(null);
    setUserInfo(null);
    if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
    if (t) {
      try { await fetch(`${API_URL}/api/auth/logout`, { method: 'POST', headers: { Authorization: `Bearer ${t}` } }); }
      catch { /* ignore */ }
    }
  }

  // Cross-tab logout sync: localStorage fires `storage` events in OTHER tabs
  // when a key changes. If another tab logs out (removes auth_token), this
  // tab also drops its session so the user isn't left with stale UI state.
  useEffect(() => {
    function onStorage(e) {
      if (e.key !== 'auth_token') return;
      if (!e.newValue) {
        // Token cleared in another tab
        setToken(null);
        setUserInfo(null);
        if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
      } else if (e.newValue !== token) {
        // Token changed (re-login in another tab) — adopt the new token
        setToken(e.newValue);
      }
    }
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, [token]);

  // Validate token via /api/auth/me — handles expired/invalid tokens on mount
  useEffect(() => {
    if (!token) { setUserInfo(null); return; }
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_URL}/api/auth/me`, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (cancelled) return;
        if (!res.ok) {
          // Token invalid/expired — force re-login
          localStorage.removeItem('auth_token');
          setToken(null);
          setUserInfo(null);
          return;
        }
        setUserInfo(await res.json());
      } catch {
        // Network issue — keep token, allow retry
      }
    })();
    return () => { cancelled = true; };
  }, [token]);

  // Reset idle timer on any user activity
  useEffect(() => {
    if (!token) return;
    resetIdleTimer();
    const events = ['mousedown', 'keydown', 'scroll', 'touchstart'];
    events.forEach(e => window.addEventListener(e, resetIdleTimer, { passive: true }));
    return () => {
      events.forEach(e => window.removeEventListener(e, resetIdleTimer));
      if (idleTimerRef.current) clearTimeout(idleTimerRef.current);
    };
  }, [token]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── End Auth ──────────────────────────────────────────────────────────────
  // Note: login gate is enforced at the JSX render below, NOT here, to avoid
  // calling fewer hooks during the unauthenticated render than the
  // authenticated one (Rules of Hooks).

  const [originalPdf, setOriginalPdf] = useState(null);
  const [translatedPdf, setTranslatedPdf] = useState(null);
  const [activeTab, setActiveTab] = useState('pdf-upload');
  const [syncScroll, setSyncScroll] = useState(true);
  const [progress, setProgress] = useState(null); // { current, total, status }
  const [currentJob, setCurrentJob] = useState(null); // { jobId, type: 'latex' | 'pdf' }
  const [pausedJob, setPausedJob] = useState(null); // { job_id, source_type } for pause/resume buttons
  const [compiling, setCompiling] = useState(false);
  const [completedJob, setCompletedJob] = useState(null); // { jobId, type: 'latex' | 'pdf' }
  const [showQuality, setShowQuality] = useState(false);
  const [showGlossary, setShowGlossary] = useState(false);
  const [showTriage, setShowTriage] = useState(true); // open by default — surfaces issues prominently
  const [triageTarget, setTriageTarget] = useState(null); // { jobId, chunkKey } — deep-link into HistoryEditor
  const [chunkBlockMap, setChunkBlockMap] = useState(null); // PDF overlay map for the active completed job
  const [awaitingGlossaryReview, setAwaitingGlossaryReview] = useState(false);
  const [approvingGlossary, setApprovingGlossary] = useState(false);
  const [awaitingStyleReview, setAwaitingStyleReview] = useState(false);
  const [approvingStyleAnchor, setApprovingStyleAnchor] = useState(false);
  const [styleAnchorReview, setStyleAnchorReview] = useState({ en: '', vi: '', source_model: '' });
  const [currentJobTitle, setCurrentJobTitle] = useState(null); // title/filename of loaded job
  const [jobSideTab, setJobSideTab] = useState('summary');
  const [glossaryPreview, setGlossaryPreview] = useState({ loading: false, terms: [], count: 0, error: null });
  const [errorToast, setErrorToast] = useState(null); // { title, detail }
  const errorToastTimerRef = useRef(null);
  const pipelineJobRef = useRef(null);
  const furthestPipelineStepRef = useRef(0);
  // Office (.docx) jobs: the right pane shows the LibreOffice-rendered
  // preview PDF, but the user wants to download the actual .docx file —
  // so we track the source download URL separately from translatedPdf.
  const [officeDownload, setOfficeDownload] = useState(null); // { url, filename, kind } | null
  const uploadLockedByActiveJob = !!currentJob
    || !!pausedJob
    || (!!progress
      && !completedJob
      && !progress.status?.startsWith('Lỗi:')
      && progress.status !== 'Đã hủy bản dịch'
      && progress.status !== 'Đã được thay bằng lượt chạy mới');

  // Sync the active-job ref so the idle timer above can read it without a
  // re-render-induced subscription. When a job ends, restart the countdown.
  useEffect(() => {
    hasActiveJobRef.current = !!currentJob;
    if (!currentJob && token) {
      resetIdleTimer();
    } else if (currentJob && idleTimerRef.current) {
      clearTimeout(idleTimerRef.current);
      idleTimerRef.current = null;
    }
  }, [currentJob, token]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (uploadLockedByActiveJob && activeTab === 'pdf-upload') {
      setActiveTab('compare');
    }
  }, [uploadLockedByActiveJob, activeTab]);

  function showError(title, detail) {
    if (errorToastTimerRef.current) clearTimeout(errorToastTimerRef.current);
    setErrorToast({ title, detail });
    errorToastTimerRef.current = setTimeout(() => setErrorToast(null), 12000);
  }

  function dismissError() {
    if (errorToastTimerRef.current) clearTimeout(errorToastTimerRef.current);
    setErrorToast(null);
  }

  // Cleanup on unmount — kill any timers/intervals so they don't keep firing
  // after the user navigates away (or React StrictMode remounts in dev).
  useEffect(() => () => {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  }, []);

  // Helper: fetch with error handling + auto-inject auth token.
  // 401 → wipe session and re-prompt login (token expired/invalid).
  async function apiFetch(url, options = {}) {
    const t = localStorage.getItem('auth_token');
    const headers = {
      ...(options.headers || {}),
      ...(t ? { Authorization: `Bearer ${t}` } : {}),
    };
    let res;
    try {
      res = await fetch(url, { ...options, headers });
    } catch (err) {
      const isNetworkErr = err.message === 'Failed to fetch' || err.name === 'TypeError';
      throw new Error(isNetworkErr
        ? `Không kết nối được backend (${API_URL}).\nKiểm tra: uvicorn có đang chạy không? venv312 đã activate chưa?`
        : err.message
      );
    }
    if (res.status === 401) {
      localStorage.removeItem('auth_token');
      setToken(null);
      setUserInfo(null);
      throw new Error('Phiên đã hết hạn. Vui lòng đăng nhập lại.');
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const body = await res.json();
        detail = body.detail || body.message || JSON.stringify(body);
      } catch {
        // Keep the generic HTTP status when the backend response is not JSON.
      }
      throw new Error(`${detail}`);
    }
    return res.json();
  }

  // Bare auth-injected fetch (returns Response, no auto-parse) for pollers / non-JSON
  function authFetch(url, options = {}) {
    const t = localStorage.getItem('auth_token');
    const headers = {
      ...(options.headers || {}),
      ...(t ? { Authorization: `Bearer ${t}` } : {}),
    };
    return fetch(url, { ...options, headers });
  }

  // Fetch chunk_block_map when a PDF job becomes the active completed job —
  // powers the clickable paragraph overlay in the original PDF viewer
  // (Gap #2 PDF-anchored feedback). 404 means the job pre-dates the map
  // build; treat that as "no overlay" without surfacing an error.
  useEffect(() => {
    if (!completedJob || completedJob.type !== 'pdf') {
      setChunkBlockMap(null);
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const res = await authFetch(`${API_URL}/api/pdf-translate/${completedJob.jobId}/chunk-map`);
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled) setChunkBlockMap(data);
      } catch { /* non-fatal — overlay just won't render */ }
    })();
    return () => { cancelled = true; };
  }, [completedJob]);

  useEffect(() => {
    const panelJob = completedJob || currentJob;
    if (!panelJob || panelJob.type === 'office') {
      setGlossaryPreview({ loading: false, terms: [], count: 0, error: null });
      return;
    }
    let cancelled = false;
    (async () => {
      setGlossaryPreview(prev => ({ ...prev, loading: true, error: null }));
      try {
        const endpoint = panelJob.type === 'pdf'
          ? `${API_URL}/api/pdf-translate/${panelJob.jobId}/glossary`
          : `${API_URL}/api/job/${panelJob.jobId}/glossary`;
        const res = await authFetch(endpoint);
        if (cancelled) return;
        if (!res.ok) {
          setGlossaryPreview({ loading: false, terms: [], count: 0, error: `HTTP ${res.status}` });
          return;
        }
        const data = await res.json();
        const lockedTerms = (data.locked || []).map(k => String(k).toLowerCase());
        const terms = Object.entries(data.terms || {}).slice(0, 4).map(([en, vi]) => ({
          en,
          vi,
          locked: lockedTerms.includes(en.toLowerCase()),
        }));
        setGlossaryPreview({
          loading: false,
          terms,
          count: data.count ?? Object.keys(data.terms || {}).length,
          error: null,
        });
      } catch (err) {
        if (!cancelled) {
          setGlossaryPreview({ loading: false, terms: [], count: 0, error: err.message || String(err) });
        }
      }
    })();
    return () => { cancelled = true; };
  }, [completedJob, currentJob]);

  // ── Browser target setting ───────────────────────────────────────────────
  const [targetBrowser, setTargetBrowser] = useState('chrome');
  const [aiBackend, setAiBackend] = useState('');

  // Wait for an auth token before hitting the settings endpoint.
  useEffect(() => {
    if (!token) return;
    fetch(`${API_URL}/api/settings/translator-mode`)
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (!d) return;
        setTargetBrowser(d.target_browser || 'chrome');
        setAiBackend(d.ai_backend || '');
      })
      .catch(() => {});
  }, [token]);

  async function handleTargetBrowserSwitch(browser) {
    try {
      const r = await fetch(`${API_URL}/api/settings/target-browser`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ browser }),
      });
      const d = await r.json();
      setTargetBrowser(d.target_browser || browser);
    } catch (err) { showError('Không thể đổi trình duyệt', err.message); }
  }

  const [theme, setTheme] = useState(() => localStorage.getItem('theme') || 'dark');

  // Apply theme to document
  if (typeof document !== 'undefined') {
    document.documentElement.setAttribute('data-theme', theme);
  }

  function toggleTheme() {
    const next = theme === 'dark' ? 'light' : 'dark';
    setTheme(next);
    localStorage.setItem('theme', next);
    document.documentElement.setAttribute('data-theme', next);
  }

  const originalScrollRef = useRef({});
  const translatedScrollRef = useRef({});
  const pollIntervalRef = useRef(null);

  const handleOriginalScroll = useCallback((percent) => {
    if (translatedScrollRef.current?.scrollToPercent) {
      translatedScrollRef.current.scrollToPercent(percent);
    }
  }, []);

  const handleTranslatedScroll = useCallback((percent) => {
    if (originalScrollRef.current?.scrollToPercent) {
      originalScrollRef.current.scrollToPercent(percent);
    }
  }, []);

  function stopPolling() {
    if (pollIntervalRef.current) {
      clearInterval(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
  }

  async function pollJobStatus(jobId) {
    // Clear any stale polling interval from a previous job
    stopPolling();

    const interval = setInterval(async () => {
      try {
        const res = await authFetch(`${API_URL}/api/job/${jobId}`);
        if (res.status === 401) { clearInterval(interval); pollIntervalRef.current = null; return; }
        const data = await res.json();

        // Load PDF gốc ngay khi backend trả về
        if (data.original_pdf_url && !originalPdf) {
          setOriginalPdf(`${API_URL}${data.original_pdf_url}`);
          setActiveTab('compare');
        }

        // Cập nhật tiến độ
        if (data.current_chunk && data.total_chunks) {
          setProgress({
            current: data.current_chunk,
            total: data.total_chunks,
            status: `Đang dịch đoạn ${data.current_chunk}/${data.total_chunks}`,
          });
        } else if (data.status === 'pending') {
          setProgress({ current: 0, total: 0, status: 'Đang tải source từ arXiv...' });
        } else if (data.status?.startsWith('preparing')) {
          const m = data.status.match(/preparing:\s*(\d+)/);
          const n = m ? m[1] : '';
          setProgress({ current: 0, total: 0, status: `Đang mở trình duyệt Gemini${n ? ` (${n} chunks)` : ''}...` });
        } else if (data.status?.startsWith('improving')) {
          const m = data.status.match(/improving:\s*(.+)/);
          setProgress({ current: 0, total: 0, status: `Đang cải thiện chất lượng: ${m ? m[1] : '...'}` });
        } else if (data.status === 'compiling') {
          setProgress({ current: 0, total: 0, status: 'Đang biên dịch PDF...' });
        }

        // Hoàn thành (done hoặc done_with_warnings)
        if ((data.status === 'done' || data.status === 'done_with_warnings') && data.translated_pdf_url) {
          clearInterval(interval);
          pollIntervalRef.current = null;
          setOriginalPdf(`${API_URL}${data.original_pdf_url}`);
          setTranslatedPdf(`${API_URL}${data.translated_pdf_url}`);
          setProgress(null);
          setCurrentJob(null);
          setCompletedJob({ jobId, type: 'latex' }); setShowQuality(false);
          setActiveTab('compare');
        }

        // Nếu lỗi hoặc bị hủy, dừng polling
        if (data.status?.startsWith('error') || data.status?.startsWith('compile_error') || data.status === 'cancelled') {
          clearInterval(interval);
          pollIntervalRef.current = null;
          const msg = data.status === 'cancelled' ? 'Đã hủy bản dịch' : `Lỗi: ${data.status}`;
          setProgress({ current: 0, total: 0, status: msg });
          setCurrentJob(null);
        }
      } catch {
        // bỏ qua lỗi mạng
      }
    }, 3000);

    pollIntervalRef.current = interval;
  }

  // ── PDF-only pipeline polling (uses /api/pdf-translate) ──
  async function pollPdfJobStatus(jobId) {
    stopPolling();

    const interval = setInterval(async () => {
      try {
        const res = await authFetch(`${API_URL}/api/pdf-translate/${jobId}/status`);
        if (res.status === 401) { clearInterval(interval); pollIntervalRef.current = null; return; }
        const data = await res.json();

        if (data.original_filename || data.title) {
          setCurrentJobTitle(data.original_filename || data.title);
        }

        if (data.original_pdf_url && !originalPdf) {
          setOriginalPdf(`${API_URL}${data.original_pdf_url}`);
          setActiveTab('compare');
        }

        // HITL gate: pause for glossary review before bulk translation.
        // The pipeline subprocess exits cleanly here; user resumes via approve.
        // Mirror backend state directly — avoids stale closure on local flag.
        setAwaitingGlossaryReview(!!data.awaiting_glossary_review);
        setAwaitingStyleReview(!!data.awaiting_style_review);
        if (data.style_anchor) {
          setStyleAnchorReview({
            en: data.style_anchor.en || '',
            vi: data.style_anchor.vi || '',
            source_model: data.style_anchor.source_model || '',
          });
        }
        if (data.awaiting_glossary_review) {
          const docTermCount = data.glossary_document_count ?? data.glossary_count ?? 0;
          setProgress({
            current: 0,
            total: 0,
            status: `Glossary đã sẵn sàng (${docTermCount} thuật ngữ mới từ tài liệu) — duyệt trước khi dịch.`,
            phase: data.phase || 'glossary_review',
          });
        }

        if (data.awaiting_style_review) {
          setProgress({
            current: 0,
            total: data.total_chunks || 0,
            status: 'Mẫu văn phong đã sẵn sàng — duyệt bản dịch mẫu trước khi dịch toàn bộ tài liệu.',
            phase: data.phase || 'style_anchor_review',
          });
        } else if (data.status === 'pausing') {
          setProgress({
            current: data.current_chunk || progress?.current || 0,
            total: data.total_chunks || progress?.total || 0,
            status: 'Đang tạm dừng sau phần đã lưu...',
            phase: data.phase,
          });
        } else if (data.status === 'paused') {
          clearInterval(interval);
          pollIntervalRef.current = null;
          setPausedJob({ job_id: jobId, source_type: 'pdf_only' });
          setProgress({
            current: data.current_chunk || progress?.current || 0,
            total: data.total_chunks || progress?.total || 0,
            status: 'Đã tạm dừng. Bấm Resume để chạy tiếp từ phần đã lưu.',
            phase: data.phase,
          });
          setCurrentJob(null);
        } else if (data.status?.startsWith('extracting glossary') || data.phase === 'glossary') {
          setProgress({ current: 0, total: data.total_chunks || 0, status: 'Đang trích xuất glossary từ tài liệu...', phase: data.phase || 'glossary' });
        } else if (data.phase === 'plan' || data.status === 'planning chunks') {
          setProgress({ current: 0, total: data.total_chunks || 0, status: 'Đang lập kế hoạch và chia chunk...', phase: 'plan' });
        } else if (data.current_chunk !== undefined && data.total_chunks && (
          data.phase === 'eval_loop'
          || data.status?.startsWith('translating')
          || data.status?.includes('eval-loop')
          || data.status?.includes('dịch')
        )) {
          const failedInfo = data.failed_chunks ? ` | ${data.failed_chunks} lỗi` : '';
          const modeInfo = data.mode === 'book' ? ' [Sách]' : '';
          const glossaryInfo = data.glossary_count ? ` | ${data.glossary_count} thuật ngữ` : '';
          // Backend exposes the in-flight chunk via last_attempted_chunk_idx
          // (0-based) and the prompt-send counter via current_chunk_attempt.
          // Show "đang dịch X" when X > completed count so user sees real
          // motion during retry/truncation loops instead of a frozen counter.
          const completed = data.current_chunk;
          const inFlight = (data.last_attempted_chunk_idx ?? -1) + 1;
          const attempt = data.current_chunk_attempt || 0;
          const chunkLabel = inFlight > completed
            ? `Đang dịch đoạn ${inFlight}/${data.total_chunks} (đã xong ${completed})`
            : `Đang dịch đoạn ${completed}/${data.total_chunks}`;
          const attemptInfo = attempt > 1 ? ` | lần thử ${attempt}` : '';
          setProgress({
            current: completed,
            total: data.total_chunks,
            status: `${chunkLabel}${modeInfo} (PDF)${glossaryInfo}${attemptInfo}${failedInfo}`,
            phase: data.phase,
          });
        } else if (data.status === 'extracting') {
          setProgress({ current: 0, total: 0, status: 'Đang trích xuất text từ PDF...', phase: data.phase || 'extract' });
        } else if (data.status === 'starting') {
          setProgress({ current: 0, total: 0, status: 'Đang khởi động pipeline...', phase: data.phase });
        } else if (data.status === 'compiling') {
          setProgress({ current: 0, total: 0, status: 'Đang tạo PDF bản dịch...', phase: data.phase || 'rebuild' });
        }

        if ((data.status === 'done' || data.status === 'done_with_warnings') && data.translated_pdf_url) {
          clearInterval(interval);
          pollIntervalRef.current = null;
          // Cache-bust so the viewer reloads the freshly-rebuilt PDF instead
          // of any partial/preview blob from an earlier compile-partial call.
          const cacheBust = Date.now();
          setOriginalPdf(`${API_URL}${data.original_pdf_url}?t=${cacheBust}`);
          setTranslatedPdf(`${API_URL}${data.translated_pdf_url}?t=${cacheBust}`);
          // Show quality score briefly
          if (data.quality_score !== undefined) {
            const qScore = data.quality_score;
            const qIssues = data.quality_issues || 0;
            setProgress({
              current: 0, total: 0,
              status: `Hoàn thành! Chất lượng: ${qScore}/100${qIssues ? ` (${qIssues} vấn đề)` : ''}`,
            });
            setTimeout(() => setProgress(null), 8000);
          } else {
            setProgress(null);
          }
          setCurrentJob(null);
          setCompletedJob({ jobId, type: 'pdf' });
          setShowQuality(false);
          setActiveTab('compare');
        }

        if (data.status?.startsWith('retrying')) {
          setProgress({ current: data.current_chunk || 0, total: data.total_chunks || 0, status: `Đang thử lại... (${data.status})`, phase: data.phase });
        } else if (data.status?.startsWith('error') || data.status === 'cancelled' || data.status === 'superseded') {
          clearInterval(interval);
          pollIntervalRef.current = null;
          const msg = data.status === 'cancelled'
            ? 'Đã hủy bản dịch'
            : data.status === 'superseded'
              ? 'Đã được thay bằng lượt chạy mới'
              : `Lỗi: ${data.status}`;
          setProgress({ current: 0, total: 0, status: msg, phase: data.phase });
          setCurrentJob(null);
        }
      } catch {
        // ignore network errors
      }
    }, 3000);

    pollIntervalRef.current = interval;
  }

  function handlePdfJobStarted(jobData) {
    stopPolling();
    setPausedJob(null);
    setAwaitingGlossaryReview(false);
    setAwaitingStyleReview(false);
    setStyleAnchorReview({ en: '', vi: '', source_model: '' });
    setTranslatedPdf(null);
    setOfficeDownload(null);
    setCurrentJob({ jobId: jobData.job_id, type: jobData.source_type === 'office' ? 'office' : 'pdf' });
    setCurrentJobTitle(jobData.original_filename || jobData.filename || jobData.title || jobData.job_id);

    // Office jobs (.docx): no PDF preview of the source — leave left
    // pane empty; the right pane will fill in with preview.pdf once
    // LibreOffice finishes rendering the translated file.
    if (jobData.source_type === 'office') {
      setOriginalPdf(null);
      setProgress({ current: 0, total: 0, status: `Đang phân tích ${(jobData.kind || 'office').toUpperCase()}...` });
      setActiveTab('compare');
      pollOfficeJobStatus(jobData.job_id);
      return;
    }

    setProgress({ current: 0, total: 0, status: 'Đang trích xuất text từ PDF...' });
    if (jobData.original_pdf_url) {
      setOriginalPdf(`${API_URL}${jobData.original_pdf_url}`);
    }
    setActiveTab('compare');
    pollPdfJobStatus(jobData.job_id);
  }

  // ── Office (.docx) pipeline polling (/api/office-translate) ──
  async function pollOfficeJobStatus(jobId) {
    stopPolling();

    const interval = setInterval(async () => {
      try {
        const res = await authFetch(`${API_URL}/api/office-translate/${jobId}/status`);
        if (res.status === 401) { clearInterval(interval); pollIntervalRef.current = null; return; }
        const data = await res.json();

        const kindLabel = (data.kind || 'office').toUpperCase();
        if (data.current_chunk && data.total_chunks) {
          setProgress({
            current: data.current_chunk,
            total: data.total_chunks,
            status: `Đang dịch đoạn ${data.current_chunk}/${data.total_chunks} (${kindLabel})`,
          });
        } else if (data.status === 'extracting') {
          setProgress({ current: 0, total: 0, status: `Đang trích xuất text từ ${kindLabel}...` });
        } else if (data.status === 'rebuilding') {
          setProgress({ current: 0, total: 0, status: `Đang ghép bản dịch vào ${kindLabel}...` });
        } else if (data.status === 'rendering preview') {
          setProgress({ current: 0, total: 0, status: 'Đang render preview PDF (LibreOffice)...' });
        } else if (data.status === 'pending' || data.status === 'starting') {
          setProgress({ current: 0, total: 0, status: 'Đang khởi động pipeline...' });
        }

        if (data.status === 'done' || data.status === 'done_with_warnings') {
          clearInterval(interval);
          pollIntervalRef.current = null;
          // Right pane = preview PDF (if LibreOffice succeeded); download
          // button targets the actual .docx via /translated.
          if (data.preview_url) {
            setTranslatedPdf(`${API_URL}${data.preview_url}`);
          } else {
            setTranslatedPdf(null);
          }
          if (data.translated_url) {
            const ext = '.docx';
            const base = (data.filename || `translated${ext}`).replace(/\.docx$/i, '');
            setOfficeDownload({
              url: `${API_URL}${data.translated_url}`,
              filename: `${base}_vi${ext}`,
              kind: data.kind || 'docx',
            });
          }
          const msg = data.preview_error
            ? `Hoàn thành! Preview không khả dụng: ${data.preview_error}`
            : 'Hoàn thành!';
          setProgress({ current: 0, total: 0, status: msg });
          setTimeout(() => setProgress(null), 8000);
          setCurrentJob(null);
          setCompletedJob(null); // no quality/glossary panels for office
          setActiveTab('compare');
        }

        if (data.status?.startsWith('error') || data.status === 'cancelled') {
          clearInterval(interval);
          pollIntervalRef.current = null;
          const msg = data.status === 'cancelled' ? 'Đã hủy bản dịch' : `Lỗi: ${data.status}`;
          setProgress({ current: 0, total: 0, status: msg });
          setCurrentJob(null);
        }
      } catch {
        // ignore network errors
      }
    }, 3000);

    pollIntervalRef.current = interval;
  }

  // Handler khi click vao 1 job trong History
  function handleViewJob(jobData) {
    stopPolling();
    setProgress(null);
    setPausedJob(null);
    setCurrentJobTitle(jobData.original_filename || jobData.title || jobData.job_id);

    // Cache-bust so a job that was just rebuilt server-side picks up the new
    // bytes instead of any cached blob from an earlier session.
    const cacheBust = Date.now();
    if (jobData.original_pdf_url) {
      setOriginalPdf(`${API_URL}${jobData.original_pdf_url}?t=${cacheBust}`);
    }
    if (jobData.translated_pdf_url) {
      setTranslatedPdf(`${API_URL}${jobData.translated_pdf_url}?t=${cacheBust}`);
    } else {
      setTranslatedPdf(null);
    }

    setActiveTab('compare');

    // Set completed job for quality panel
    const jType = jobData.source_type === 'pdf_only' || jobData.source_type === 'pdf' ? 'pdf'
      : 'latex';
    if (jobData.status === 'done' || jobData.status === 'done_with_warnings') {
      setCompletedJob({ jobId: jobData.job_id, type: jType });
    } else {
      setCompletedJob(null);
    }
    setShowQuality(false);

    // Neu dang translating, bat dau polling.
    // Backend status string is "translating N/M" once it knows the chunk count,
    // so use startsWith — strict equality only matches the brief pre-chunking
    // window. PDF jobs need pollPdfJobStatus, LaTeX uses pollJobStatus.
    if (typeof jobData.status === 'string' && jobData.status.startsWith('translating')) {
      setCurrentJob({ jobId: jobData.job_id, type: jType });
      setProgress({ current: 0, total: 0, status: 'Đang dịch...' });
      if (jType === 'pdf') pollPdfJobStatus(jobData.job_id);
      else pollJobStatus(jobData.job_id);
    }
  }

  // Handler khi user muon tiep tuc dich 1 job cancelled
  async function handleResumeJob(jobData) {
    stopPolling();
    setPausedJob(null);
    setTranslatedPdf(null);
    setProgress({ current: 0, total: 0, status: 'Đang tiếp tục dịch...' });
    setActiveTab('compare');

    const isPdf = jobData.source_type === 'pdf' || jobData.source_type === 'pdf_only';

    // Load original PDF
    if (isPdf) {
      setOriginalPdf(`${API_URL}/api/pdf-translate/${jobData.job_id}/original`);
    } else {
      setOriginalPdf(`${API_URL}/api/pdf/${jobData.job_id}/original`);
    }

    try {
      let data;
      if (isPdf) {
        data = await apiFetch(`${API_URL}/api/pdf-translate/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_id: jobData.job_id, force: false }),
        });
      } else {
        data = await apiFetch(`${API_URL}/api/translate/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_id: jobData.job_id, resume: true }),
        });
      }
      const jobId = data.job_id || jobData.job_id;
      setCurrentJob({ jobId, type: isPdf ? 'pdf' : 'latex' });
      if (isPdf) pollPdfJobStatus(jobId);
      else pollJobStatus(jobId);
    } catch (err) {
      setProgress(null);
      showError('Không thể tiếp tục dịch', err.message);
    }
  }

  // Handler khi user muon dich lai 1 job tu dau (force=true)
  async function handleRetranslateJob(jobData) {
    stopPolling();
    setPausedJob(null);
    setTranslatedPdf(null);
    setProgress({ current: 0, total: 0, status: 'Đang dịch lại từ đầu...' });
    setActiveTab('compare');

    const isPdf = jobData.source_type === 'pdf' || jobData.source_type === 'pdf_only';

    if (isPdf) {
      setOriginalPdf(`${API_URL}/api/pdf-translate/${jobData.job_id}/original`);
    } else {
      setOriginalPdf(`${API_URL}/api/pdf/${jobData.job_id}/original`);
    }

    try {
      let data;
      if (isPdf) {
        data = await apiFetch(`${API_URL}/api/pdf-translate/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_id: jobData.job_id, force: true }),
        });
      } else {
        data = await apiFetch(`${API_URL}/api/translate/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_id: jobData.job_id, force: true }),
        });
      }
      const jobId = data.job_id || jobData.job_id;
      setCurrentJob({ jobId, type: isPdf ? 'pdf' : 'latex' });
      if (isPdf) pollPdfJobStatus(jobId);
      else pollJobStatus(jobId);
    } catch (err) {
      setProgress(null);
      showError('Không thể bắt đầu dịch lại', err.message);
    }
  }

  async function handleDownloadPdf(url, filename) {
    // Need to fetch with auth, then trigger download via blob — direct <a href>
    // wouldn't include the bearer token.
    if (url.startsWith('blob:') || url.startsWith('data:')) {
      const a = document.createElement('a');
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      return;
    }
    try {
      const res = await authFetch(url);
      if (!res.ok) { showError('Tải xuống thất bại', `HTTP ${res.status}`); return; }
      const blob = await res.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl; a.download = filename;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
    } catch (err) {
      showError('Tải xuống thất bại', err.message);
    }
  }

  async function handleCancelJob() {
    if (!currentJob) return;
    stopPolling();
    try {
      if (currentJob.type === 'pdf') {
        await authFetch(`${API_URL}/api/pdf-translate/${currentJob.jobId}/pause`, { method: 'POST' });
      } else if (currentJob.type === 'office') {
        await authFetch(`${API_URL}/api/office-translate/${currentJob.jobId}/cancel`, { method: 'POST' });
      } else {
        await authFetch(`${API_URL}/api/cancel?job_id=${currentJob.jobId}`, { method: 'POST' });
      }
    } catch {
      // ignore
    }
    setProgress({ current: 0, total: 0, status: 'Đã hủy bản dịch' });
    setCurrentJob(null);
  }

  async function handlePauseJob() {
    if (!currentJob) return;
    const jobForResume = {
      job_id: currentJob.jobId,
      source_type: currentJob.type === 'pdf' ? 'pdf_only' : currentJob.type,
    };
    try {
      if (currentJob.type === 'pdf') {
        await authFetch(`${API_URL}/api/pdf-translate/${currentJob.jobId}/pause`, { method: 'POST' });
        setProgress({
          current: progress?.current || 0,
          total: progress?.total || 0,
          status: 'Đang tạm dừng sau phần đã lưu...',
        });
        return;
      } else if (currentJob.type === 'office') {
        stopPolling();
        await authFetch(`${API_URL}/api/office-translate/${currentJob.jobId}/cancel`, { method: 'POST' });
      } else {
        stopPolling();
        await authFetch(`${API_URL}/api/cancel?job_id=${currentJob.jobId}`, { method: 'POST' });
      }
      setPausedJob(jobForResume);
      setProgress({ current: progress?.current || 0, total: progress?.total || 0, status: 'Đã tạm dừng. Bấm Resume để chạy tiếp từ phần đã lưu.' });
    } catch (err) {
      showError('Không thể tạm dừng job', err.message);
    } finally {
      if (currentJob?.type !== 'pdf') setCurrentJob(null);
    }
  }

  function handleResumePausedJob() {
    if (!pausedJob) return;
    handleResumeJob(pausedJob);
  }

  async function handleCompilePartial() {
    if (!currentJob || compiling) return;
    setCompiling(true);
    try {
      const res = await authFetch(`${API_URL}/api/pdf-translate/${currentJob.jobId}/compile-partial`, {
        method: 'POST',
      });
      if (!res.ok) {
        const data = await res.json();
        alert(data.detail || 'Compile failed');
        return;
      }
      const data = await res.json();
      if (data.translated_pdf_url) {
        setTranslatedPdf(`${API_URL}${data.translated_pdf_url}?t=${Date.now()}`);
      }
    } catch (err) {
      alert(`Compile error: ${err.message}`);
    } finally {
      setCompiling(false);
    }
  }

  async function handleApproveGlossary() {
    if (!currentJob || approvingGlossary) return;
    setApprovingGlossary(true);
    try {
      const res = await authFetch(
        `${API_URL}/api/pdf-translate/${currentJob.jobId}/approve-glossary`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        },
      );
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`HTTP ${res.status}: ${txt.slice(0, 200)}`);
      }
      const data = await res.json();
      if (data.status === 'awaiting_style_review') {
        setAwaitingGlossaryReview(false);
        setAwaitingStyleReview(true);
        if (data.style_anchor) {
          setStyleAnchorReview({
            en: data.style_anchor.en || '',
            vi: data.style_anchor.vi || '',
            source_model: data.style_anchor.source_model || '',
          });
        }
        setProgress({
          current: 0,
          total: progress?.total || 0,
          status: 'Mẫu văn phong đã sẵn sàng — duyệt bản dịch mẫu trước khi dịch toàn bộ tài liệu.',
          phase: 'style_anchor_review',
        });
        return;
      }
      // Optimistic UI flip — next poll will confirm via backend status.
      setAwaitingGlossaryReview(false);
      setProgress({ current: 0, total: 0, status: 'Đang khởi động pipeline...' });
    } catch (err) {
      showError('Không thể bắt đầu dịch', err.message || String(err));
    } finally {
      setApprovingGlossary(false);
    }
  }

  async function handleApproveStyleAnchor() {
    if (!currentJob || approvingStyleAnchor) return;
    setApprovingStyleAnchor(true);
    try {
      const res = await authFetch(
        `${API_URL}/api/pdf-translate/${currentJob.jobId}/approve-style-anchor`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            en: styleAnchorReview.en,
            vi: styleAnchorReview.vi,
          }),
        },
      );
      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`HTTP ${res.status}: ${txt.slice(0, 200)}`);
      }
      setAwaitingStyleReview(false);
      setProgress({ current: 0, total: 0, status: 'Đang bắt đầu dịch tài liệu...', phase: 'eval_loop' });
    } catch (err) {
      showError('Không thể duyệt văn phong', err.message || String(err));
    } finally {
      setApprovingStyleAnchor(false);
    }
  }

  const progressPercent = progress && progress.total > 0
    ? Math.round((progress.current / progress.total) * 100)
    : 0;
  const terminalProgress = progress?.status?.startsWith('Lỗi:')
    || progress?.status === 'Đã hủy bản dịch'
    || progress?.status === 'Đã được thay bằng lượt chạy mới';
  const progressStatusText = (progress?.status || '').toLowerCase();
  const pipelineIsRunning = !!currentJob
    && !!progress
    && !terminalProgress
    && !awaitingGlossaryReview
    && !awaitingStyleReview
    && !progressStatusText.includes('tạm dừng')
    && !progressStatusText.includes('chờ duyệt')
    && !progressStatusText.includes('sẵn sàng');
  const canPauseJob = !!currentJob && currentJob.type !== 'office';
  const canResumeJob = !!pausedJob && !currentJob;
  const canCancelJob = !!currentJob;
  const hasTranslationJob = !!(currentJob || completedJob || progress || originalPdf || translatedPdf || officeDownload);
  const warningItems = progress?.status?.startsWith('Lỗi:')
    ? [progress.status]
    : progress?.status === 'Đã hủy bản dịch'
      ? ['Job đã bị hủy trước khi hoàn tất.']
      : progress?.status === 'Đã được thay bằng lượt chạy mới'
        ? ['Job cũ đã dừng vì có lượt chạy mới hơn cho cùng tài liệu.']
      : progress?.status?.includes('Preview không khả dụng')
        ? [progress.status]
        : [];
  const rawJobTitle = currentJobTitle || currentJob?.jobId || completedJob?.jobId || 'Chưa chọn tài liệu';
  const compareJobBase = (rawJobTitle || 'document')
    .replace(/^pdf_/, '')
    .replace(/\.pdf$/i, '')
    .replace(/[\\/:*?"<>|\r\n\t]+/g, '_')
    .trim() || 'document';
  const jobStatusClass = progress
    ? (terminalProgress ? 'warning' : (awaitingGlossaryReview || awaitingStyleReview) ? 'warning' : 'running')
    : completedJob
      ? 'done'
      : hasTranslationJob
        ? 'ready'
        : 'idle';
  const jobStatusLabel = progress
    ? (terminalProgress ? progress.status : awaitingGlossaryReview ? 'Chờ duyệt glossary' : awaitingStyleReview ? 'Chờ duyệt văn phong' : 'Đang chạy')
    : completedJob
      ? 'Hoàn tất'
      : hasTranslationJob
        ? 'Đã tải tài liệu'
        : 'Chưa có job';
  const chunkLabel = progress && progress.total > 0
    ? `${progress.current}/${progress.total}`
    : completedJob
      ? 'Hoàn tất'
      : '0/0';
  const pipelineSteps = [
    { label: 'Trích xuất', icon: 'extract' },
    { label: 'Lập kế hoạch', icon: 'plan' },
    { label: 'Glossary', icon: 'glossary' },
    { label: 'Văn phong', icon: 'style' },
    { label: 'Dịch', icon: 'translate' },
    { label: 'Dựng lại', icon: 'rebuild' },
    { label: 'Kiểm tra', icon: 'check' },
    { label: 'Báo cáo', icon: 'report' },
  ];
  const rawActivePipelineStep = (() => {
    const phase = (progress?.phase || '').toLowerCase();
    const status = (progress?.status || '').toLowerCase();
    if (completedJob && !progress) return 7;
    if (phase === 'report' || phase === 'done') return 7;
    if (phase === 'proofread') return 6;
    if (phase === 'rebuild') return 5;
    if (phase === 'eval_loop') return 4;
    if (phase === 'style_anchor_review') return 3;
    if (phase === 'style_anchor') return 3;
    if (phase === 'glossary' || phase === 'glossary_review') return 2;
    if (phase === 'plan') return 1;
    if (phase === 'extract') return 0;
    if (terminalProgress || status.includes('tạm dừng')) return furthestPipelineStepRef.current || 1;
    if (awaitingGlossaryReview || status.includes('glossary')) return 2;
    if (status.includes('văn phong') || status.includes('style') || status.includes('anchor')) return 3;
    if (status.includes('proofread') || status.includes('kiểm tra')) return 6;
    if (status.includes('báo cáo') || status.includes('report')) return 7;
    if (status.includes('trích xuất') || status.includes('extract')) return 0;
    if (status.includes('lập kế hoạch') || status.includes('planning') || status.includes('chia chunk')) return 1;
    if (status.includes('khởi động') || status.includes('tải source') || status.includes('mở trình duyệt')) return furthestPipelineStepRef.current || 1;
    if (status.includes('compile') || status.includes('biên dịch') || status.includes('tạo pdf') || status.includes('render')) return 5;
    if (
      status.includes('dịch')
      || status.includes('eval-loop')
      || status.includes('translate')
      || status.includes('review/sửa')
    ) return 4;
    if (progress && progress.current > 0) return 4;
    if (hasTranslationJob) return 1;
    return 0;
  })();
  const visiblePipelineJobId = currentJob?.jobId || completedJob?.jobId || null;
  if (pipelineJobRef.current !== visiblePipelineJobId) {
    pipelineJobRef.current = visiblePipelineJobId;
    furthestPipelineStepRef.current = 0;
  }
  const activePipelineStep = progress && !terminalProgress
    ? Math.max(rawActivePipelineStep, furthestPipelineStepRef.current)
    : rawActivePipelineStep;
  if (progress && !terminalProgress) {
    furthestPipelineStepRef.current = activePipelineStep;
  }
  const pipelineStepMax = pipelineSteps.length - 1;
  const markerProgressPercent = Math.round((activePipelineStep / pipelineStepMax) * 100);
  const translationStartPercent = Math.round((4 / pipelineStepMax) * 100);
  const translationEndPercent = Math.round((5 / pipelineStepMax) * 100);
  const translationPhasePercent = progress && progress.total > 0
    ? translationStartPercent + Math.round(
        (Math.min(progress.current, progress.total) / progress.total)
        * (translationEndPercent - translationStartPercent)
      )
    : translationStartPercent;
  const progressBarPercent = completedJob
    ? 100
    : activePipelineStep === 4 && progress && !terminalProgress
      ? translationPhasePercent
      : progress && !terminalProgress
        ? markerProgressPercent
        : 0;
  const progressPercentLabel = completedJob ? '100%' : `${progressBarPercent}%`;

  // Login gate — placed AFTER all hooks (Rules of Hooks compliance).
  // Returning early before hooks above would cause "Rendered more hooks
  // than during the previous render" crash on first login.
  if (!token) return <LoginScreen onLogin={handleLogin} />;

  return (
    <div className="app">
      {errorToast && (
        <div className="error-toast" onClick={dismissError}>
          <div className="error-toast-title">⚠ {errorToast.title}</div>
          <div className="error-toast-detail">{errorToast.detail}</div>
          <button className="error-toast-close" onClick={dismissError}>✕</button>
        </div>
      )}
      <header className="app-header">
        <div className="header-top">
          <div className="brand-lockup">
            <span className="brand-mark" aria-hidden="true">文A</span>
            <h1>Web AI Translator</h1>
          </div>
          <div className="header-controls">
            <button className="theme-toggle" onClick={toggleTheme} title={theme === 'dark' ? 'Chế độ sáng' : 'Chế độ tối'}>
              {theme === 'dark' ? '\u2600' : '\u263E'}
            </button>
            {userInfo && (
              <span className="user-badge" title={userInfo.is_admin ? 'Quản trị viên' : 'Người dùng'}>
                {userInfo.is_admin ? '★ ' : ''}{userInfo.username}
              </span>
            )}
            <button className="logout-btn" onClick={handleLogout} title="Đăng xuất">
              Đăng xuất
            </button>
          </div>
        </div>
        <nav>
          <button
            className={activeTab === 'pdf-upload' ? 'active' : ''}
            onClick={() => setActiveTab('pdf-upload')}
            disabled={uploadLockedByActiveJob}
            title={uploadLockedByActiveJob ? 'Đang có job dịch, hãy dừng/hủy hoặc hoàn tất trước khi tải tài liệu mới.' : 'Tải tài liệu'}
          >
            Upload
          </button>
          <button className={activeTab === 'compare' ? 'active' : ''} onClick={() => setActiveTab('compare')}>
            Translation Jobs
          </button>
          <button className={activeTab === 'history' ? 'active' : ''} onClick={() => setActiveTab('history')}>
            History
          </button>
          <button className={activeTab === 'db' ? 'active' : ''} onClick={() => setActiveTab('db')}>
            Review
          </button>
          <button className={activeTab === 'guide' ? 'active' : ''} onClick={() => setActiveTab('guide')}>
            Settings
          </button>
        </nav>
      </header>

      {activeTab === 'history' && (
        <JobHistory onViewJob={handleViewJob} onResumeJob={handleResumeJob} onRetranslateJob={handleRetranslateJob} />
      )}


      {activeTab === 'pdf-upload' && (
        <PdfUploadPanel
          targetBrowser={targetBrowser}
          preferredBackend={aiBackend}
          onTargetBrowserChange={handleTargetBrowserSwitch}
          onJobStarted={handlePdfJobStarted}
          onViewExisting={(jobData) => {
            stopPolling();
            setProgress(null);
            setOfficeDownload(null);

            // Office: source pane stays empty (no PDF render of the .docx);
            // right pane shows the LibreOffice preview PDF; download button
            // targets the actual translated office file.
            if (jobData.source_type === 'office') {
              setOriginalPdf(null);
              setTranslatedPdf(jobData.preview_url ? `${API_URL}${jobData.preview_url}` : null);
              if (jobData.translated_url) {
                const ext = '.docx';
                setOfficeDownload({
                  url: `${API_URL}${jobData.translated_url}`,
                  filename: `translated_vi${ext}`,
                  kind: jobData.kind || 'docx',
                });
              }
              setCompletedJob(null);
              setShowQuality(false);
              setActiveTab('compare');
              return;
            }

            if (jobData.original_pdf_url) {
              setOriginalPdf(`${API_URL}${jobData.original_pdf_url}`);
            }
            if (jobData.translated_pdf_url) {
              setTranslatedPdf(`${API_URL}${jobData.translated_pdf_url}`);
            }
            setCompletedJob({ jobId: jobData.job_id, type: 'pdf' });
            setShowQuality(false);
            setActiveTab('compare');
          }}
        />
      )}

      {activeTab === 'compare' && (
        <div className="compare-view">
          <div className="translation-job-grid">
            <section className="job-progress-card">
              <div className="job-card-header">
                <div>
                  <h2>Tiến độ: <span>{rawJobTitle}</span></h2>
                  <div className="job-meta-row">
                    <span>Đoạn: <strong>{chunkLabel}</strong></span>
                    <span className="job-meta-dot" aria-hidden="true" />
                    <span>Loại: <strong>{currentJob?.type || completedJob?.type || (officeDownload ? 'office' : 'pdf')}</strong></span>
                    <span className="job-meta-dot" aria-hidden="true" />
                    <span>Đồng bộ: <strong>{syncScroll ? 'Bật' : 'Tắt'}</strong></span>
                  </div>
                </div>
                <div className="job-header-actions">
                  <button
                    className="job-action-btn"
                    onClick={handlePauseJob}
                    disabled={!canPauseJob}
                  >
                    <span aria-hidden="true">Ⅱ</span>
                    Pause
                  </button>
                  <button
                    className="job-action-btn primary"
                    onClick={handleResumePausedJob}
                    disabled={!canResumeJob}
                  >
                    <span aria-hidden="true">▶</span>
                    Resume
                  </button>
                  <button
                    className="job-action-btn danger"
                    onClick={handleCancelJob}
                    disabled={!canCancelJob}
                  >
                    <span aria-hidden="true">×</span>
                    Cancel
                  </button>
                  {currentJob && currentJob.type === 'pdf' && progress?.current > 0 && (
                    <button
                      className="btn-compile-partial"
                      onClick={handleCompilePartial}
                      disabled={compiling}
                    >
                      {compiling ? 'Đang compile...' : 'Compile bản tạm'}
                    </button>
                  )}
                </div>
              </div>

              <div className="job-progress-body">
                <div className="job-progress-meter" aria-hidden="true">
                  <div className="job-progress-track">
                    <div
                      className="job-progress-fill"
                      style={{ width: `${progressBarPercent}%` }}
                    />
                  </div>
                  <span>{progressPercentLabel}</span>
                </div>
                <div className="job-pipeline" aria-label="Các bước dịch">
                  <div className="job-pipeline-rail" aria-hidden="true" />
                  {pipelineSteps.map((step, idx) => {
                    const isDone = idx < activePipelineStep;
                    const isActive = idx === activePipelineStep;
                    return (
                    <div
                      key={step.label}
                      className={`job-step ${isDone ? 'done' : ''} ${isActive ? 'active' : ''} ${isActive && pipelineIsRunning ? 'running' : ''}`}
                    >
                      <span className="job-step-dot">
                        <PipelineIcon name={isDone ? 'done' : step.icon} />
                      </span>
                      <span>{step.label}</span>
                    </div>
                    );
                  })}
                </div>
                <div className="job-current-status">
                  {progress?.status || (completedJob ? 'Bản dịch đã sẵn sàng để kiểm tra.' : 'Chưa có job dịch đang mở.')}
                </div>
              </div>
            </section>

            <section className="job-summary-card">
              <div className="job-side-toolbar">
                <h2 className="job-side-title">
                  <span aria-hidden="true">▣</span>
                  {jobSideTab === 'summary' ? 'Tóm tắt job' : 'Glossary'}
                </h2>
                <div className="job-side-actions">
                  <button
                    className={`job-side-action subtle ${jobSideTab === 'glossary' ? 'active' : ''}`}
                    onClick={() => setJobSideTab(jobSideTab === 'summary' ? 'glossary' : 'summary')}
                  >
                    {jobSideTab === 'summary' ? 'Glossary' : 'Tóm tắt'}
                  </button>
                  <button
                    className="job-side-action"
                    disabled={!translatedPdf && !officeDownload}
                    onClick={() => {
                      if (officeDownload) handleDownloadPdf(officeDownload.url, officeDownload.filename);
                      else if (translatedPdf) handleDownloadPdf(translatedPdf, `${compareJobBase}_vi_translated.pdf`);
                    }}
                  >
                    ⇩ PDF
                  </button>
                  <button
                    className="job-side-action warning"
                    disabled={!completedJob}
                    onClick={() => {
                      setShowTriage(true);
                      setShowQuality(false);
                    }}
                  >
                    ⚒ Sửa lỗi
                  </button>
                </div>
              </div>

              {jobSideTab === 'summary' ? (
                <div className="job-side-content">
                  <div className="job-summary-grid">
                    <div>
                      <span>Trạng thái</span>
                      <strong className={`summary-status ${jobStatusClass}`}>
                        <i aria-hidden="true" />
                        {jobStatusLabel}
                      </strong>
                    </div>
                    <div>
                      <span>Điểm chất lượng</span>
                      <strong className="summary-score">—</strong>
                    </div>
                    <div>
                      <span>Đoạn đạt</span>
                      <strong>{chunkLabel}</strong>
                    </div>
                    <div>
                      <span>Cảnh báo</span>
                      <strong className={warningItems.length ? 'summary-warning-count' : ''}>{warningItems.length}</strong>
                    </div>
                  </div>
                  <div className="warning-list">
                    <span>Danh sách cảnh báo</span>
                    {warningItems.length ? (
                      warningItems.map((item) => (
                        <div className="warning-row" key={item}>
                          <span aria-hidden="true">⚠</span>
                          {item}
                        </div>
                      ))
                    ) : (
                      <div className="warning-row muted">Chưa có cảnh báo.</div>
                    )}
                  </div>
                </div>
              ) : (
                <div className="job-side-content glossary-side-content">
                  <div className="glossary-side-search">
                    <span aria-hidden="true">⌕</span>
                    <input placeholder="Tìm kiếm thuật ngữ..." readOnly />
                  </div>
                  <div className="glossary-side-chips">
                    <button className="active">TẤT CẢ ({glossaryPreview.count})</button>
                    <button>KỸ THUẬT</button>
                    <button>PHÁP LÝ</button>
                  </div>
                  <div className="glossary-side-table">
                    <table>
                      <thead>
                        <tr>
                          <th><input type="checkbox" readOnly /> Thuật ngữ</th>
                          <th>Bản dịch</th>
                          <th>Trạng thái</th>
                        </tr>
                      </thead>
                      <tbody>
                        {glossaryPreview.terms.length ? (
                          glossaryPreview.terms.map((term) => (
                            <tr key={term.en}>
                              <td><input type="checkbox" readOnly /> {term.en}</td>
                              <td>{term.vi}</td>
                              <td>
                                <span className={term.locked ? 'term-approved' : 'term-suggested'}>
                                  <i aria-hidden="true" />
                                  {term.locked ? 'Duyệt' : 'Đề xuất'}
                                </span>
                              </td>
                            </tr>
                          ))
                        ) : (
                          <tr>
                            <td colSpan="3" className="glossary-side-empty">
                              {glossaryPreview.loading
                                ? 'Đang tải glossary...'
                                : glossaryPreview.error
                                  ? 'Chưa tải được glossary'
                                  : 'Chưa có thuật ngữ'}
                            </td>
                          </tr>
                        )}
                      </tbody>
                    </table>
                  </div>
                  <div className="glossary-side-footer">
                    <button onClick={() => setShowGlossary(true)}>✓ Duyệt đã chọn</button>
                    <span>{glossaryPreview.count} thuật ngữ tìm thấy</span>
                    <button onClick={() => setShowGlossary(true)}>+ Thêm mới</button>
                  </div>
                </div>
              )}

            </section>
          </div>

          {/* HITL gate — pause for glossary review before bulk translation */}
          {awaitingGlossaryReview && currentJob && currentJob.type === 'pdf' && (
            <div className="glossary-review-gate">
              <div className="glossary-review-banner">
                <div className="glossary-review-text">
                  <strong>Bước 1/3 · Duyệt glossary</strong>
                  <p>
                    Hệ thống đã trích xuất thuật ngữ từ tài liệu. Hãy chỉnh sửa,
                    khóa các thuật ngữ quan trọng, rồi bấm <em>Bắt đầu dịch</em>
                    {' '}để tiếp tục.
                  </p>
                </div>
                <div className="glossary-review-actions">
                  <button
                    className="btn-primary btn-approve-glossary"
                    onClick={handleApproveGlossary}
                    disabled={approvingGlossary}
                  >
                    {approvingGlossary ? 'Đang khởi động...' : 'Bắt đầu dịch →'}
                  </button>
                  <button
                    className="btn-secondary"
                    onClick={handleCancelJob}
                    disabled={approvingGlossary}
                  >
                    Hủy
                  </button>
                </div>
              </div>
              <GlossaryEditor
                jobId={currentJob.jobId}
                jobType={currentJob.type}
                onError={showError}
                onClose={() => { /* no-op while gating — keep editor mounted */ }}
              />
            </div>
          )}
          {awaitingStyleReview && currentJob && currentJob.type === 'pdf' && (
            <div className="glossary-review-gate style-anchor-review-gate">
              <div className="glossary-review-banner">
                <div className="glossary-review-text">
                  <strong>Bước 2/3 · Duyệt văn phong</strong>
                  <p>
                    Hệ thống đã dịch một đoạn mẫu để làm chuẩn văn phong cho toàn bộ tài liệu.
                    Hãy chỉnh bản dịch mẫu nếu cần, rồi tiếp tục dịch.
                  </p>
                  {styleAnchorReview.source_model && (
                    <span className="style-anchor-model">
                      Model tạo mẫu: {styleAnchorReview.source_model}
                    </span>
                  )}
                </div>
                <div className="glossary-review-actions">
                  <button
                    className="btn-primary btn-approve-glossary"
                    onClick={handleApproveStyleAnchor}
                    disabled={approvingStyleAnchor || !styleAnchorReview.en.trim() || !styleAnchorReview.vi.trim()}
                  >
                    {approvingStyleAnchor ? 'Đang khởi động...' : 'Bắt đầu dịch →'}
                  </button>
                  <button
                    className="btn-secondary"
                    onClick={handleCancelJob}
                    disabled={approvingStyleAnchor}
                  >
                    Hủy
                  </button>
                </div>
              </div>
              <div className="style-anchor-editor">
                <label>
                  <span>Đoạn gốc</span>
                  <textarea
                    value={styleAnchorReview.en}
                    onChange={e => setStyleAnchorReview(v => ({ ...v, en: e.target.value }))}
                  />
                </label>
                <label>
                  <span>Bản dịch mẫu văn phong</span>
                  <textarea
                    value={styleAnchorReview.vi}
                    onChange={e => setStyleAnchorReview(v => ({ ...v, vi: e.target.value }))}
                  />
                </label>
              </div>
            </div>
          )}
          <section className="translation-verification">
            <div className="verification-toolbar">
              <div className="verification-title">
                <span className="verification-icon" aria-hidden="true">⇄</span>
                <h2>Kiểm tra bản dịch</h2>
              </div>
              <div className="verification-actions">
                <button
                  className={`sync-toggle-btn ${syncScroll ? 'active' : ''}`}
                  onClick={() => setSyncScroll(s => !s)}
                >
                  {syncScroll ? 'Đồng bộ cuộn: Bật' : 'Đồng bộ cuộn: Tắt'}
                </button>
              </div>
            </div>
            <div className="pdf-compare">
              <PdfViewer
                file={originalPdf}
                title="Bản gốc (English)"
                scrollRef={originalScrollRef}
                syncScroll={syncScroll}
                onSyncScroll={handleOriginalScroll}
                chunkBlockMap={completedJob?.type === 'pdf' ? chunkBlockMap : null}
                onBlockClick={(chunkIdx) => {
                  if (!completedJob) return;
                  setTriageTarget({ jobId: completedJob.jobId, chunkKey: String(chunkIdx) });
                  setActiveTab('db');
                }}
              />
              <div className="pdf-viewer-wrap">
              <PdfViewer
                file={translatedPdf}
                title={progress ? `Bản dịch (đang dịch... ${progressPercent}%)` : "Bản dịch (Tiếng Việt)"}
                placeholder={progress ? `Đang dịch bài báo...\n${progress.status}\nBản dịch sẽ hiển thị tại đây khi hoàn tất.` : undefined}
                scrollRef={translatedScrollRef}
                syncScroll={syncScroll}
                onSyncScroll={handleTranslatedScroll}
              />
              {currentJob && progress && (
                <button className="btn-stop-overlay" onClick={handleCancelJob} title="Dừng dịch">
                  ■ Dừng
                </button>
              )}
              </div>
            </div>
          </section>
          {completedJob && !progress && (
            <div className="quality-trigger-wrap">
              <div className="quality-action-row">
                {completedJob.type === 'pdf' && (
                  <button
                    className={`btn-quality-trigger ${showTriage ? 'btn-quality-active' : ''}`}
                    onClick={() => setShowTriage(v => !v)}
                  >
                    {showTriage ? 'Ẩn vấn đề cần xem' : 'Vấn đề cần xem lại'}
                  </button>
                )}
                {!showQuality ? (
                  <button className="btn-quality-trigger" onClick={() => setShowQuality(true)}>
                    Đánh giá chất lượng dịch
                  </button>
                ) : (
                  <button className="btn-quality-trigger btn-quality-active" onClick={() => setShowQuality(false)}>
                    Ẩn đánh giá chất lượng
                  </button>
                )}
                <button
                  className="btn-glossary-trigger"
                  onClick={() => setShowGlossary(v => !v)}
                >
                  {showGlossary ? 'Ẩn Glossary' : 'Sửa Glossary'}
                </button>
              </div>
              {completedJob.type === 'pdf' && showTriage && (
                <IssueTriagePanel
                  jobId={completedJob.jobId}
                  onJumpToChunk={(jid, chunkIdx) => {
                    setTriageTarget({ jobId: jid, chunkKey: String(chunkIdx) });
                    setActiveTab('db');
                  }}
                />
              )}
              {showGlossary && (
                <GlossaryEditor
                  jobId={completedJob.jobId}
                  jobType={completedJob.type}
                  onError={showError}
                  onClose={() => setShowGlossary(false)}
                />
              )}
              {showQuality && (
                <QualityPanel jobId={completedJob.jobId} jobType={completedJob.type} />
              )}
            </div>
          )}
        </div>
      )}

      {activeTab === 'db' && (
        <HistoryEditor
          target={triageTarget}
          onTargetConsumed={() => setTriageTarget(null)}
          onViewJob={(jobData) => {
            stopPolling();
            setProgress(null);
            if (jobData.original_pdf_url) setOriginalPdf(`${API_URL}${jobData.original_pdf_url}`);
            if (jobData.translated_pdf_url) setTranslatedPdf(`${API_URL}${jobData.translated_pdf_url}`);
            const jType = jobData.source_type === 'pdf' ? 'pdf' : 'latex';
            setCompletedJob({ jobId: jobData.job_id, type: jType });
            setShowQuality(false);
            setActiveTab('compare');
          }} />
      )}

      {activeTab === 'admin' && userInfo?.is_admin && (
        <SchedulerPanel onError={showError} />
      )}

      {activeTab === 'guide' && (
        <div className="guide-panel">
          <h2>Hướng dẫn sử dụng</h2>

          <section className="guide-section">
            <h3>1. Tìm kiếm & Dịch (arXiv)</h3>
            <p>Nhập từ khóa hoặc arXiv ID (vd: <code>2502.12525</code>) vào ô tìm kiếm. Chọn bài báo, bấm <strong>"Dịch sang tiếng Việt"</strong>. Hệ thống tải source LaTeX, dịch qua Gemini, và compile thành PDF tiếng Việt.</p>
          </section>

          <section className="guide-section">
            <h3>2. Dịch PDF (upload)</h3>
            <p>Vào tab <strong>"Dịch PDF"</strong>, kéo thả hoặc chọn file PDF. Chỉ hỗ trợ PDF digital (có text layer), không hỗ trợ scan/ảnh.</p>
            <ul>
              <li><strong>Tiêu chuẩn</strong>: Bài báo dưới 50 trang</li>
              <li><strong>Sách dài</strong>: Tài liệu 100+ trang — session rotation thường xuyên hơn, retry nhiều hơn</li>
            </ul>
          </section>

          <section className="guide-section">
            <h3>3. So sánh PDF</h3>
            <p>Tab <strong>"So sánh PDF"</strong> hiển thị bản gốc và bản dịch song song. Bật <strong>"Đồng bộ cuộn"</strong> để cuộn cùng lúc. Nút <strong>"⬇ Tải bản dịch"</strong> để lưu file về máy.</p>
          </section>

          <section className="guide-section">
            <h3>4. Lịch sử dịch</h3>
            <p>Xem tất cả các job đã dịch. Mỗi job có các nút:</p>
            <ul>
              <li><strong>Xem bản dịch</strong>: Mở trong So sánh PDF</li>
              <li><strong>⬇ Tải xuống</strong>: Lưu PDF dịch về máy trực tiếp</li>
              <li><strong>Tiếp tục dịch</strong>: Resume job bị hủy/lỗi giữa chừng</li>
              <li><strong>Dịch lại từ đầu</strong>: Xóa bản cũ và dịch lại</li>
            </ul>
          </section>

          <section className="guide-section">
            <h3>5. Đánh giá chất lượng dịch</h3>
            <p>Sau khi dịch xong, bấm <strong>"Đánh giá chất lượng dịch"</strong> trong tab So sánh PDF. Có 3 tab:</p>
            <ul>
              <li><strong>Vấn đề heuristic</strong>: Phát hiện block chưa dịch, tỉ lệ dài bất thường, số liệu mất</li>
              <li><strong>ChrF++</strong>: Đánh giá character n-gram (Popović 2015) — phù hợp nhất cho tiếng Việt (ngôn ngữ đơn lập). Cần bản dịch tham chiếu. Điểm 60+ là tốt.</li>
              <li><strong>LLM Judge</strong>: Đánh giá MQM qua Ollama (local LLM). Cần cài Ollama + pull model: <code>ollama pull qwen2.5:7b</code></li>
              <li><strong>Chẩn đoán</strong>: Phát hiện nguyên nhân chất lượng thấp (truncated response, session limit, math contamination...)</li>
            </ul>
          </section>

          <section className="guide-section">
            <h3>6. Glossary thuật ngữ</h3>
            <p>Hệ thống tự động xây dựng glossary 3 tầng cho mỗi tài liệu:</p>
            <ul>
              <li><strong>Seed</strong>: 315 thuật ngữ Toán/CS/AI tích hợp sẵn</li>
              <li><strong>Tài liệu</strong>: Trích xuất từ abstract/introduction qua Gemini</li>
              <li><strong>Khám phá</strong>: Phát hiện thêm sau mỗi 5 chunk dịch</li>
            </ul>
            <p>Sau khi dịch xong, bấm <strong>"Xem Glossary"</strong> để xem danh sách thuật ngữ đã sử dụng.</p>
          </section>

          <section className="guide-section">
            <h3>7. Yêu cầu hệ thống</h3>
            <ul>
              <li>Python 3.12+, Node.js 18+</li>
              <li>Chromium: <code>playwright install chromium</code></li>
              <li>MiKTeX (cho luồng LaTeX/arXiv)</li>
              <li>Ollama (tùy chọn, cho LLM Judge): <a href="https://ollama.com" target="_blank" rel="noopener noreferrer">ollama.com</a></li>
              <li>Đã đăng nhập backend web AI muốn dùng trên Chromium trước khi dịch</li>
            </ul>
          </section>
        </div>
      )}
      <footer className="app-footer">
        <span>v2.4.0-stable | Local-first Processing Active</span>
        <span>Provider Health: Online</span>
        <span>System Status</span>
        <span>API Docs</span>
      </footer>
    </div>
  );
}

export default App;
