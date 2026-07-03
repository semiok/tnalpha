"""全局配置。开发用 SQLite（零安装），生产用 Postgres（设 DATABASE_URL）。"""
import os

# 数据库：默认 SQLite 文件（clone 即跑），生产用 env 覆盖为 postgresql://…
DATABASE_URL = os.environ.get("TNALPHA_DATABASE_URL", "sqlite:///./tnalpha.db")

# cookie 签名密钥（生产必须用 env 覆盖，否则可伪造）
SECRET_KEY = os.environ.get("TNALPHA_SECRET_KEY", "tnalpha-dev-secret-change-me")

# 文件上传根目录
DATA_DIR = os.environ.get("TNALPHA_DATA_DIR", "data")

# 知识库写功能开关：false=前端隐藏所有写入口、页面退化为静态只读预览（功能开发中）。
# 后端路由代码保留不动，翻开此开关即恢复全功能。部署演示时设 TNALPHA_KNOWLEDGE_WRITABLE=false。
KNOWLEDGE_WRITABLE = os.environ.get("TNALPHA_KNOWLEDGE_WRITABLE", "true").strip().lower() not in ("false", "0", "no")

# ── LLM provider（DB 设置为主，下面是建默认行时的初值 / env 兜底）──
# 文本 provider：stub | openai(兼容API) | claude-cli(订阅授权)；图像：stub | codex(订阅授权)
TEXT_PROVIDER = os.environ.get("TNALPHA_TEXT_PROVIDER", "stub")
IMAGE_PROVIDER = os.environ.get("TNALPHA_IMAGE_PROVIDER", "stub")
# openai 兼容（OpenAI/DeepSeek/Moonshot/MiniMax/Ollama…）
OPENAI_BASE_URL = os.environ.get("TNALPHA_OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.environ.get("TNALPHA_OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("TNALPHA_OPENAI_MODEL", "gpt-4o-mini")
# claude-cli（本机 claude CLI 订阅授权）
CLAUDE_MODEL = os.environ.get("TNALPHA_CLAUDE_MODEL", "sonnet")
LLM_TIMEOUT = int(os.environ.get("TNALPHA_LLM_TIMEOUT", "180"))
# codex（本机 ~/.codex/auth.json OAuth 出图）
CODEX_AUTH_PATH = os.environ.get("TNALPHA_CODEX_AUTH", "~/.codex/auth.json")
CODEX_RESPONSES_URL = os.environ.get("TNALPHA_CODEX_URL",
                                     "https://chatgpt.com/backend-api/codex/responses")
CODEX_ENVELOPE_MODEL = os.environ.get("TNALPHA_CODEX_ENVELOPE_MODEL", "gpt-5.5")
CODEX_CLIENT_VERSION = os.environ.get("TNALPHA_CODEX_CLIENT_VERSION", "2026.5.7")
IMAGE_MODEL = os.environ.get("TNALPHA_IMAGE_MODEL", "gpt-image-1-mini")
IMAGE_SIZE = os.environ.get("TNALPHA_IMAGE_SIZE", "1024x1024")
IMAGE_QUALITY = os.environ.get("TNALPHA_IMAGE_QUALITY", "low")
IMAGE_FORMAT = os.environ.get("TNALPHA_IMAGE_FORMAT", "png")
IMAGE_TIMEOUT = int(os.environ.get("TNALPHA_IMAGE_TIMEOUT", "400"))

# 三角色账号（密码默认 admin@123，env 可覆盖）。role: owner/editor/publisher
USERS = {
    os.environ.get("TNALPHA_OWNER_USER", "admin"):      (os.environ.get("TNALPHA_OWNER_PASS", "admin@123"), "owner"),
    os.environ.get("TNALPHA_EDITOR_USER", "admin1"):    (os.environ.get("TNALPHA_EDITOR_PASS", "admin@123"), "editor"),
    os.environ.get("TNALPHA_PUBLISHER_USER", "admin2"): (os.environ.get("TNALPHA_PUBLISHER_PASS", "admin@123"), "publisher"),
}
