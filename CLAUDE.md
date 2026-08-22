# ultimate-sitemap-parser — fork tối ưu hiệu năng

## Bối cảnh
Fork của [`GateNLP/ultimate-sitemap-parser`](https://github.com/GateNLP/ultimate-sitemap-parser)
@ v1.8.1, phát triển độc lập tại
[`dttad/usp-max`](https://github.com/dttad/usp-max).

Mục tiêu: tối ưu I/O concurrency và memory theo `MASTER-PLAN-usp-perf.md`.
**KHÔNG rewrite sang ngôn ngữ khác.** Mọi thay đổi là additive — public API
của upstream giữ nguyên.

## Ràng buộc bất di bất dịch
1. **Public API không đổi.** `sitemap_tree_for_homepage()` phải giữ nguyên
   chữ ký và hành vi. Mọi API mới (async, parser tăng tốc) là ADDITIVE.
2. **License GPL-3.0-or-later.** Không xoá header, không đổi license.
3. **Không hạ security.** Giữ nguyên hardening expat (DOCTYPE/ENTITY handlers,
   SetParamEntityParsing(NEVER)) và giới hạn 100MB.
4. **Không đụng network Google khi dev.** Dùng `bench/serve_corpus.py`.
   LIVE run phải được người dùng (operator) phê duyệt rõ ràng từng lần.
5. **Mọi thay đổi perf phải kèm số đo.** Chạy `uv run bench/run.py` và
   `bench/compare.py` trước khi báo hoàn thành.

## Kết quả hiện tại (vn-google cap=200, replay corpus)

| Stage | urls/s | Speedup |
|---|---:|---:|
| USP v1.8.1 gốc | 602 | 1.00× |
| + Phase 3 async (httpx+anyio) | 1 400 | 2.33× |
| + Phase 7a (lxml.iterparse) | 2 647 | 4.40× |
| + Phase 7b (CPython 3.14t) | 2 936 | 4.88× |
| **+ Phase 7c (Rust+quick-xml)** | **8 767** | **14.55×** |

## Lệnh

### Test & lint
```
uv run pytest
uv run ruff check --fix
uv run ruff format
```

### Build Rust extension (một lần, cần cargo/maturin)
```
uv venv .venv-rust --python 3.12
uv pip install --python .venv-rust/bin/python -e . 'httpx[http2]' anyio lxml maturin pytest vcrpy requests-mock pytest-mock
PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1 .venv-rust/bin/maturin develop --release
```

### Benchmark
```
.venv-rust/bin/python bench/serve_corpus.py --profile vn-google --port 8765 &
USP_USE_RUST=1 USP_USE_LXML=1 .venv-rust/bin/python bench/run_async.py \
  --profile vn-google --fanout-cap 200 --concurrency 16 \
  --name benchmark --phase X
.venv/bin/python bench/compare.py bench/results/baseline.json bench/results/<your>.json
```

### Tuỳ chọn runtime (env vars)
- `USP_USE_LXML=1` — dùng lxml parser (mặc định 1; 0 để tắt)
- `USP_USE_RUST=1` — dùng `usp_fast` Rust parser (mặc định 1; 0 để tắt)
- Nếu cả hai đều bật, Rust được ưu tiên; fall back qua lxml rồi expat.

## Cấu trúc thư mục quan trọng
```
usp/
├── tree.py                      # public sync API (không đổi)
├── fetch_parse.py               # parser gốc (không đổi)
├── objects/                     # domain types
└── fetcher/                     # Phase 3+7: async crawler
    ├── async_client.py          # httpx-based async web client
    ├── crawler.py               # AsyncCrawler worker pool
    ├── lxml_parser.py           # Phase 7a: lxml backend
    └── rust_parser.py           # Phase 7c: usp_fast wrapper

bench/                           # harness + corpus
├── corpus/                       # 250 sub-sitemaps + 2 indexes
├── serve_corpus.py              # replay server (vn-google, hostile, lan, zero)
├── run.py / run_async.py        # sync vs async benchmark
├── compare.py / aggregate.py    # regression gates
└── results/                      # JSONs + PROFILE.md + PHASE3.md + PHASE7*.md + FINAL-REPORT.md

rust/                            # Phase 7c: PyO3 + quick-xml
├── Cargo.toml
└── src/lib.rs                    # parse_pages(), parse_index()
```

## Trước khi merge / release
- [ ] `uv run pytest` xanh (kể cả VCR cassettes)
- [ ] `uv run ruff check` + `uv run ruff format` xanh
- [ ] `bench/compare.py` xanh (không regress vượt ngưỡng cấu hình)
- [ ] Số benchmark mới đã ghi vào `bench/results/` và cập nhật FINAL-REPORT.md
- [ ] Gate criteria trong `MASTER-PLAN-usp-perf.md` đã đối chiếu từng dòng
- [ ] Không có `print()` — ruff T20 đã enforce
