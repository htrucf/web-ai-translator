# Prototype Hybrid (userscript ↔ bridge server)

Bản thử **độc lập** để kiểm chứng hướng đi mới: thay vì lái browser qua CDP/Playwright
(bị Cloudflare/Copilot chặn vì `Runtime.enable`), ta để **người dùng tự mở tab thật đã đăng
nhập**, một **userscript Tampermonkey** chạy trong tab đóng vai *executor* — nhận job từ bridge
server, điền prompt, đợi trả lời, scrape kết quả gửi về.

> Vì userscript dùng `GM_xmlhttpRequest` (chạy ở context extension, bỏ qua CORS/CSP/mixed-content)
> và **không** có lớp automation-protocol nào, tab trông y hệt người dùng thật → tránh được vector
> dò bot mà bản CDP đang dính.

Bản gốc (Playwright, `backend/app`) **không bị đụng tới**. Prototype chạy ở cổng riêng **8765**.

## Thành phần
| File | Vai trò |
|------|---------|
| `bridge_server.py` | FastAPI standalone: hàng đợi job + long-poll + ghi log đo lường |
| `executor.user.js` | Userscript cho 6 trang: ChatGPT, Gemini, AI Studio, DeepSeek, Grok, Copilot |
| `test_client.py` | CLI submit prompt / benchmark song song (chỉ dùng stdlib) |
| `logs/events.jsonl` | Log sự kiện (created/claimed/done/error) — dữ liệu cho chương kết quả DATN |

## Chạy thử

### 1. Khởi động bridge server
```bash
# Tu thu muc goc repo (dung venv cua backend)
web-ai-translator/backend/venv312/Scripts/python.exe web-ai-translator/prototype_hybrid/bridge_server.py
```
Mở http://localhost:8765/ để xem trang trạng thái (job + worker, tự refresh 3s).

### 2. Cài userscript
1. Cài tiện ích **Tampermonkey** (Chrome/Edge/Firefox).
2. Tampermonkey → *Create a new script* → dán toàn bộ `executor.user.js` → Save.
   (Nếu bridge chạy ở máy/cổng khác, sửa hằng `BRIDGE_URL` đầu file.)
3. Mở và **đăng nhập** một (hoặc nhiều) trang AI được hỗ trợ:
   - `chatgpt.com` · `gemini.google.com` · `aistudio.google.com` · `chat.deepseek.com` ·
     `grok.com` · `copilot.microsoft.com`
   Góc dưới phải mỗi tab sẽ hiện overlay *“Hybrid executor · <tên backend>”* trạng thái `idle`.

### 3. Gửi job
```bash
# 1 job toi ChatGPT
web-ai-translator/backend/venv312/Scripts/python.exe web-ai-translator/prototype_hybrid/test_client.py \
    --prompt "Dich sang tieng Viet: Hello world." --backend chatgpt

# 1 job toi Copilot
web-ai-translator/backend/venv312/Scripts/python.exe web-ai-translator/prototype_hybrid/test_client.py --backend copilot
```
Worker trong tab sẽ điền prompt, đợi trả lời, gửi kết quả về; CLI in bản dịch + thời gian.

### 4. Test chạy song song
Mở **nhiều tab/cửa sổ** (mỗi tab là 1 worker — xem mục dưới), rồi:
```bash
web-ai-translator/backend/venv312/Scripts/python.exe web-ai-translator/prototype_hybrid/test_client.py \
    --benchmark 6 --backend any
```
CLI gửi 6 job cùng lúc; quan sát các worker chia tải, in throughput. Chi tiết timeline ở
`logs/events.jsonl`.

## Muốn N worker *thật sự* song song
- Rate-limit tính theo **account**, không theo tab → 3 tab cùng account chỉ chia chung quota.
- Muốn nhân năng lực thật:
  - **3 account cùng ChatGPT** → 3 **profile/cửa sổ Chrome** riêng (Chrome → *Add profile*), mỗi
    profile cài Tampermonkey + đăng nhập 1 account; hoặc
  - **các AI khác nhau** (1 tab ChatGPT + 1 tab Copilot) → khác domain, phiên độc lập.
- Nên cho **mỗi worker một cửa sổ riêng** (không nhồi nhiều tab 1 cửa sổ) để tránh trình duyệt bóp
  JS của tab nền (*background-tab throttling*).

## Giới hạn đã biết
- **Điền prompt ChatGPT (ProseMirror):** dùng `execCommand('insertText')`; prompt nhiều dòng có thể
  cần tinh chỉnh.
- **Selector Copilot mềm** (`cib-*`, UI hay đổi): nếu không bắt được trả lời, mở DevTools xem DOM
  thật rồi chỉnh mảng selector trong `executor.user.js`.
- Cần giữ tab mở; chưa nối pipeline PDF/agent (đây chỉ là bản kiểm chứng cơ chế).
