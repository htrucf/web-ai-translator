import { useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { Document, Page, pdfjs } from 'react-pdf';
import 'react-pdf/dist/Page/AnnotationLayer.css';
import 'react-pdf/dist/Page/TextLayer.css';

pdfjs.GlobalWorkerOptions.workerSrc = `//unpkg.com/pdfjs-dist@${pdfjs.version}/build/pdf.worker.min.mjs`;

// Build the file prop for react-pdf's <Document>.
// String URLs to our protected backend get the bearer token attached;
// blob:/data: URLs and pre-built {url, httpHeaders} objects pass through.
function buildFile(file) {
  if (!file) return null;
  if (typeof file !== 'string') return file;
  if (file.startsWith('blob:') || file.startsWith('data:')) return file;
  const token = localStorage.getItem('auth_token');
  if (!token) return file;
  return { url: file, httpHeaders: { Authorization: `Bearer ${token}` }, withCredentials: false };
}

export default function PdfViewer({
  file, title, placeholder, scrollRef, syncScroll, onSyncScroll,
  chunkBlockMap, onBlockClick,
}) {
  const fileProp = useMemo(() => buildFile(file), [file]);
  const [numPages, setNumPages] = useState(null);
  const [scale, setScale] = useState(1.0);
  const containerRef = useRef(null);
  const isExternalScroll = useRef(false);

  // Group chunk_block_map entries by page once per map change so the overlay
  // doesn't re-flatten on every render.
  const blocksByPage = useMemo(() => {
    const map = new Map();
    if (!chunkBlockMap?.chunks) return map;
    chunkBlockMap.chunks.forEach((blocks, chunkIdx) => {
      blocks.forEach(b => {
        if (!map.has(b.page)) map.set(b.page, []);
        map.get(b.page).push({ chunkIdx, bbox: b.bbox });
      });
    });
    return map;
  }, [chunkBlockMap]);

  // Expose imperative scroll handle to parent via scrollRef.
  // Parent calls `scrollRef.current?.scrollToPercent(p)` — both pieces must
  // live on `.current`, otherwise the optional-chaining call is a no-op.
  useEffect(() => {
    if (!scrollRef) return;
    scrollRef.current = {
      scrollToPercent: (percent) => {
        const el = containerRef.current;
        if (!el) return;
        const maxScroll = el.scrollHeight - el.clientHeight;
        if (maxScroll <= 0) return;
        isExternalScroll.current = true;
        el.scrollTop = percent * maxScroll;
      },
    };
    return () => {
      if (scrollRef.current && scrollRef.current.scrollToPercent) {
        scrollRef.current = null;
      }
    };
  }, [scrollRef]);

  function onDocumentLoadSuccess({ numPages }) {
    setNumPages(numPages);
  }

  const handleScroll = useCallback(() => {
    // If this scroll was triggered programmatically by the other panel, skip
    if (isExternalScroll.current) {
      isExternalScroll.current = false;
      return;
    }
    if (!syncScroll || !onSyncScroll) return;

    const el = containerRef.current;
    if (!el) return;
    const maxScroll = el.scrollHeight - el.clientHeight;
    if (maxScroll <= 0) return;

    const percentage = el.scrollTop / maxScroll;
    onSyncScroll(percentage);
  }, [syncScroll, onSyncScroll]);

  return (
    <div className="pdf-viewer">
      <div className="pdf-header">
        <h3>{title}</h3>
        <div className="pdf-controls">
          <button onClick={() => setScale(s => Math.max(0.5, s - 0.1))}>-</button>
          <span>{Math.round(scale * 100)}%</span>
          <button onClick={() => setScale(s => Math.min(2.5, s + 0.1))}>+</button>
          <span className="page-count">{numPages ? `${numPages} trang` : ''}</span>
        </div>
      </div>
      <div className="pdf-content" ref={containerRef} onScroll={handleScroll}>
        {fileProp ? (
          <Document file={fileProp} onLoadSuccess={onDocumentLoadSuccess} loading="Đang tải PDF...">
            {numPages && Array.from({ length: numPages }, (_, i) => {
              const pageSize = chunkBlockMap?.page_sizes?.[i];
              const pageBlocks = blocksByPage.get(i);
              const showOverlay = pageSize && pageBlocks?.length && onBlockClick;
              return (
                <div key={i + 1} className="pdf-page-wrap">
                  <Page pageNumber={i + 1} scale={scale} />
                  {showOverlay && (
                    <div className="pdf-block-overlay">
                      {pageBlocks.map((b, idx) => (
                        <button
                          key={idx}
                          type="button"
                          className="pdf-block-hit"
                          title={`Đoạn #${b.chunkIdx + 1} — bấm để mở trong Lịch sử dịch`}
                          style={{
                            left: `${(b.bbox[0] / pageSize.width) * 100}%`,
                            top: `${(b.bbox[1] / pageSize.height) * 100}%`,
                            width: `${((b.bbox[2] - b.bbox[0]) / pageSize.width) * 100}%`,
                            height: `${((b.bbox[3] - b.bbox[1]) / pageSize.height) * 100}%`,
                          }}
                          onClick={() => onBlockClick(b.chunkIdx)}
                        />
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </Document>
        ) : (
          <div className="pdf-empty">{placeholder || 'Chưa có file PDF'}</div>
        )}
      </div>
    </div>
  );
}
