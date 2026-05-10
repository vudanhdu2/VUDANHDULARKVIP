"""Bootstrap minimal — auto-detect cùng tenant Larksuite.

Triết lý: user cung cấp THIỂU SỐ inputs, code auto-detect rest.

Use cases:

  CASE A — Source cùng tenant DST (đơn giản nhất, 2 inputs):
    1. Link wiki nguồn (vd https://yourtenant.sg.larksuite.com/wiki/XXX)
    2. lark-cli auth login đã setup
    + LLM API key (hoặc llm_keys.json)

  CASE B — Source ở Feishu CN (cross-cloud, 4 inputs):
    1. Link wiki nguồn (https://example.feishu.cn/wiki/XXX)
    2. lark-cli auth login (DST Larksuite)
    3. FEISHU_APP_ID + FEISHU_APP_SECRET (cross-tenant read)
    + LLM API key

  CASE C — Source ở Larksuite tenant KHÁC:
    Same as CASE B but FEISHU_APP_* đăng ký ở source tenant.

Bootstrap logic:
  - Parse source URL → detect domain
  - Detect lark-cli config → DST tenant info
  - So sánh source vs DST domain → cùng tenant? skip prompt Feishu
  - Auto-create DST resources (working space, parents, Bitable) qua Lark API
  - Generate .env + llm_keys.json

Usage:
    python bootstrap.py
    python bootstrap.py --source-url <URL> --llm-key <KEY>
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def section(title: str) -> None:
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def info(msg: str) -> None:
    print(f"  ℹ️  {msg}")


def success(msg: str) -> None:
    print(f"  ✓ {msg}")


def warn(msg: str) -> None:
    print(f"  ⚠️  {msg}")


def error(msg: str) -> None:
    print(f"  ❌ {msg}")


def prompt(question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"  {question}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("    → Không được rỗng")


def prompt_secret(question: str) -> str:
    import getpass

    while True:
        value = getpass.getpass(f"  {question}: ").strip()
        if value:
            return value
        print("    → Không được rỗng")


# ============================================================
# Step 1: Parse source URL
# ============================================================


def parse_source_url(url: str) -> tuple[str, str | None]:
    """Parse source URL → (domain, wiki_node_token)."""
    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if not re.match(r"^[a-z0-9][a-z0-9.-]*\.(feishu\.cn|larksuite\.com)$", domain):
        raise ValueError(f"Source URL không phải Feishu/Larksuite: {url!r}")
    m = re.match(r"^/wiki/([A-Za-z0-9]+)", parsed.path)
    return domain, m.group(1) if m else None


def is_larksuite(domain: str) -> bool:
    return domain.endswith(".larksuite.com")


def is_feishu_cn(domain: str) -> bool:
    return domain.endswith(".feishu.cn")


# ============================================================
# Step 2: Detect lark-cli config
# ============================================================


def find_lark_cli() -> str:
    """Find lark-cli binary path."""
    candidates = [
        "lark-cli",
        "lark-cli.cmd",
        str(Path.home() / "AppData/Roaming/npm/lark-cli.cmd"),
        "/usr/local/bin/lark-cli",
    ]
    for c in candidates:
        try:
            r = subprocess.run(
                [c, "--version"],
                capture_output=True, text=True, timeout=10,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            if r.returncode == 0:
                return c
        except (FileNotFoundError, OSError):
            continue
    raise RuntimeError(
        "lark-cli không tìm thấy. Cài: npm install -g @larksuite/lark-cli"
    )


def list_lark_profiles(cli_path: str) -> list[dict[str, Any]]:
    """List tất cả logged-in profiles (qua `lark-cli auth list --json`)."""
    r = subprocess.run(
        [cli_path, "auth", "list"],
        capture_output=True, text=True, timeout=30,
        creationflags=0x08000000 if sys.platform == "win32" else 0,
    )
    if r.returncode != 0:
        return []
    try:
        data = json.loads(r.stdout)
        return list(data) if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def detect_lark_profiles(cli_path: str) -> dict[str, dict[str, Any]]:
    """Detect 2 profiles cần thiết: feishu-src + lark-dst.

    Returns:
        dict mapping profile_role → profile_info
        - 'feishu_src': profile cho Feishu CN source
        - 'lark_dst': profile cho Larksuite DST

        Empty dict nếu chưa setup. User cần chạy `lark-cli config init` + `auth login` cho từng cái.
    """
    profiles = list_lark_profiles(cli_path)
    result: dict[str, dict[str, Any]] = {}

    # Hiện tại lark-cli chỉ có 1 active profile/brand mỗi lần.
    # Nếu user dùng config bind hoặc --profile, có thể parse từ auth list.
    for p in profiles:
        # Cần verify brand — gọi auth status với app_id specific
        app_id = p.get("appId", "")
        if not app_id:
            continue
        # Get brand info
        try:
            r = subprocess.run(
                [cli_path, "config", "show"],
                capture_output=True, text=True, timeout=10,
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            cfg = json.loads(r.stdout) if r.returncode == 0 else {}
            brand = cfg.get("brand", "")
        except Exception:
            brand = ""

        info_dict = {
            "app_id": app_id,
            "user_name": p.get("userName", ""),
            "user_open_id": p.get("userOpenId", ""),
            "token_status": p.get("tokenStatus", ""),
            "brand": brand,
        }
        if brand == "feishu" and "feishu_src" not in result:
            result["feishu_src"] = info_dict
        elif brand == "lark" and "lark_dst" not in result:
            result["lark_dst"] = info_dict

    return result


def print_setup_instructions(cli_path: str, source_domain: str) -> None:
    """Print instructions cho user setup 2 profiles."""
    needs_feishu = is_feishu_cn(source_domain)
    print()
    print("  ❌ Chưa đủ profiles. Setup theo các bước sau:")
    print()
    if needs_feishu:
        print("  1️⃣  Tạo Feishu app (cho source CN):")
        print("       https://open.feishu.cn/app → New custom app")
        print("       Scopes: wiki:wiki:readonly, docx:document:readonly,")
        print("               drive:file:download, drive:drive")
        print()
        print("       Sau khi có App ID + Secret:")
        print(f"       {cli_path} config init --brand feishu \\")
        print("           --app-id cli_FEISHU_XXX --name feishu-src")
        print(f"       {cli_path} auth login")
        print()
    print("  2️⃣  Tạo Larksuite app (cho DST):")
    print("       https://open.larksuite.com/app → New custom app")
    print("       Scopes: wiki/docs/base/drive full")
    print()
    print(f"       {cli_path} config init --brand lark \\")
    print("           --app-id cli_LARK_YYY --name lark-dst")
    print(f"       {cli_path} auth login")
    print()
    print("  3️⃣  Verify:")
    print(f"       {cli_path} auth list")
    print("       (cả 2 profiles → tokenStatus=valid)")
    print()


# ============================================================
# Step 3: Detect source mode (cùng tenant?)
# ============================================================


def detect_source_mode(source_domain: str, dst_user_name: str) -> str:
    """Determine source mode:
      - 'feishu_cn': source ở Feishu CN cloud → CẦN Feishu App credentials
        (use case phổ biến nhất: clone từ Feishu CN sang Larksuite)
      - 'same_tenant_larksuite': source cùng tenant DST → KHÔNG cần Feishu App
      - 'cross_tenant_larksuite': source khác tenant Larksuite → CẦN Source App
    """
    if is_feishu_cn(source_domain):
        return "feishu_cn"
    if is_larksuite(source_domain):
        ans = input(
            f"\n  ❓ Source `{source_domain}` có cùng tenant với DST của bạn ({dst_user_name})? [Y/n]: "
        ).strip().lower()
        if ans in ("", "y", "yes"):
            return "same_tenant_larksuite"
        return "cross_tenant_larksuite"
    return "unknown"


# ============================================================
# Step 4: Auto-create DST resources (stubs — implement với Lark API)
# ============================================================


def auto_create_dst_resources(lark_info: dict[str, Any]) -> dict[str, str]:
    """Auto-create working space, mirror space, parent folders, Bitable trong DST.

    TODO: implement actual Lark API calls. Hiện tại prompt manual.
    """
    info("Auto-create DST resources (TODO: implement Lark API calls)")
    info("Hiện tại bạn cần tạo manual + paste IDs.")
    info("(Nếu để rỗng, code sẽ skip stage tương ứng cho đến khi setup đầy đủ.)")

    return {
        "working_space": prompt(
            "Working space_id (chứa CN clone + VI translate), Enter = skip",
            default="",
        ),
        "dst_space": prompt("DST mirror space_id (public), Enter = skip", default=""),
        "cn_parent": prompt("CN parent node_token, Enter = skip", default=""),
        "vi_parent": prompt("VI parent node_token, Enter = skip", default=""),
        "base_token": prompt("Bitable app_token, Enter = skip", default=""),
        "table_id": prompt("Table ID (tblxxx), Enter = skip", default=""),
    }


# ============================================================
# Step 5: Write files
# ============================================================


def write_files(
    source_url: str,
    source_domain: str,
    source_mode: str,
    lark_info: dict[str, Any],
    dst_resources: dict[str, str],
    feishu_app_id: str,
    feishu_app_secret: str,
    llm_endpoints: list[dict[str, str]],
    output_dir: Path,
) -> None:
    """Write .env + llm_keys.json."""
    if source_mode == "feishu_cn":
        source_open_url = "https://open.feishu.cn/open-apis"
    else:
        source_open_url = "https://open.larksuite.com/open-apis"

    same_tenant_hint = "(cùng tenant DST — dùng Larksuite App qua lark-cli)"
    if source_mode == "same_tenant_larksuite":
        feishu_section = f"# Source CÙNG TENANT với DST {same_tenant_hint}\n# FEISHU_APP_* để rỗng — code dùng LARK_APP_* tự động."
        feishu_id_line = "FEISHU_APP_ID="
        feishu_secret_line = "FEISHU_APP_SECRET="
    else:
        feishu_section = f"# Source khác tenant ({source_mode}) — cần app riêng cho source"
        feishu_id_line = f"FEISHU_APP_ID={feishu_app_id}"
        feishu_secret_line = f"FEISHU_APP_SECRET={feishu_app_secret}"

    env_content = f"""# WaytoAGI v2 — Bootstrapped {source_mode}
# Source: {source_url}

# ============================================================
# Source tenant
# ============================================================
SOURCE_OPEN_URL={source_open_url}
{feishu_section}
{feishu_id_line}
{feishu_secret_line}
LARK_SRC_SPACE=  # TODO: detect từ wiki_node_token (manual nhập nếu auto fail)

# ============================================================
# DST (Larksuite — auto-detected từ lark-cli)
# ============================================================
LARK_APP_ID={lark_info["app_id"]}
LARK_APP_SECRET=  # cần cung cấp manual (lark-cli không expose secret)
LARK_OPEN_URL=https://open.larksuite.com/open-apis
LARK_DOMAIN={lark_info.get("domain", "")}
LARK_CLI_PATH={lark_info["cli_path"]}
LARK_WORKING_SPACE={dst_resources["working_space"]}
LARK_DST_SPACE={dst_resources["dst_space"]}
LARK_CN_PARENT={dst_resources["cn_parent"]}
LARK_VI_PARENT={dst_resources["vi_parent"]}

# ============================================================
# Lark Base (state)
# ============================================================
LARK_BASE_TOKEN={dst_resources["base_token"]}
LARK_TABLE_ID={dst_resources["table_id"]}

# ============================================================
# LLM POOL
# ============================================================
LLM_KEYS_FILE=llm_keys.json

# ============================================================
# Tuning
# ============================================================
LARK_RATE_LIMIT_RPS=5
LLM_RATE_LIMIT_RPS=10
LOG_LEVEL=INFO
LOG_FORMAT=json
TRANSLATION_CACHE_DB=.cache/translations.sqlite
"""
    (output_dir / ".env").write_text(env_content, encoding="utf-8")
    success(f".env written ({(output_dir / '.env').stat().st_size} bytes)")

    (output_dir / "llm_keys.json").write_text(
        json.dumps(llm_endpoints, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    success(f"llm_keys.json written ({len(llm_endpoints)} endpoints)")


# ============================================================
# Main
# ============================================================


def main() -> int:
    ap = argparse.ArgumentParser(description="Bootstrap waytoagi v2 — minimal config")
    ap.add_argument("--source-url", help="Source wiki URL (skip prompt)")
    ap.add_argument("--llm-key", help="LLM API key")
    ap.add_argument("--llm-endpoint", default="http://localhost:20128/v1")
    ap.add_argument("--llm-model", default="gpt-4o")
    ap.add_argument("--output-dir", default=".", help="Output directory")
    args = ap.parse_args()

    output_dir = Path(args.output_dir).resolve()

    print()
    print("╔" + "═" * 58 + "╗")
    print("║  WAYTOAGI v2 — Bootstrap (smart minimal config)         ║")
    print("╚" + "═" * 58 + "╝")
    print()

    # Backup existing
    for fname in (".env", "llm_keys.json"):
        p = output_dir / fname
        if p.exists():
            backup = p.with_suffix(f"{p.suffix}.bak.bootstrap")
            backup.write_bytes(p.read_bytes())
            info(f"Backup: {fname} → {backup.name}")

    # ============================================================
    section("1️⃣  SOURCE WIKI URL")
    # ============================================================
    source_url = args.source_url or prompt(
        "Paste link wiki nguồn (vd https://yourtenant.sg.larksuite.com/wiki/XXX)"
    )
    try:
        source_domain, wiki_token = parse_source_url(source_url)
        success(f"Domain: {source_domain}")
        if wiki_token:
            success(f"Wiki node token: {wiki_token}")
    except ValueError as e:
        error(str(e))
        return 1

    # ============================================================
    section("2️⃣  LARK-CLI AUTH (DST tenant)")
    # ============================================================
    try:
        lark_info = detect_lark_cli()
        success(f"App ID: {lark_info['app_id']} ({lark_info['brand']})")
        success(f"User: {lark_info['user_name']}")
        info(f"Token expires: {lark_info['expires_at']}")
        info(f"Scopes: {len(lark_info['scopes'])} granted")
    except RuntimeError as e:
        error(str(e))
        return 1

    # ============================================================
    section("3️⃣  SOURCE MODE DETECTION")
    # ============================================================
    source_mode = detect_source_mode(source_domain, lark_info["user_name"])
    feishu_app_id = ""
    feishu_app_secret = ""
    if source_mode == "same_tenant_larksuite":
        success("Source CÙNG TENANT với DST → KHÔNG cần Feishu App credentials")
        success("Code sẽ dùng Larksuite App của bạn cho cả source + DST")
    elif source_mode == "feishu_cn":
        warn("Source ở Feishu CN cloud — CẦN Feishu App credentials (cross-cloud)")
        info("Tạo app tại: https://open.feishu.cn/app")
        info("Scopes: wiki:wiki:readonly, docx:document:readonly, drive:file:download")
        feishu_app_id = prompt("Feishu App ID (cli_xxx)")
        feishu_app_secret = prompt_secret("Feishu App Secret")
    elif source_mode == "cross_tenant_larksuite":
        warn(f"Source ở Larksuite tenant KHÁC ({source_domain})")
        info("Cần Larksuite App đăng ký Ở SOURCE TENANT")
        feishu_app_id = prompt("Source tenant App ID (cli_xxx)")
        feishu_app_secret = prompt_secret("Source tenant App Secret")
    else:
        warn(f"Source mode unknown: {source_mode}")

    # ============================================================
    section("4️⃣  LLM API KEY")
    # ============================================================
    if args.llm_key:
        llm_endpoints = [{
            "name": "Default",
            "endpoint": args.llm_endpoint,
            "api_key": args.llm_key,
            "model": args.llm_model,
        }]
        success(f"LLM endpoint: {args.llm_endpoint} ({args.llm_model})")
    elif (output_dir / "llm_keys.json").exists():
        info("Found existing llm_keys.json — re-use")
        llm_endpoints = json.loads((output_dir / "llm_keys.json").read_text(encoding="utf-8"))
        success(f"Loaded {len(llm_endpoints)} endpoints")
    else:
        endpoint = prompt("LLM endpoint", default="http://localhost:20128/v1")
        api_key = prompt_secret("LLM API key")
        model = prompt("LLM model", default="gpt-4o")
        llm_endpoints = [{
            "name": "Default", "endpoint": endpoint, "api_key": api_key, "model": model
        }]

    # ============================================================
    section("5️⃣  AUTO-CREATE DST RESOURCES")
    # ============================================================
    dst_resources = auto_create_dst_resources(lark_info)

    # ============================================================
    section("6️⃣  WRITE FILES")
    # ============================================================
    write_files(
        source_url=source_url,
        source_domain=source_domain,
        source_mode=source_mode,
        lark_info=lark_info,
        dst_resources=dst_resources,
        feishu_app_id=feishu_app_id,
        feishu_app_secret=feishu_app_secret,
        llm_endpoints=llm_endpoints,
        output_dir=output_dir,
    )

    # ============================================================
    section("✅ DONE — Next steps")
    # ============================================================
    print()
    print("  1. Verify config:")
    print(
        "       python -c \"from waytoagi.config import get_settings; s=get_settings(); "
        "print(f'source_same_tenant={s.source_same_tenant_as_dst}, "
        "effective_app={s.effective_source_app_id}')\""
    )
    print()
    print("  2. Smoke test 1 record:")
    print("       waytoagi pipeline --workers 1 --limit 1")
    print()
    print("  3. Run full:")
    print("       waytoagi orchestrate")
    print()
    if source_mode == "same_tenant_larksuite":
        print("  💡 Source CÙNG TENANT — chỉ cần lark-cli auth là đủ.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
