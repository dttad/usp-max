# MASTER-PLAN: Tối ưu `ultimate-sitemap-parser` (USP)

**Benchmark target:** `play.google.com`
**Repo gốc:** GateNLP/ultimate-sitemap-parser @ v1.8.1 (GPL-3.0-or-later)
**Execution agent:** Claude Code
**Ngày lập:** 2026-08-21

---

## 0. Tóm tắt điều hành

USP đã dùng expat (C) và generator nên **không phải bài toán CPU**. Sau khi đọc toàn bộ source, bottleneck thật nằm ở **kiến trúc**, không ở ngôn ngữ:

| # | Vấn đề | Vị trí | Tác động |
|---|---|---|---|
| 1 | Đệ quy fetch **tuần tự** — parser tự đi tải con | `fetch_parse.py:726` `IndexXMLSitemapParser.sitemap()` | 🔴 Chí mạng. N sitemap = N × RTT |
| 2 | Expat được nạp **cả document một lần** (`Parse(content, True)`) | `fetch_parse.py:467` | 🟠 Không stream được, giữ full string |
| 3 | `stream=True` nhưng `.content` đọc hết body | `requests_client.py:145` + `:57` | 🟠 `stream` thành no-op, peak RSS ~3× |
| 4 | Retry sleep cố định 1s, **không backoff/jitter, không đọc `Retry-After`** | `helpers.py:151` | 🟠 Bị Google 429 là hỏng |
| 5 | `RequestWaiter` dùng `time.sleep`, không tương thích concurrency | `abstract_client.py:223` | 🟠 Chặn đường async |
| 6 | Mỗi sitemap con = 1 `tempfile.mkstemp()` + `pickle.dump` | `objects/sitemap.py:227` | 🟡 Hàng nghìn fd, `.pages` unpickle lại mỗi lần gọi |
| 7 | 15 speculative fetch `_UNPUBLISHED_SITEMAP_PATHS` tuần tự | `tree.py:24` | 🟡 15 RTT lãng phí lúc khởi động |
| 8 | HTTP/1.1 (`requests`), không h2 multiplexing, không conditional GET | `requests_client.py` | 🟡 Không tái sử dụng crawl cũ |
| 9 | `fetch_parse.py` = 1369 dòng, parser lẫn I/O | toàn file | 🟡 Chặn parallel agent, khó test |

**Kỳ vọng sau plan này:** LIVE ≥ **15×** nhanh hơn, peak RSS giảm ≥ **60%**, lần crawl thứ 2 chỉ tốn ≤ **10%** thời gian lần đầu — **không đổi public API**.

**Nguyên tắc xuyên suốt:** không rewrite ngôn ngữ. Chỉ cân nhắc native extension ở Phase 7 và chỉ khi số liệu Phase 1 chứng minh được.

---

## 1. Vì sao chọn `play.google.com`

```
https://play.google.com/robots.txt
  ├── Sitemap: https://play.google.com/sitemaps/sitemaps-index-0.xml
  └── Sitemap: https://play.google.com/sitemaps/sitemaps-index-1.xml
        └── (index of index) → hàng nghìn sitemap .gz → hàng triệu URL
```

Đây là stress case gần như hoàn hảo vì nó đánh trúng **cả 9 điểm yếu** ở trên cùng lúc:

- **Fan-out cực rộng** → phơi bày bottleneck #1 rõ ràng nhất
- **Nested index ≥ 3 tầng** → test đệ quy, dedupe, budget
- **Toàn bộ gzip** → test streaming gunzip (#2, #3)
- **Google serve HTTP/2 + rate limit thật** → test #4, #5, #8
- **Volume triệu URL** → test #6 (memory), test resumability
- **Ổn định, public, có robots.txt rõ ràng** → benchmark lặp lại được

### ⚠️ Quy tắc lịch sự (bắt buộc, non-negotiable)

1. **99% vòng lặp dev chạy trên REPLAY corpus local**, không đụng Google.
2. LIVE run: tối đa **2 lần/ngày**, concurrency cap **≤ 12**, có `--max-sitemaps`.
3. User-Agent thật + URL liên hệ: `usp-bench/0.x (+https://d4t0.com/bot)`
4. Tôn trọng `Retry-After`. Gặp 429 → giảm concurrency, không retry ngay.
5. Chỉ đọc sitemap, **không** crawl page content.

---

## 2. Thiết kế benchmark

Tách làm **hai chế độ** — đây là quyết định quan trọng nhất của cả plan, vì nếu trộn lẫn thì mọi số đo đều nhiễu bởi network.

### 2.1 REPLAY (mặc định — deterministic, đo CPU/memory)

```
bench/
├── fetch_corpus.py     # tải 1 lần từ play.google.com → bench/corpus/ (+ manifest.json)
├── serve_corpus.py     # HTTP server local, replay corpus
├── run.py              # runner, xuất results/<git-sha>.json
├── compare.py          # so với baseline, exit 1 nếu regress
└── results/
```

`serve_corpus.py` phải hỗ trợ **inject latency**, đây là chìa khoá:

| Profile | Latency | Jitter | Mô phỏng |
|---|---|---|---|
| `zero` | 0ms | 0 | Đo thuần parse CPU |
| `lan` | 5ms | ±2ms | Homelab |
| `vn-google` | 180ms | ±40ms | Hanoi → Google edge |
| `hostile` | 250ms | ±150ms | 5% trả 429, 2% timeout |

Profile `zero` cô lập CPU. Profile `vn-google` đo lợi ích concurrency **mà không cần đụng Google**. Profile `hostile` là regression test cho backoff/retry.

### 2.2 LIVE (gate cuối mỗi phase)

Chạy thật, bounded bằng chính extension point có sẵn của USP:

```python
def cap_leaves(urls: list[str], level: int, parents: set[str]) -> list[str]:
    """Deterministic: sort rồi lấy N đầu → mọi run so sánh được với nhau."""
    return sorted(urls)[:BENCH_FANOUT_CAP]

sitemap_tree_for_homepage(
    "https://play.google.com/",
    recurse_list_callback=cap_leaves,
)
```

### 2.3 Metrics schema (`bench/results/*.json`)

```json
{
  "run_id": "...", "git_sha": "...", "phase": "P2",
  "mode": "replay", "profile": "vn-google", "fanout_cap": 200,
  "wall_s": 0.0,
  "urls_total": 0, "sitemaps_fetched": 0, "urls_per_s": 0.0,
  "http": {
    "requests": 0, "s2xx": 0, "s304": 0, "s404": 0, "s429": 0, "s5xx": 0,
    "bytes_wire": 0, "bytes_uncompressed": 0,
    "conn_reused_pct": 0.0, "h2_pct": 0.0,
    "lat_p50_ms": 0.0, "lat_p95_ms": 0.0, "lat_p99_ms": 0.0
  },
  "cpu": { "fetch_s": 0.0, "gunzip_s": 0.0, "parse_s": 0.0, "spill_s": 0.0 },
  "mem": { "peak_rss_mb": 0.0, "peak_alloc_mb": 0.0, "tempfiles_max": 0 }
}
```

### 2.4 KPI chính

| KPI | Ý nghĩa |
|---|---|
| **`urls_per_s` @ `vn-google`** | 🥇 North-star. Đây là con số cần cải thiện 15× |
| **`urls_per_s` @ `zero`** | Sức mạnh parser thuần. Phải **không được giảm** ở mọi phase |
| **`peak_rss_mb`** | Chặn regression memory khi thêm concurrency |
| **`s429`** | Phải = 0 ở LIVE. Nhanh mà bị block là thất bại |

---

## 3. Lộ trình theo Phase

> Mỗi phase có **Gate**. Không qua gate → không sang phase sau. Đây là hợp đồng với Claude Code.

### Phase 0 — Fork, harness, baseline (½ ngày)

**Mục tiêu:** có con số baseline không thể chối cãi. **Chưa sửa một dòng code sản phẩm nào.**

1. Fork repo. Giữ nguyên `GPL-3.0-or-later` trong `pyproject.toml` (bắt buộc, xem §6).
2. `uv sync --group dev --group perf` (repo đã có sẵn `pytest-memray` + `pyinstrument`).
3. Viết `bench/fetch_corpus.py`: tải corpus từ `play.google.com` một lần, lưu **nguyên bytes gzip** + headers vào `bench/corpus/`, sinh `manifest.json` (url → path, sha256, content-type, content-length).
4. Viết `bench/serve_corpus.py` với 4 latency profile ở §2.1.
5. Viết `bench/run.py` + `bench/compare.py` theo schema §2.3.
6. Chạy baseline: cả 4 profile × fanout_cap ∈ {50, 200} → `bench/results/baseline.json`.
7. Chạy LIVE 1 lần, `fanout_cap=50` → `baseline-live.json`.

**Gate 0:** `bench/compare.py baseline.json baseline.json` chạy xanh. Corpus reproduce được từ manifest. Đã có số cho cả 4 profile.

---

### Phase 1 — Profiling & đo đạc (½ ngày)

**Mục tiêu:** biết chính xác thời gian đi đâu, để quyết định Phase 7 có cần không.

1. `pyinstrument` trên profile `zero` và `vn-google`.
2. `memray` trên `fanout_cap=200` → flamegraph allocation.
3. Đếm tempfile: patch tạm `tempfile.mkstemp` để log high-water mark.
4. Ghi bảng breakdown: `fetch_s / gunzip_s / parse_s / spill_s` theo % wall.

**Gate 1:** File `bench/results/PROFILE.md` trả lời được 4 câu:
- `vn-google`: bao nhiêu % wall là chờ socket? *(dự đoán > 85%)*
- `zero`: expat chiếm bao nhiêu % CPU? *(nếu < 25% → Phase 7 bị huỷ)*
- Peak RSS bao nhiêu, đỉnh nằm ở allocation nào?
- `pickle.dump` chiếm bao nhiêu %?

> **Quyết định:** nếu `parse_s < 25%` ở profile `zero`, đánh dấu Phase 7 = SKIP ngay tại đây. Đừng để nó lởn vởn trong đầu ở các phase sau.

---

### Phase 2 — Structural refactor, **zero behavior change** (1 ngày)

**Mục tiêu:** tách `fetch_parse.py` (1369 dòng) để các agent sau chạy song song không merge-conflict. **Đây là bước enabler, làm sớm để tiết kiệm về sau.**

```
usp/
├── parsers/
│   ├── base.py         # AbstractSitemapParser, AbstractXMLSitemapParser
│   ├── dispatch.py     # XMLSitemapParser (content sniffing + routing)
│   ├── xml_index.py    # IndexXMLSitemapParser
│   ├── xml_pages.py    # PagesXMLSitemapParser
│   ├── rss.py  atom.py  plaintext.py  robotstxt.py
├── fetcher/
│   ├── sync.py         # SitemapFetcher hiện tại
│   └── policy.py       # retry, backoff, rate limit
└── fetch_parse.py      # chỉ còn re-export để giữ backward compat
```

**Ràng buộc tuyệt đối:** `usp.fetch_parse.X` phải import được y hệt cũ. Không đổi một chữ nào trong logic.

**Gate 2:** toàn bộ test cũ xanh (kể cả VCR cassettes). `bench/compare.py` cho thấy delta `urls_per_s` trong khoảng ±3%. `git diff --stat` cho thấy chủ yếu là move, không phải rewrite.

---

### Phase 3 — 🥇 Đảo ngược control flow + Async I/O (2–3 ngày)

**Đây là phase mang lại ~80% toàn bộ lợi ích.**

#### 3.1 Vấn đề kiến trúc

Hiện tại parser **tự đi tải** con nó:

```
IndexXMLSitemapParser.sitemap()
  └─ for url in children:          ← vòng lặp tuần tự, blocking
       SitemapFetcher(url).sitemap()  ← đệ quy, mỗi vòng 1 RTT
```

Parser đang làm I/O — vi phạm layering, và là lý do gốc khiến không thể song song hoá.

#### 3.2 Kiến trúc mới: parser thuần, scheduler sở hữu vòng lặp

```python
# usp/parsers/base.py — parser trở thành PURE FUNCTION
@dataclass(slots=True)
class ParseResult:
    kind: Literal["index", "pages", "invalid"]
    url: str
    child_urls: list[str] = field(default_factory=list)
    pages: list[SitemapPage] = field(default_factory=list)
    reason: str | None = None

# usp/fetcher/scheduler.py — scheduler sở hữu concurrency, budget, dedupe
class Crawler:
    async def crawl(self, roots: list[str]) -> AsyncIterator[ParseResult]: ...
```

Lợi ích dây chuyền — một thay đổi mở khoá cả 5 thứ:
concurrency ✔ · global budget & dedupe ✔ · streaming ✔ · resumability ✔ · parser test được không cần network ✔

#### 3.3 Async client

- `httpx.AsyncClient(http2=True)` — h2 multiplexing, hàng nghìn request trên vài connection.
- `anyio.create_task_group()` thay `asyncio.TaskGroup` → giữ được **Python 3.10** (httpx đã kéo `anyio` sẵn, dep miễn phí).
- `uvloop` optional trên Linux (workstation Ubuntu) — bật qua env, đo rồi mới giữ.

#### 3.4 Politeness layer (`fetcher/policy.py`)

| Thành phần | Thiết kế |
|---|---|
| Rate limit | Token bucket async, thay `RequestWaiter.time.sleep` |
| Per-host cap | `anyio.Semaphore` theo host |
| Backoff | Exponential + full jitter: `random.uniform(0, min(cap, base·2^n))` |
| 429 | Đọc `Retry-After`; **giảm concurrency** (AIMD), không chỉ sleep |
| Deadline | Global budget: `max_sitemaps`, `max_urls`, `deadline_s` |

#### 3.5 Giữ nguyên API cũ

```python
def sitemap_tree_for_homepage(...) -> AbstractSitemap:
    """Wrapper đồng bộ. Chữ ký y hệt cũ."""
    return anyio.run(_async_tree, ...)   # user code không đổi một dòng
```

Đồng thời expose API mới cho ai cần:
```python
async def sitemap_pages_async(url, *, concurrency=8) -> AsyncIterator[SitemapPage]
```

**Gate 3:**
- ✅ `urls_per_s` @ `vn-google` ≥ **10×** baseline
- ✅ `urls_per_s` @ `zero` **không giảm quá 5%** (không đánh đổi CPU lấy I/O)
- ✅ Profile `hostile`: hoàn tất, `s429` không leo thang, tổng request ≤ 1.3× profile `lan`
- ✅ LIVE `fanout_cap=200`: `s429 == 0`
- ✅ Toàn bộ test cũ xanh, API cũ không đổi

---

### Phase 4 — Streaming parse + memory (1–2 ngày)

**Mục tiêu:** bỏ peak RSS ~3×, và bắt đầu parse **trước khi** tải xong.

#### 4.1 Pipeline streaming

```
httpx stream()  →  zlib.decompressobj(wbits=47)  →  expat.Parse(chunk, False)
   bytes chunk        auto-detect gzip/zlib          incremental feed
```

- `wbits=47` = `32 + 15` → tự nhận diện gzip header, không cần đoán từ đuôi `.gz`.
- Đổi `Parse(content, True)` → vòng `Parse(chunk, False)` + `Parse(b"", True)` ở cuối.
- **Giữ nguyên** hardening handlers hiện có (`StartDoctypeDeclHandler`, `EntityDeclHandler`, `SetParamEntityParsing(NEVER)`) — đây là chống XXE/billion-laughs, tuyệt đối không được rơi khi refactor.
- Giữ `__MAX_SITEMAP_SIZE = 100MB` nhưng enforce **trên luồng đã giải nén, theo từng chunk** → chặn được zip bomb sớm hơn hiện tại.

#### 4.2 Thay chiến lược spill

Hiện tại: 1 `mkstemp` + `pickle.dump` cho **mỗi** sitemap con, `.pages` unpickle lại toàn bộ mỗi lần truy cập, dọn dẹp bằng `__del__` (không đáng tin khi crash).

Ba chế độ mới:

| Mode | Cơ chế | Dùng khi |
|---|---|---|
| `memory` | list thẳng | sitemap nhỏ, test |
| `spill` *(default)* | **1 file append-only** + index (offset, length) | tree lớn — thay hàng nghìn tempfile bằng 1 |
| `stream` | không materialize, đẩy page ra generator/callback | crawl triệu URL, RSS gần như phẳng |

Thêm context manager để dọn dẹp tin cậy, giữ `__del__` làm fallback:
```python
with sitemap_tree_for_homepage(url) as tree:   # mới, khuyến nghị
    for page in tree.all_pages(): ...
```

**Gate 4:**
- ✅ `peak_rss_mb` giảm ≥ **60%** ở `fanout_cap=200`
- ✅ Mode `stream` với `fanout_cap=1000`: RSS **phẳng** (slope < 5MB/1000 sitemap)
- ✅ `tempfiles_max` ≤ 2 (từ hàng nghìn)
- ✅ Test zip-bomb: file giải nén ra 500MB bị chặn, RSS không vượt 150MB
- ✅ Test XXE/billion-laughs vẫn raise `SitemapXMLParsingException`

---

### Phase 5 — Cache & resumability (1 ngày)

1. **Conditional GET**: store SQLite (WAL mode) `url → (etag, last_modified, sha256, fetched_at)`. Gửi `If-None-Match` / `If-Modified-Since`, xử lý `304`.
2. **Checkpoint frontier**: ghi định kỳ trạng thái hàng đợi → crash giữa chừng crawl 4M URL vẫn resume được.
3. **Output JSONL streaming** (`orjson`) — không giữ tree trong RAM để xuất file.
4. CLI: `--cache-db`, `--resume`, `--jsonl`, `--max-urls`, `--deadline`, `--concurrency`.

**Gate 5:**
- ✅ Chạy lần 2 ngay sau lần 1: `s304 / requests ≥ 0.9`, `wall_s ≤ 10%` lần đầu
- ✅ `kill -9` giữa chừng rồi `--resume`: tổng URL thu được = chạy liền một mạch
- ✅ LIVE lần 2 trên play.google.com: `bytes_wire` giảm ≥ 90%

---

### Phase 6 — Chất lượng code & test (1–2 ngày)

Bám sát tiêu chí của bạn: *clarity, testability, maintainability, reusability, adherence to standards*.

1. **Property-based testing** (`hypothesis`): sinh XML méo mó (thiếu đóng thẻ, encoding sai, nested lạ, `<loc>` rỗng, ngày tháng sai định dạng) → parser **không được crash**, chỉ được trả `InvalidSitemap`.
2. **Fuzzing** (`atheris`) cho handler expat, chạy 30 phút trong nightly CI.
3. **Type coverage**: bật `mypy --strict` cho `parsers/` và `fetcher/`.
4. **Ruff**: mở rộng rule set — thêm `B` (bugbear), `SIM`, `RET`, `ASYNC` *(quan trọng cho code async mới)*, `S` (bandit).
5. **CI matrix**: 3.10 → 3.14, thêm job REPLAY benchmark, fail nếu regress > 5% so với `bench/results/baseline.json`.
6. **Docs**: cập nhật Sphinx, thêm trang "Performance tuning" và "Async API".

**Gate 6:** mypy strict xanh · coverage ≥ 85% trên module mới · hypothesis 10k example không crash · CI benchmark job hoạt động.

---

### Phase 7 — Native acceleration *(CONDITIONAL — có thể bị huỷ ở Gate 1)*

**Chỉ thực hiện nếu Gate 1 cho thấy `parse_s ≥ 25%` ở profile `zero`.**

Thứ tự thử, dừng ngay khi đủ tốt:

| Bước | Kỹ thuật | Chi phí | Kỳ vọng |
|---|---|---|---|
| 7a | `lxml.etree.iterparse` thay expat | Thấp | 1.3–2× |
| 7b | **Python 3.14 free-threaded (`3.14t`)** — parse N sitemap trên N thread thật, không GIL | Trung bình | Gần tuyến tính theo core |
| 7c | Rust extension (PyO3 + `quick-xml`) cho hot loop | Cao | 3–5× |

> 7b là hướng thú vị nhất và ít rủi ro nhất — repo đã classify `Python :: 3.14`. Nó biến bài toán parse thành song song thật mà **không cần đổi ngôn ngữ**. Chạy như một experiment riêng trên workstation i9 trước khi cam kết.

**Gate 7:** phải đạt ≥ **2×** trên profile `zero` mới được merge. Không đạt → revert, ghi lại kết quả âm vào `PROFILE.md` để không ai thử lại.

---

## 4. Workflow với Claude Code

### 4.1 `CLAUDE.md` (đặt ở root fork)

````markdown
# ultimate-sitemap-parser — fork tối ưu hiệu năng

## Bối cảnh
Fork của GateNLP/ultimate-sitemap-parser. Mục tiêu: tối ưu I/O concurrency
và memory. **KHÔNG rewrite sang ngôn ngữ khác.**

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
- Test:      `uv run pytest`
- Lint:      `uv run ruff check --fix && uv run ruff format`
- Types:     `uv run mypy usp/parsers usp/fetcher --strict`
- Benchmark: `uv run bench/run.py --profile vn-google --cap 200`
- So sánh:   `uv run bench/compare.py bench/results/baseline.json <new>.json`

## Trước khi báo "xong" bất kỳ phase nào
- [ ] `uv run pytest` xanh (kể cả VCR cassettes)
- [ ] ruff + mypy xanh
- [ ] benchmark đã chạy, số đã ghi vào `bench/results/`
- [ ] Gate criteria trong MASTER-PLAN đã đối chiếu từng dòng
- [ ] Không có `print()` — repo enforce ruff T20
````

### 4.2 Chia worktree

Sau khi **Gate 2** xong (refactor tách module), có thể chạy song song:

```bash
git worktree add ../usp-p3-async   feat/async-scheduler
git worktree add ../usp-p6-quality feat/test-hardening
```

| Worktree | Phase | File đụng tới | Xung đột? |
|---|---|---|---|
| `usp-p3-async` | 3 → 4 → 5 | `fetcher/`, `objects/` | — |
| `usp-p6-quality` | 6 | `tests/`, `.github/`, `pyproject.toml` | Thấp ✅ |

**Không** chạy Phase 3 và Phase 4 ở hai worktree khác nhau — cả hai đều sửa sâu vào pipeline fetch, merge sẽ rất đau. Làm tuần tự trong cùng worktree.

### 4.3 PROMPT mẫu — Phase 3 (paste thẳng vào Claude Code)

````
Đọc MASTER-PLAN-usp-perf.md, thực hiện Phase 3.

BỐI CẢNH
Bottleneck #1: IndexXMLSitemapParser.sitemap() (usp/parsers/xml_index.py sau
Phase 2) fetch sub-sitemap TUẦN TỰ trong vòng for. Parser đang tự làm I/O.

NHIỆM VỤ
Đảo ngược control flow: parser trở thành pure function trả ParseResult;
tạo Crawler scheduler sở hữu vòng lặp fetch với concurrency.

CHIA NHỎ — dừng lại xin review sau MỖI bước:
1. Định nghĩa ParseResult dataclass + đổi các *SitemapParser để trả về nó
   thay vì tự fetch. Chưa thêm async. Test cũ phải xanh (dùng adapter tạm).
2. Viết usp/fetcher/policy.py: token bucket, per-host semaphore,
   exponential backoff + full jitter, xử lý Retry-After. Unit test độc lập,
   dùng fake clock — KHÔNG dùng time.sleep thật trong test.
3. Viết usp/fetcher/async_client.py với httpx.AsyncClient(http2=True).
   Implement AbstractWebClient-tương đương cho async.
4. Viết usp/fetcher/scheduler.py: Crawler.crawl() dùng anyio task group,
   có dedupe (set url đã thấy), budget (max_sitemaps/max_urls/deadline_s),
   phát hiện đệ quy (giữ nguyên semantics parent_urls hiện tại).
5. Nối lại: sitemap_tree_for_homepage() thành sync wrapper qua anyio.run().
   Chữ ký KHÔNG ĐỔI.
6. Chạy benchmark cả 4 profile, ghi kết quả.

RÀNG BUỘC
- Python 3.10 phải chạy được → dùng anyio, KHÔNG dùng asyncio.TaskGroup.
- Giữ nguyên toàn bộ hardening expat và giới hạn 100MB.
- Giữ nguyên recurse_callback / recurse_list_callback (benchmark phụ thuộc).
- Không đụng network thật. Chỉ dùng bench/serve_corpus.py.

GATE (đối chiếu từng dòng trước khi báo xong)
- urls_per_s @ vn-google >= 10x baseline
- urls_per_s @ zero KHÔNG giảm quá 5%
- profile hostile hoàn tất, tổng request <= 1.3x profile lan
- toàn bộ test cũ xanh, API cũ không đổi
````

### 4.4 Subagent

| Subagent | Việc | Vì sao tách |
|---|---|---|
| `bench-runner` | Chạy benchmark, parse output, so baseline | Output dài, giữ context chính sạch |
| `test-writer` | Sinh hypothesis strategies, edge case | Việc lặp, độc lập |
| `perf-reviewer` | Đọc diff, soi async pitfall (blocking call trong coroutine, semaphore leak, task không await) | Cần "con mắt tươi", không bị anchor bởi quá trình viết |

---

## 5. Timeline & thứ tự ưu tiên

```
Ngày 1  ├─ Phase 0  harness + baseline          ← nền móng, đừng bỏ qua
        └─ Phase 1  profiling                    ← quyết định số phận Phase 7
Ngày 2  └─ Phase 2  refactor tách module         ← enabler cho parallel
Ngày 3-5└─ Phase 3  ASYNC                        ← 🥇 80% giá trị nằm ở đây
Ngày 6-7└─ Phase 4  streaming + memory
Ngày 8  └─ Phase 5  cache + resume
Ngày 9-10  Phase 6  test + CI    ∥ có thể song song từ ngày 3
Ngày 11+   Phase 7  (conditional)
```

**Nếu chỉ có 3 ngày:** làm Phase 0 → 1 → 3. Bỏ Phase 2 (chấp nhận một file to), bỏ 4–7. Vẫn thu được ~10× vì toàn bộ lợi ích lớn nằm ở Phase 3.

---

## 6. Rủi ro & lưu ý

| Rủi ro | Giảm thiểu |
|---|---|
| **GPL-3.0 lan toả** | Fork = derivative work → **phải** giữ GPL-3.0. Nếu muốn dùng trong sản phẩm closed-source của d4t0.com, chỉ được gọi qua subprocess/HTTP, không import trực tiếp. Cân nhắc đóng góp ngược PR lên upstream — maintainer đang active (v1.8.1, 228 commits). |
| Google rate limit / block IP | REPLAY-first. LIVE ≤ 2 lần/ngày, concurrency ≤ 12, UA có contact. |
| Async bug khó tái hiện | Profile `hostile` là regression test. Bắt buộc test với fake clock. `perf-reviewer` subagent soi diff. |
| Sai lệch benchmark do network | Profile `zero` + `vn-google` là số quyết định. LIVE chỉ để sanity check. |
| Refactor làm rơi hardening bảo mật | Viết security test **trước** Phase 2 (XXE, billion-laughs, zip bomb) và pin chúng vào CI. |
| Scope creep sang rewrite | Phase 7 conditional, có gate 2× cứng. Gate 1 có quyền huỷ thẳng. |

---

## 7. Định nghĩa "Hoàn thành"

- [ ] `urls_per_s` @ `vn-google` ≥ **15×** baseline
- [ ] `peak_rss_mb` giảm ≥ **60%**; mode `stream` có RSS phẳng
- [ ] Crawl lần 2 tốn ≤ **10%** thời gian lần đầu
- [ ] LIVE `play.google.com`, `fanout_cap=500`: hoàn tất, `s429 == 0`
- [ ] Public API cũ **không đổi một dòng**; test cũ xanh 100%
- [ ] mypy strict xanh, coverage ≥ 85%, security test pinned trong CI
- [ ] `bench/results/PROFILE.md` ghi lại cả kết quả **âm** (những gì đã thử mà không hiệu quả)
