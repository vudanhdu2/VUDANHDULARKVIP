"""Interactive wizard — setup waytoagi v2 cho project mới.

Standalone script, không depend ngoài stdlib. Chạy trên máy bất kỳ có Python 3.11+:

    python setup_new_project.py

Wizard sẽ:
  1. Hỏi info source wiki (Feishu CN tenant)
  2. Hỏi info dst Larksuite tenant
  3. Hỏi Lark Base app + table
  4. Hỏi LLM endpoints (or load từ existing llm_keys.json)
  5. Validate format các IDs
  6. Generate .env + llm_keys.json
  7. Print next steps

Reusable cho project sau — chỉ cần chạy lại với info mới.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# ============================================================
# Validators (no external deps — pure stdlib)
# ============================================================


def validate_lark_app_id(value: str) -> str:
    """Lark App ID format: cli_xxxxxxxxxx (~16 chars sau prefix)."""
    if not re.match(r"^cli_[a-z0-9]{12,20}$", value):
        raise ValueError(
            f"App ID không đúng format. Expected 'cli_xxxxx', got: {value!r}"
        )
    return value


def validate_lark_token(value: str, prefix: str = "") -> str:
    """Lark token format: alphanumeric, 18-30 chars."""
    if not re.match(r"^[A-Za-z0-9]{18,40}$", value):
        raise ValueError(
            f"Token không đúng format ({prefix}). Expected 18-40 alphanumeric chars, got: {value!r}"
        )
    return value


def validate_space_id(value: str) -> str:
    """Lark space_id format: 19-digit numeric string."""
    if not re.match(r"^\d{15,25}$", value):
        raise ValueError(
            f"Space ID không đúng format. Expected 15-25 digits, got: {value!r}"
        )
    return value


def validate_table_id(value: str) -> str:
    """Lark Bitable table_id format: tblxxxxxxxxxx."""
    if not re.match(r"^tbl[A-Za-z0-9]{12,30}$", value):
        raise ValueError(
            f"Table ID không đúng format. Expected 'tblxxxxx', got: {value!r}"
        )
    return value


def validate_domain(value: str) -> str:
    """Domain format: foo.feishu.cn hoặc foo.bar.larksuite.com (cho phép subdomain)."""
    if not re.match(
        r"^[a-z0-9][a-z0-9.-]*\.(feishu\.cn|larksuite\.com)$", value, re.IGNORECASE
    ):
        raise ValueError(
            f"Domain không đúng format. Expected 'foo.feishu.cn' hoặc 'foo.[region.]larksuite.com', got: {value!r}"
        )
    return value.lower()


def validate_url(value: str) -> str:
    """URL format check."""
    if not re.match(r"^https?://", value):
        raise ValueError(f"URL phải bắt đầu http:// hoặc https://, got: {value!r}")
    return value


def validate_nonempty(value: str) -> str:
    """Non-empty string."""
    if not value or not value.strip():
        raise ValueError("Giá trị không được rỗng")
    return value.strip()


# ============================================================
# Prompt helpers
# ============================================================


def prompt(
    question: str,
    *,
    default: str | None = None,
    validator: Any = validate_nonempty,
    secret: bool = False,
) -> str:
    """Prompt user input với validation + default + retry on invalid.

    Args:
        question: prompt text
        default: default value nếu user nhập rỗng
        validator: callable(str) -> str — raise ValueError nếu invalid
        secret: nếu True, không echo input (cho secret/password)

    Returns:
        Validated value
    """
    suffix = f" [{default}]" if default is not None else ""
    if secret:
        import getpass

        while True:
            value = getpass.getpass(f"{question}{suffix}: ").strip()
            if not value and default is not None:
                value = default
            try:
                return str(validator(value))
            except ValueError as e:
                print(f"  ❌ {e}\n  → Vui lòng nhập lại.")
    else:
        while True:
            value = input(f"{question}{suffix}: ").strip()
            if not value and default is not None:
                value = default
            try:
                return str(validator(value))
            except ValueError as e:
                print(f"  ❌ {e}\n  → Vui lòng nhập lại.")


def prompt_yes_no(question: str, *, default: bool = True) -> bool:
    """Prompt yes/no."""
    default_str = "Y/n" if default else "y/N"
    while True:
        value = input(f"{question} [{default_str}]: ").strip().lower()
        if not value:
            return default
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        print("  → Nhập 'y' hoặc 'n'.")


def section(title: str) -> None:
    """Print section header."""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


# ============================================================
# Main wizard
# ============================================================


def run_wizard() -> dict[str, Any]:
    """Run interactive wizard, return collected config dict."""
    config: dict[str, Any] = {}

    print()
    print("╔" + "═" * 58 + "╗")
    print("║  WAYTOAGI v2 — Setup wizard cho project mới             ║")
    print("║  (Wiki nguồn + APP ID + workspace tham số hóa)          ║")
    print("╚" + "═" * 58 + "╝")
    print()
    print("Wizard sẽ thu thập config + generate .env + llm_keys.json.")
    print("Mỗi bước có default — Enter để dùng default, hoặc nhập giá trị mới.")
    print()
    input("Nhấn ENTER để bắt đầu...")

    # ============================================================
    section("1️⃣  SOURCE WIKI (Feishu CN tenant)")
    # ============================================================
    print("Wiki nguồn để crawl + clone TỪ. Vd waytoagi.feishu.cn")
    config["source_domain"] = prompt(
        "Source domain",
        default="waytoagi.feishu.cn",
        validator=validate_domain,
    )
    config["source_space_id"] = prompt(
        "Source space_id (19 digit, từ URL wiki nguồn)",
        validator=validate_space_id,
    )
    print("\n💡 Source Feishu app credentials (cần để đọc blocks cross-tenant):")
    print("   Tạo tại: https://open.feishu.cn/app → New custom app")
    print("   Scopes cần: wiki:wiki:readonly, docx:document:readonly, drive:file:download")
    config["feishu_app_id"] = prompt(
        "Feishu App ID (cli_xxxxx)",
        validator=validate_lark_app_id,
    )
    config["feishu_app_secret"] = prompt(
        "Feishu App Secret",
        validator=validate_nonempty,
        secret=True,
    )

    # ============================================================
    section("2️⃣  DST LARKSUITE TENANT (Vietnamese mirror)")
    # ============================================================
    print("Lark Suite tenant để clone + dịch + mirror VÀO.")
    config["dst_domain"] = prompt(
        "DST domain",
        default="vudanhdu.sg.larksuite.com",
        validator=validate_domain,
    )
    config["dst_space_working"] = prompt(
        "Working space_id (chứa CN clone + VI translate)",
        validator=validate_space_id,
    )
    config["dst_space_mirror"] = prompt(
        "Mirror space_id (public dst, nơi user xem)",
        validator=validate_space_id,
    )
    config["dst_cn_parent"] = prompt(
        "CN clone parent node (token nơi chứa CN clones trong working)",
        validator=lambda v: validate_lark_token(v, "CN parent"),
    )
    config["dst_vi_parent"] = prompt(
        "VI translate parent node (token nơi chứa VI docs trong working)",
        validator=lambda v: validate_lark_token(v, "VI parent"),
    )
    print("\n💡 Larksuite app credentials (user OAuth):")
    print("   Tạo tại: https://open.larksuite.com/app → New custom app")
    print("   Scopes: wiki/docs/base full + drive:file:upload")
    config["lark_app_id"] = prompt(
        "Larksuite App ID (cli_xxxxx)",
        validator=validate_lark_app_id,
    )
    config["lark_brand"] = "lark"  # always lark for SG/JP/US

    lark_cli_default = r"C:\Users\vudan\AppData\Roaming\npm\lark-cli.cmd"
    config["lark_cli_path"] = prompt(
        "Path tới lark-cli executable",
        default=lark_cli_default,
        validator=validate_nonempty,
    )

    # ============================================================
    section("3️⃣  LARK BASE (state table)")
    # ============================================================
    print("Bitable lưu state của pipeline (records, status, links).")
    config["base_app_token"] = prompt(
        "Base app token (từ URL Bitable)",
        validator=lambda v: validate_lark_token(v, "Base app"),
    )
    config["base_table_id"] = prompt(
        "Base table_id (tblxxxxx)",
        validator=validate_table_id,
    )

    # ============================================================
    section("4️⃣  LLM POOL (translate engine)")
    # ============================================================
    print("LLM endpoints OpenAI-compatible cho translate CN→VI.")
    if prompt_yes_no("Có file llm_keys.json sẵn để import?", default=True):
        path_str = prompt(
            "Path tới llm_keys.json",
            default="llm_keys.json",
            validator=validate_nonempty,
        )
        try:
            data = json.loads(Path(path_str).read_text(encoding="utf-8"))
            assert isinstance(data, list)
            config["llm_endpoints"] = data
            print(f"  ✓ Loaded {len(data)} LLM endpoints từ {path_str}")
        except Exception as e:
            print(f"  ❌ Failed to load: {e}")
            config["llm_endpoints"] = _prompt_llm_endpoints()
    else:
        config["llm_endpoints"] = _prompt_llm_endpoints()

    # ============================================================
    section("5️⃣  RATE LIMITING + LOGGING")
    # ============================================================
    config["lark_rate_limit_rps"] = int(
        prompt("Lark API rate limit (req/s)", default="5", validator=validate_nonempty)
    )
    config["llm_rate_limit_rps"] = int(
        prompt("LLM rate limit (req/s)", default="10", validator=validate_nonempty)
    )
    config["log_level"] = prompt(
        "Log level (DEBUG/INFO/WARNING/ERROR)",
        default="INFO",
        validator=validate_nonempty,
    )
    config["log_format"] = prompt(
        "Log format (json|console)",
        default="json",
        validator=lambda v: (v if v in ("json", "console") else (_ for _ in ()).throw(
            ValueError("phải là 'json' hoặc 'console'")
        )),
    )

    return config


def _prompt_llm_endpoints() -> list[dict[str, str]]:
    """Prompt nhiều LLM endpoints."""
    endpoints: list[dict[str, str]] = []
    while True:
        print(f"\n📡 Endpoint #{len(endpoints) + 1}:")
        ep: dict[str, str] = {}
        ep["name"] = prompt("  Name (vd 'GPT-5.4-local')", validator=validate_nonempty)
        ep["endpoint"] = prompt(
            "  Endpoint URL", default="http://localhost:20128/v1", validator=validate_url
        )
        ep["api_key"] = prompt("  API key", validator=validate_nonempty, secret=True)
        ep["model"] = prompt("  Model name (vd 'cx/gpt-5.4')", validator=validate_nonempty)
        endpoints.append(ep)
        if not prompt_yes_no("Thêm endpoint nữa?", default=False):
            break
    return endpoints


# ============================================================
# Output writers
# ============================================================


def write_env(config: dict[str, Any], path: Path) -> None:
    """Write .env file."""
    lines = [
        "# WaytoAGI v2 — Project config",
        "# Auto-generated by setup_new_project.py",
        "",
        "# ============================================================",
        "# Source wiki (Feishu CN tenant)",
        "# ============================================================",
        f"FEISHU_APP_ID={config['feishu_app_id']}",
        f"FEISHU_APP_SECRET={config['feishu_app_secret']}",
        f"LARK_SRC_SPACE={config['source_space_id']}",
        f"# Source domain (informational): {config['source_domain']}",
        "",
        "# ============================================================",
        "# DST Larksuite tenant",
        "# ============================================================",
        f"LARK_APP_ID={config['lark_app_id']}",
        f"LARK_DOMAIN={config['dst_domain']}",
        f"LARK_CLI_PATH={config['lark_cli_path']}",
        f"LARK_WORKING_SPACE={config['dst_space_working']}",
        f"LARK_DST_SPACE={config['dst_space_mirror']}",
        f"LARK_CN_PARENT={config['dst_cn_parent']}",
        f"LARK_VI_PARENT={config['dst_vi_parent']}",
        "",
        "# ============================================================",
        "# Lark Base (state table)",
        "# ============================================================",
        f"LARK_BASE_TOKEN={config['base_app_token']}",
        f"LARK_TABLE_ID={config['base_table_id']}",
        "",
        "# ============================================================",
        "# LLM POOL (loaded từ file)",
        "# ============================================================",
        "LLM_KEYS_FILE=llm_keys.json",
        "",
        "# ============================================================",
        "# Rate limiting",
        "# ============================================================",
        f"LARK_RATE_LIMIT_RPS={config['lark_rate_limit_rps']}",
        f"LLM_RATE_LIMIT_RPS={config['llm_rate_limit_rps']}",
        "",
        "# ============================================================",
        "# Cache",
        "# ============================================================",
        "TRANSLATION_CACHE_DB=.cache/translations.sqlite",
        "",
        "# ============================================================",
        "# Logging",
        "# ============================================================",
        f"LOG_LEVEL={config['log_level']}",
        f"LOG_FORMAT={config['log_format']}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  ✓ Written: {path}")


def write_llm_keys(endpoints: list[dict[str, str]], path: Path) -> None:
    """Write llm_keys.json."""
    path.write_text(json.dumps(endpoints, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ Written: {path} ({len(endpoints)} endpoints)")


def write_setup_guide(config: dict[str, Any], path: Path) -> None:
    """Write PROJECT_SETUP.md với info project + next steps."""
    content = f"""# Project setup — Generated {Path(__file__).stem}

## 📌 Project info

| Param | Value |
|-------|-------|
| Source domain | `{config['source_domain']}` |
| Source space_id | `{config['source_space_id']}` |
| Feishu App ID | `{config['feishu_app_id']}` |
| DST domain | `{config['dst_domain']}` |
| DST working space | `{config['dst_space_working']}` |
| DST mirror space | `{config['dst_space_mirror']}` |
| CN parent | `{config['dst_cn_parent']}` |
| VI parent | `{config['dst_vi_parent']}` |
| Larksuite App ID | `{config['lark_app_id']}` |
| Base app | `{config['base_app_token']}` |
| Table | `{config['base_table_id']}` |
| LLM endpoints | {len(config['llm_endpoints'])} |
| Rate limit Lark | {config['lark_rate_limit_rps']} req/s |
| Rate limit LLM | {config['llm_rate_limit_rps']} req/s |

## 🚀 Next steps

### 1. Login Lark CLI

```bash
{config['lark_cli_path']} config init --app-id {config['lark_app_id']} --brand lark
{config['lark_cli_path']} auth login --as user
```

Verify scopes (cần wiki/docs/base + drive:file:upload):

```bash
{config['lark_cli_path']} auth status --verify
```

### 2. Setup Feishu source app

App `{config['feishu_app_id']}` cần các scopes:
- `wiki:wiki:readonly` — đọc wiki nodes
- `docx:document:readonly` — đọc blocks
- `drive:file:download` — download media (images, files)

### 3. Bitable schema

Đảm bảo Bitable `{config['base_app_token']}` có table `{config['base_table_id']}` với fields:
- `STT` (number, primary)
- `Tiêu đề` (text)
- `Title` (text — original CN)
- `Trạng thái`, `Trạng thái dịch` (single select: Pending/Done/Failed/Skipped/Translating)
- `Liên kết gốc`, `Liên kết clone`, `Liên kết dịch`, `Liên kết wiki dịch mới` (URL)
- `Mirror Wiki Node Token`, `Node Token`, `Obj Token` (text)
- `% Dịch`, `Số segment dịch` (number)
- `Lỗi`, `Lỗi dịch` (text)
- `Crawled At`, `Last Seen At`, `Last Edit Time` (datetime)
- ... (full list xem `src/waytoagi/models/base.py`)

### 4. Install + run

```bash
pip install -e ".[dev]"
waytoagi crawl --no-resume       # Detect source nodes
waytoagi pipeline --workers 4    # Clone + translate
waytoagi mirror --resume         # Mirror VI → DST
waytoagi sync --workers 3        # Sync content
waytoagi audit                   # Final audit
```

Or run all:

```bash
waytoagi orchestrate
```

### 5. Daily run (Windows Task Scheduler)

```powershell
schtasks /create /tn "WaytoAGI-Daily" /tr "python -m waytoagi.cli orchestrate" /sc daily /st 06:00 /ru "{config.get('user', 'vudan')}"
```

## 🔄 Re-run wizard

Nếu cần update config (đổi App ID, thêm endpoint, etc.):

```bash
python setup_new_project.py
```

Wizard sẽ overwrite `.env` và `llm_keys.json` (backup tự động trước).
"""
    path.write_text(content, encoding="utf-8")
    print(f"  ✓ Written: {path}")


# ============================================================
# Main
# ============================================================


def main() -> int:
    """Entry point."""
    cwd = Path.cwd()
    env_path = cwd / ".env"
    llm_path = cwd / "llm_keys.json"
    guide_path = cwd / "PROJECT_SETUP.md"

    # Backup existing files
    for p in (env_path, llm_path):
        if p.exists():
            backup = p.with_suffix(f"{p.suffix}.bak.{Path(__file__).stem}")
            backup.write_bytes(p.read_bytes())
            print(f"  📦 Backup existing {p.name} → {backup.name}")

    try:
        config = run_wizard()
    except (KeyboardInterrupt, EOFError):
        print("\n\n❌ Wizard cancelled by user.")
        return 1

    section("📝 Generating files...")
    try:
        write_env(config, env_path)
        write_llm_keys(config["llm_endpoints"], llm_path)
        write_setup_guide(config, guide_path)
    except Exception as e:
        print(f"\n❌ Failed to write files: {e}")
        return 1

    section("✅ DONE")
    print(f"  Config files generated tại: {cwd}")
    print(f"    - {env_path.name}")
    print(f"    - {llm_path.name}")
    print(f"    - {guide_path.name}")
    print()
    print("📖 Đọc PROJECT_SETUP.md cho next steps (login Lark CLI, schema, run).")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
