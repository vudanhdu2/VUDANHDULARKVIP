# Project Requirements — waytoagi v2

**Tóm tắt rõ ràng**: mục tiêu, đầu vào, đầu ra, ràng buộc cứng. Đọc trước khi config dự án mới.

---

## 1. 🎯 MỤC TIÊU dự án

Tự động hóa pipeline:

```
[SOURCE Feishu CN tenant]                    [DST Larksuite tenant]
  Wiki nguồn (CN)                              Wiki public (VI)
       │                                              ▲
       ▼                                              │
  ┌──────────┐  ┌──────────┐  ┌──────────────┐  ┌──────────┐  ┌──────┐
  │  CRAWL   │→│  CLONE   │→│   TRANSLATE  │→│  MIRROR  │→│ SYNC │
  │detect    │  │CN→working│  │CN→VI inplace │  │VI→DST    │  │edited│
  │NEW/EDIT/ │  │block-by- │  │LLM POOL      │  │tạo nodes │  │records│
  │RENAME/   │  │block     │  │+ cache       │  │match     │  │       │
  │DELETE    │  │preserve  │  │              │  │source    │  │       │
  │          │  │media     │  │              │  │tree order│  │       │
  └──────────┘  └──────────┘  └──────────────┘  └──────────┘  └──────┘
       │             │              │                │             │
       └─────────────┴──────────────┴────────────────┴─────────────┘
                            │
                            ▼
                  ┌──────────────────────┐
                  │   LARK BASE          │
                  │  state table         │
                  │  73 fields tracking  │
                  │  real-time updates   │
                  └──────────────────────┘
```

**Use case**: dịch + đồng bộ kho tài liệu CN sang VI cho người dùng Việt đọc/tra cứu, giữ nguyên cấu trúc tree, images, files, tables, formatting.

---

## 2. 📥 ĐẦU VÀO (Inputs)

### 🎯 Minimal mode — CHỈ 3 INPUTS

User cung cấp 3 thứ:

| # | Input | Hình thức |
|---|-------|-----------|
| **1** | **Link wiki nguồn** | URL paste (vd `https://example.feishu.cn/wiki/XXX`) |
| **2** | **lark-cli auth login** | Chạy `lark-cli auth login --as user` (DST tenant) |
| **3** | **LLM API key** | String hoặc file `llm_keys.json` |

→ Chạy `python bootstrap.py` → script auto-derive + auto-create rest.

**Auto-derive được**:
- Source domain + space_id (parse URL + initial crawl)
- DST domain + App ID + App Secret + scopes (qua `lark-cli auth status --verify`)
- DST tenant info (user OAuth credentials)

**Auto-create được** (qua Lark API):
- DST working space (chứa CN clone + VI translate)
- DST mirror space (public)
- CN parent + VI parent folders
- Lark Base table với 73 fields theo schema

### ⚠️ Cần thêm trong 1 case

**Source CN restrict** (block-level read denied) → cần thêm:
- `FEISHU_APP_ID` + `FEISHU_APP_SECRET` (Feishu source app)

→ Bootstrap script detect tự động, prompt khi cần.

### Full config (manual override)

Nếu muốn config từng param manual (vd reuse existing space/parent), dùng `setup_new_project.py` thay vì `bootstrap.py`. Liệt kê chi tiết bên dưới:



### A. Source tenant (Feishu CN — read only)

| Param | ENV var | Mô tả | Ví dụ |
|-------|---------|-------|-------|
| Source domain | (info) | Domain wiki nguồn | `waytoagi.feishu.cn` |
| Source space | `LARK_SRC_SPACE` | Wiki space_id (19 digits) | `7226178700923011075` |
| Feishu App ID | `FEISHU_APP_ID` | App ID đọc cross-tenant | `cli_xxxxxxxxxxxxxxxx` |
| Feishu App Secret | `FEISHU_APP_SECRET` | App secret | `(redacted)` |
| Feishu API URL | `FEISHU_OPEN_URL` | Base API endpoint | `https://open.feishu.cn/open-apis` |

**Scopes Feishu app cần**:
- `wiki:wiki:readonly`, `wiki:node:read`
- `docx:document:readonly`, `docx:document.media:download`
- `drive:file:download`

### B. DST tenant (Larksuite — read+write)

| Param | ENV var | Mô tả | Ví dụ |
|-------|---------|-------|-------|
| Lark App ID | `LARK_APP_ID` | App ID user OAuth | `cli_a9dfb41098795ed4` |
| Lark App Secret | `LARK_APP_SECRET` | App secret | `(redacted)` |
| Lark API URL | `LARK_OPEN_URL` | Base API endpoint | `https://open.larksuite.com/open-apis` |
| Lark domain | `LARK_DOMAIN` | Tenant subdomain | `vudanhdu.sg.larksuite.com` |
| Lark CLI path | `LARK_CLI_PATH` | Path lark-cli (optional fallback) | `C:/Users/.../lark-cli.cmd` |
| Working space | `LARK_WORKING_SPACE` | Internal space (CN clone + VI translate) | `7632174671093321442` |
| DST space | `LARK_DST_SPACE` | Public mirror space | `7636576307039473372` |
| CN parent | `LARK_CN_PARENT` | Folder chứa CN clones (working) | `Z414w7CgRiWno8kRw0ElStLngnh` |
| VI parent | `LARK_VI_PARENT` | Folder chứa VI translates (working) | `NTA6wNmhEi8vPdkosMwlm6ilgoe` |

**Scopes Lark app cần** (user OAuth):
- `wiki/*` (read+write+copy+move)
- `docx/*` (read+write+create)
- `bitable/*` + `base/*` (read+write field/record)
- `drive/*` (file:download + file:upload)
- `space:document:*`

### C. Lark Base (state tracking)

| Param | ENV var | Mô tả |
|-------|---------|-------|
| Base app | `LARK_BASE_TOKEN` | Bitable app token (`IXXX...`) |
| Table | `LARK_TABLE_ID` | Table ID (`tblXXX...`) |

Schema 73 fields theo `src/waytoagi/models/base.py:BaseRecord`. Wizard auto-generate template SQL nếu cần.

### D. LLM POOL (translate engine)

File `llm_keys.json` — JSON array of OpenAI-compatible endpoints:

```json
[
  {
    "name": "Local-GPT5",
    "endpoint": "http://localhost:20128/v1",
    "api_key": "sk-xxx",
    "model": "cx/gpt-5.4"
  },
  {
    "name": "Cloud-Claude",
    "endpoint": "https://api.anthropic.com/v1",
    "api_key": "sk-ant-xxx",
    "model": "claude-sonnet-4"
  }
]
```

Round-robin load balance giữa các endpoints.

### E. Tuning (optional)

| Param | ENV var | Default | Range |
|-------|---------|---------|-------|
| Lark rate limit | `LARK_RATE_LIMIT_RPS` | 5 | 1-50 |
| LLM rate limit | `LLM_RATE_LIMIT_RPS` | 10 | 1-100 |
| Cache DB path | `TRANSLATION_CACHE_DB` | `.cache/translations.sqlite` | path |
| Log level | `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |
| Log format | `LOG_FORMAT` | `json` | `json` \| `console` |

---

## 3. 📤 ĐẦU RA (Outputs)

### A. Lark Base table — state tracking (real-time)

73 fields theo `BaseRecord` schema. Updated mỗi stage:

| Field nhóm | Fields | Updated by |
|-----------|--------|-----------|
| Identity | STT, Tiêu đề (VI), Title (CN), Node Token, Obj Token | Crawl |
| Status | Trạng thái, Trạng thái dịch, Source Status, Change Status | All stages |
| Links | Liên kết gốc, clone, dịch, wiki dịch mới | Clone/Translate/Mirror |
| Mirror | Mirror Wiki Node Token, Mirror Wiki Status, Mirror Last Synced At | Mirror/Sync |
| Translate quality | % Dịch, Số segment dịch | Translate |
| Errors | Lỗi, Lỗi dịch, Số lần thử | Any stage on fail |
| Backlinks | Backlink Fix Status, Total, Replaced | Sync |
| Timestamps | Crawled At, Last Seen At, Last Edit Time, Thời gian dịch | Crawl/All |

### B. Working space (DST tenant — internal)

- **CN clones**: bản sao CN trong Lark workspace, giữ block-by-block (text, image, file, table, grid, callout, mention_doc), preserve formatting.
- **VI translates**: in-place block-level translation, cùng node structure CN nhưng content = VI.

### C. DST mirror space (public)

- Wiki tree match thứ tự source CN
- Doc titles VI đầy đủ
- Inline link tiles render VI title (không CN)
- Images/files render đúng (không loading vô tận)
- Tables: cells populated VI text
- URLs internal: `vudanhdu.sg.larksuite.com/wiki/...` (không còn `waytoagi.feishu.cn/...`)

### D. Logs (JSON structured)

`logs/<stage>_<timestamp>.log` — JSON Lines format:

```json
{"timestamp":"2026-05-10T07:30:00","level":"info","correlation_id":"pipe-a1b2c3d4","stage":"clone","record_id":"rec123","stt":12800,"event":"clone_done","duration_seconds":45.2}
```

Audit trail: every Lark Base field write, every LLM call, every API retry.

### E. Cache (SQLite)

`.cache/translations.sqlite` — atomic, indexed, persistent.

```sql
CREATE TABLE translations (
  cn_text_hash TEXT PRIMARY KEY,
  cn_text TEXT NOT NULL,
  vi_text TEXT NOT NULL,
  llm_endpoint TEXT,
  cached_at INTEGER
);
```

---

## 4. 🔒 RÀNG BUỘC CỨNG (Hard Constraints)

### A. Pipeline correctness

1. **Idempotent**: re-run pipeline KHÔNG tạo duplicate records, KHÔNG ghi đè data tốt
2. **Atomic state transitions**: 1 record không thể stuck half-stage
3. **Real-time updates**: mỗi stage xong → write Lark Base NGAY (no batching delay)
4. **Order strict**: CRAWL → CLONE → TRANSLATE → MIRROR → SYNC (KHÔNG skip)
5. **No silent failures**: mọi error log + cập nhật `Lỗi` field

### B. Lark API rules

1. **Rate limit**: tuân thủ `LARK_RATE_LIMIT_RPS` qua aiolimiter token bucket
2. **Transient retry**: codes `99991400, 230001, 131009, 1770001, 1254606` → exponential backoff (2/5/10/15/30s)
3. **Cross-tenant**: source CN dùng Feishu app token, DST dùng user OAuth (Lark app)
4. **Block create order**: image/file phải CREATE block TRƯỚC → upload (block_id) → PATCH replace (NEVER đảo)
5. **Table cells**: populate từng cell qua `update_text_elements` PATCH; fallback `_process_block` nếu PATCH fail
6. **mention_doc title**: translate inline (cache + LLM) khi convert sang text_run

### C. Tenant boundaries

1. **Source**: Feishu CN tenant (`*.feishu.cn`)
2. **DST**: Larksuite tenant (`*.[region.]larksuite.com`)
3. **Clone capabilities**:
   - ✅ docx (full block-by-block)
   - ⚠️ bitable (recreate fields/records, partial)
   - ⚠️ sheet (recreate cells, partial)
   - ❌ file/slides/mindnote (Lark API limit — cần edit perm trên source)
4. **App ID phải khác** giữa source và DST (cross-tenant không share)

### D. Translation rules (CLAUDE.md)

1. **TIẾNG VIỆT có dấu đầy đủ Unicode** (á, à, ả, ã, ạ, â, ầ, ấ, ...)
2. **ZERO ký tự CJK** trong VI output
3. **Hán-Việt** cho tên người (王小明 → Vương Tiểu Minh)
4. **Latin** cho thương hiệu (飞书 → Feishu, 抖音 → Douyin)
5. **Cache hit ≥ 80%** mục tiêu (giảm LLM cost)
6. **LLM POOL round-robin**: KHÔNG hardcode 1 endpoint

### E. Code quality (V2 strict)

1. **Type-safe**: pydantic v2 cho ALL models, `mypy --strict` pass
2. **Test coverage ≥ 70%** (CI fail nếu thấp hơn)
3. **Structured logging**: JSON output, correlation IDs trong mọi log entry
4. **NO hardcoded constants**: tất cả qua `Settings` (env-based)
5. **CI passes**: `ruff` lint + `mypy --strict` + `pytest`
6. **Async-first**: dùng httpx async, aiolimiter, asyncio
7. **Pydantic-validated** mọi Lark API response (no raw dict access)

### F. Security

1. **Secrets từ env vars** — KHÔNG commit `.env`, `llm_keys.json` vào git
2. **`.gitignore` exclude**: `.env`, `llm_keys.json`, `.cache/`, `logs/`, `*.bak.*`
3. **Token rotation**: support refresh tokens, không hardcode access tokens
4. **Audit log**: lưu mọi state change trong Lark Base + correlation ID

### G. Performance

1. **Workers parallel**: 2-4 default, auto-scale lên 10 (cap)
2. **Adaptive throttle**: nếu rate-limit cao → giảm workers
3. **Multipart upload** files > 20MB
4. **Cache disk persistent**: SQLite atomic writes
5. **Connection pooling**: httpx async client reuse
6. **Streaming**: read blocks paginated (not load all in memory)

### H. Operational

1. **Daily cron**: Windows Task Scheduler 06:00 (configurable)
2. **PATH fix**: inject `npm/`, `Python/Scripts/`, `System32/` cho Task Scheduler restricted env
3. **Graceful shutdown**: SIGINT/SIGTERM → flush cache + close connections + persist state
4. **Resume from checkpoint**: pipeline crash mid-run, restart pickup từ checkpoint cuối
5. **Alert on failure**: send Lark IM nếu success rate < 90%

### I. Internationalization

1. **Communication với user**: tiếng Việt có dấu đầy đủ
2. **Code comments**: tiếng Anh OR tiếng Việt có dấu (KHÔNG telex/VNI)
3. **Log messages internal**: tiếng Anh (compatibility với log analysis tools)
4. **Field labels Lark Base**: tiếng Việt có dấu (vd "Tiêu đề", "Trạng thái")

---

## 5. 📋 Config checklist cho project mới

Trước khi run wizard:

- [ ] Source Feishu app created + scopes granted
- [ ] DST Larksuite app created + scopes granted
- [ ] User logged in `lark-cli auth login --as user`
- [ ] Working space created trong DST tenant
- [ ] DST mirror space created
- [ ] CN parent + VI parent folders created
- [ ] Bitable schema match `BaseRecord` model
- [ ] LLM POOL endpoints reachable (curl test)
- [ ] Python 3.11+, Node 18+, lark-cli 1.0.26+ installed

Sau khi setup:

- [ ] `python setup_new_project.py` → fill info
- [ ] `pip install -e ".[dev]"`
- [ ] `pytest` → 23+ tests pass
- [ ] `mypy src/` → no issues
- [ ] `waytoagi crawl --no-resume --dry-run` → fetch source nodes OK
- [ ] `waytoagi pipeline --workers 1 --limit 1` → smoke test 1 record

---

## 6. 🚀 Run modes

| Mode | Command | Use case |
|------|---------|----------|
| Crawl only | `waytoagi crawl --no-resume` | Detect source changes |
| Pipeline | `waytoagi pipeline --workers 4` | Clone + translate Pending |
| Mirror | `waytoagi mirror --resume` | VI → DST tạo nodes |
| Sync | `waytoagi sync --workers 3` | Sync edited records |
| Audit | `waytoagi audit ui\|unaccented\|tree-order` | Read-only quality check |
| **Orchestrate** | `waytoagi orchestrate` | **Full chain auto** |
| Single record | `waytoagi pipeline --from-stt 100 --to-stt 100` | Test/debug |

---

## 7. 🔄 Reset / restart

| Tình huống | Command |
|-----------|---------|
| Reset 1 record về Pending | `waytoagi reset --stt 100` |
| Reset tất cả Failed | `waytoagi reset --status Failed` |
| Clear cache | `rm -rf .cache/` (idempotent — sẽ rebuild) |
| Fresh setup mới | `python setup_new_project.py` (auto backup `.env` cũ) |

---

**Version**: 2.0.0
**Last updated**: 2026-05-10
