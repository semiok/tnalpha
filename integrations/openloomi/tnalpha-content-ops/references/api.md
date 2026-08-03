# TN-Alpha Agent API P0

Base URL is supplied by `TNALPHA_API_URL`; the installed Skill defaults to
`https://alpha.traditionow.ai`. Set it to `http://127.0.0.1:8810` for dev.
Authentication is `Authorization: Bearer <token>`. The token maps to the existing
TN-Alpha administrator role for this experiment.

The client resolves credentials in this order:

1. `TNALPHA_AGENT_API_TOKEN` environment variable.
2. macOS Keychain service from `TNALPHA_AGENT_KEYCHAIN_SERVICE`.
3. Default Keychain service `ai.traditionow.tnalpha.agent-api.prod` for remote
   URLs, or `ai.traditionow.tnalpha.agent-api.dev` for localhost.

Only localhost may fall back to the dev LaunchAgent. This prevents a dev token
from being sent to the production endpoint accidentally.

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

The API is the stable business and authorization boundary. A future MCP server
should remain a thin adapter over these endpoints.
