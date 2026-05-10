# SETUP — Hướng dẫn cài đặt WaytoAGI V2

## 1. Yêu cầu môi trường

- **Python 3.13+**
- **lark-cli** (Node.js global) — multi-profile support cho Feishu + Larksuite
- **SQLite 3.35+** (built-in Python 3.13)
- **Tài khoản Lark** với quyền:
  - Read trên source space CN (vd `waytoagi.feishu.cn`)
  - Write trên destination space Larksuite
  - CRUD trên Bitable (Lark Base) table

## 2. Cài đặt Python package

```bash
git clone <repo-url> vddclonelark-v2
cd vddclonelark-v2
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

## 3. Cài đặt lark-cli (Node)

```bash
npm install -g @larksuiteoapi/lark-cli
```

Verify:
```bash
lark-cli --version
```

## 4. Tạo 2 lark-cli profiles

**Profile cho source CN tenant** (Feishu):
```bash
lark-cli config init --brand feishu --name feishu-src
# Nhập App ID + App Secret từ Feishu open platform
```

**Profile cho dst Larksuite tenant**:
```bash
lark-cli config init --brand lark --name lark-dst
# Nhập App ID + App Secret từ Larksuite open platform
```

Verify:
```bash
lark-cli config list
# Phải thấy 2 profiles: feishu-src + lark-dst
```

## 5. Tạo Lark Base table

1. Vào Lark Base → tạo bitable mới
2. Lấy `app_token` từ URL (vd `bascn1234567890`)
3. Lấy `table_id` của default table

## 6. Chuẩn bị LLM keys

Tạo file `llm_keys.json` (hoặc dùng env vars):
```json
[
  {
    "name": "local",
    "endpoint": "http://localhost:20128/v1",
    "api_key": "...",
    "model": "gpt-5.4"
  },
  {
    "name": "llmgate",
    "endpoint": "https://llmgate.example.com/v1",
    "api_key": "...",
    "model": "gpt-4-turbo"
  }
]
```

## 7. Chạy wizard setup

```bash
python setup_new_project.py
```

Wizard hỏi:
- Source URL (vd `https://waytoagi.feishu.cn/wiki/SOURCE_NODE_TOKEN`)
- Lark Base table credentials
- LLM keys path
- DST space + parent token

→ Wizard tạo `.env` + verify connection.

## 8. Migrate Lark Base schema

```bash
waytoagi schema-migrate --dry-run    # xem sẽ tạo gì
waytoagi schema-migrate              # tạo thật
```

Sẽ tạo 80 fields trong table theo schema V2 (đã có thì skip).

## 9. Verify môi trường

```bash
waytoagi preflight
```

Kiểm tra:
- ✅ Source token có scope read
- ✅ DST token có scope write
- ✅ Bitable table tồn tại
- ✅ LLM POOL alive
- ✅ DST parent slot count

## 10. Chạy thử với 5 records

```bash
waytoagi crawl --max-records 5
waytoagi pipeline --workers 2
waytoagi mirror
waytoagi reorder
```

Kiểm tra Lark Base — 5 records mới với:
- `Pipeline Stage = Done`
- `Liên kết wiki dịch mới` đã có URL DST
- `Audit Trail` ghi đầy đủ events

## 11. Chạy thật

```bash
# Toàn bộ pipeline tự động
waytoagi orchestrate --workers 4

# Hoặc step-by-step
waytoagi crawl
waytoagi pipeline --workers 4
waytoagi mirror
waytoagi sync       # cho records edited
waytoagi reorder
waytoagi audit all
```

## 12. Lập lịch chạy hàng ngày

### Windows Task Scheduler

```powershell
# Đăng ký
$action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "-m waytoagi.cli orchestrate --workers 4" `
    -WorkingDirectory "C:\path\to\vddclonelark-v2"
$trigger = New-ScheduledTaskTrigger -Daily -At 6am
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -RunLevel Highest
Register-ScheduledTask -TaskName "WaytoAGI-Daily" `
    -Action $action -Trigger $trigger -Principal $principal
```

### Linux/macOS cron

```cron
0 6 * * * cd /path/to/vddclonelark-v2 && python -m waytoagi.cli orchestrate --workers 4
```

---

## 🐛 Khi gặp lỗi

Xem [`TROUBLESHOOTING.md`](./TROUBLESHOOTING.md) cho danh sách lỗi thường gặp.
