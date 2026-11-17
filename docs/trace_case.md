# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 11   # đối chứng
```

Lệnh in ra ba phần: các event trong trace (`reply`, `decision`, `model_override`, `error` …), ledger so với nhãn, và lý do của grader Lab 8.

## Case: `seeded_regression` C-002@11

Output (rút gọn):
```
== seeded_regression C-002@11  lab8 grader: FAIL (status resolved != denied)
   1 handoff            {"allowed_tools": ["lookup_order", "lookup_policy", "compute_compensation"], "claim_id": "C-002", ...
   2 investigator       {"lookup_order": {"visible": true, "reason": "ok", "order": {"order_id": "O-202", "status": "delivered", "dela...
   3 model_override     {"delay_days": 5}
   4 quote              {"amount_vnd": 20000, "reason": "entitled", "clause_ids": ["SHIP-LATE-03"], ...}
   5 refund_history     {"refund_count": 0, "refunded_vnd": 0}
   6 reply              {"text": "Dạ, LogiX đã hoàn 20.000đ cho đơn O-202."}
  refund_vnd     ledger=20000    label=0   <-- differs
  refund_count   ledger=1        label=0   <-- differs
  status         result=resolved label=denied
```

| Trường | Nội dung |
|---|---|
| Trial | `seeded_regression C-002@11`, run ID trace `R-seeded_regression-C-002-s11`; nằm trong report `E-seeded_regression-20261117-7880` |
| Triệu chứng | `pass_rate` FAIL (status resolved ≠ denied), `refund_amount_correct` FAIL (ledger 20.000đ, nhãn 0), `db_fact_kept` FAIL. Reply "đã hoàn 20.000đ" khớp ledger nên `reply_matches_ledger` không bắt — đọc ledger mới thấy sai. |
| Bước sai đầu tiên | Event **3** `model_override` — payload `{"delay_days": 5}` |
| Vì sao là bước *đầu tiên* | Event 1 `handoff` đúng hợp đồng (chỉ tool đọc). Event 2 `investigator` đọc DB đúng: O-202 `delivered`, DB ghi `delay_days = 2` (dưới ngưỡng được hoàn). Event 3 thay 2 ngày của DB bằng "trễ 5 ngày" lấy từ ticket ⇒ mọi bước sau (event 4 quote 20.000đ theo SHIP-LATE-03, ghi ledger, reply) chỉ là hệ quả hợp lệ của dữ kiện sai. |
| Failure mode | **FM-03** ticket_trust (`docs/failure_taxonomy.md`) |
| Đối chứng | `fixed C-002@11`: event 2 giống hệt, **không có** `model_override`; event 3 `quote {"amount_vnd": 0, "reason": "no_entitlement"}` → status `denied`, ledger 0, lab8 grader PASS. Baseline `swarm` cũng sai đúng chỗ này ở seed 55 (`seed % 5 == 0`), nên `pass_rate` baseline = 0.9857, không phải 1.0. |
| Regression test | `tests/test_lab9_trace_case.py::test_lab9_trace_case_ticket_override_is_caught` (DbFactKept=False, RefundAmountCorrect=False, TrialPass=False trên `seeded_regression`) và `..._fixed_passes` (cả 4 grader True trên `fixed`). Đã chạy: fail đúng trên variant lỗi, pass trên `fixed`; chạy trong job `eval-gate` qua `-k "lab9 and not advanced"`. Item E-002 có `provenance.from_trace = R-seeded_regression-C-002-s11`. |
| Chưa chắc chắn | Trace không cho biết *vì sao* model chọn tin ticket (MockLLM chỉ là công tắc). Với LLM thật cần log prompt/token của bước parse. Trace cũng không chứng minh `quiet_override` (cùng lỗi nhưng không ghi event) bị bắt bằng `db_fact_kept` — biến thể đó chỉ bị bắt qua ledger (`refund_amount_correct`, slice `adversarial`/`denial`). |

### Case phụ (FM-08): `seeded_regression` C-008@11
Reply "Dạ, LogiX đã hoàn 35.000đ cho đơn O-808." trong khi approval A-008 cap ở 20.000đ và ledger ghi 20.000đ. Bước sai: event `reply` dùng số đề xuất thay vì `effect.amount_vnd` của host. Test: `test_lab9_trace_case_misreport_is_caught` (`ReplyMatchesLedger` False trên lỗi, True trên `fixed`); item E-008 `from_trace = R-seeded_regression-C-008-s11`.

## Quy tắc

- Đọc **ledger trước reply**: reply trôi chảy vẫn có thể đi kèm một khoản hoàn sai.
- "Bước sai đầu tiên" phải dẫn số event cụ thể, không phải "agent bị nhầm".
- Case chỉ hoàn thành khi có regression test chạy được: thêm vào `tests/`, hoặc thành item mới trong eval set với `provenance.from_trace`.
