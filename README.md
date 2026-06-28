# Web AI Translator - gói công bố mã nguồn và thực nghiệm

Gói này chứa mã nguồn hệ thống, dữ liệu thử nghiệm và kết quả thực nghiệm dùng trong đồ án.

## Cấu trúc

- `code/web-ai-translator/`: mã nguồn hệ thống, gồm backend, frontend, cấu hình Docker, benchmark và test.
- `data/original_files/`: các tài liệu thử nghiệm gốc.
- `data/Danh_sach_tai_lieu.xlsx`: danh sách tài liệu dùng trong thực nghiệm.
- `results/result_each/`: kết quả dịch theo từng tài liệu/phương án.
- `results/extracted_text/`: văn bản trích xuất từ tài liệu gốc và kết quả, phục vụ đối chiếu chất lượng.
- `results/benchmarks/`: kết quả benchmark kỹ thuật.
- `results/bang_so_sanh_chat_luong.md`: bảng so sánh chất lượng tổng hợp.
- `docs/DoAn.pdf`: bản PDF đồ án tại thời điểm đóng gói.

## Kho lưu trữ

<https://github.com/htrucf/web-ai-translator>

## Gợi ý push lên GitHub

```powershell
cd github_release\web-ai-translator-github-package
git init
git add .
git commit -m "Publish thesis source code and experiment artifacts"
git branch -M main
git remote add origin https://github.com/htrucf/web-ai-translator.git
git push -u origin main
```
