# WaytoAGI V2 — Wiki Mirror tiếng Việt

**Phiên bản 2.0** — rewrite từ đầu với ràng buộc nghiêm ngặt: Pydantic v2,
mypy strict, structlog, async-first, tests ≥ 70% coverage, CI/CD đầy đủ.

Đồng bộ + dịch ~12,800 docs CN từ wiki **WaytoAGI** (`waytoagi.feishu.cn`)
→ Larksuite tiếng Việt (`vudanhdu.sg.larksuite.com`) cho người Việt đọc.

---

## 🎯 Mục tiêu

- **Đầy đủ**: clone toàn bộ blocks (text/heading/list/image/file/table/grid/callout/board)
- **Thuần Việt**: dịch chuẩn, văn phong tự nhiên, ZERO ký tự CJK trong output
- **Tự động**: pipeline self-healing, resume sau crash, scale theo rate-limit
- **Đo lường được**: real-time updates Lark Base + audit trail per stage

---

## 🏗️ Kiến trúc 7 stages

```
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 0: PreflightHealthCheck  (~10s, fail-fast)               │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 1: CrawlStage + PlaceholderCreator (eager)               │
│   • Walk source CN DFS                                          │
│   • Detect NEW/EDITED/RENAMED/UNCHANGED/DELETED                 │
│   • Tạo dst placeholder NGAY → src→dst map có sẵn              │
│   • CrawlCheckpoint resume sau crash                            │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 2: CloneStage (block-by-block)                           │
│   • Per block_type handler (text/image/file/table/grid/...)     │
│   • MediaHandler: download CN → upload DST → PATCH replace      │
│   • UrlMapper swap URL CN→DST INLINE (eager backlink)           │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 3: TranslateStage (in-place)                             │
│   • BatchTranslator: 30 blocks / 1 LLM call                     │
│   • TranslationCache SHA-256                                    │
│   • Glossary CN→VI (~100 entries chuẩn)                         │
│   • Layered prompts (style guide + few-shot)                    │
│   • Quality gate: CJK leak detector + auto-retry strict         │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 4: MirrorStage                                           │
│   • Fill VI content vào DST placeholder                         │
│   • Update DST wiki node title (CN → VI)                        │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 5: SmartSyncStage (block-level diff)                     │
│   • Hash mỗi block, chỉ patch blocks changed                    │
│   • 100× ít PATCH calls cho doc edit nhỏ                        │
└──────────────────────────┬──────────────────────────────────────┘
                           ▼
┌─────────────────────────────────────────────────────────────────┐
│  STAGE 6: TreeOrderStage                                        │
│   • Reorder dst tree match source DFS order                     │
│   • Prefix-match optimization (giảm N→K moves)                  │
│   • Per-parent threshold (skip subtree > 50 children)           │
└─────────────────────────────────────────────────────────────────┘
```

### Resilience layer (chạy xuyên suốt)

```
ErrorClassifier  → phân loại errors (rate-limit/network/auth/quota/lock)
CircuitBreaker   → fail-fast khi endpoint dead → route khác
QuotaTracker     → sliding window, predict + proactive throttle
PersistentQueue  → SQLite durable queue, recover sau crash/mất điện
GracefulShutdown → SIGTERM/SIGINT → flush cleanup, không hang
RetryPolicy      → centralized retry với category-aware backoff
```

---

## 🚀 Cài đặt nhanh

```bash
# 1. Clone repo
git clone <repo-url> vddclonelark-v2
cd vddclonelark-v2

# 2. Cài đặt
pip install -e ".[dev]"

# 3. Chạy wizard setup (tạo .env + cài Lark Base table)
python setup_new_project.py

# 4. Verify môi trường
waytoagi preflight

# 5. Tạo schema Lark Base (80 fields, 12 groups)
waytoagi schema-migrate

# 6. Chạy thử
waytoagi crawl --dry-run --max-records 5
waytoagi orchestrate --dry-run

# 7. Chạy thực
waytoagi orchestrate --workers 4
```

Xem thêm [`SETUP.md`](./SETUP.md) cho hướng dẫn chi tiết.

---

## 📋 Commands

```bash
waytoagi preflight              # Kiểm tra sức khoẻ (~10s)
waytoagi schema-migrate         # Tạo missing fields trong Lark Base
waytoagi crawl                  # Quét source CN + tạo placeholder DST
waytoagi pipeline               # Clone + translate cho records pending
waytoagi mirror                 # Fill VI content vào DST placeholder
waytoagi sync                   # Sync block-level diff (records edited)
waytoagi reorder                # Fix tree order DST match source
waytoagi orchestrate            # Chạy toàn bộ pipeline tự động
waytoagi status [--watch]       # Xem tiến độ realtime
waytoagi audit [ui|translation|tree|all]  # Audit chất lượng
waytoagi resume                 # Resume sau crash
```

Mọi command hỗ trợ:
- `--dry-run` — chạy thử không ghi xuống Lark
- `--workers N` — số records song song (default 4)
- `--verbose` / `-v` — log chi tiết
- `--config PATH` — đường dẫn .env (default `.env`)

---

## 🧪 Tests + Quality

```bash
# Unit tests + coverage
pytest tests/unit/ -v

# Type check (strict)
mypy src/

# Lint
ruff check src/ tests/

# Format
ruff format src/ tests/
```

Hiện tại: **458+ tests passing**, **mypy strict OK**, **coverage ≥ 70%**.

---

## 📂 Cấu trúc thư mục

```
src/waytoagi/
├── config/         # Pydantic Settings (env)
├── models/         # Pydantic schemas (BaseRecord, Block, ...)
├── lark/           # Lark API clients (auth, base, wiki, document, media)
├── llm/            # LLM POOL + glossary + prompts + quality gates
├── cache/          # SQLite cache (translation + media tokens)
├── backlinks/      # UrlMapper CN→DST swap
├── crawl/          # CrawlCheckpoint (resume)
├── preflight/      # PreflightHealthCheck (fail-fast)
├── reorder/        # TreeOrder pure diff algorithm
├── sync/           # Block-level diff (smart sync)
├── stages/         # 7 stages (Crawl/Clone/Translate/Mirror/Sync/Reorder)
├── optimize/       # BatchTranslator, AdaptiveConcurrency, StreamingPipeline
├── resilience/     # CircuitBreaker, QuotaTracker, RetryPolicy, ...
├── base_schema/    # Lark Base 80-field schema + AuditTrail + migration
├── orchestrator.py # PipelineOrchestrator wire 7 stages
├── cli.py          # Click CLI entry point
└── logging.py      # structlog setup
```

---

## 🔗 Tài liệu liên quan

- [`SETUP.md`](./SETUP.md) — hướng dẫn cài đặt step-by-step
- [`TROUBLESHOOTING.md`](./TROUBLESHOOTING.md) — lỗi thường gặp + cách sửa
- [`PROJECT_REQUIREMENTS.md`](./PROJECT_REQUIREMENTS.md) — đầu vào/ra + ràng buộc
- [`PROJECT_TEMPLATE.md`](./PROJECT_TEMPLATE.md) — tutorial setup project mới

---

## 📜 License

Internal project. Bản quyền thuộc về VŨ DANH DỰ.
