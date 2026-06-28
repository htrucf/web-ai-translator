import { useEffect, useMemo, useState } from 'react';

import API_URL, { apiFetch } from '../api.js';

/**
 * Glossary editor with lock toggle.
 *
 * Props:
 *   jobId: string
 *   jobType: 'pdf' | 'latex'  — picks the right endpoint
 *   onError: (title, detail) => void  — surface error toasts
 *   onClose: () => void
 */
export default function GlossaryEditor({ jobId, jobType, onError, onClose }) {
  const endpoint = jobType === 'pdf'
    ? `${API_URL}/api/pdf-translate/${jobId}/glossary`
    : `${API_URL}/api/job/${jobId}/glossary`;

  const [loading, setLoading]     = useState(true);
  const [saving, setSaving]       = useState(false);
  const [terms, setTerms]         = useState({});       // { en: vi }
  const [locked, setLocked]       = useState(new Set()); // Set<en lowercased>
  const [fields, setFields]       = useState({});       // { en_lowercased: lĩnh vực }
  const [enabled, setEnabled]     = useState(true);
  const [filter, setFilter]       = useState('');
  const [newEn, setNewEn]         = useState('');
  const [newVi, setNewVi]         = useState('');
  const [newField, setNewField]   = useState('');
  const [dirty, setDirty]         = useState(false);
  const [errMsg, setErrMsg]       = useState(null);
  const [globalTerms, setGlobalTerms] = useState(new Set()); // Set<en lowercased> — promoted to "kho"
  const [globalFields, setGlobalFields] = useState([]);      // distinct lĩnh vực (autocomplete)
  const [promoting, setPromoting]     = useState(new Set()); // in-flight per-row promotions
  const [selected, setSelected]       = useState(new Set()); // Set<original en> — bulk-selection
  const [bulkSaving, setBulkSaving]   = useState(false);
  const [bulkResult, setBulkResult]   = useState(null);      // {added, skipped, failed}
  const [packs, setPacks]             = useState([]);        // available domain packs (metadata only)
  const [selectedPacks, setSelectedPacks] = useState(new Set()); // pack ids the user has ticked
  const [importingPacks, setImportingPacks] = useState(false);
  const [packsOpen, setPacksOpen]     = useState(false);     // collapsed by default
  const [packsResult, setPacksResult] = useState(null);      // last import outcome

  // ── Initial load ────────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setErrMsg(null);
      try {
        const res = await apiFetch(endpoint);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled) return;
        setTerms(data.terms || {});
        setLocked(new Set((data.locked || []).map(k => String(k).toLowerCase())));
        setFields(data.fields || {});
        setEnabled(data.enabled !== false);
        setDirty(false);
      } catch (err) {
        if (!cancelled) setErrMsg(err.message || String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, [endpoint]);

  // ── Load global glossary so we can mark which terms are already in the "kho" ──
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiFetch(`${API_URL}/api/pdf-translate/global-glossary?limit=2000`);
        if (!res.ok || cancelled) return;
        const data = await res.json();
        const keys = Object.keys(data.terms || {}).map(k => k.toLowerCase());
        setGlobalTerms(new Set(keys));
        if (Array.isArray(data.fields)) setGlobalFields(data.fields);
      } catch {
        // Non-fatal — if global glossary endpoint is unreachable, just hide the badge.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // ── Load available domain packs (metadata only) ─────────────────────────
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await apiFetch(`${API_URL}/api/pdf-translate/glossary-packs`);
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (!cancelled) setPacks(Array.isArray(data.packs) ? data.packs : []);
      } catch {
        // Non-fatal — pack picker just won't render.
      }
    })();
    return () => { cancelled = true; };
  }, []);

  // ── Helpers ─────────────────────────────────────────────────────────────
  const sortedEntries = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return Object.entries(terms)
      .filter(([en, vi]) => !f || en.toLowerCase().includes(f) || vi.toLowerCase().includes(f))
      .sort(([a], [b]) => {
        const aLocked = locked.has(a.toLowerCase());
        const bLocked = locked.has(b.toLowerCase());
        if (aLocked !== bLocked) return aLocked ? -1 : 1;
        return a.localeCompare(b);
      });
  }, [terms, locked, filter]);

  function updateVi(en, newVal) {
    setTerms(prev => ({ ...prev, [en]: newVal }));
    setDirty(true);
  }

  // Per-term lĩnh vực (free text). Stored lowercased to match the backend map.
  function updateField(en, val) {
    const key = en.toLowerCase();
    setFields(prev => {
      const next = { ...prev };
      if (val && val.trim()) next[key] = val;
      else delete next[key];
      return next;
    });
    setDirty(true);
  }

  function deleteTerm(en) {
    setTerms(prev => {
      const next = { ...prev };
      delete next[en];
      return next;
    });
    setLocked(prev => {
      const next = new Set(prev);
      next.delete(en.toLowerCase());
      return next;
    });
    setFields(prev => {
      const next = { ...prev };
      delete next[en.toLowerCase()];
      return next;
    });
    setDirty(true);
  }

  function toggleLock(en) {
    setLocked(prev => {
      const next = new Set(prev);
      const key = en.toLowerCase();
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
    setDirty(true);
  }

  function addTerm() {
    const en = newEn.trim();
    const vi = newVi.trim();
    if (!en || !vi) {
      setErrMsg('Nhập đủ cả tiếng Anh và tiếng Việt');
      return;
    }
    if (terms[en]) {
      setErrMsg(`Thuật ngữ "${en}" đã tồn tại — sửa trực tiếp ở bảng dưới.`);
      return;
    }
    setTerms(prev => ({ [en]: vi, ...prev }));
    const fld = newField.trim();
    if (fld) setFields(prev => ({ ...prev, [en.toLowerCase()]: fld }));
    setNewEn('');
    setNewVi('');
    setNewField('');
    setDirty(true);
    setErrMsg(null);
  }

  async function save() {
    setSaving(true);
    setErrMsg(null);
    try {
      const res = await apiFetch(endpoint, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          terms,
          enabled,
          locked: Array.from(locked),
          fields,
        }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
      }
      const data = await res.json();
      setTerms(data.terms || {});
      setLocked(new Set((data.locked || []).map(k => String(k).toLowerCase())));
      setFields(data.fields || {});
      setEnabled(data.enabled !== false);
      setDirty(false);
    } catch (err) {
      const msg = err.message || String(err);
      setErrMsg(msg);
      onError?.('Lưu glossary thất bại', msg);
    } finally {
      setSaving(false);
    }
  }

  function toggleEnabled() {
    setEnabled(v => !v);
    setDirty(true);
  }

  function togglePack(packId) {
    setSelectedPacks(prev => {
      const next = new Set(prev);
      if (next.has(packId)) next.delete(packId); else next.add(packId);
      return next;
    });
  }

  // Import the selected packs into this job's glossary. Backend merges with
  // first-wins so anything the user already has (incl. locked terms) is
  // preserved. Refresh local state from the response so the table updates
  // immediately without waiting for a save round-trip.
  async function importSelectedPacks() {
    if (!selectedPacks.size || importingPacks) return;
    setImportingPacks(true);
    setErrMsg(null);
    setPacksResult(null);
    try {
      const res = await apiFetch(
        `${API_URL}/api/pdf-translate/${jobId}/import-packs`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pack_ids: Array.from(selectedPacks) }),
        },
      );
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
      }
      const data = await res.json();
      setTerms(data.terms || {});
      setSelectedPacks(new Set());
      setPacksResult({ added: data.added, skipped: data.skipped, total: data.total });
      // Edits already persisted server-side; no extra save needed.
      setDirty(false);
    } catch (err) {
      const msg = err.message || String(err);
      setErrMsg(msg);
      onError?.('Nhập kho thuật ngữ thất bại', msg);
    } finally {
      setImportingPacks(false);
    }
  }

  // ── Bulk selection helpers ──────────────────────────────────────────────
  // Visible rows that aren't already in the kho — the universe a "Select all"
  // checkbox should reason about. Items already promoted are intentionally
  // excluded so the user can't waste a round-trip re-promoting them.
  const promotableVisible = useMemo(
    () => sortedEntries
      .filter(([en]) => !globalTerms.has(en.toLowerCase()))
      .map(([en]) => en),
    [sortedEntries, globalTerms],
  );

  const allVisibleSelected = useMemo(() => (
    promotableVisible.length > 0
    && promotableVisible.every(en => selected.has(en))
  ), [promotableVisible, selected]);

  const someVisibleSelected = useMemo(() => (
    !allVisibleSelected && promotableVisible.some(en => selected.has(en))
  ), [promotableVisible, selected, allVisibleSelected]);

  function toggleSelect(en) {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(en)) next.delete(en); else next.add(en);
      return next;
    });
    setBulkResult(null);
  }

  function toggleSelectAllVisible() {
    setBulkResult(null);
    if (allVisibleSelected) {
      // Deselect only the visible promotable rows — don't blow away off-screen picks.
      setSelected(prev => {
        const next = new Set(prev);
        promotableVisible.forEach(en => next.delete(en));
        return next;
      });
    } else {
      setSelected(prev => {
        const next = new Set(prev);
        promotableVisible.forEach(en => next.add(en));
        return next;
      });
    }
  }

  function clearSelection() {
    setSelected(new Set());
    setBulkResult(null);
  }

  // Bulk-promote everything currently selected. Hits the batch endpoint in
  // one round-trip so 100+ terms don't fan out into 100 HTTP calls.
  async function promoteSelectedToGlobal() {
    if (!selected.size || bulkSaving) return;
    // Drop anything that's been promoted since the user ticked it.
    const payload = {};
    const skipped = [];
    const fieldPayload = {};
    selected.forEach(en => {
      const vi = terms[en];
      if (vi === undefined) return;  // term deleted while selected
      if (globalTerms.has(en.toLowerCase())) {
        skipped.push(en);
        return;
      }
      payload[en] = vi;
      const fld = fields[en.toLowerCase()];
      if (fld) fieldPayload[en] = fld;
    });
    if (!Object.keys(payload).length) {
      setBulkResult({ added: 0, skipped: skipped.length, failed: 0 });
      return;
    }
    setBulkSaving(true);
    setErrMsg(null);
    try {
      const res = await apiFetch(`${API_URL}/api/pdf-translate/global-glossary/batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ terms: payload, fields: fieldPayload }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
      }
      const data = await res.json();
      // Mark successfully added terms as already-in-global so the UI flips
      // their badges immediately.
      setGlobalTerms(prev => {
        const next = new Set(prev);
        (data.added || []).forEach(k => next.add(String(k).toLowerCase()));
        return next;
      });
      setSelected(new Set());
      setBulkResult({
        added: data.added_count || 0,
        skipped: skipped.length,
        failed: data.failed_count || 0,
      });
    } catch (err) {
      const msg = err.message || String(err);
      setErrMsg(msg);
      onError?.('Lưu hàng loạt vào kho thất bại', msg);
    } finally {
      setBulkSaving(false);
    }
  }

  // Promote a single term to the cross-document "kho" so future jobs pre-seed
  // from it. Posts directly to the global-glossary endpoint; doesn't touch
  // this job's progress.json.
  async function promoteToGlobal(en, vi) {
    const key = en.toLowerCase();
    if (promoting.has(key) || globalTerms.has(key)) return;
    setPromoting(prev => new Set(prev).add(key));
    try {
      const res = await apiFetch(`${API_URL}/api/pdf-translate/global-glossary`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ en_term: en, vi_term: vi, field: fields[en.toLowerCase()] || null }),
      });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
      }
      setGlobalTerms(prev => new Set(prev).add(key));
    } catch (err) {
      const msg = err.message || String(err);
      setErrMsg(msg);
      onError?.('Lưu vào kho thất bại', msg);
    } finally {
      setPromoting(prev => {
        const next = new Set(prev);
        next.delete(key);
        return next;
      });
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────
  return (
    <div className="glossary-panel">
      <div className="glossary-panel-header">
        <span className="glossary-panel-title">
          Glossary thuật ngữ
          {dirty && <span className="glossary-dirty"> ● chưa lưu</span>}
        </span>
        <button className="glossary-panel-close" onClick={onClose}>✕</button>
      </div>

      {loading ? (
        <div className="glossary-loading">Đang tải...</div>
      ) : (
        <div className="glossary-content">
          {packs.length > 0 && (
            <div className={`glossary-packs ${packsOpen ? 'open' : ''}`}>
              <button
                type="button"
                className="glossary-packs-toggle"
                onClick={() => setPacksOpen(v => !v)}
              >
                <span className="glossary-packs-caret">{packsOpen ? '▾' : '▸'}</span>
                Nhập từ kho thuật ngữ chuyên ngành
                <span className="glossary-packs-hint">
                  ({packs.length} kho · không ghi đè thuật ngữ đã có)
                </span>
              </button>
              {packsOpen && (
                <div className="glossary-packs-body">
                  <div className="glossary-packs-list">
                    {packs.map(p => {
                      const checked = selectedPacks.has(p.id);
                      return (
                        <label key={p.id} className={`glossary-pack-card ${checked ? 'checked' : ''}`}>
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => togglePack(p.id)}
                          />
                          <div className="glossary-pack-meta">
                            <div className="glossary-pack-name">
                              {p.name}
                              <span className="glossary-pack-count">{p.term_count} thuật ngữ</span>
                            </div>
                            {p.description && (
                              <div className="glossary-pack-desc">{p.description}</div>
                            )}
                          </div>
                        </label>
                      );
                    })}
                  </div>
                  <div className="glossary-packs-actions">
                    <button
                      type="button"
                      className="btn-glossary-import"
                      onClick={importSelectedPacks}
                      disabled={!selectedPacks.size || importingPacks}
                    >
                      {importingPacks
                        ? 'Đang nhập...'
                        : `Nhập ${selectedPacks.size || ''} kho đã chọn`}
                    </button>
                    {packsResult && (
                      <span className="glossary-packs-result">
                        Đã thêm {packsResult.added} thuật ngữ mới
                        {packsResult.skipped ? ` (${packsResult.skipped} đã có, bỏ qua)` : ''}
                        {' '}· tổng {packsResult.total}
                      </span>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          <div className="glossary-toolbar">
            <label className="glossary-enable-toggle">
              <input type="checkbox" checked={enabled} onChange={toggleEnabled} />
              Bật glossary khi dịch
            </label>
            <input
              type="text"
              className="glossary-filter"
              placeholder="Lọc theo từ khóa..."
              value={filter}
              onChange={e => setFilter(e.target.value)}
            />
            <button
              className="btn-glossary-save"
              onClick={save}
              disabled={!dirty || saving}
            >
              {saving ? 'Đang lưu...' : 'Lưu thay đổi'}
            </button>
          </div>

          {errMsg && <div className="glossary-error">{errMsg}</div>}

          <div className="glossary-add-row">
            <input
              type="text"
              placeholder="Thuật ngữ tiếng Anh"
              value={newEn}
              onChange={e => setNewEn(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addTerm()}
            />
            <span className="glossary-arrow">→</span>
            <input
              type="text"
              placeholder="Bản dịch tiếng Việt"
              value={newVi}
              onChange={e => setNewVi(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addTerm()}
            />
            <input
              type="text"
              list="glossary-field-options"
              placeholder="Lĩnh vực (tùy chọn)"
              value={newField}
              onChange={e => setNewField(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && addTerm()}
            />
            <button className="btn-glossary-add" onClick={addTerm}>+ Thêm</button>
          </div>

          {/* Free-text lĩnh vực suggestions, sourced from the global "kho". */}
          <datalist id="glossary-field-options">
            {globalFields.map(f => <option key={f} value={f} />)}
          </datalist>

          {(promotableVisible.length > 0 || selected.size > 0) && (
            <div className="glossary-bulk-bar">
              <label className="glossary-bulk-selectall">
                <input
                  type="checkbox"
                  checked={allVisibleSelected}
                  ref={el => { if (el) el.indeterminate = someVisibleSelected; }}
                  onChange={toggleSelectAllVisible}
                  disabled={promotableVisible.length === 0}
                />
                {allVisibleSelected
                  ? `Bỏ chọn tất cả (${promotableVisible.length})`
                  : `Chọn tất cả ${filter ? 'kết quả lọc' : ''} (${promotableVisible.length})`}
              </label>
              <span className="glossary-bulk-count">
                Đã chọn: <b>{selected.size}</b>
              </span>
              <button
                type="button"
                className="btn-glossary-bulk-promote"
                onClick={promoteSelectedToGlobal}
                disabled={!selected.size || bulkSaving}
                title="Lưu các thuật ngữ đã tick vào kho dùng chung trong một lần gọi"
              >
                {bulkSaving
                  ? 'Đang lưu...'
                  : `⬆ Lưu ${selected.size || ''} vào kho`}
              </button>
              {selected.size > 0 && !bulkSaving && (
                <button
                  type="button"
                  className="btn-glossary-bulk-clear"
                  onClick={clearSelection}
                >
                  Bỏ chọn
                </button>
              )}
              {bulkResult && (
                <span className="glossary-bulk-result">
                  Đã thêm {bulkResult.added}
                  {bulkResult.skipped ? ` · bỏ qua ${bulkResult.skipped}` : ''}
                  {bulkResult.failed ? ` · lỗi ${bulkResult.failed}` : ''}
                </span>
              )}
            </div>
          )}

          <div className="glossary-table-wrap">
            <table className="glossary-table">
              <thead>
                <tr>
                  <th style={{ width: 36 }}>
                    <input
                      type="checkbox"
                      checked={allVisibleSelected}
                      ref={el => { if (el) el.indeterminate = someVisibleSelected; }}
                      onChange={toggleSelectAllVisible}
                      disabled={promotableVisible.length === 0}
                      title={allVisibleSelected ? 'Bỏ chọn tất cả' : 'Chọn tất cả (chưa có trong kho)'}
                    />
                  </th>
                  <th style={{ width: 40 }}></th>
                  <th>Tiếng Anh</th>
                  <th>Tiếng Việt</th>
                  <th style={{ width: 150 }}>Lĩnh vực</th>
                  <th style={{ width: 110 }}>Kho chung</th>
                  <th style={{ width: 60 }}></th>
                </tr>
              </thead>
              <tbody>
                {sortedEntries.length === 0 ? (
                  <tr><td colSpan="7" className="glossary-empty">
                    {filter ? 'Không khớp với bộ lọc.' : 'Chưa có thuật ngữ nào.'}
                  </td></tr>
                ) : (
                  sortedEntries.map(([en, vi]) => {
                    const isLocked = locked.has(en.toLowerCase());
                    const enKey = en.toLowerCase();
                    const inGlobal = globalTerms.has(enKey);
                    const isPromoting = promoting.has(enKey);
                    const isSelected = selected.has(en);
                    const isSelectable = !inGlobal;
                    return (
                      <tr key={en} className={`${isLocked ? 'glossary-row-locked' : ''}${isSelected ? ' glossary-row-selected' : ''}`}>
                        <td>
                          <input
                            type="checkbox"
                            checked={isSelected}
                            onChange={() => toggleSelect(en)}
                            disabled={!isSelectable}
                            title={inGlobal ? 'Đã có trong kho' : 'Tick để lưu vào kho theo lô'}
                          />
                        </td>
                        <td>
                          <button
                            className="btn-glossary-lock"
                            onClick={() => toggleLock(en)}
                            title={isLocked ? 'Bỏ khóa — Gemini có thể đề xuất bản dịch khác' : 'Khóa — bản dịch này luôn được dùng, không bị ghi đè'}
                          >
                            {isLocked ? '🔒' : '🔓'}
                          </button>
                        </td>
                        <td className="glossary-en">{en}</td>
                        <td>
                          <input
                            type="text"
                            className="glossary-vi-input"
                            value={vi}
                            onChange={e => updateVi(en, e.target.value)}
                          />
                        </td>
                        <td>
                          <input
                            type="text"
                            list="glossary-field-options"
                            className="glossary-field-input"
                            placeholder="—"
                            value={fields[enKey] || ''}
                            onChange={e => updateField(en, e.target.value)}
                            title="Lĩnh vực chuyên ngành (tự do nhập)"
                          />
                        </td>
                        <td>
                          {inGlobal ? (
                            <span
                              className="glossary-in-global"
                              title="Đã có trong kho dùng chung — các job sau sẽ tự động dùng"
                            >
                              ✓ Có sẵn
                            </span>
                          ) : (
                            <button
                              className="btn-glossary-promote"
                              onClick={() => promoteToGlobal(en, vi)}
                              disabled={isPromoting}
                              title="Lưu cặp thuật ngữ này vào kho dùng chung — các job dịch sau sẽ tự động pre-seed"
                            >
                              {isPromoting ? '...' : '⬆ Lưu vào kho'}
                            </button>
                          )}
                        </td>
                        <td>
                          <button
                            className="btn-glossary-del"
                            onClick={() => deleteTerm(en)}
                            title="Xóa thuật ngữ này"
                          >
                            ✕
                          </button>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>

          <div className="glossary-stats">
            <span>Tổng: {Object.keys(terms).length} thuật ngữ</span>
            <span>Đã khóa: {locked.size}</span>
            <span>
              Trong kho:{' '}
              {Object.keys(terms).filter(k => globalTerms.has(k.toLowerCase())).length}
            </span>
            {filter && <span>Hiển thị: {sortedEntries.length}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
