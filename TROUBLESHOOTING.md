# TROUBLESHOOTING — Lỗi thường gặp + Cách sửa

## Lark API Errors

### `99991400` — Rate limit

**Triệu chứng**: pipeline fail nhanh sau vài phút, log có `[99991400]`.

**Nguyên nhân**: vượt RPS cap (Lark Base writes 50 req/s, Wiki 100 req/s).

**Cách sửa**:
1. Giảm `--workers` xuống 2-3
2. Đợi 5-10 phút cho quota reset
3. Verify `QuotaTracker` đang track đúng resource:
   ```bash
   waytoagi status --watch
   ```

V2 đã tự động xử lý qua:
- `AdaptiveConcurrency` → tự giảm workers khi rate-limit > 15%
- `RetryPolicy` → exponential backoff 2/4/8/16/32/60s
- `CircuitBreaker` → fail-fast nếu liên tục rate-limit

---

### `230001` — Frequency control

**Nguyên nhân**: tương tự 99991400 nhưng kiểm tra theo daily cap.

**Cách sửa**: đợi đến hôm sau hoặc dùng app credential khác.

---

### `131005` — Resource not found

**Nguyên nhân**: source CN node đã bị xoá / token sai.

**Cách sửa**: V2 tự mark `Source Status = Deleted` cho record. Không cần can thiệp.

---

### `131006` — Permission denied

**Nguyên nhân**: lark-cli profile thiếu scope.

**Cách sửa**:
```bash
# Re-init profile với scopes đầy đủ
lark-cli config init --brand feishu --name feishu-src
# Khi prompt scopes, chọn ALL: wiki, base, docs, drive, im
```

Verify:
```bash
lark-cli auth status --as user --name feishu-src
# Check trường "scopes" có đủ các permissions cần thiết
```

---

### `131009` / `1254606` / `4000080` — Resource locked

**Nguyên nhân**: Lark đang lock resource (vd "Page is being copied").

**Cách sửa**: V2 tự retry với backoff 5/10/20/40/60s. Nếu vẫn fail sau 5 attempts, mark `STAGE1-LOCK-FAIL` — chạy lại sau 30 phút.

---

### `99991663` — Token expired

**Nguyên nhân**: tenant_access_token hết hạn (Lark cấp 2h/lần).

**Cách sửa**: V2 `LarkAuth` tự refresh proactive trước khi expire. Nếu fail:
```bash
lark-cli auth refresh --as user --name feishu-src
```

---

## Network Errors

### `Connection refused` / `Connection reset`

**Nguyên nhân**: Lark API server tạm down hoặc network local issue.

**Cách sửa**: V2 `ErrorClassifier` mark `TRANSIENT_NETWORK` → retry quick 1/2/4/8s. Nếu liên tục, kiểm tra:
```bash
ping open.feishu.cn
ping open.larksuite.com
```

---

### `read timed out`

**Nguyên nhân**: response slow (file lớn upload, doc 5000+ blocks).

**Cách sửa**: V2 đã tăng timeout cho download/upload lên 120s. Nếu vẫn fail, doc quá lớn → split bằng cách dùng `--max-md-chars 100000`.

---

## LLM Errors

### `quota exceeded` / `insufficient_quota`

**Triệu chứng**: log có `[TRANSIENT_QUOTA]`, translate fail rate cao.

**Nguyên nhân**: hết quota LLM endpoint.

**Cách sửa**:
1. Verify `llm_keys.json` có ≥ 2 endpoints
2. V2 `LLMPool` tự rotate; `CircuitBreaker` fail-fast endpoint dead
3. Nếu cả pool dead, đợi reset hoặc top-up quota

---

### `rate_limit reached`

**Cách sửa**: V2 backoff theo `TRANSIENT_RATE_LIMIT` category — 2/4/8/16s. Tự động.

---

### `empty LLM stream`

**Nguyên nhân**: LLM trả response rỗng (model issue, content filter).

**Cách sửa**: V2 retry với strict prompt; sau 1 retry vẫn fail → cache fallback hoặc giữ source text.

---

## Pipeline State Issues

### Records stuck `Trạng thái = Pending` mãi

**Cách check**:
```bash
waytoagi status --watch
# Xem Pipeline Stage column trong Lark Base
```

**Nguyên nhân thường gặp**:
- `Mirror Wiki Node Token` rỗng → CrawlStage chưa tạo placeholder
- Source Status = Deleted → V2 skip
- Number of attempts ≥ 5 → `STAGE_FAIL` permanent

**Cách sửa**: reset state thủ công trong Lark Base:
- Clear `Trạng thái`, `Trạng thái dịch`, `Lỗi`
- Set `Pipeline Stage = Pending`
- Chạy lại `waytoagi pipeline`

---

### Crash/mất điện giữa chừng

V2 tự resume nhờ `CrawlCheckpoint` + `PersistentQueue`:

```bash
waytoagi resume
# Hoặc đơn giản chạy lại lệnh cũ — V2 tự pickup checkpoint
waytoagi orchestrate
```

---

## Schema Issues

### Field bị thiếu trong Lark Base

```bash
waytoagi schema-migrate --dry-run    # xem missing fields
waytoagi schema-migrate              # tạo thật
```

V2 idempotent: re-run skip fields đã có.

---

### Sai field type (vd Number nhưng tạo Text)

**Cách sửa thủ công**:
1. Xoá field sai trong Lark Base UI
2. `waytoagi schema-migrate` để tạo lại đúng type

V2 sẽ alert qua `MigrationDiff.type_mismatch`.

---

## Performance Issues

### Doc 5000+ blocks chạy quá lâu

V2 đã tự xử lý qua:
- `BatchTranslator`: 30 blocks / 1 LLM call → giảm 30× round-trip
- `StreamingPipeline`: clone + translate overlap
- `AdaptiveConcurrency`: tự scale workers

Nếu vẫn chậm:
```bash
waytoagi pipeline --workers 8 --max-md-chars 200000
```

---

### Memory leak / OOM

**Triệu chứng**: process bị kill sau 1-2h chạy.

**Cách sửa**:
1. `StreamingPipeline` đã có backpressure (queue_size limit)
2. Nếu vẫn OOM, giảm `--workers` xuống 2
3. Cleanup cache cũ:
   ```bash
   rm -rf .cache/translation_cache.db
   waytoagi orchestrate
   ```

---

## CI/CD Issues

### GitHub Actions test fail nhưng local pass

**Nguyên nhân**: Python version mismatch.

**Cách sửa**: ensure local Python 3.13:
```bash
python --version
```

CI dùng Python 3.13 — local cũ hơn có thể có behaviour khác.

---

### `mypy strict` fail mới

V2 yêu cầu mypy strict pass 100%. Khi thêm code mới:
```bash
python -m mypy src/
# Fix errors trước khi commit
```

---

## Khẩn cấp — Liên hệ

Nếu lỗi không trong danh sách trên, gửi log + screenshot kèm:
- File `.env` (đã redact secrets)
- Output `waytoagi preflight`
- Last 100 dòng log

Liên hệ admin VŨ DANH DỰ qua Lark IM.
