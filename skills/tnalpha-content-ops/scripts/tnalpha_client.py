#!/usr/bin/env python3
"""Deterministic client for the TN-Alpha Agent API."""

from __future__ import annotations

import argparse
import getpass
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


DEFAULT_URL = "https://alpha.traditionow.ai"
DEV_KEYCHAIN_SERVICE = "ai.traditionow.tnalpha.agent-api.dev"
PROD_KEYCHAIN_SERVICE = "ai.traditionow.tnalpha.agent-api.prod"
DEFAULT_ACCOUNT = "tnalpha-agent"
DEV_PLIST = Path.home() / "Library/LaunchAgents/ai.openclaw.tnalpha.plist"
PROD_PLIST = Path.home() / "Library/LaunchAgents/ai.openclaw.tnalpha-app.plist"


def _config_dir() -> Path:
    override = os.environ.get("TNALPHA_CONFIG_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".config" / "tnalpha"


def _config_path() -> Path:
    return _config_dir() / "agent.json"


def _load_config() -> dict[str, str]:
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    allowed = {"base_url", "account", "org_id", "keychain_service"}
    return {
        key: str(item).strip()
        for key, item in value.items()
        if key in allowed and item is not None and str(item).strip()
    }


def _save_config(config: dict[str, str]) -> None:
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    safe = {
        key: str(value).strip()
        for key, value in config.items()
        if key in {"base_url", "account", "org_id", "keychain_service"}
        and str(value).strip()
    }
    path.write_text(json.dumps(safe, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _base_url() -> str:
    configured = _load_config().get("base_url", DEFAULT_URL)
    return os.environ.get("TNALPHA_API_URL", configured).strip().rstrip("/")


def _actor_id() -> str:
    configured = _load_config().get("account", DEFAULT_ACCOUNT)
    return os.environ.get("TNALPHA_AGENT_ACTOR", configured).strip() or DEFAULT_ACCOUNT


def _org_id() -> str:
    configured = _load_config().get("org_id", "")
    return os.environ.get("TNALPHA_AGENT_ORG", configured).strip()


def _is_local_url(base_url: str) -> bool:
    host = (urlparse(base_url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost"}


def _default_keychain_service(base_url: str) -> str:
    if _is_local_url(base_url):
        return DEV_KEYCHAIN_SERVICE
    if base_url == DEFAULT_URL:
        return PROD_KEYCHAIN_SERVICE
    digest = sha256(base_url.encode("utf-8")).hexdigest()[:12]
    return f"ai.traditionow.tnalpha.agent-api.{digest}"


def _keychain_service(base_url: str) -> str:
    config = _load_config()
    return (
        os.environ.get("TNALPHA_AGENT_KEYCHAIN_SERVICE", "").strip()
        or config.get("keychain_service", "")
        or _default_keychain_service(base_url)
    )


def _keychain_account() -> str:
    return _load_config().get("account", DEFAULT_ACCOUNT)


def _run_security(
    arguments: list[str], *, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["security", *arguments],
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    )


def _keychain_get(service: str, account: str) -> str:
    try:
        return _run_security(
            ["find-generic-password", "-s", service, "-a", account, "-w"]
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


def _legacy_keychain_get(service: str) -> str:
    try:
        return _run_security(["find-generic-password", "-s", service, "-w"]).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


def _keychain_set(service: str, account: str, token: str) -> None:
    if sys.platform != "darwin":
        raise SystemExit(
            "Secure local credential storage is currently available on macOS. "
            "Set TNALPHA_AGENT_API_TOKEN in the process environment on this platform."
        )
    # A trailing -w makes `security` read the secret from stdin instead of argv.
    _run_security(
        ["add-generic-password", "-U", "-s", service, "-a", account, "-w"],
        input_text=f"{token}\n",
    )


def _keychain_delete(service: str, account: str) -> bool:
    if sys.platform != "darwin":
        return False
    try:
        _run_security(["delete-generic-password", "-s", service, "-a", account])
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _plist_token(path: Path) -> str:
    try:
        result = subprocess.run(
            [
                "/usr/libexec/PlistBuddy",
                "-c",
                "Print :EnvironmentVariables:TNALPHA_AGENT_API_TOKEN",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        return ""


def _credential(base_url: str) -> tuple[str, str]:
    environment = os.environ.get("TNALPHA_AGENT_API_TOKEN", "").strip()
    if environment:
        return environment, "environment"

    service = _keychain_service(base_url)
    value = _keychain_get(service, _keychain_account())
    if value:
        return value, "keychain"

    # Existing deployments used service-only Keychain entries. Read them during migration.
    value = _legacy_keychain_get(service)
    if value:
        return value, "legacy-keychain"

    # Maintainer-only compatibility: never use plist credentials for arbitrary hosts.
    if _is_local_url(base_url):
        value = _plist_token(DEV_PLIST)
        if value:
            return value, "legacy-dev-launch-agent"
    elif base_url == DEFAULT_URL:
        value = _plist_token(PROD_PLIST)
        if value:
            return value, "legacy-prod-launch-agent"
    return "", "unavailable"


def _token(base_url: str) -> str:
    value, _ = _credential(base_url)
    if value:
        return value
    raise SystemExit(
        "TN-Alpha account is not connected. Run `tnalpha_client.py configure` "
        "or set TNALPHA_AGENT_API_TOKEN in the process environment."
    )


def _call(method: str, path: str, payload=None, query=None):
    base_url = _base_url()
    url = f"{base_url}{path}"
    if query:
        clean = {key: value for key, value in query.items() if value is not None}
        if clean:
            url = f"{url}?{urlencode(clean)}"
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {_token(base_url)}",
        "Content-Type": "application/json",
        "User-Agent": "TNAlpha-Agent-Skill/1.0",
        "X-TNAlpha-Actor": _actor_id(),
    }
    org_id = _org_id()
    if org_id:
        headers["X-TNAlpha-Org"] = org_id
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"TN-Alpha API returned HTTP {error.code}: {body}")
    except URLError as error:
        raise SystemExit(f"Cannot reach TN-Alpha API at {base_url}: {error.reason}")


def _configure(args) -> dict:
    base_url = args.url.strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--url must be an absolute http(s) URL")
    account = args.account.strip() or DEFAULT_ACCOUNT
    org_id = args.org.strip()
    service = args.keychain_service.strip() or _default_keychain_service(base_url)
    config = {
        "base_url": base_url,
        "account": account,
        "org_id": org_id,
        "keychain_service": service,
    }
    _save_config(config)

    token = ""
    if args.token_stdin:
        token = sys.stdin.read().strip()
    elif sys.stdin.isatty() and sys.platform == "darwin":
        token = getpass.getpass("TN-Alpha access credential (stored in Keychain): ").strip()
    if token:
        _keychain_set(service, account, token)
    credential, source = _credential(base_url)
    return {
        "configured": True,
        "base_url": base_url,
        "account": account,
        "org_id": org_id,
        "credential_configured": bool(credential),
        "credential_source": source,
        "config_path": str(_config_path()),
    }


def _status() -> dict:
    config = _load_config()
    base_url = _base_url()
    credential, source = _credential(base_url)
    result = {
        "configured": bool(config),
        "base_url": base_url,
        "account": _actor_id(),
        "org_id": _org_id(),
        "credential_configured": bool(credential),
        "credential_source": source,
        "config_path": str(_config_path()),
    }
    if credential:
        try:
            result["remote"] = _call("GET", "/api/v1/me")
            result["reachable"] = True
        except SystemExit as error:
            result["reachable"] = False
            result["error"] = str(error)
    return result


def _disconnect(confirm: bool) -> dict:
    if not confirm:
        raise SystemExit("Refusing to disconnect without --confirm")
    config = _load_config()
    base_url = _base_url()
    service = config.get("keychain_service") or _default_keychain_service(base_url)
    account = config.get("account") or DEFAULT_ACCOUNT
    credential_removed = _keychain_delete(service, account)
    path = _config_path()
    if path.exists():
        path.unlink()
    return {
        "disconnected": True,
        "credential_removed": credential_removed,
        "environment_override_active": bool(os.environ.get("TNALPHA_AGENT_API_TOKEN", "")),
    }


def _parser():
    parser = argparse.ArgumentParser(description="Operate TN-Alpha through its Agent API")
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure", help="Configure a local TN-Alpha account")
    configure.add_argument("--url", default=DEFAULT_URL)
    configure.add_argument("--account", default=DEFAULT_ACCOUNT)
    configure.add_argument("--org", default="")
    configure.add_argument("--keychain-service", default="")
    configure.add_argument(
        "--token-stdin",
        action="store_true",
        help="Read the access credential from stdin instead of an interactive prompt",
    )
    sub.add_parser("status", help="Show connection state without revealing credentials")
    disconnect = sub.add_parser("disconnect", help="Remove local connection metadata")
    disconnect.add_argument("--confirm", action="store_true")

    for name in ("me", "capabilities", "brands", "audit"):
        sub.add_parser(name)

    campaigns = sub.add_parser("campaigns")
    campaigns.add_argument("--brand-id", type=int, required=True)

    overview = sub.add_parser("overview")
    overview.add_argument("--brand-id", type=int, required=True)

    topics = sub.add_parser("topics")
    topics.add_argument("--brand-id", type=int)
    topics.add_argument("--campaign-id", type=int)
    topics.add_argument("--status")
    topics.add_argument("--limit", type=int, default=50)

    create = sub.add_parser("create-topic")
    create.add_argument("--brand-id", type=int, required=True)
    create.add_argument("--campaign-id", type=int)
    create.add_argument("--title", required=True)
    create.add_argument("--outline", default="")
    create.add_argument("--confirm", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "configure":
        result = _configure(args)
    elif args.command == "status":
        result = _status()
    elif args.command == "disconnect":
        result = _disconnect(args.confirm)
    elif args.command == "me":
        result = _call("GET", "/api/v1/me")
    elif args.command == "capabilities":
        result = _call("GET", "/api/v1/capabilities")
    elif args.command == "brands":
        result = _call("GET", "/api/v1/brands")
    elif args.command == "campaigns":
        result = _call("GET", f"/api/v1/brands/{args.brand_id}/campaigns")
    elif args.command == "overview":
        result = _call("GET", f"/api/v1/brands/{args.brand_id}/overview")
    elif args.command == "topics":
        result = _call(
            "GET",
            "/api/v1/topics",
            query={
                "brand_id": args.brand_id,
                "campaign_id": args.campaign_id,
                "status": args.status,
                "limit": args.limit,
            },
        )
    elif args.command == "audit":
        result = _call("GET", "/api/v1/audit")
    elif args.command == "create-topic":
        if not args.confirm:
            raise SystemExit("Refusing write without --confirm")
        result = _call(
            "POST",
            "/api/v1/topics/manual",
            {
                "brand_id": args.brand_id,
                "campaign_id": args.campaign_id,
                "title": args.title,
                "outline": args.outline,
            },
        )
    else:
        raise AssertionError(args.command)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
