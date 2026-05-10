# Project setup — Tutorial cho dự án MỚI

Hướng dẫn chi tiết để bắt đầu **waytoagi v2** với:
- Wiki nguồn của bạn (Feishu CN tenant)
- APP ID Larksuite của bạn
- Workspace mới (Lark Suite tenant của bạn)

## 0. Pre-requisites

| Tool | Version | Verify |
|------|---------|--------|
| Python | 3.11+ | `python --version` |
| Node.js | 18+ (for lark-cli) | `node --version` |
| lark-cli | 1.0.26+ | `lark-cli --version` |
| Git | any | `git --version` |

Install lark-cli nếu chưa có:

```bash
npm install -g @larksuite/lark-cli
```

## 1. Clone repo

```bash
git clone <repo-url> waytoagi-myproject
cd waytoagi-myproject
```

## 2. Tạo Larksuite app (DST tenant)

1. Truy cập https://open.larksuite.com/app
2. **New custom app** → đặt tên (vd "MyProject Mirror Bot")
3. Vào tab **Permissions & scopes** → grant:
   - `wiki:wiki:readonly`
   - `wiki:node:read`, `wiki:node:create`, `wiki:node:copy`, `wiki:node:update`, `wiki:node:move`
   - `wiki:space:read`, `wiki:space:retrieve`, `wiki:space:write_only`
   - `docx:document:readonly`, `docx:document:write_only`, `docx:document:create`
   - `bitable:app`, `bitable:app:readonly`
   - `base:app:create`, `base:app:read`, `base:app:update`
   - `base:table:create`, `base:table:read`, `base:table:update`, `base:table:delete`
   - `base:field:create`, `base:field:read`, `base:field:update`, `base:field:delete`
   - `base:record:create`, `base:record:read`, `base:record:update`, `base:record:delete`
   - `drive:drive`, `drive:file:download`, `drive:file:upload`
   - `space:document:retrieve`, `space:document:move`, `space:document:shortcut`
4. **Save app** → ghi nhớ:
   - **App ID** (`cli_xxxxx`)
   - **App Secret**

## 3. Tạo Feishu source app (CN tenant)

App này dùng để đọc wiki nguồn cross-tenant.

1. Truy cập https://open.feishu.cn/app
2. **New custom app** với scopes minimum:
   - `wiki:wiki:readonly`
   - `docx:document:readonly`
   - `drive:file:download`
3. **Publish to internal** trong tenant của bạn (nếu cần, hoặc dùng tenant access token)
4. Ghi nhớ App ID + App Secret

## 4. Lấy IDs cần thiết

### Source space_id (Feishu CN)

URL wiki nguồn: `https://example.feishu.cn/wiki/SPACE_TOKEN_HERE...`

Open wiki nguồn → Inspect → tìm `spaceId` trong network tab. Hoặc:

```bash
lark-cli api GET "/open-apis/wiki/v2/spaces" --as user --format json
```

### Working space + Mirror space (Larksuite DST)

Tạo 2 spaces trong Lark của bạn:
- **Working space**: chứa CN clone + VI translate (private, dev only)
- **Mirror space**: public, end users xem

Lấy `space_id` từ URL: `https://yourtenant.sg.larksuite.com/wiki/settings/SPACE_ID`

### CN parent + VI parent

Trong Working space, tạo 2 thư mục:
- "CN clones" → ghi nhớ `node_token` (URL: `/wiki/NODE_TOKEN`)
- "VI translates" → ghi nhớ `node_token`

### Lark Base (state table)

1. Tạo Bitable mới (app token là `IXXXXXXX...`)
2. Tạo table với schema theo `src/waytoagi/models/base.py` (auto fill nếu dùng wizard)
3. Ghi nhớ `app_token` + `table_id`

## 5. Setup LLM POOL

Tạo file `llm_keys.json` (sẽ được wizard load):

```json
[
  {
    "name": "GPT-4-local",
    "endpoint": "http://localhost:20128/v1",
    "api_key": "sk-xxxxx",
    "model": "gpt-4"
  },
  {
    "name": "GPT-4-cloud",
    "endpoint": "https://api.openai.com/v1",
    "api_key": "sk-yyyyy",
    "model": "gpt-4o"
  }
]
```

(Hoặc OpenAI compatible: Anthropic Claude, Ollama, vLLM, etc.)

## 6. Run wizard

```bash
python setup_new_project.py
```

Wizard hỏi tuần tự ALL params trên + write `.env`, `llm_keys.json`, `PROJECT_SETUP.md`.

## 7. Login Lark CLI

```bash
lark-cli config init --app-id <YOUR_LARK_APP_ID> --brand lark
lark-cli auth login --as user
lark-cli auth status --verify  # verify scopes đầy đủ
```

## 8. Install + verify

```bash
pip install -e ".[dev]"
python -c "from waytoagi.config import get_settings; print(get_settings().model_dump_json(indent=2))"
```

Output sẽ show config đã load.

## 9. Run

```bash
# Crawl source CN wiki, detect changes
waytoagi crawl --no-resume

# Pipeline: clone + translate Pending records
waytoagi pipeline --workers 4 --auto-scale

# Mirror VI → DST
waytoagi mirror --resume

# Sync content VI → DST cho records edited
waytoagi sync --workers 3

# Audit UI
waytoagi audit ui
waytoagi audit unaccented
waytoagi audit tree-order

# Run all stages tự động
waytoagi orchestrate
```

## 10. Daily automation (Windows Task Scheduler)

```powershell
$taskAction = New-ScheduledTaskAction `
  -Execute "python" `
  -Argument "-m waytoagi.cli orchestrate" `
  -WorkingDirectory "C:\path\to\waytoagi-myproject"

$taskTrigger = New-ScheduledTaskTrigger -Daily -At "06:00"

Register-ScheduledTask -TaskName "WaytoAGI-MyProject-Daily" `
  -Action $taskAction -Trigger $taskTrigger -RunLevel Highest
```

## Troubleshooting

### `131006 permission denied` cho wiki.copy

Source CN node cần edit perm. Code tự fallback `clone_typed.clone_docx_blocks` (block-by-block via Feishu app token) — đảm bảo Feishu app có scopes đúng.

### `99991400 rate limit`

Giảm `LARK_RATE_LIMIT_RPS` trong `.env`. Hoặc giảm workers trong CLI:

```bash
waytoagi pipeline --workers 2  # giảm từ default
```

### `FileNotFoundError: lark-cli.cmd`

Thêm `LARK_CLI_PATH` vào `.env` với absolute path:

```
LARK_CLI_PATH=C:/Users/yourname/AppData/Roaming/npm/lark-cli.cmd
```

### Reset token Lark CLI

```bash
lark-cli auth logout --as user
lark-cli auth login --as user
```

## Migration giữa projects

Nếu chuyển từ project cũ sang project mới:
1. Backup `.env`, `llm_keys.json`, `.cache/translations.sqlite` của project cũ
2. Run wizard cho project mới
3. Optional: copy `.cache/translations.sqlite` sang project mới (reuse cache CN→VI translate)

## Resources

- Lark API docs: https://open.larksuite.com/document
- Feishu API docs: https://open.feishu.cn/document
- Pydantic v2: https://docs.pydantic.dev
- structlog: https://www.structlog.org

---

**Note**: Wizard `setup_new_project.py` reusable cho project sau. Re-run bất cứ lúc nào để update config (auto backup `.env` cũ).
