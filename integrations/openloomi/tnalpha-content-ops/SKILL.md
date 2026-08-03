---
name: tnalpha-content-ops
description: "Operate TN-Alpha through natural language: inspect brands, campaigns, topics and content status, or create an exact manual candidate topic. Use when the user mentions TN-Alpha, TNAlpha, 溯肤内容平台, 品牌知识库, 选题库, 写作库, 排期 or 数据反馈."
---

# TN-Alpha Content Operations

Use the deterministic client in `scripts/tnalpha_client.py`. TN-Alpha remains the
source of truth; do not infer database state from conversation memory.
The installed Skill targets `https://alpha.traditionow.ai` by default. Set
`TNALPHA_API_URL=http://127.0.0.1:8810` explicitly when testing against dev.

## Safety and identity

- This P0 skill runs as TN-Alpha role `admin0` for integration testing.
- Never print, echo, request, or persist the bearer token in chat or artifacts.
- Read operations can run directly.
- Creating a candidate topic requires explicit user intent and `--confirm`.
- Publishing, deleting, model changes, prompt changes, and user management are
  deliberately unavailable even though the identity is an administrator.
- Preserve a user-supplied manual topic title exactly.

## Commands

```bash
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

For brand evergreen content, omit `--campaign-id`. If a name is supplied instead
of an ID, resolve it with `brands` and `campaigns` first. Summarize API responses
in Chinese and include created resource IDs. Do not claim success unless the API
returned a successful response.
