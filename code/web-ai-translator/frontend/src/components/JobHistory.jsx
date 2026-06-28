import { useCallback, useMemo, useState, useEffect } from 'react';

import API_URL, { apiFetch } from '../api.js';

function deriveJobBasename(job, fallbackTitle) {
  const raw = job.original_filename || fallbackTitle || job.title || job.job_id || 'document';
  return raw.replace(/^pdf_/, '').replace(/\.pdf$/i, '')
            .replace(/[\\/:*?"<>|\r\n\t]+/g, '_').trim() || 'document';
}

async function downloadWithAuth(url, filename, onError) {
  try {
    const res = await apiFetch(url);
    if (!res.ok) {
      onError?.(`Không tải được (HTTP ${res.status})`);
      return;
    }
    const blob = await res.blob();
    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
  } catch (err) {
    onError?.(err.message || 'Lỗi mạng');
  }
}

function getStatusInfo(job) {
  const s = String(job.status || 'unknown');
  if (s === 'done_with_warnings') {
    return { label: 'Cảnh báo', className: 'status-warning', icon: '!' };
  }
  if (s === 'done' || (job.has_translated_pdf && !s.startsWith('done_with'))) {
    return { label: 'Hoàn thành', className: 'status-done', icon: '✓' };
  }
  if (s === 'paused') {
    return { label: 'Tạm dừng', className: 'status-paused', icon: 'Ⅱ' };
  }
  if (s === 'cancelled') {
    return { label: 'Đã hủy', className: 'status-cancelled', icon: '■' };
  }
  if (s === 'superseded') {
    return { label: 'Đã thay thế', className: 'status-cancelled', icon: '↷' };
  }
  if (s === 'pausing') {
    return { label: 'Đang dừng', className: 'status-running', icon: 'Ⅱ' };
  }
  if (s.startsWith('retrying') || s === 'resuming') {
    return { label: 'Gián đoạn', className: 'status-error', icon: '!' };
  }
  if (
    s.startsWith('translating') || s.includes('eval-loop') || s === 'starting'
    || s === 'extracting' || s === 'compiling'
  ) {
    return { label: 'Đang dịch', className: 'status-running', icon: '▶' };
  }
  if (s.startsWith('error') || s.startsWith('compile_error')) {
    return { label: 'Thất bại', className: 'status-error', icon: '!' };
  }
  return { label: s, className: 'status-unknown', icon: '?' };
}

function getPdfUrls(job) {
  if (job.source_type === 'pdf' || job.source_type === 'pdf_only') {
    return {
      original: `/api/pdf-translate/${job.job_id}/original`,
      translated: `/api/pdf-translate/${job.job_id}/translated`,
    };
  }
  return {
    original: `/api/pdf/${job.job_id}/original`,
    translated: `/api/pdf/${job.job_id}/translated`,
  };
}

function getTitle(job) {
  return (
    job.original_filename
    || job.title
    || String(job.job_id || '').replace(/^pdf_/, '').replace(/_/g, ' ')
    || 'Không có tiêu đề'
  );
}

function getRunTimestamp(job) {
  return job.started_at || job.created_at || job.updated_at || null;
}

function getRunLabel(job) {
  const stamp = formatDate(getRunTimestamp(job));
  return stamp === '-' ? `Job ${job.job_id}` : `Lần chạy: ${stamp}`;
}

function getFormat(job) {
  const name = (job.original_filename || job.title || '').toLowerCase();
  const ext = name.match(/\.([a-z0-9]+)$/)?.[1];
  if (ext) return ext.toUpperCase();
  if (job.source_type === 'pdf' || job.source_type === 'pdf_only') return 'PDF';
  if (job.source_type === 'office') return 'OFFICE';
  return 'LATEX';
}

function getModelLabel(job) {
  const models = job.model_preference || job.models;
  if (Array.isArray(models) && models.length) {
    return models.map(m => String(m).replace(/^./, c => c.toUpperCase())).join(', ');
  }
  return '-';
}

function formatDate(value) {
  if (!value) return '-';
  const ms = Number(value) > 1e12 ? Number(value) : Number(value) * 1000;
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return '-';
  return date.toLocaleString('vi-VN', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatDuration(seconds) {
  if (seconds == null || Number.isNaN(Number(seconds))) return '-';
  const total = Math.max(0, Math.round(Number(seconds)));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return [h, m, s].map(v => String(v).padStart(2, '0')).join(':');
}

function isWarningJob(job) {
  return (
    job.status === 'done_with_warnings'
    || job.validation?.status === 'warning'
    || Number(job.quality_issues || 0) > 0
  );
}

function isResumable(job) {
  const s = String(job.status || '');
  return s === 'paused' || s === 'cancelled' || s.startsWith('error') || s.startsWith('retrying') || s === 'resuming';
}

function sumTimelineDuration(entries, phases) {
  const wanted = new Set(phases);
  return entries
    .filter(item => wanted.has(item.phase))
    .reduce((total, item) => total + Number(item.duration_seconds || 0), 0);
}

function buildTimelineItems(job) {
  const timeline = Array.isArray(job.phase_timeline) ? job.phase_timeline : [];
  const modelLabel = getModelLabel(job);
  const totalChunks = job.total_chunks || job.translated_chunks_count || job.eval_loop?.passed_chunks || 0;
  const loopSeconds = sumTimelineDuration(timeline, ['eval_loop']) || Number(job.eval_loop?.duration_seconds || 0);

  return [
    {
      title: 'Trích xuất (Extraction)',
      description: job.page_count
        ? `${job.page_count} trang tài liệu nguồn.`
        : (job.has_original_pdf ? 'Đã nhận tài liệu nguồn.' : 'Chưa có file gốc.'),
      seconds: sumTimelineDuration(timeline, ['extract']),
    },
    {
      title: 'Lập kế hoạch & Thuật ngữ',
      description: job.glossary_count
        ? `${job.glossary_count} thuật ngữ được ghi nhận.`
        : (job.total_chunks ? `${job.total_chunks} chunk đã lập kế hoạch.` : 'Đang chờ dữ liệu.'),
      seconds: sumTimelineDuration(timeline, ['plan', 'glossary']),
    },
    {
      title: 'Vòng lặp Dịch/Review/Sửa',
      description: totalChunks
        ? `${totalChunks} chunks xử lý qua ${modelLabel !== '-' ? modelLabel : 'model dịch'}.`
        : `${job.progress_percent || 0}% hoàn tất.`,
      seconds: loopSeconds,
    },
    {
      title: 'Xây dựng lại (Rebuild)',
      description: job.has_translated_pdf ? 'Bản dịch đã sẵn sàng.' : 'Chưa có bản dịch hoàn chỉnh.',
      seconds: sumTimelineDuration(timeline, ['rebuild']),
    },
  ];
}

export default function JobHistory({ onViewJob, onResumeJob, onRetranslateJob }) {
  const [jobs, setJobs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selectedId, setSelectedId] = useState(null);
  const [confirmResume, setConfirmResume] = useState(null);
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('all');
  const [formatFilter, setFormatFilter] = useState('all');
  const [warningsOnly, setWarningsOnly] = useState(false);

  const fetchJobs = useCallback(async () => {
    try {
      const res = await apiFetch(`${API_URL}/api/jobs`);
      const data = await res.json();
      setJobs(data.jobs || []);
      setLoading(false);
    } catch {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const firstLoad = setTimeout(fetchJobs, 0);
    const interval = setInterval(fetchJobs, 5000);
    return () => {
      clearTimeout(firstLoad);
      clearInterval(interval);
    };
  }, [fetchJobs]);

  const filteredJobs = useMemo(() => {
    const q = query.trim().toLowerCase();
    return jobs.filter(job => {
      const status = getStatusInfo(job);
      const format = getFormat(job).toLowerCase();
      const haystack = `${getTitle(job)} ${job.job_id || ''} ${job.arxiv_id || ''}`.toLowerCase();
      if (q && !haystack.includes(q)) return false;
      if (statusFilter !== 'all' && status.className !== statusFilter) return false;
      if (formatFilter !== 'all' && format !== formatFilter) return false;
      if (warningsOnly && !isWarningJob(job)) return false;
      return true;
    });
  }, [jobs, query, statusFilter, formatFilter, warningsOnly]);

  const selectedJob = filteredJobs.find(job => job.job_id === selectedId) || null;

  function viewJob(job) {
    const urls = getPdfUrls(job);
    onViewJob({
      job_id: job.job_id,
      arxiv_id: job.arxiv_id,
      original_pdf_url: job.has_original_pdf ? urls.original : job.original_pdf_url,
      translated_pdf_url: job.has_translated_pdf ? urls.translated : job.translated_pdf_url,
      status: job.has_translated_pdf ? 'done' : job.status,
      source_type: job.source_type,
      title: getTitle(job),
      original_filename: job.original_filename,
    });
  }

  function handleResume(job) {
    setConfirmResume(null);
    onResumeJob({
      job_id: job.job_id,
      arxiv_id: job.arxiv_id,
      source_type: job.source_type || 'latex',
      title: getTitle(job),
      original_filename: job.original_filename,
    });
  }

  function handleRetranslate(job) {
    setConfirmResume(null);
    onRetranslateJob({
      job_id: job.job_id,
      arxiv_id: job.arxiv_id,
      source_type: job.source_type || 'latex',
      title: getTitle(job),
      original_filename: job.original_filename,
    });
  }

  function handleViewOriginal(job) {
    if (!job.has_original_pdf) return;
    const urls = getPdfUrls(job);
    onViewJob({
      job_id: job.job_id,
      arxiv_id: job.arxiv_id,
      original_pdf_url: urls.original,
      status: job.status,
      source_type: job.source_type,
      title: getTitle(job),
      original_filename: job.original_filename,
    });
  }

  function handleDownload(job) {
    if (!job.has_translated_pdf) return;
    const urls = getPdfUrls(job);
    const base = deriveJobBasename(job, getTitle(job));
    downloadWithAuth(
      `${API_URL}${urls.translated}`,
      `${base}_vi_translated.pdf`,
      msg => alert(`Tải xuống thất bại: ${msg}`),
    );
  }

  if (loading) {
    return (
      <div className="history-shell history-state">
        <p>Đang tải danh sách...</p>
      </div>
    );
  }

  if (jobs.length === 0) {
    return (
      <div className="history-shell history-state">
        <p>Chưa có tài liệu nào được dịch. Hãy bắt đầu từ tab Upload.</p>
      </div>
    );
  }

  const selectedStatus = selectedJob ? getStatusInfo(selectedJob) : null;
  const timelineItems = selectedJob ? buildTimelineItems(selectedJob) : [];
  const displayStart = filteredJobs.length ? 1 : 0;
  const displayEnd = filteredJobs.length;

  return (
    <div className={`history-shell ${selectedJob ? 'history-detail-open' : ''}`}>
      <section className="history-table-pane">
        <div className="history-toolbar">
          <div className="history-search">
            <span aria-hidden="true">⌕</span>
            <input
              type="text"
              value={query}
              onChange={e => setQuery(e.target.value)}
              placeholder="Tìm kiếm tài liệu..."
            />
          </div>
          <div className="history-filter-row">
            <select value={statusFilter} onChange={e => setStatusFilter(e.target.value)} aria-label="Lọc trạng thái">
              <option value="all">Trạng thái</option>
              <option value="status-running">Đang dịch</option>
              <option value="status-done">Hoàn thành</option>
              <option value="status-warning">Cảnh báo</option>
              <option value="status-paused">Tạm dừng</option>
              <option value="status-error">Thất bại</option>
              <option value="status-cancelled">Đã hủy</option>
            </select>
            <select value={formatFilter} onChange={e => setFormatFilter(e.target.value)} aria-label="Lọc định dạng">
              <option value="all">Định dạng</option>
              <option value="pdf">PDF</option>
              <option value="docx">DOCX</option>
              <option value="tex">TEX</option>
              <option value="md">MD</option>
              <option value="txt">TXT</option>
            </select>
            <label className="history-warning-toggle">
              <input
                type="checkbox"
                checked={warningsOnly}
                onChange={e => setWarningsOnly(e.target.checked)}
              />
              Chỉ cảnh báo
            </label>
          </div>
        </div>

        <div className="history-table-wrap">
          <table className="history-table">
            <thead>
              <tr>
                <th>Tên tài liệu</th>
                <th>Định dạng</th>
                <th>Trạng thái</th>
                <th>Ngày tạo</th>
                <th>Thời lượng</th>
                <th>Mô hình</th>
                <th>Thao tác</th>
              </tr>
            </thead>
            <tbody>
              {filteredJobs.map(job => {
                const status = getStatusInfo(job);
                const active = selectedJob?.job_id === job.job_id;
                return (
                  <tr
                    key={job.job_id}
                    className={`${active ? 'selected' : ''} ${status.className}`}
                    onClick={() => setSelectedId(job.job_id)}
                  >
                    <td className="history-title-cell">
                      <span className={`history-row-icon ${status.className}`} aria-hidden="true">{status.icon}</span>
                      <span className="history-title-stack">
                        <strong title={getTitle(job)}>{getTitle(job)}</strong>
                        <small>{getRunLabel(job)}</small>
                      </span>
                    </td>
                    <td><span className="history-format-chip">{getFormat(job)}</span></td>
                    <td>
                      <span className={`history-status-dot ${status.className}`} />
                      <span className={`history-status-text ${status.className}`}>{status.label}</span>
                    </td>
                    <td className="history-muted">{formatDate(getRunTimestamp(job))}</td>
                    <td className="history-mono">{formatDuration(job.duration_seconds)}</td>
                    <td className="history-muted">{getModelLabel(job)}</td>
                    <td className="history-row-actions" onClick={e => e.stopPropagation()}>
                      {job.has_translated_pdf && (
                        <button type="button" title="Tải xuống" onClick={() => handleDownload(job)}>⇩</button>
                      )}
                      <button type="button" title="Mở" onClick={() => viewJob(job)}>↗</button>
                    </td>
                  </tr>
                );
              })}
              {filteredJobs.length === 0 && (
                <tr>
                  <td colSpan="7" className="history-no-results">Không có job phù hợp với bộ lọc.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <div className="history-pagination">
          <span>Hiển thị {displayStart}-{displayEnd} trên {jobs.length} công việc</span>
          <div>
            <button type="button" disabled>‹</button>
            <button type="button" disabled>›</button>
          </div>
        </div>
      </section>

      {selectedJob && selectedStatus && (
        <aside className="history-detail-pane">
          <>
            <div className="history-detail-header">
              <h2>Chi tiết công việc</h2>
              <div className="history-detail-header-actions">
                <span className={`history-detail-status ${selectedStatus.className}`}>
                  <span className={`history-status-dot ${selectedStatus.className}`} />
                  {selectedStatus.label}
                </span>
                <button
                  type="button"
                  className="history-detail-close"
                  onClick={() => setSelectedId(null)}
                  aria-label="Đóng chi tiết công việc"
                  title="Đóng"
                >
                  ×
                </button>
              </div>
            </div>

            <div className="history-detail-scroll">
              <div className="history-detail-actions">
                <button type="button" className="history-primary-action" disabled={!selectedJob.has_translated_pdf} onClick={() => handleDownload(selectedJob)}>
                  ⇩ Tải bản dịch
                </button>
                <button type="button" onClick={() => viewJob(selectedJob)}>
                  ⇄ Mở tệp đối chiếu
                </button>
              </div>

              <div className="history-metric-grid">
                <div className="history-metric-card wide">
                  <span>Tiêu đề tài liệu</span>
                  <strong title={getTitle(selectedJob)}>{getTitle(selectedJob)}</strong>
                  <small className="history-run-label">{getRunLabel(selectedJob)}</small>
                  <div className="history-format-flow">
                    <span>{getFormat(selectedJob)}</span>
                    <em>→</em>
                    <span>PDF</span>
                  </div>
                </div>
                <div className="history-metric-card">
                  <span>Trạng thái cuối</span>
                  <strong className={selectedStatus.className}>{selectedStatus.label}</strong>
                </div>
                <div className="history-metric-card">
                  <span>Thời gian xử lý</span>
                  <strong className="history-mono">{formatDuration(selectedJob.duration_seconds)}</strong>
                </div>
                <div className="history-metric-card">
                  <span>Tổng số chunk</span>
                  <strong className="history-mono">
                    {selectedJob.total_chunks || selectedJob.translated_chunks_count || '-'}
                    {Number(selectedJob.quality_issues || 0) > 0 && (
                      <small> / {selectedJob.quality_issues} cảnh báo</small>
                    )}
                  </strong>
                </div>
                <div className="history-metric-card">
                  <span>Mô hình chính</span>
                  <strong>{getModelLabel(selectedJob)}</strong>
                </div>
              </div>

              <section className="history-report-card">
                <div className="history-section-title">
                  <h3>Tổng quan báo cáo</h3>
                  {selectedJob.has_translated_pdf && <button type="button" onClick={() => viewJob(selectedJob)}>Chi tiết ↗</button>}
                </div>
                <div className="history-report-grid">
                  <div>
                    <span>Điểm chất lượng</span>
                    <strong>{selectedJob.quality_score ?? '-'}/100</strong>
                  </div>
                  <div>
                    <span>Tiến độ</span>
                    <strong>{selectedJob.progress_percent ?? 0}%</strong>
                  </div>
                  <div>
                    <span>Số trang</span>
                    <strong>{selectedJob.validation?.translated_pages || selectedJob.page_count || '-'}</strong>
                  </div>
                  <div>
                    <span>Judge</span>
                    <strong>{selectedJob.judge_backend || '-'}</strong>
                  </div>
                </div>
              </section>

              <section className="history-timeline-card">
                <h3>Tiến trình (Timeline)</h3>
                <div className="history-timeline">
                  {timelineItems.map(item => (
                    <div className="history-timeline-item" key={item.title}>
                      <span />
                      <div>
                        <div className="history-timeline-row">
                          <strong>{item.title}</strong>
                          <time>{formatDuration(item.seconds || 0)}</time>
                        </div>
                        <p>{item.description}</p>
                      </div>
                    </div>
                  ))}
                </div>
              </section>

              <section className="history-advanced-actions">
                <h3>Tác vụ nâng cao</h3>
                <button type="button" onClick={() => setConfirmResume(selectedJob)} disabled={!isResumable(selectedJob)}>
                  ↻ Tiếp tục dịch
                </button>
                <button type="button" onClick={() => handleRetranslate(selectedJob)}>
                  ⚙ Dịch lại từ đầu
                </button>
                <button type="button" onClick={() => handleViewOriginal(selectedJob)} disabled={!selectedJob.has_original_pdf}>
                  ⧉ Xem bản gốc
                </button>
              </section>
            </div>
          </>
        </aside>
      )}

      {confirmResume && (
        <div className="confirm-overlay">
          <div className="confirm-dialog">
            <h3>{confirmResume.status === 'cancelled' ? 'Bản dịch bị hủy' : 'Tiếp tục công việc'}</h3>
            <p>
              Job <strong>{getTitle(confirmResume)}</strong> đang ở mức{' '}
              <strong>{confirmResume.progress_percent || 0}%</strong>.
              <span className="confirm-run-label">{getRunLabel(confirmResume)}</span>
              Bạn muốn xử lý thế nào?
            </p>
            <div className="confirm-actions">
              <button className="btn-primary" onClick={() => handleResume(confirmResume)}>
                Tiếp tục dịch
              </button>
              <button className="btn-warning" onClick={() => handleRetranslate(confirmResume)}>
                Dịch lại từ đầu
              </button>
              {confirmResume.has_original_pdf && (
                <button className="btn-secondary" onClick={() => handleViewOriginal(confirmResume)}>
                  Xem bản gốc
                </button>
              )}
              <button className="btn-secondary" onClick={() => setConfirmResume(null)}>
                Đóng
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
