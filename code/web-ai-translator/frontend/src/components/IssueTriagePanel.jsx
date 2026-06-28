import { useEffect, useState } from 'react';

import API_URL, { apiFetch } from '../api.js';

/**
 * Surface heuristic quality issues + diagnostic findings as a single triage list,
 * with deep-links into HistoryEditor for chunks that need attention.
 *
 * Props:
 *   jobId: string
 *   onJumpToChunk: (jobId, chunkIdx: number) => void
 *     — called when user clicks "Xem chunk #N" so the host can switch tabs and scroll.
 */
export default function IssueTriagePanel({ jobId, onJumpToChunk }) {
  const [quality, setQuality]         = useState(null);
  const [diagnostics, setDiagnostics] = useState(null);
  const [loading, setLoading]         = useState(true);
  const [errMsg, setErrMsg]           = useState(null);
  const [refreshing, setRefreshing]   = useState(false);

  async function loadAll() {
    setLoading(true);
    setErrMsg(null);
    try {
      const [qRes, dRes] = await Promise.all([
        apiFetch(`${API_URL}/api/pdf-translate/${jobId}/quality`),
        apiFetch(`${API_URL}/api/pdf-translate/${jobId}/diagnostics`),
      ]);
      const qData = qRes.ok ? await qRes.json() : null;
      const dData = dRes.ok ? await dRes.json() : null;
      setQuality(qData);
      setDiagnostics(dData);
    } catch (err) {
      setErrMsg(err.message || String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (jobId) loadAll();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  async function recomputeDiagnostics() {
    setRefreshing(true);
    try {
      const res = await apiFetch(
        `${API_URL}/api/pdf-translate/${jobId}/diagnostics?refresh=true`,
      );
      if (res.ok) setDiagnostics(await res.json());
    } catch (err) {
      setErrMsg(err.message || String(err));
    } finally {
      setRefreshing(false);
    }
  }

  if (loading) {
    return <div className="triage-panel"><div className="triage-loading">Đang tải...</div></div>;
  }

  const issues = quality?.issues || [];
  const findings = diagnostics?.findings || [];
  const hasNothing = issues.length === 0 && findings.length === 0;

  return (
    <div className="triage-panel">
      <div className="triage-header">
        <div className="triage-title">
          <strong>Bước 3/3 · Cần xem lại</strong>
          <span className="triage-subtitle">
            Vấn đề tự động phát hiện. Bấm <em>Xem chunk</em> để mở trong Lịch sử dịch và sửa.
          </span>
        </div>
        <button
          className="triage-refresh"
          onClick={recomputeDiagnostics}
          disabled={refreshing}
          title="Tính lại chẩn đoán"
        >
          {refreshing ? 'Đang tính...' : 'Tính lại'}
        </button>
      </div>

      {errMsg && <div className="triage-error">Lỗi: {errMsg}</div>}

      {hasNothing && !errMsg && (
        <div className="triage-empty">
          Không phát hiện vấn đề nào — bản dịch trông ổn.
        </div>
      )}

      {findings.length > 0 && (
        <div className="triage-section">
          <div className="triage-section-title">
            Chẩn đoán nguyên nhân ({findings.length})
            {diagnostics?.summary && (
              <span className="triage-summary">— {diagnostics.summary}</span>
            )}
          </div>
          <ul className="triage-list">
            {findings.map((f, i) => (
              <li key={i} className={`triage-item triage-${f.severity}`}>
                <div className="triage-item-head">
                  <span className={`triage-badge triage-badge-${f.severity}`}>
                    {f.severity}
                  </span>
                  <span className="triage-cause">{f.cause_label || f.cause}</span>
                  {typeof f.confidence === 'number' && (
                    <span className="triage-confidence">
                      {Math.round(f.confidence * 100)}%
                    </span>
                  )}
                </div>
                {f.evidence && <div className="triage-evidence">{f.evidence}</div>}
                {f.recommendation && (
                  <div className="triage-recommendation">→ {f.recommendation}</div>
                )}
                {Array.isArray(f.affected_chunks) && f.affected_chunks.length > 0 && (
                  <div className="triage-chunks">
                    {f.affected_chunks.slice(0, 12).map(ci => (
                      <button
                        key={ci}
                        className="triage-chunk-btn"
                        onClick={() => onJumpToChunk?.(jobId, ci)}
                      >
                        Xem chunk #{ci + 1}
                      </button>
                    ))}
                    {f.affected_chunks.length > 12 && (
                      <span className="triage-chunks-more">
                        +{f.affected_chunks.length - 12} nữa
                      </span>
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {issues.length > 0 && (
        <div className="triage-section">
          <div className="triage-section-title">
            Vấn đề heuristic ({issues.length})
            {quality?.score != null && (
              <span className="triage-summary">— điểm {quality.score}/100</span>
            )}
          </div>
          <ul className="triage-list">
            {issues.slice(0, 50).map((iss, i) => (
              <li key={i} className={`triage-item triage-${iss.severity}`}>
                <div className="triage-item-head">
                  <span className={`triage-badge triage-badge-${iss.severity}`}>
                    {iss.severity}
                  </span>
                  <span className="triage-cause">{iss.category}</span>
                  <span className="triage-page">trang {iss.page}</span>
                </div>
                <div className="triage-evidence">{iss.message}</div>
                {(iss.original || iss.translated) && (
                  <div className="triage-snippets">
                    {iss.original && (
                      <div className="triage-snippet">
                        <span className="triage-snippet-label">EN:</span>{' '}
                        {iss.original.slice(0, 160)}
                      </div>
                    )}
                    {iss.translated && (
                      <div className="triage-snippet">
                        <span className="triage-snippet-label">VI:</span>{' '}
                        {iss.translated.slice(0, 160)}
                      </div>
                    )}
                  </div>
                )}
              </li>
            ))}
            {issues.length > 50 && (
              <li className="triage-item-more">
                +{issues.length - 50} vấn đề nữa — xem chi tiết trong tab Đánh giá chất lượng.
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}
