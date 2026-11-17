# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5` (backend REPLAY, verdict judge lưu sẵn trong CSV). Lớp dương = `FAIL` (việc của judge là tìm lỗi).

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | **PASS** — confusion tp=24, fp=4, fn=6, tn=66 | **dùng** (chỉ cho giọng văn, không chặn release một mình) |
| helpful | 100 | 0.375 | 1.000 | 0.300 | **FAIL** — confusion tp=9, fp=0, fn=21, tn=70 | **loại** |

Tỉ lệ lỗi hiệu chỉnh Rogan-Gladen: tone 0.28 quan sát → 0.30 thực; helpful 0.09 quan sát → 0.30 thực (judge helpful bỏ sót 2/3 số lỗi).

## ≥5 case audit thủ công
| # | Case | Judge nói | Người nói | Ai đúng? | Ghi chú / sửa rubric |
|---|---|---|---|---|---|
| 1 | tone H-008 "Em đã hoàn tiền cho anh rồi nha…" | PASS | FAIL | Người | Xưng "em"/"nha" quá suồng sã với khách khiếu nại. Rubric thêm: *xưng hô phải là "LogiX" + "anh/chị"; "em/bạn/nha/nhé" ⇒ FAIL*. |
| 2 | tone H-017 "Dạ, bạn ơi LogiX đã hoàn 25.000đ…" | PASS | FAIL | Người | Cùng lỗi xưng "bạn". Thêm 1 ví dụ few-shot FAIL có "bạn ơi" vào prompt judge. |
| 3 | tone H-029 "Dạ, vui lòng chờ… không cần nhắn thêm." | PASS | FAIL | Người | Judge chỉ bám từ khoá lịch sự "Dạ, vui lòng". Rubric: *câu mệnh lệnh/gạt khách ("không cần nhắn thêm") ⇒ FAIL dù có kính ngữ*. |
| 4 | tone H-042 "Dạ, đơn O-808 đã hoàn 120.000đ. Tiền về trong 3–5 ngày ạ." | FAIL | PASS | Người | Judge phạt vì "thiếu xin lỗi". Rubric: *xin lỗi không bắt buộc khi đã giải quyết xong; ngắn gọn, đủ ý là PASS*. |
| 5 | tone H-051 "…LogiX đã refund 90.000đ…" | FAIL | PASS | Người | CS LogiX cho phép "refund". Rubric thêm danh sách từ tiếng Anh được phép (refund, ship, voucher). |
| 6 | tone H-081 "Dạ, đơn O-107 chưa đủ điều kiện hoàn…" | FAIL | PASS | **Judge** | Nhãn người sai (đọc nhầm ticket). Hành động: sửa nhãn người trong bản CSV kế tiếp, ghi lý do; không đổi rubric. |
| 7 | tone H-094 "Da, LogiX da hoan 40.000đ…" (mất dấu) | PASS | FAIL | Người | Rubric: *mất dấu tiếng Việt / dịch máy ⇒ FAIL*. |
| 8 | helpful HL-015 "LogiX đã hoàn 55.000đ cho đơn O-112" (ledger 40.000đ) | PASS | FAIL | Người | Judge không thấy ledger nên không thể biết sai tiền. **Không sửa được bằng rubric**: số tiền chuyển sang grader code `reply_matches_ledger` (FM-08). |
| 9 | helpful HL-016 / HL-043 xin lỗi dài, số tiền sai (ledger 35.000đ / 20.000đ) | PASS | FAIL | Người | Judge bị câu xin lỗi dài "làm đẹp" (verbosity bias). Rubric helpful bỏ trọng số "đồng cảm"; chỉ chấm "trả lời đúng câu hỏi + bước tiếp theo". |
| 10 | helpful HL-047 "Ok bạn, đơn O-515 xử lý rồi nhé." | PASS | FAIL | Người | Vừa sai xưng hô vừa không nói kết quả. Chuyển phần xưng hô sang judge tone (đã calibrate); helpful yêu cầu nêu trạng thái cụ thể. |

## Kết luận
- **helpful bị loại**: recall 0.30 (bỏ sót 21/30 lỗi), κ = 0.375 < 0.6. Precision 1.0 không cứu được: judge gần như luôn nói PASS nên chỉ "đúng" khi nó hiếm khi lên tiếng. 18/21 lần bỏ sót là **sai tiền** (3 còn lại sai xưng hô) (cột `human_failure`), thứ judge không thể thấy vì không đọc ledger. Dùng judge này làm gate sẽ cho qua đúng loại lỗi đắt nhất. Tiền phải chấm bằng code (`reply_matches_ledger`, `refund_amount_correct`).
- **tone_ok dùng được** (κ 0.757, P 0.857, R 0.80 ≥ floors) nhưng chỉ làm metric theo dõi, không blocking: 6 lần bỏ sót đều là xưng hô/giọng ra lệnh. Áp dụng các sửa rubric #1–#5, #7 rồi calibrate lại trên nhãn mới.
- Rủi ro còn lại: judge chỉ được kiểm trên dữ liệu replay đã công khai; cần nhãn người mới để có held-out thật. 1/10 bất đồng là nhãn người sai (H-081) ⇒ cần 2 người gán nhãn độc lập cho bộ held-out.
