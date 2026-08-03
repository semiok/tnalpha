---
name: tnalpha-content-ops
description: "Operate TN-Alpha through natural language: inspect brands, campaigns, topics and content status, or create an exact manual candidate topic. Use when the user mentions TN-Alpha, TNAlpha, 溯肤内容平台, 品牌知识库, 选题库, 写作库, 排期 or 数据反馈."
---

# TN-Alpha Content Operations

Use the deterministic client in `scripts/tnalpha_client.py`. TN-Alpha remains
the source of truth; do not infer database state from conversation memory.

Before the first use, configure a local account connection:

```bash
python3 scripts/tnalpha_client.py configure \
  --url https://alpha.traditionow.ai \
  --account YOUR_ACCOUNT \
  --org YOUR_ORG
python3 scripts/tnalpha_client.py status
```

On macOS the credential is stored in Keychain. In CI or on other platforms,
provide `TNALPHA_AGENT_API_TOKEN` through the process environment. Never put a
token in this Skill directory, a command argument, chat, source control, logs,
or generated artifacts.

Set `TNALPHA_API_URL=http://127.0.0.1:8810` explicitly when testing against dev.

## Safety and identity

- Read operations can run directly.
- Creating a candidate topic requires explicit user intent and `--confirm`.
- Publishing, deleting, model changes, prompt changes, and user management are unavailable.
- Preserve a user-supplied manual topic title exactly.
- Report the configured account and organization, but never reveal credentials.

## Commands

```bash
python3 scripts/tnalpha_client.py status
python3 scripts/tnalpha_client.py me
python3 scripts/tnalpha_client.py capabilities
python3 scripts/tnalpha_client.py brands
python3 scripts/tnalpha_client.py campaigns --brand-id BRAND_ID
python3 scripts/tnalpha_client.py overview --brand-id BRAND_ID
python3 scripts/tnalpha_client.py topics --brand-id BRAND_ID --status 候选
python3 scripts/tnalpha_client.py audit
```

Create an exact candidate topic only after the user clearly asks for it:

```bash
python3 scripts/tnalpha_client.py create-topic \
  --brand-id BRAND_ID \
  --campaign-id CAMPAIGN_ID \
  --title "用户给出的原始标题" \
  --outline "可选纲要" \
  --confirm
```

For brand evergreen content, omit `--campaign-id`. Resolve names with `brands`
and `campaigns` before using IDs. Summarize API responses in Chinese and include
created resource IDs. Do not claim success unless the API returned success.

Disconnect the local account only when the user explicitly requests it:

```bash
python3 scripts/tnalpha_client.py disconnect --confirm
```
