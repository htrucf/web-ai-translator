import { useState, useEffect, useCallback } from 'react';

import API_URL, { apiFetch } from '../api.js';

export default function HistoryEditor({ onViewJob, target, onTargetConsumed }) {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedJob, setSelectedJob] = useState(null);  // job metadata
  const [chunks, setChunks] = useState([]);
  const [chunksLoading, setChunksLoading] = useState(false);
  const [editingChunk, setEditingChunk] = useState(null); // { chunk_key, mt_latex }
  const [editText, setEditText] = useState('');
  const [editNote, setEditNote] = useState('');
  const [saving, setSaving] = useState(false);
  const [hintingChunk, setHintingChunk] = useState(null); // chunk_key being hint-refined
  const [hintText, setHintText] = useState('');
  const [hintRunning, setHintRunning] = useState(false);
  const [hintError, setHintError] = useState(null);
  const [recompiling, setRecompiling] = useState(false);
  const [recompileMsg, setRecompileMsg] = useState('');
  const [search, setSearch] = useState('');
  const [filter, setFilter] = useState('all'); // 'all' | 'done' | 'error' | 'edited' | 'low'
  const [jobNotes, setJobNotes] = useState('');
  const [editingNotes, setEditingNotes] = useState(false);
  const [highlightedKey, setHighlightedKey] = useState(null); // brief flash on deep-link arrival

  // Load jobs list
  const loadJobs = useCallback(async () => {
    try {
      const r = await apiFetch(`${API_URL}/api/history`);
      const d = await r.json();
      setJobs(d.jobs || []);
    } catch {
      setJobs([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadJobs(); }, [loadJobs]);

  // Deep-link handler: when a target arrives (e.g. from IssueTriagePanel),
  // auto-select the matching job. Scrolling to the chunk happens after chunks
  // load — see the second effect below.
  useEffect(() => {
    if (!target?.jobId || !jobs.length) return;
    if (selectedJob?.job_id === target.jobId) return; // already on it
    const match = jobs.find(j => j.job_id === target.jobId);
    if (match) selectJob(match);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, jobs]);

  // After chunks load, scroll to the targeted chunk and flash-highlight it.
  useEffect(() => {
    if (!target?.chunkKey || chunksLoading || chunks.length === 0) return;
    if (selectedJob?.job_id !== target.jobId) return;
    const el = document.getElementById(`he-chunk-${target.chunkKey}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setHighlightedKey(target.chunkKey);
      const t = setTimeout(() => setHighlightedKey(null), 2400);
      onTargetConsumed?.();
      return () => clearTimeout(t);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, chunks, chunksLoading, selectedJob]);

  // Load chunks for selected job
  async function selectJob(job) {
    setSelectedJob(job);
    setJobNotes(job.notes || '');
    setChunks([]);
    setEditingChunk(null);
    setRecompileMsg('');
    setChunksLoading(true);
    try {
      const r = await apiFetch(`${API_URL}/api/history/${job.job_id}/chunks`);
      const d = await r.json();
      setChunks(d.chunks || []);
    } catch {
      setChunks([]);
    } finally {
      setChunksLoading(false);
    }
  }

  function startEdit(chunk) {
    setEditingChunk(chunk);
    setEditText(chunk.mt_latex || '');
    setEditNote(chunk.edit_note || '');
  }

  function cancelEdit() {
    setEditingChunk(null);
    setEditText('');
    setEditNote('');
  }

  async function saveEdit() {
    if (!editingChunk || !selectedJob) return;
    setSaving(true);
    try {
      const r = await apiFetch(
        `${API_URL}/api/history/${selectedJob.job_id}/chunks/${encodeURIComponent(editingChunk.chunk_key)}`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mt_latex: editText, edit_note: editNote }),
        }
      );
      if (r.ok) {
        setChunks(prev => prev.map(c =>
          c.chunk_key === editingChunk.chunk_key
            ? { ...c, mt_latex: editText, edited: 1, edit_note: editNote }
            : c
        ));
        cancelEdit();
      }
    } finally {
      setSaving(false);
    }
  }

  function startHint(chunk) {
    setHintingChunk(chunk.chunk_key);
    setHintText('');
    setHintError(null);
  }

  function cancelHint() {
    setHintingChunk(null);
    setHintText('');
    setHintError(null);
  }

  async function runHint(chunk) {
    if (!selectedJob || !hintText.trim()) return;
    setHintRunning(true);
    setHintError(null);
    try {
      const r = await apiFetch(
        `${API_URL}/api/history/${selectedJob.job_id}/chunks/${encodeURIComponent(chunk.chunk_key)}/retranslate`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ hint: hintText, persist: true }),
        }
      );
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        throw new Error(d.detail || `HTTP ${r.status}`);
      }
      setChunks(prev => prev.map(c =>
        c.chunk_key === chunk.chunk_key
          ? { ...c, mt_latex: d.translation, edited: 1, edit_note: 'hint-refined' }
          : c
      ));
      cancelHint();
    } catch (e) {
      setHintError(e.message || String(e));
    } finally {
      setHintRunning(false);
    }
  }

  async function saveNotes() {
    if (!selectedJob) return;
    await apiFetch(`${API_URL}/api/history/${selectedJob.job_id}/notes`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: jobNotes }),
    });
    setSelectedJob(prev => ({ ...prev, notes: jobNotes }));
    setEditingNotes(false);
  }

  async function recompile() {
    if (!selectedJob) return;
    setRecompiling(true);
    setRecompileMsg('Đang compile...');
    try {
      const r = await apiFetch(`${API_URL}/api/history/${selectedJob.job_id}/recompile`, { method: 'POST' });
      const d = await r.json();
      if (r.ok) {
        setRecompileMsg(`Hoàn thành! PDF đã cập nhật.`);
        if (onViewJob) onViewJob({ job_id: selectedJob.job_id, translated_pdf_url: d.pdf_url, original_pdf_url: `/api/pdf/${selectedJob.job_id}/original` });
      } else {
        setRecompileMsg(`Lỗi: ${d.detail}`);
      }
    } catch (e) {
      setRecompileMsg(`Lỗi: ${e.message}`);
    } finally {
      setRecompiling(false);
    }
  }

  async function syncJob(jobId) {
    await apiFetch(`${API_URL}/api/history/${jobId}/sync`, { method: 'POST' });
    loadJobs();
  }

  // Filter + search jobs
  const filteredJobs = jobs.filter(j => {
    if (filter === 'done' && !j.status?.startsWith('done')) return false;
    if (filter === 'error' && !j.status?.startsWith('error')) return false;
    if (filter === 'low' && (j.quality_score == null || j.quality_score >= 60)) return false;
    const q = search.toLowerCase();
    if (q && !j.job_id.toLowerCase().includes(q) && !(j.title || '').toLowerCase().includes(q)) return false;
    return true;
  });

  function scoreBadge(score) {
    if (score == null) return null;
    const cls = score >= 80 ? 'he-badge-good' : score >= 60 ? 'he-badge-ok' : 'he-badge-low';
    return <span className={`he-badge ${cls}`}>{score}%</span>;
  }

  function statusBadge(status) {
    const s = status || 'unknown';
      const cls = s.startsWith('done') ? 'he-status-done'
      : s.startsWith('error') ? 'he-status-error'
      : (s === 'cancelled' || s === 'superseded') ? 'he-status-cancelled'
      : 'he-status-running';
    const label = s.startsWith('done') ? 'done' : s.startsWith('error') ? 'error' : s;
    return <span className={`he-status ${cls}`}>{label}</span>;
  }

  return (
    <div className="he-layout">
      {/* ── Left: job list ── */}
      <div className="he-sidebar">
        <div className="he-sidebar-head">
          <input
            className="he-search"
            placeholder="Tìm theo ID hoặc tiêu đề..."
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
          <div className="he-filters">
            {['all','done','error','low'].map(f => (
              <button key={f} className={`he-filter ${filter === f ? 'active' : ''}`} onClick={() => setFilter(f)}>
                {f === 'all' ? 'Tất cả' : f === 'done' ? 'Hoàn thành' : f === 'error' ? 'Lỗi' : 'Điểm thấp'}
              </button>
            ))}
          </div>
          <button className="he-refresh" onClick={loadJobs} title="Làm mới">↺</button>
        </div>

        {loading ? (
          <div className="he-empty">Đang tải...</div>
        ) : filteredJobs.length === 0 ? (
          <div className="he-empty">Không có job nào</div>
        ) : (
          <div className="he-job-list">
            {filteredJobs.map(job => (
              <div
                key={job.job_id}
                className={`he-job-item ${selectedJob?.job_id === job.job_id ? 'active' : ''}`}
                onClick={() => selectJob(job)}
              >
                <div className="he-job-top">
                  <span className="he-job-id">{job.job_id}</span>
                  {statusBadge(job.status)}
                </div>
                <div className="he-job-scores">
                  {job.quality_score != null && scoreBadge(job.quality_score)}
                  {job.done_chunks > 0 && (
                    <span className="he-badge he-badge-neutral">{job.done_chunks} chunks</span>
                  )}
                </div>
                {job.notes && <div className="he-job-notes-preview">{job.notes.slice(0, 60)}…</div>}
              </div>
            ))}
          </div>
        )}
      </div>

      {/* ── Right: job detail + chunks ── */}
      <div className="he-main">
        {!selectedJob ? (
          <div className="he-empty-main">Chọn một job để xem lịch sử dịch</div>
        ) : (
          <>
            {/* Job header */}
            <div className="he-job-header">
              <div className="he-job-header-left">
                <h3 className="he-job-title">{selectedJob.job_id}</h3>
                <div className="he-job-meta">
                  {statusBadge(selectedJob.status)}
                  {selectedJob.quality_score != null && scoreBadge(selectedJob.quality_score)}
                  <span className="he-meta-text">
                    {selectedJob.source_type === 'pdf' ? 'PDF' : 'LaTeX'} ·{' '}
                    {selectedJob.done_chunks} chunks ·{' '}
                    {selectedJob.updated_at?.slice(0, 10)}
                  </span>
                </div>
              </div>
              <div className="he-job-header-actions">
                {selectedJob.source_type !== 'pdf' && (
                  <button className="he-btn he-btn-primary" onClick={recompile} disabled={recompiling}>
                    {recompiling ? 'Đang compile...' : 'Recompile PDF'}
                  </button>
                )}
                {onViewJob && selectedJob.status?.startsWith('done') && (
                  <button className="he-btn he-btn-secondary" onClick={() => onViewJob({
                    job_id: selectedJob.job_id,
                    original_pdf_url: `/api/pdf/${selectedJob.job_id}/original`,
                    translated_pdf_url: `/api/pdf/${selectedJob.job_id}/translated`,
                    source_type: selectedJob.source_type,
                  })}>
                    Xem PDF
                  </button>
                )}
                <button className="he-btn he-btn-ghost" onClick={() => syncJob(selectedJob.job_id)}>
                  Sync DB
                </button>
              </div>
            </div>

            {recompileMsg && (
              <div className={`he-compile-msg ${recompileMsg.startsWith('Lỗi') ? 'error' : 'ok'}`}>
                {recompileMsg}
              </div>
            )}

            {/* Notes */}
            <div className="he-notes-section">
              {editingNotes ? (
                <div className="he-notes-edit">
                  <textarea
                    className="he-notes-textarea"
                    value={jobNotes}
                    onChange={e => setJobNotes(e.target.value)}
                    placeholder="Ghi chú về bản dịch này..."
                    rows={3}
                  />
                  <div className="he-notes-actions">
                    <button className="he-btn he-btn-primary" onClick={saveNotes}>Lưu</button>
                    <button className="he-btn he-btn-ghost" onClick={() => { setEditingNotes(false); setJobNotes(selectedJob.notes || ''); }}>Hủy</button>
                  </div>
                </div>
              ) : (
                <div className="he-notes-display" onClick={() => setEditingNotes(true)}>
                  {jobNotes ? <span>{jobNotes}</span> : <span className="he-notes-placeholder">+ Thêm ghi chú...</span>}
                </div>
              )}
            </div>

            {/* Chunks table */}
            {chunksLoading ? (
              <div className="he-empty">Đang tải chunks...</div>
            ) : chunks.length === 0 ? (
              <div className="he-empty">Chưa có chunk nào được lưu. <button className="he-btn he-btn-ghost" onClick={() => syncJob(selectedJob.job_id)}>Sync từ progress.json</button></div>
            ) : (
              <div className="he-chunks-wrap">
                <div className="he-chunks-info">
                  {chunks.length} chunks · {chunks.filter(c => c.edited).length} đã chỉnh sửa
                </div>
                <div className="he-chunks-list">
                  {chunks.map((chunk, i) => {
                    const isEditing = editingChunk?.chunk_key === chunk.chunk_key;
                    const isHinting = hintingChunk === chunk.chunk_key;
                    const isFlash = highlightedKey === chunk.chunk_key;
                    return (
                      <div
                        key={chunk.id}
                        id={`he-chunk-${chunk.chunk_key}`}
                        className={`he-chunk ${chunk.edited ? 'he-chunk-edited' : ''} ${isFlash ? 'he-chunk-flash' : ''}`}
                      >
                        <div className="he-chunk-head">
                          <span className="he-chunk-idx">#{i + 1}</span>
                          <span className="he-chunk-key">{chunk.chunk_key}</span>
                          {chunk.edited && <span className="he-badge he-badge-edited">chỉnh sửa</span>}
                          {chunk.edit_note && <span className="he-chunk-note">{chunk.edit_note}</span>}
                          <button className="he-edit-btn" onClick={() => isEditing ? cancelEdit() : startEdit(chunk)}>
                            {isEditing ? 'Đóng' : 'Sửa'}
                          </button>
                          <button
                            className="he-edit-btn"
                            title="Dịch lại với gợi ý — gửi prompt cải thiện cho Gemini"
                            onClick={() => isHinting ? cancelHint() : startHint(chunk)}
                            disabled={hintRunning}
                          >
                            {isHinting ? 'Đóng gợi ý' : 'Dịch lại với gợi ý'}
                          </button>
                        </div>

                        <div className="he-chunk-body">
                          <div className="he-chunk-col">
                            <div className="he-chunk-label">Gốc (EN / LaTeX)</div>
                            <pre className="he-chunk-text">{chunk.src_latex || '—'}</pre>
                          </div>
                          <div className="he-chunk-col">
                            <div className="he-chunk-label">Dịch (VI){chunk.edited ? ' ✏' : ''}</div>
                            {isEditing ? (
                              <div className="he-edit-block">
                                <textarea
                                  className="he-edit-textarea"
                                  value={editText}
                                  onChange={e => setEditText(e.target.value)}
                                  rows={Math.max(6, (editText.match(/\n/g) || []).length + 2)}
                                />
                                <input
                                  className="he-edit-note-input"
                                  placeholder="Ghi chú lý do chỉnh sửa..."
                                  value={editNote}
                                  onChange={e => setEditNote(e.target.value)}
                                />
                                <div className="he-edit-actions">
                                  <button className="he-btn he-btn-primary" onClick={saveEdit} disabled={saving}>
                                    {saving ? 'Đang lưu...' : 'Lưu chỉnh sửa'}
                                  </button>
                                  <button className="he-btn he-btn-ghost" onClick={cancelEdit}>Hủy</button>
                                </div>
                              </div>
                            ) : (
                              <pre className="he-chunk-text">{chunk.mt_latex || '—'}</pre>
                            )}
                          </div>
                        </div>

                        {isHinting && (
                          <div className="he-hint-block">
                            <div className="he-hint-label">
                              Gợi ý cho Gemini (vd: "Dịch <code>transformer</code> là <code>mô hình biến đổi</code>")
                            </div>
                            <textarea
                              className="he-edit-textarea"
                              placeholder="Mô tả cách dịch mong muốn..."
                              value={hintText}
                              onChange={e => setHintText(e.target.value)}
                              rows={3}
                              disabled={hintRunning}
                            />
                            {hintError && <div className="he-hint-error">Lỗi: {hintError}</div>}
                            <div className="he-edit-actions">
                              <button
                                className="he-btn he-btn-primary"
                                onClick={() => runHint(chunk)}
                                disabled={hintRunning || !hintText.trim()}
                              >
                                {hintRunning ? 'Đang gửi cho Gemini...' : 'Dịch lại'}
                              </button>
                              <button className="he-btn he-btn-ghost" onClick={cancelHint} disabled={hintRunning}>
                                Hủy
                              </button>
                              <span className="he-hint-warn">
                                Có thể mất 30-60s · Sau khi xong, bấm <strong>Recompile</strong> để cập nhật PDF
                              </span>
                            </div>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
