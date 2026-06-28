// ==UserScript==
// @name         Hybrid Executor (ChatGPT/Gemini/AIStudio/DeepSeek/Grok/Copilot)
// @namespace    web-ai-translator.prototype
// @version      0.3.0
// @description  Userscript executor — poll bridge server, dien prompt vao tab AI that, scrape ket qua tra ve. Tu mo chat moi sau moi X prompt (tranh context phinh). Tranh hoan toan CDP/Runtime.enable nen khong bi chan bot.
// @match        https://chatgpt.com/*
// @match        https://gemini.google.com/*
// @match        https://aistudio.google.com/*
// @match        https://chat.deepseek.com/*
// @match        https://grok.com/*
// @match        https://copilot.microsoft.com/*
// @grant        GM_xmlhttpRequest
// @connect      localhost
// @connect      127.0.0.1
// @run-at       document-idle
// ==/UserScript==

/*
 * Cach dung: cai Tampermonkey -> them script nay -> chay bridge_server.py ->
 * mo & dang nhap trang AI bat ky trong danh sach @match. Overlay goc duoi phai
 * cho biet trang thai. Doi job tu test_client.py.
 *
 * Neu bridge chay o may/cong khac, doi BRIDGE_URL ben duoi.
 *
 * Moi backend khai bao:
 *   input   : selector o nhap (uu tien tu tren xuong)
 *   send    : selector nut gui (de click)
 *   stop    : selector hien khi DANG generate (visible => chua xong)
 *   done    : selector hien khi DA xong (visible => xong). Bo trong => dung 'send'.
 *   response: selector cac luot tra loi (count + scrape last)
 *   newChat : selector nut 'New chat' (de bam mo cuoc tro chuyen moi)
 *   submitKey: phim gui khi khong bam duoc nut ('Enter' | 'Control+Enter')
 * Selector port thang tu backend/app/services/translator.py.
 */
(function () {
  'use strict';

  // Dung 127.0.0.1 (KHONG 'localhost') de tranh truong hop Windows phan giai
  // localhost -> IPv6 ::1 trong khi bridge bind IPv4 127.0.0.1 -> network error.
  const BRIDGE_URL = 'http://127.0.0.1:8765';
  // Cu X prompt thi tu bam 'New chat' mot lan (tranh context phinh tren tai lieu
  // dai, van giu nhat quan thuat ngu trong tung cum). 0 = khong bao gio reset.
  const NEW_CHAT_EVERY = 6;

  const BACKENDS = {
    'chatgpt.com': {
      key: 'chatgpt',
      input: [
        '#prompt-textarea',
        'div[contenteditable="true"][data-id="root"]',
        'textarea[placeholder]',
      ],
      send: [
        'button[data-testid="send-button"]',
        'button[aria-label="Send prompt"]',
        'button[aria-label="Send message"]',
      ],
      stop: [
        'button[aria-label="Stop streaming"]',
        'button[data-testid="stop-button"]',
      ],
      done: [],
      response: [
        'article[data-testid^="conversation-turn-"][data-testid$="-assistant"]',
        'div[data-message-author-role="assistant"]',
        '.agent-turn',
      ],
      newChat: [
        'a[aria-label="New chat"]',
        'button[aria-label="New chat"]',
        'a[href="/"]',
      ],
      submitKey: 'Enter',
    },

    'gemini.google.com': {
      key: 'gemini',
      input: [
        'div.ql-editor[role="textbox"]',
        'div[contenteditable="true"][role="textbox"]',
      ],
      send: [
        'button.send-button',
        'button[aria-label="Send message"]',
      ],
      stop: [
        'button[aria-label="Stop response"]',
        'button[aria-label="Stop"]',
        'button.stop-button',
        'button[mattooltip="Stop response"]',
        'mat-progress-bar',
        '.loading-indicator',
        '.streaming',
      ],
      // Gemini: nut Send hien lai HOAC o nhap san sang -> xong
      done: [
        'button.send-button',
        'button[aria-label="Send message"]',
        'div.ql-editor[role="textbox"]',
      ],
      response: [
        'message-content.model-response',
        '.model-response-text',
        'model-response',
        '.response-container',
      ],
      newChat: [
        'button[aria-label="New chat"]',
        'button[aria-label*="New chat"]',
        'a[aria-label*="New chat"]',
      ],
      submitKey: 'Control+Enter',
    },

    'aistudio.google.com': {
      key: 'aistudio',
      input: [
        'textarea[placeholder]',
        'textarea',
        'div[contenteditable="true"]',
      ],
      send: [
        'button[aria-label*="Run"]',
        'button[aria-label*="Send"]',
        'button[type="submit"]',
      ],
      stop: [
        'button[aria-label*="Stop"]',
        'button[aria-label*="Cancel"]',
      ],
      done: [
        'button[aria-label*="Run"]',
        'button[aria-label*="Send"]',
      ],
      response: [
        'ms-chat-turn',
        'div[class*="model-response"]',
        'div[class*="response"]',
        '[class*="markdown"]',
        'article',
      ],
      newChat: [
        'a[href*="new_chat"]',
        'button[aria-label*="New chat"]',
        'button[aria-label*="New"]',
      ],
      submitKey: 'Control+Enter',
    },

    'chat.deepseek.com': {
      key: 'deepseek',
      input: [
        'textarea#chat-input',
        'textarea[placeholder]',
        'div[contenteditable="true"]',
      ],
      send: [
        'div[role="button"][aria-label*="end"]',
        'button[type="submit"]',
      ],
      stop: [
        'div[role="button"][aria-label="Stop"]',
        'div[aria-label="Stop generating"]',
        'button[aria-label*="Stop"]',
      ],
      // DeepSeek: toolbar copy/regenerate duoi response -> xong (nut send kho bat)
      done: [
        'div[class*="ds-icon-button"]',
        'div[class*="_toolbar"]',
      ],
      response: [
        '.ds-markdown',
        'div[class*="ds-markdown"]',
        'div[class*="_assistant"]',
      ],
      newChat: [
        'div[class*="_new"][role="button"]',
        'div[class*="new-chat"]',
        'a[href="/"]',
      ],
      submitKey: 'Enter',
    },

    'grok.com': {
      key: 'grok',
      input: [
        'textarea[placeholder]',
        'textarea',
        'div[contenteditable="true"]',
      ],
      send: [
        'button[aria-label*="Send"]',
        'button[type="submit"]',
        'button[data-testid*="send"]',
      ],
      stop: [
        'button[aria-label*="Stop"]',
        'button[data-testid*="stop"]',
        '[aria-label*="Stop generating"]',
      ],
      done: [],
      response: [
        '[data-testid*="message"]',
        '[class*="message"] [class*="markdown"]',
        '[class*="response"]',
        'article',
      ],
      newChat: [
        'a[href="/"]',
        'button[aria-label*="New"]',
        'a[aria-label*="New"]',
      ],
      submitKey: 'Enter',
    },

    'copilot.microsoft.com': {
      key: 'copilot',
      input: [
        'textarea[placeholder]',
        'textarea',
        'div[contenteditable="true"]',
        'cib-text-input textarea',
      ],
      send: [
        'button[aria-label*="Submit"]',
        'button[aria-label*="Send"]',
        'button[type="submit"]',
      ],
      stop: [
        'button[aria-label*="Stop"]',
        'button[aria-label*="Cancel"]',
      ],
      done: [],
      response: [
        'cib-message',
        '[data-content="ai-message"]',
        '[class*="ac-container"]',
        '[class*="markdown"]',
        'article',
      ],
      newChat: [
        'button[aria-label*="New topic"]',
        'button[aria-label*="New"]',
        'a[aria-label*="New"]',
        'a[href="/"]',
      ],
      submitKey: 'Enter',
    },
  };

  // Normalize host (bo www.) de match BACKENDS
  const HOST = location.hostname.replace(/^www\./, '');
  const BK = BACKENDS[HOST];
  if (!BK) {
    console.log('[hybrid] hostname khong ho tro:', location.hostname);
    return;
  }
  // 'done' rong -> dung 'send' lam tin hieu xong
  const DONE_SELECTORS = (BK.done && BK.done.length) ? BK.done : BK.send;

  // worker_id on dinh theo tab (giu qua reload)
  const WORKER_ID = (function () {
    let id = sessionStorage.getItem('hybrid_worker_id');
    if (!id) {
      id = BK.key + '-' + Math.random().toString(36).slice(2, 8);
      sessionStorage.setItem('hybrid_worker_id', id);
    }
    return id;
  })();

  let running = true;
  let jobsDone = 0;

  // ── HTTP qua GM_xmlhttpRequest (bo qua CORS/CSP/mixed-content) ─────────────
  function gmRequest(method, url, data) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method,
        url,
        headers: { 'Content-Type': 'application/json' },
        data: data ? JSON.stringify(data) : undefined,
        timeout: 60000, // > LONGPOLL_SECONDS cua server (25s)
        onload: (r) => {
          if (r.status >= 200 && r.status < 300) {
            try { resolve(r.responseText ? JSON.parse(r.responseText) : {}); }
            catch (e) { resolve({}); }
          } else {
            reject(new Error('HTTP ' + r.status));
          }
        },
        onerror: () => reject(new Error('network error')),
        ontimeout: () => reject(new Error('timeout')),
      });
    });
  }

  // ── DOM helpers ───────────────────────────────────────────────────────────
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function isVisible(el) {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && el.offsetParent !== null;
  }

  function pick(selList) {
    for (const sel of selList) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function pickVisible(selList) {
    for (const sel of selList) {
      for (const el of document.querySelectorAll(sel)) {
        if (isVisible(el)) return el;
      }
    }
    return null;
  }

  function anyVisible(selList) {
    for (const sel of selList) {
      for (const el of document.querySelectorAll(sel)) {
        if (isVisible(el)) return true;
      }
    }
    return false;
  }

  function countResponses() {
    for (const sel of BK.response) {
      const els = document.querySelectorAll(sel);
      if (els.length) return els.length;
    }
    return 0;
  }

  function getLastText() {
    for (const sel of BK.response) {
      const els = document.querySelectorAll(sel);
      if (els.length) return (els[els.length - 1].innerText || '').trim();
    }
    return '';
  }

  function isDone() {
    // Dang generate (stop/loading hien) -> chua xong
    if (anyVisible(BK.stop)) return false;
    // Tin hieu xong (nut send hien lai / toolbar / o nhap san sang)
    if (anyVisible(DONE_SELECTORS)) return true;
    return false;
  }

  function fillPrompt(text) {
    const input = pick(BK.input);
    if (!input) throw new Error('khong tim thay o nhap');
    input.focus();

    if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
      // Set qua native setter de framework (React/Angular) nhan onChange
      const proto = input.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      setter.call(input, text);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return;
    }

    // contenteditable (ProseMirror cua ChatGPT / Quill cua Gemini):
    // execCommand kich dung beforeinput/input ma editor mong doi
    try { document.execCommand('selectAll', false, null); } catch (e) {}
    try { document.execCommand('delete', false, null); } catch (e) {}
    const ok = document.execCommand('insertText', false, text);
    if (!ok) {
      // Fallback: set textContent (KHONG dung innerHTML -> tranh Trusted Types)
      input.dispatchEvent(new InputEvent('beforeinput', {
        bubbles: true, cancelable: true, inputType: 'insertText', data: text,
      }));
      input.textContent = text;
      input.dispatchEvent(new InputEvent('input', {
        bubbles: true, inputType: 'insertText', data: text,
      }));
    }
  }

  function pressSubmitKey() {
    const input = pick(BK.input);
    if (!input) return;
    const ctrl = BK.submitKey === 'Control+Enter';
    const ev = {
      bubbles: true, cancelable: true,
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, ctrlKey: ctrl,
    };
    input.dispatchEvent(new KeyboardEvent('keydown', ev));
    input.dispatchEvent(new KeyboardEvent('keyup', ev));
  }

  function clickSend() {
    const btn = pickVisible(BK.send);
    if (btn && !btn.disabled) { btn.click(); return; }
    pressSubmitKey(); // fallback theo submitKey cua backend
  }

  // ── New chat rotation (cu X prompt mot lan) ───────────────────────────────
  let lastResetCount = 0;

  async function startNewChat() {
    const btn = pickVisible(BK.newChat || []);
    if (!btn) return false;          // khong tim thay nut -> giu chat cu
    try { btn.click(); } catch (e) { return false; }
    await sleep(1500);               // cho UI mo cuoc tro chuyen moi
    return true;
  }

  // Goi TRUOC khi nhan job moi (pre-claim): neu New Chat lam reload trang thi
  // chua co job nao bi bo do. Guard lastResetCount de khong reset lap khi idle.
  async function maybeNewChat() {
    if (NEW_CHAT_EVERY <= 0) return;
    if (jobsDone > 0 && jobsDone !== lastResetCount &&
        jobsDone % NEW_CHAT_EVERY === 0) {
      setStatus('mo chat moi (sau ' + jobsDone + ' prompt)');
      const ok = await startNewChat();
      lastResetCount = jobsDone;     // danh dau da xu ly nguong nay
      if (ok) await sleep(500);
    }
  }

  // ── Vong doi 1 job (port _send_prompt_and_get_response) ───────────────────
  async function waitUntil(fn, timeoutMs, intervalMs, errMsg) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try { if (fn()) return true; } catch (e) {}
      await sleep(intervalMs);
    }
    throw new Error(errMsg || 'timeout');
  }

  async function waitForDone(timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      if (isDone()) {
        await sleep(2000);            // xac nhan 2 lan cach nhau 2s
        if (isDone()) return true;
      }
      await sleep(2000);
    }
    throw new Error('response khong hoan thanh trong gio');
  }

  async function processJob(job) {
    const tStart = Date.now();
    const before = countResponses();

    fillPrompt(job.prompt);
    await sleep(400);
    clickSend();
    const tSent = Date.now();

    // Giai doan 1: cho response xuat hien
    await waitUntil(() => countResponses() > before, 90000, 1000,
      'response khong xuat hien sau 90s');
    const tAppear = Date.now();
    await sleep(2500); // cho text bat dau on dinh

    // Giai doan 2: cho generate xong
    await waitForDone(240000);
    await sleep(500);

    const text = getLastText();
    if (!text) throw new Error('scrape duoc chuoi rong');

    return {
      text,
      timings: {
        send_ms: tSent - tStart,
        appear_ms: tAppear - tSent,
        generate_ms: Date.now() - tAppear,
        total_ms: Date.now() - tStart,
      },
    };
  }

  // ── Overlay quan sat ──────────────────────────────────────────────────────
  function makeOverlay() {
    const box = document.createElement('div');
    box.id = 'hybrid-overlay';
    Object.assign(box.style, {
      position: 'fixed', right: '12px', bottom: '12px', zIndex: 2147483647,
      background: 'rgba(20,20,20,.92)', color: '#eee',
      font: '12px system-ui, Segoe UI, Arial', padding: '10px 12px',
      borderRadius: '8px', maxWidth: '300px',
      boxShadow: '0 2px 10px rgba(0,0,0,.4)',
    });

    const title = document.createElement('div');
    title.textContent = 'Hybrid executor · ' + BK.key;
    Object.assign(title.style, { fontWeight: '700', marginBottom: '4px' });

    const status = document.createElement('div');
    status.id = 'hybrid-status';
    status.textContent = 'khoi dong...';

    const meta = document.createElement('div');
    meta.textContent = WORKER_ID;
    Object.assign(meta.style, { opacity: '.65', marginTop: '4px', fontSize: '11px' });

    const btn = document.createElement('button');
    btn.textContent = 'Tat poll';
    Object.assign(btn.style, {
      marginTop: '8px', cursor: 'pointer', fontSize: '12px', padding: '3px 8px',
    });
    btn.onclick = () => {
      running = !running;
      btn.textContent = running ? 'Tat poll' : 'Bat poll';
      if (running) loop();
    };

    box.append(title, status, meta, btn);
    document.body.appendChild(box);
  }

  function setStatus(s) {
    const el = document.getElementById('hybrid-status');
    if (el) el.textContent = s + '  · da xong: ' + jobsDone;
  }

  // ── Vong lap chinh ────────────────────────────────────────────────────────
  async function loop() {
    while (running) {
      await maybeNewChat();   // xoay vong chat truoc khi nhan job (an toan)
      let job = null;
      try {
        const url = BRIDGE_URL + '/jobs/next?worker_id='
          + encodeURIComponent(WORKER_ID) + '&backend=' + BK.key;
        const res = await gmRequest('GET', url);
        job = res && res.job_id ? res : null;
      } catch (e) {
        setStatus('loi poll: ' + (e.message || e));
        await sleep(3000);
        continue;
      }

      if (!job) { setStatus('idle (cho job...)'); continue; }

      setStatus('dang xu ly ' + job.job_id);
      try {
        const out = await processJob(job);
        await gmRequest('POST', BRIDGE_URL + '/jobs/' + job.job_id + '/result',
          { text: out.text, timings: out.timings });
        jobsDone++;
        setStatus('xong ' + job.job_id + ' (' + out.text.length + ' ky tu)');
      } catch (e) {
        const msg = String((e && e.message) || e);
        try {
          await gmRequest('POST', BRIDGE_URL + '/jobs/' + job.job_id + '/result',
            { error: msg });
        } catch (e2) {}
        setStatus('LOI job ' + job.job_id + ': ' + msg);
      }
    }
  }

  makeOverlay();
  setStatus('idle (cho job...)');
  loop();
  console.log('[hybrid] executor khoi dong:', WORKER_ID, '->', BRIDGE_URL);
})();
