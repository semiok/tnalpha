# TN-Alpha Agent API v1

The production base URL is `https://alpha.traditionow.ai`. Use
`http://127.0.0.1:8810` only for explicit local development.

Authentication uses `Authorization: Bearer <token>`. The local client resolves
credentials in this order:

1. `TNALPHA_AGENT_API_TOKEN` environment variable.
2. The macOS Keychain service stored in `~/.config/tnalpha/agent.json`.
3. The default production or development Keychain service.
4. Existing maintainer LaunchAgent configuration for the exact prod/dev URL,
   retained only for backward compatibility.

The client never sends a prod/dev fallback credential to an arbitrary remote
host. Account, organization, and base URL are non-secret local configuration.

Implemented endpoints:

- `GET /api/v1/health`
- `GET /api/v1/me`
- `GET /api/v1/capabilities`
- `GET /api/v1/brands`
- `GET /api/v1/brands/{brand_id}/campaigns`
- `GET /api/v1/brands/{brand_id}/overview`
- `GET /api/v1/topics`
- `POST /api/v1/topics/manual`
- `GET /api/v1/audit`

Public distribution endpoints:

- `GET /skills/registry.json`
- `GET /skills/tnalpha-content-ops/manifest.json`
- `GET /skills/tnalpha-content-ops/{version}.zip`

The API is the stable business and authorization boundary. A future MCP server
must remain a thin adapter over these endpoints.
