#!/usr/bin/env python3
"""Deterministic OpenLoomi client for the TN-Alpha agent API."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_URL = "https://alpha.traditionow.ai"
DEV_KEYCHAIN_SERVICE = "ai.traditionow.tnalpha.agent-api.dev"
PROD_KEYCHAIN_SERVICE = "ai.traditionow.tnalpha.agent-api.prod"
DEV_PLIST = Path.home() / "Library/LaunchAgents/ai.openclaw.tnalpha.plist"
PROD_PLIST = Path.home() / "Library/LaunchAgents/ai.openclaw.tnalpha-app.plist"


def _base_url() -> str:
    return os.environ.get("TNALPHA_API_URL", DEFAULT_URL).rstrip("/")


def _is_local_url(base_url: str) -> bool:
    return base_url.startswith(("http://127.0.0.1", "http://localhost"))


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


def _token(base_url: str) -> str:
    value = os.environ.get("TNALPHA_AGENT_API_TOKEN", "").strip()
    if value:
        return value
    keychain_service = os.environ.get("TNALPHA_AGENT_KEYCHAIN_SERVICE", "").strip()
    if not keychain_service:
        keychain_service = DEV_KEYCHAIN_SERVICE if _is_local_url(base_url) else PROD_KEYCHAIN_SERVICE
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", keychain_service, "-w"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    if _is_local_url(base_url):
        value = _plist_token(DEV_PLIST)
        if value:
            return value
    elif base_url == DEFAULT_URL:
        value = _plist_token(PROD_PLIST)
        if value:
            return value
    raise SystemExit(
        "TN-Alpha agent token is unavailable. Configure TNALPHA_AGENT_API_TOKEN "
        f"in the environment or Keychain service {keychain_service!r}."
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
        "User-Agent": "OpenLoomi-TNAlpha/1.0",
        "X-TNAlpha-Actor": os.environ.get("TNALPHA_AGENT_ACTOR", "openloomi-admin"),
    }
    org_id = os.environ.get("TNALPHA_AGENT_ORG", "").strip()
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


def _parser():
    parser = argparse.ArgumentParser(description="Operate TN-Alpha through its agent API")
    sub = parser.add_subparsers(dest="command", required=True)
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
    if args.command == "me":
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
        result = _call("GET", "/api/v1/topics", query={
            "brand_id": args.brand_id,
            "campaign_id": args.campaign_id,
            "status": args.status,
            "limit": args.limit,
        })
    elif args.command == "audit":
        result = _call("GET", "/api/v1/audit")
    elif args.command == "create-topic":
        if not args.confirm:
            raise SystemExit("Refusing write without --confirm")
        result = _call("POST", "/api/v1/topics/manual", {
            "brand_id": args.brand_id,
            "campaign_id": args.campaign_id,
            "title": args.title,
            "outline": args.outline,
        })
    else:
        raise AssertionError(args.command)
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
