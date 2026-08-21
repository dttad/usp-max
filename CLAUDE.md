# ultimate-sitemap-parser — fork tối ưu hiệu năng

## Bối cảnh
Fork của GateNLP/ultimate-sitemap-parser @ v1.8.1. Mục tiêu: tối ưu I/O concurrency
và memory theo `MASTER-PLAN-usp-perf.md`. **KHÔNG rewrite sang ngôn ngữ khác.**

## Ràng buộc bất di bất dịch
1. **Public API không đổi.** `sitemap_tree_for_homepage()` phải giữ nguyên
   chữ ký và hành vi. Mọi API async là ADDITIVE.
2. **License GPL-3.0-or-later.** Không xoá header, không đổi license.
3. **Không hạ security.** Giữ nguyên hardening expat (DOCTYPE/ENTITY handlers,
   SetParamEntityParsing(NEVER)) và giới hạn 100MB.
4. **Không đụng network Google khi dev.** Dùng `bench/serve_corpus.py`.
   LIVE run phải được người dùng phê duyệt rõ ràng từng lần.
5. **Mọi thay đổi perf phải kèm số đo.** Chạy `uv run bench/run.py` và
   `bench/compare.py` trước khi báo hoàn thành.

## Lệnh
- Test:           `uv run pytest`
- Lint:           `uv run ruff check --fix && uv run ruff format`
- Types:          `uv run mypy usp/parsers usp/fetcher --strict`
- Replay server:  `uv run bench/serve_corpus.py --profile vn-google --port 8765`
- Benchmark:      `uv run bench/run.py --mode replay --profile vn-google --fanout-cap 200 --phase P1`
- So sánh:        `uv run bench/compare.py bench/results/baseline.json bench/results/P1-vn-google-cap200.json`

## Trước khi báo "xong" bất kỳ phase nào
- [ ] `uv run pytest` xanh (kể cả VCR cassettes)
- [ ] ruff + mypy xanh
- [ ] benchmark đã chạy, số đã ghi vào `bench/results/`
- [ ] Gate criteria trong `MASTER-PLAN-usp-perf.md` đã đối chiếu từng dòng
- [ ] Không có `print()` — ruff T20 enforce
