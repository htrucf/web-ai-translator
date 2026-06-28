import { useEffect, useState, useRef } from 'react';

import API_URL, { apiFetch } from '../api.js';

// File extensions → kind mapping (giữ đồng bộ với backend _detect_upload_kind)
const SUPPORTED_EXTS = ['.pdf', '.tex', '.tar.gz', '.tgz', '.zip', '.txt', '.md', '.markdown', '.html', '.htm', '.docx'];
const ACCEPT_STR = SUPPORTED_EXTS.join(',');
const TRANSLATOR_MODELS = ['gemini', 'chatgpt', 'aistudio', 'deepseek', 'grok', 'copilot'];
const MODEL_PREF_STORAGE_KEY = 'wat_model_preference';
const MODEL_PREF_BACKEND_KEY = 'wat_model_preference_backend';

function normalizeModelPreference(value) {
  const raw = Array.isArray(value) ? value : [];
  const out = [];
  raw.forEach((item) => {
    const model = String(item || '').trim().toLowerCase();
    if (TRANSLATOR_MODELS.includes(model) && !out.includes(model)) {
      out.push(model);
    }
  });
  TRANSLATOR_MODELS.forEach((model) => {
    if (!out.includes(model)) out.push(model);
  });
  return out;
}

function readStoredModelPreference() {
  try {
    const raw = localStorage.getItem(MODEL_PREF_STORAGE_KEY);
    return normalizeModelPreference(raw ? JSON.parse(raw) : TRANSLATOR_MODELS);
  } catch {
    return TRANSLATOR_MODELS;
  }
}

function promotePrimaryModel(models, primary) {
  const model = String(primary || '').trim().toLowerCase();
  const normalized = normalizeModelPreference(models);
  if (!TRANSLATOR_MODELS.includes(model)) return normalized;
  return [model, ...normalized.filter((item) => item !== model)];
}

function modelLabel(model) {
  return {
    gemini: 'Gemini',
    chatgpt: 'ChatGPT',
    aistudio: 'AI Studio',
    deepseek: 'DeepSeek',
    grok: 'Grok',
    copilot: 'Copilot',
  }[model] || model;
}

function detectKind(filename) {
  const lower = (filename || '').toLowerCase();
  if (lower.endsWith('.pdf')) return 'pdf';
  if (lower.endsWith('.docx')) return 'docx';
  if (lower.endsWith('.tex') || lower.endsWith('.tar.gz') || lower.endsWith('.tgz') || lower.endsWith('.zip')) return 'latex';
  if (lower.endsWith('.md') || lower.endsWith('.markdown')) return 'markdown';
  if (lower.endsWith('.txt')) return 'text';
  if (lower.endsWith('.html') || lower.endsWith('.htm')) return 'html';
  return null;
}

function kindLabel(kind) {
  return {
    pdf: 'PDF',
    latex: 'LaTeX',
    text: 'Text',
    markdown: 'Markdown',
    html: 'HTML',
    docx: 'DOCX',
  }[kind] || 'FILE';
}

export default function PdfUploadPanel({
  onJobStarted,
  onViewExisting,
  targetBrowser = 'chrome',
  preferredBackend = '',
  onTargetBrowserChange,
}) {
  const [file, setFile] = useState(null);
  const [fileKind, setFileKind] = useState(null); // 'pdf' | 'latex' | 'text' | 'html'
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState('');
  const [dragOver, setDragOver] = useState(false);
  const [existingDialog, setExistingDialog] = useState(null);
  const [mode, setMode] = useState('standard');
  const [numTabs, setNumTabs] = useState(3);  // số tab/luồng dịch song song (benchmark)
  const [modelPreference, setModelPreference] = useState(readStoredModelPreference);
  const [judgeBackend, setJudgeBackend] = useState('web');
  const [showTabDialog, setShowTabDialog] = useState(false);
  const [pendingAction, setPendingAction] = useState(null); // {type:'upload'} | {type:'retranslate', dialog}
  const [draggedModel, setDraggedModel] = useState(null);
  const fileInputRef = useRef(null);
  // CometKiwi-XL (judge QE): modal hỏi tải + tiến độ; disable nút nếu user từ chối.
  const [qeModal, setQeModal] = useState(null);   // null | {state:'ask'|'downloading'|'error', percent, message, sizeGb}
  const [qeDisabled, setQeDisabled] = useState(false);
  const qePollRef = useRef(null);

  useEffect(() => () => { if (qePollRef.current) clearInterval(qePollRef.current); }, []);

  useEffect(() => {
    try {
      localStorage.setItem(MODEL_PREF_STORAGE_KEY, JSON.stringify(modelPreference));
    } catch {
      // ignore storage failures
    }
  }, [modelPreference]);

  useEffect(() => {
    const primary = String(preferredBackend || '').trim().toLowerCase();
    if (!TRANSLATOR_MODELS.includes(primary)) return;
    let lastApplied = '';
    try {
      lastApplied = localStorage.getItem(MODEL_PREF_BACKEND_KEY) || '';
    } catch {
      // ignore storage failures
    }
    if (lastApplied === primary) return;
    setModelPreference((prev) => promotePrimaryModel(prev, primary));
    try {
      localStorage.setItem(MODEL_PREF_BACKEND_KEY, primary);
    } catch {
      // ignore storage failures
    }
  }, [preferredBackend]);

  function acceptFile(picked) {
    if (!picked) return;
    const kind = detectKind(picked.name);
    if (!kind) {
      setError('Định dạng không hỗ trợ. Chấp nhận: PDF, .tex, .tar.gz, .zip, .txt, .md, .html, .docx');
      return;
    }
    setFile(picked);
    setFileKind(kind);
    setError('');
    if (picked.size > 50 * 1024 * 1024) {
      setError('File lớn hơn 50MB — quá trình dịch có thể chậm. Cân nhắc dùng chế độ "Sách dài".');
    }
  }

  function handleFileSelect(e) {
    acceptFile(e.target.files[0]);
  }

  function handleDrop(e) {
    e.preventDefault();
    setDragOver(false);
    acceptFile(e.dataTransfer.files[0]);
  }

  function handleDragOver(e) {
    e.preventDefault();
    setDragOver(true);
  }

  function handleDragLeave() {
    setDragOver(false);
  }

  async function handleUpload(opts = {}) {
    if (!file) return;
    const agentic = !!(opts && opts.agentic);

    setUploading(true);
    setError('');

    try {
      const formData = new FormData();
      formData.append('file', file);
      // PDF route uses `mode`; LaTeX/text/html routes ignore it harmlessly
      formData.append('mode', mode);
      formData.append('num_tabs', String(numTabs));
      formData.append('models', JSON.stringify(modelPreference));
      formData.append('judge_backend', judgeBackend);
      if (agentic) formData.append('agentic', 'true');

      // Unified dispatcher — auto-routes based on file extension server-side
      const res = await apiFetch(`${API_URL}/api/documents/upload`, {
        method: 'POST',
        body: formData,
      });

      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || `Upload failed (${res.status})`);
      }

      const data = await res.json();
      const kind = data.kind || fileKind;

      // Dialog flow: PDF route returns this. LaTeX/text/html routes also return it.
      if (data.status === 'already_done') {
        setExistingDialog({ ...data, kind });
        return;
      }

      setFile(null);
      setFileKind(null);
      if (fileInputRef.current) fileInputRef.current.value = '';

      // source_type buckets which polling endpoints the App uses.
      //   pdf_only → /api/pdf-translate/*       (pdf)
      //   office   → /api/office-translate/*    (docx)
      //   latex    → /api/job/*                  (latex/text/html via LaTeX flow)
      let sourceType;
      if (kind === 'pdf') sourceType = 'pdf_only';
      else if (kind === 'docx') sourceType = 'office';
      else sourceType = 'latex';

      onJobStarted({
        job_id: data.job_id,
        original_pdf_url: data.original_pdf_url || null,
        pages: data.pages,
        source_type: sourceType,
        kind,
        title: data.title || file.name,
        original_filename: data.original_filename || file.name,
      });
    } catch (err) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  }

  // ── CometKiwi-XL judge: chọn nút → kiểm tra đã tải chưa, chưa thì hỏi tải ──
  async function handleSelectJudge(value) {
    if (value !== 'cometkiwi') { setJudgeBackend(value); return; }
    try {
      const res = await apiFetch(`${API_URL}/api/quality/qe-status?backend=cometkiwi`);
      const s = await res.json();
      if (s.ready) { setJudgeBackend('cometkiwi'); return; }
      setQeModal({ state: 'ask', percent: 0, message: '', sizeGb: s.approx_size_gb || 13.9 });
    } catch (e) {
      setError('Không kiểm tra được trạng thái CometKiwi: ' + e.message);
    }
  }

  async function qeStartDownload() {
    if (qePollRef.current) clearInterval(qePollRef.current);
    setQeModal({ state: 'downloading', percent: 0, message: 'Bắt đầu tải…' });
    try {
      await apiFetch(`${API_URL}/api/quality/qe-download?backend=cometkiwi`, { method: 'POST' });
    } catch (e) {
      setQeModal({ state: 'error', percent: 0, message: 'Lỗi khởi động tải: ' + e.message });
      return;
    }
    qePollRef.current = setInterval(async () => {
      try {
        const res = await apiFetch(`${API_URL}/api/quality/qe-download-status?backend=cometkiwi`);
        const st = await res.json();
        if (st.state === 'done') {
          clearInterval(qePollRef.current); qePollRef.current = null;
          setQeModal(null); setQeDisabled(false); setJudgeBackend('cometkiwi');
        } else if (st.state === 'error') {
          clearInterval(qePollRef.current); qePollRef.current = null;
          setQeModal({ state: 'error', percent: st.percent || 0, message: st.message || 'Lỗi tải' });
        } else {
          setQeModal({ state: 'downloading', percent: st.percent || 0, message: st.message || 'Đang tải…' });
        }
      } catch { /* giữ poll, mạng chập chờn */ }
    }, 1500);
  }

  function qeDeclineDownload() {
    if (qePollRef.current) { clearInterval(qePollRef.current); qePollRef.current = null; }
    setQeModal(null);
    setQeDisabled(true);                       // nút CometKiwi không được chọn nữa
    if (judgeBackend === 'cometkiwi') setJudgeBackend('off');
  }

  // Khi user bấm "Dịch": với PDF thì hỏi số tab song song trước (dialog),
  // các định dạng khác không có multi-tab nên dịch thẳng.
  function handleTranslateClick() {
    if (!file) return;
    // Confirm lại lần nữa khi đã chọn CometKiwi để chấm chất lượng.
    if (judgeBackend === 'cometkiwi' &&
        !window.confirm('Dùng CometKiwi để chấm chất lượng cho lần dịch này?')) {
      return;
    }
    if (fileKind === 'pdf') {
      setPendingAction({ type: 'upload' });
      setShowTabDialog(true);
    } else {
      handleUpload();
    }
  }

  async function doPdfRetranslate(dialog) {
    try {
      const res = await apiFetch(`${API_URL}/api/pdf-translate/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          job_id: dialog.job_id, force: true, mode,
          num_tabs: numTabs, agentic: true, models: modelPreference, judge_backend: judgeBackend,
        }),
      });
      const data = await res.json();
      onJobStarted({
        job_id: data.job_id,
        original_pdf_url: `/api/pdf-translate/${data.job_id}/original`,
        source_type: 'pdf_only',
        kind: 'pdf',
        title: dialog.title || dialog.original_filename || data.job_id,
        original_filename: dialog.original_filename || dialog.filename || null,
      });
    } catch (err) {
      setError(err.message);
    }
  }

  // User xác nhận số tab trong dialog → chạy pipeline đa tác tử với numTabs đã chọn.
  function handleConfirmTranslate() {
    setShowTabDialog(false);
    const action = pendingAction;
    setPendingAction(null);
    if (action?.type === 'retranslate') {
      doPdfRetranslate(action.dialog);
    } else {
      handleUpload({ agentic: true });
    }
  }

  function moveModelBefore(fromModel, toModel) {
    if (!fromModel || !toModel || fromModel === toModel) return;
    setModelPreference((prev) => {
      const fromIdx = prev.indexOf(fromModel);
      const toIdx = prev.indexOf(toModel);
      if (fromIdx < 0 || toIdx < 0) return prev;
      const next = [...prev];
      const [picked] = next.splice(fromIdx, 1);
      next.splice(toIdx, 0, picked);
      return next;
    });
  }

  function handleProviderDragStart(e, model) {
    setDraggedModel(model);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', model);
  }

  function handleProviderDrop(e, targetModel) {
    e.preventDefault();
    const sourceModel = e.dataTransfer.getData('text/plain') || draggedModel;
    moveModelBefore(sourceModel, targetModel);
    setDraggedModel(null);
  }

  function handleOpenExisting() {
    if (!existingDialog) return;
    const kind = existingDialog.kind || 'pdf';
    setExistingDialog(null);
    setFile(null);
    setFileKind(null);
    if (fileInputRef.current) fileInputRef.current.value = '';

    let sourceType;
    if (kind === 'pdf') sourceType = 'pdf_only';
    else if (kind === 'docx') sourceType = 'office';
    else sourceType = 'latex';

    if (onViewExisting) {
      onViewExisting({
        job_id: existingDialog.job_id,
        original_pdf_url: existingDialog.original_pdf_url,
        translated_pdf_url: existingDialog.translated_pdf_url,
        preview_url: existingDialog.preview_url,
        original_url: existingDialog.original_url,
        translated_url: existingDialog.translated_url,
        source_type: sourceType,
        kind,
      });
    }
  }

  async function handleForceRetranslate() {
    if (!existingDialog) return;
    const kind = existingDialog.kind || 'pdf';
    const savedFile = file;
    setExistingDialog(null);
    setFile(null);
    setFileKind(null);
    if (fileInputRef.current) fileInputRef.current.value = '';

    try {
      // PDF and office jobs both expose dedicated /start endpoints keyed by
      // job_id; LaTeX/text/html jobs don't, so they re-upload with force=true.
      if (kind === 'pdf') {
        const res = await apiFetch(`${API_URL}/api/pdf-translate/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            job_id: existingDialog.job_id,
            force: true,
            mode,
            num_tabs: numTabs,
            agentic: true,
            models: modelPreference,
            judge_backend: judgeBackend,
          }),
        });
        const data = await res.json();
        onJobStarted({
          job_id: data.job_id,
          original_pdf_url: `/api/pdf-translate/${data.job_id}/original`,
          source_type: 'pdf_only',
          kind: 'pdf',
          title: existingDialog.title || existingDialog.original_filename || data.job_id,
          original_filename: existingDialog.original_filename || existingDialog.filename || null,
        });
      } else if (kind === 'docx') {
        const res = await apiFetch(`${API_URL}/api/office-translate/start`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ job_id: existingDialog.job_id, force: true }),
        });
        const data = await res.json();
        onJobStarted({
          job_id: data.job_id,
          original_url: `/api/office-translate/${data.job_id}/original`,
          source_type: 'office',
          kind: data.kind || kind,
          title: existingDialog.title || existingDialog.original_filename || data.job_id,
          original_filename: existingDialog.original_filename || existingDialog.filename || null,
        });
      } else {
        if (!savedFile) {
          setError('Cần chọn lại file để dịch lại.');
          return;
        }
        const formData = new FormData();
        formData.append('file', savedFile);
        formData.append('force', 'true');
        const res = await apiFetch(`${API_URL}/api/documents/upload`, {
          method: 'POST',
          body: formData,
        });
        const data = await res.json();
        onJobStarted({
          job_id: data.job_id,
          original_pdf_url: data.original_pdf_url || null,
          source_type: 'latex',
          kind: data.kind || kind,
          title: data.title || savedFile.name,
          original_filename: data.original_filename || savedFile.name,
        });
      }
    } catch (err) {
      setError(err.message);
    }
  }

  return (
    <div className="pdf-upload-panel">
      <div className="upload-dashboard-grid">
        <section className="upload-module upload-source-module">
          <h2>Tài liệu nguồn</h2>
          <p className="pdf-upload-desc">
            Tải lên tài liệu để dịch sang tiếng Việt. Hỗ trợ PDF, DOCX, LaTeX, văn bản và HTML.
          </p>

          <div
            className={`pdf-drop-zone ${dragOver ? 'drag-over' : ''} ${file ? 'has-file' : ''}`}
            onDrop={handleDrop}
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onClick={() => fileInputRef.current?.click()}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPT_STR}
              onChange={handleFileSelect}
              style={{ display: 'none' }}
            />
            {file ? (
              <div className="pdf-file-info">
                <span className="pdf-file-icon">{kindLabel(fileKind)}</span>
                <div>
                  <div className="pdf-file-name">{file.name}</div>
                  <div className="pdf-file-size">Loại: {kindLabel(fileKind)} · {(file.size / 1024 / 1024).toFixed(2)} MB</div>
                </div>
                <span className="pdf-file-valid">Đã kiểm tra</span>
                <button
                  type="button"
                  className="pdf-file-remove"
                  onClick={(e) => {
                    e.stopPropagation();
                    setFile(null);
                    setFileKind(null);
                    setError('');
                    if (fileInputRef.current) fileInputRef.current.value = '';
                  }}
                  disabled={uploading}
                  title="Xóa file để tải lại"
                >
                  Xóa
                </button>
              </div>
            ) : (
              <div className="pdf-drop-text">
                <span className="pdf-drop-icon">⇧</span>
                <span>Kéo thả file vào đây hoặc bấm để chọn</span>
                <span className="pdf-drop-hint">
                  PDF / DOCX / LaTeX / Markdown / HTML / TXT
                </span>
              </div>
            )}
          </div>

          {error && <div className="pdf-upload-error">{error}</div>}
        </section>

        <section className="upload-module upload-config-module">
          <h2>Cấu hình dịch</h2>

          <div className="pdf-mode-toggle">
            <label className="mode-label">Chế độ xử lý</label>
            <div className="mode-options">
              <button
                className={`mode-btn ${mode === 'standard' ? 'active' : ''}`}
                onClick={() => setMode('standard')}
              >
                Tiêu chuẩn
              </button>
              <button
                className={`mode-btn ${mode === 'book' ? 'active' : ''}`}
                onClick={() => setMode('book')}
              >
                Sách dài
              </button>
            </div>
            {mode === 'book' && (
              <p className="mode-hint">
                Phù hợp tài liệu dài: xoay phiên làm việc thường xuyên hơn, thêm thời gian chờ và retry nhiều lần hơn.
              </p>
            )}
          </div>

          <div className="parallel-control">
            <div className="parallel-control-head">
              <label className="mode-label">Số tab trình duyệt song song</label>
              <span className="parallel-value">{numTabs}</span>
            </div>
            <input
              type="range"
              min="1"
              max="4"
              value={numTabs}
              onChange={(e) => setNumTabs(Number(e.target.value))}
            />
            <div className="parallel-scale">
              <span>1 · chậm, ổn định</span>
              <span>4 · nhanh, dễ bị giới hạn</span>
            </div>
          </div>

          <div className="target-browser-control">
            <label className="mode-label" htmlFor="target-browser">Trình duyệt đích</label>
            <select
              id="target-browser"
              value={targetBrowser}
              onChange={(e) => onTargetBrowserChange?.(e.target.value)}
            >
              <option value="chrome">Chrome</option>
              <option value="chromium">Chromium</option>
            </select>
          </div>

          <div className="provider-preview">
            <span className="mode-label">Ưu tiên model</span>
            <div className="provider-chip-row">
              {modelPreference.map((model, idx) => (
                <span className={`provider-chip ${idx === 0 ? 'primary' : ''}`} key={model}>
                  {String(idx + 1).padStart(2, '0')} · {modelLabel(model)}
                </span>
              ))}
            </div>
          </div>
        </section>
      </div>

      <section className="upload-module model-health-module">
        <div className="model-health-head">
          <div>
            <h2>Chiến lược model & tình trạng nhà cung cấp</h2>
            <p>Kéo thả từng dòng để đổi thứ tự ưu tiên; hệ thống sẽ thử từ model số 01 rồi fallback lần lượt khi lỗi hoặc bị giới hạn.</p>
          </div>
          <div className="judge-toggle-preview" aria-label="Judge backend">
            {[
              ['web', 'Web AI'],
              ['cometkiwi', 'COMETKiwi'],
              ['off', 'Tắt'],
            ].map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={judgeBackend === value ? 'active' : ''}
                disabled={value === 'cometkiwi' && qeDisabled}
                title={value === 'cometkiwi' && qeDisabled ? 'Bạn đã từ chối tải model CometKiwi' : ''}
                onClick={() => handleSelectJudge(value)}
              >
                {label}
              </button>
            ))}
          </div>
        </div>
        <div className="provider-health-list">
          {modelPreference.map((model, idx) => {
            const warning = model === 'chatgpt';
            const degraded = model === 'deepseek';
            return (
              <div
                className={`provider-row active ${idx === 0 ? 'primary' : ''} ${draggedModel === model ? 'dragging' : ''} ${warning ? 'warning' : ''} ${degraded ? 'degraded' : ''}`}
                key={model}
                draggable
                onDragStart={(e) => handleProviderDragStart(e, model)}
                onDragOver={(e) => e.preventDefault()}
                onDragEnter={(e) => e.currentTarget.classList.add('drag-over')}
                onDragLeave={(e) => e.currentTarget.classList.remove('drag-over')}
                onDrop={(e) => {
                  e.currentTarget.classList.remove('drag-over');
                  handleProviderDrop(e, model);
                }}
                onDragEnd={() => setDraggedModel(null)}
              >
                <span className="provider-grip">⋮⋮</span>
                <span className="provider-order">{String(idx + 1).padStart(2, '0')}</span>
                <strong>{modelLabel(model)}</strong>
                <span className="provider-status">
                  {degraded ? 'Suy giảm' : warning ? 'Dễ bị giới hạn' : 'Sẵn sàng'}
                </span>
              </div>
            );
          })}
        </div>
      </section>

      <div className="upload-actions">
        <button
          type="button"
          className="upload-cancel-btn"
          onClick={() => {
            setFile(null);
            setFileKind(null);
            setError('');
            if (fileInputRef.current) fileInputRef.current.value = '';
          }}
          disabled={!file || uploading}
        >
          Hủy
        </button>
        <button
          className="pdf-upload-btn"
          onClick={handleTranslateClick}
          disabled={!file || uploading}
        >
          {uploading ? 'Đang tải lên...' : 'Bắt đầu dịch tài liệu'}
        </button>
      </div>

      {/* Dialog: document already translated */}
      {existingDialog && (
        <div className="confirm-overlay">
          <div className="confirm-dialog">
            <h3>Tài liệu đã được dịch</h3>
            <p>
              <strong>{existingDialog.title || existingDialog.job_id}</strong>
              {existingDialog.pages ? ` (${existingDialog.pages} trang)` : ''} đã có bản dịch sẵn.
              Bạn muốn làm gì?
            </p>
            <div className="confirm-actions">
              <button className="btn-primary" onClick={handleOpenExisting}>
                Mở bản dịch có sẵn
              </button>
              <button className="btn-warning" onClick={() => {
                const k = existingDialog.kind || 'pdf';
                if (k === 'pdf') {
                  const data = existingDialog;
                  setExistingDialog(null);
                  setPendingAction({ type: 'retranslate', dialog: data });
                  setShowTabDialog(true);   // hỏi số tab trước khi dịch lại
                } else {
                  handleForceRetranslate();
                }
              }}>
                Dịch lại từ đầu
              </button>
              <button className="btn-secondary" onClick={() => setExistingDialog(null)}>
                Hủy
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Dialog: hỏi số tab song song trước khi dịch */}
      {showTabDialog && (
        <div className="confirm-overlay">
          <div className="confirm-dialog compact-confirm-dialog">
            <h3>Xác nhận bắt đầu dịch</h3>
            <p>
              Dùng <strong>{numTabs} tab</strong>, ưu tiên <strong>{modelLabel(modelPreference[0])}</strong>.
              Cấu hình chi tiết đã lấy từ màn Upload.
            </p>
            {numTabs >= 4 && (
              <p className="compact-confirm-warning">
                4 tab nhanh hơn nhưng dễ bị giới hạn tần suất. Nếu muốn ổn định hơn, quay lại Upload và chọn 3 tab.
              </p>
            )}
            <div className="confirm-actions">
              <button className="btn-primary" onClick={handleConfirmTranslate}>
                Bắt đầu dịch
              </button>
              <button className="btn-secondary" onClick={() => { setShowTabDialog(false); setPendingAction(null); }}>
                Hủy
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Dialog: tải model CometKiwi (judge QE) + thanh tiến độ */}
      {qeModal && (
        <div className="confirm-overlay">
          <div className="confirm-dialog compact-confirm-dialog">
            {qeModal.state === 'ask' && (
              <>
                <h3>Tải mô hình chấm chất lượng?</h3>
                <p>Bạn chưa tải <strong>CometKiwi</strong> (~{qeModal.sizeGb} GB). Tải xuống để bật chấm điểm MQM?</p>
                <p className="compact-confirm-warning">Tải 1 lần; có thể mất nhiều phút tuỳ mạng. Máy chỉ có CPU nên khi chấm sẽ chậm.</p>
                <div className="confirm-actions">
                  <button className="btn-primary" onClick={qeStartDownload}>Tải xuống</button>
                  <button className="btn-secondary" onClick={qeDeclineDownload}>Không</button>
                </div>
              </>
            )}
            {qeModal.state === 'downloading' && (
              <>
                <h3>Đang tải CometKiwi…</h3>
                <p>Vui lòng chờ, đừng đóng trình duyệt.</p>
                <div style={{ height: 10, background: '#e5e7eb', borderRadius: 5, overflow: 'hidden', margin: '12px 0' }}>
                  <div style={{ height: '100%', width: `${qeModal.percent}%`, background: '#2563eb', transition: 'width .3s' }} />
                </div>
                <p className="qe-progress-label">{qeModal.percent}% · {qeModal.message}</p>
              </>
            )}
            {qeModal.state === 'error' && (
              <>
                <h3>Tải thất bại</h3>
                <p className="compact-confirm-warning">{qeModal.message}</p>
                <div className="confirm-actions">
                  <button className="btn-primary" onClick={qeStartDownload}>Thử lại</button>
                  <button className="btn-secondary" onClick={() => { setQeModal(null); setQeDisabled(true); }}>Đóng</button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
