# Bảng so sánh chất lượng bản dịch trong thư mục `results`

## Cách đọc điểm

Thang điểm: 1 = rất yếu, 2 = yếu, 3 = trung bình, 4 = tốt, 5 = rất tốt.

Các tiêu chí:

- `Đầy đủ nội dung`: mức độ giữ đủ nội dung so với file gốc, không tóm tắt, không mất trang/đoạn.
- `Nhất quán thuật ngữ`: mức độ dùng thuật ngữ ổn định, đúng ngữ cảnh chuyên ngành.
- `Câu tự nhiên`: độ trôi chảy của tiếng Việt, không dịch máy thô, không dính chữ, không lủng củng.
- `Giữ cấu trúc trình bày ban đầu`: mức độ giữ số trang, bố cục, bảng, công thức, danh sách, heading, thứ tự nội dung.

## Bảng đánh giá chính

| File gốc | File kết quả | Đầy đủ nội dung | Nhất quán thuật ngữ | Câu tự nhiên | Giữ cấu trúc trình bày ban đầu | Nhận xét ngắn |
|---|---|---:|---:|---:|---:|---|
| `Attention Is All You Need.pdf` | `attention-aistudio.pdf` | 2.0 | 3.5 | 3.0 | 2.0 | Chỉ có 3 trang so với 15 trang gốc nên thiếu phần lớn nội dung. Phần đã dịch dùng thuật ngữ tương đối đúng nhưng bố cục và độ bao phủ không đạt. |
| `Attention Is All You Need.pdf` | `Attention_Is_All_You_Need_VI-gpt.pdf` | 4.5 | 4.0 | 3.5 | 4.0 | Đủ 15/15 trang, nội dung chính được dịch khá đầy đủ. Thuật ngữ như Transformer, attention, encoder/decoder được giữ/giải thích tương đối ổn. Trừ điểm vì nhiều chỗ text layer bị dính chữ như `hiệu năng`, `bộ mã hóa`, `cơ chế chú ý` khi trích xuất, làm câu kém tự nhiên. |
| `Attention Is All You Need.pdf` | `AttentionIsAllYouNeed_vi-latexgpt.pdf` | 4.5 | 4.0 | 3.5 | 4.5 | Đủ 15/15 trang, cấu trúc học thuật và hình/bảng được giữ tốt hơn bản PDF GPT thông thường. Tên bài được Việt hóa. Còn pha trộn một số thuật ngữ Anh như encoder, decoder, ensemble, codebase; câu vẫn có hiện tượng dính khoảng trắng ở bản PDF trích xuất. |
| `AttentionIsAllYouNeed.tar.gz` / `Attention Is All You Need.pdf` | `attention_is_all_you_need_vi_source-gpt.zip` | 4.5 | 4.0 | 4.0 | 4.0 | Source LaTeX tiếng Việt có cấu trúc rõ, giữ công thức, hình, bảng và lệnh LaTeX tương đối tốt. Vì đây là source chứ không phải bản đọc cuối cùng nên tiêu chí trình bày chỉ đánh giá ở mức cấu trúc mã nguồn. |
| `e_mc2.pdf` | `e_mc2-ggtranslate.pdf` | 3.0 | 2.5 | 2.5 | 1.5 | Có đủ 3 trang nhưng thứ tự đọc bị đảo/lộn mạnh, có dòng `Machine Translated by Google`, ký hiệu công thức bị lỗi và nhiều đoạn rời rạc. Nội dung còn nhận ra được nhưng cấu trúc trình bày rất kém. |
| `e_mc2.pdf` | `e_mc2_vietnamese-gpt.pdf` | 4.5 | 4.0 | 3.5 | 4.0 | Đủ 3/3 trang, mạch lập luận và công thức chính được giữ. Thuật ngữ vật lý khá ổn. Trừ điểm vì nhiều chỗ dính chữ quanh tiếng Việt và ký hiệu toán, ví dụ `phụthuộc`, `kết quảcủa`, `hệtọa độ`. |
| `e_mc2.pdf` | `emc2_vi-claudefree.pdf` | 4.0 | 3.5 | 3.0 | 4.0 | Đủ 3/3 trang và giữ công thức/bố cục tương đối. Tuy nhiên cách dùng từ như `hàm lượng năng lượng` chưa tự nhiên bằng `nội dung năng lượng` hoặc `năng lượng chứa trong nó`; dấu tiếng Việt trong text layer có dấu hiệu mã hóa tổ hợp, đọc được nhưng không đẹp. |
| `e_mc2.pdf` | `einstein_vi-gptfree.pdf` | 2.0 | 3.0 | 4.0 | 1.0 | Đây là bản tóm lược 1 trang, không phải bản dịch đầy đủ 3 trang. Câu tiếng Việt khá tự nhiên nhưng thiếu phần lớn văn bản gốc, mất bố cục bài báo và công thức chi tiết. |
| `demo.docx` | `demo_vi-gpt.docx` | 4.5 | 4.0 | 4.5 | 5.0 | Giữ đúng 205 đoạn, 5 bảng, 263 ô bảng như file gốc. Dịch tự nhiên, phù hợp văn bản hướng dẫn. Một số nhãn phần mềm như calibre, EPUB, AZW3 giữ nguyên là hợp lý. |
| `Full-Paper-template.docx` | `Full-Paper-template_vi-gpt.docx` | 4.5 | 4.0 | 4.0 | 5.0 | Giữ đúng 91 đoạn, 2 bảng, 3 section và 14 style như bản gốc. Nội dung hướng dẫn được dịch đầy đủ, thuật ngữ định dạng học thuật tương đối nhất quán. Câu đôi chỗ còn hơi sát tiếng Anh nhưng nhìn chung dùng được. |
| `CULTAI_Report of the Independent Expert Group on AI and Culture-short.pdf` | `translated_report_vi-gpt.pdf` | 4.5 | 4.0 | 4.5 | 4.5 | Đủ 10/10 trang của bản short. Dịch tự nhiên, thuật ngữ về AI, văn hóa, quản trị, quyền văn hóa tương đối ổn. Mục lục và các nhóm bullet được giữ tốt; một vài bullet/page number khi trích text còn hơi rời nhưng không ảnh hưởng lớn. |

## Xếp hạng nhanh theo từng tiêu chí

| Tiêu chí | Bản tốt nhất | Lý do |
|---|---|---|
| Đầy đủ nội dung | `AttentionIsAllYouNeed_vi-latexgpt.pdf`, `Attention_Is_All_You_Need_VI-gpt.pdf`, `e_mc2_vietnamese-gpt.pdf`, `translated_report_vi-gpt.pdf`, hai file DOCX | Các bản này giữ đủ số trang/đoạn chính so với gốc. |
| Nhất quán thuật ngữ | `AttentionIsAllYouNeed_vi-latexgpt.pdf`, `Attention_Is_All_You_Need_VI-gpt.pdf`, `e_mc2_vietnamese-gpt.pdf`, `translated_report_vi-gpt.pdf` | Thuật ngữ chuyên ngành được giữ ổn định hơn, ít dịch sai nghiêm trọng. |
| Câu tự nhiên | `demo_vi-gpt.docx`, `translated_report_vi-gpt.pdf`, `Full-Paper-template_vi-gpt.docx` | Văn bản tiếng Việt trôi chảy hơn, ít lỗi dính chữ hơn các bản PDF học thuật. |
| Giữ cấu trúc trình bày ban đầu | `demo_vi-gpt.docx`, `Full-Paper-template_vi-gpt.docx`, `AttentionIsAllYouNeed_vi-latexgpt.pdf` | DOCX giữ gần như nguyên số đoạn/bảng/style; bản LaTeX Attention giữ bố cục học thuật tốt. |

## File gốc chưa có bản kết quả tương ứng rõ ràng

| File gốc | Ghi chú |
|---|---|
| `CalculusVolume1.pdf` | Không thấy bản dịch tương ứng trong `result_each`. |
| `ReAct Synergizing Reasoning and Acting.pdf` | Không thấy bản dịch tương ứng trong `result_each`. |
| `CULTAI_Report of the Independent Expert Group on AI and Culture.pdf` | Có bản `translated_report_vi-gpt.pdf` nhưng bản này khớp với file short 10 trang, không phải bản full 80 trang. |

## Nhận xét tổng hợp

- Nhóm DOCX đang cho kết quả giữ cấu trúc tốt nhất: số đoạn, số bảng, số section và style gần như không đổi.
- Nhóm PDF học thuật dài có nội dung khá đầy đủ nhưng thường bị lỗi khoảng trắng trong text layer tiếng Việt, làm giảm tiêu chí câu tự nhiên.
- Bản Google Translate của `e_mc2` có lỗi cấu trúc nghiêm trọng: đảo thứ tự nội dung, ký hiệu công thức lỗi và còn watermark dịch máy.
- Bản `einstein_vi-gptfree.pdf` không nên tính là bản dịch đầy đủ vì thực chất là bản tóm tắt.
- Với báo cáo CULTAI short, bản dịch đạt chất lượng khá tốt cả về nội dung lẫn độ tự nhiên; đây là một trong các bản PDF đọc tốt nhất trong thư mục kết quả.
