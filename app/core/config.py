"""全局配置。开发用 SQLite（零安装），生产用 Postgres（设 DATABASE_URL）。"""
import os

# 数据库：默认 SQLite 文件（clone 即跑），生产用 env 覆盖为 postgresql://…
DATABASE_URL = os.environ.get("TNALPHA_DATABASE_URL", "sqlite:///./tnalpha.db")

# cookie 签名密钥（生产必须用 env 覆盖，否则可伪造）
SECRET_KEY = os.environ.get("TNALPHA_SECRET_KEY", "tnalpha-dev-secret-change-me")

# 文件上传根目录
DATA_DIR = os.environ.get("TNALPHA_DATA_DIR", "data")

# 知识库/全站模式开关的【初始默认值】。运行时真实状态存 DB（AppSetting，见 core/settings.py），
# 定义者在页面右上角「开发/演示」按钮点击切换、持久保存（重启/reload 不丢）。
#   True  = 开发模式：GET / 是动态知识库（能新建品牌/上传/AI解析）
#   False = 演示模式：GET / 是原型六模块只读演示壳
# 默认 False：clone 下来/未配 env 时先看演示壳（与线上一致，协作者不困惑），要开发点右上角切换即可。
# env TNALPHA_KNOWLEDGE_WRITABLE 只决定「DB 首次建行时」的初值；之后以 DB 为准。
KNOWLEDGE_WRITABLE = os.environ.get("TNALPHA_KNOWLEDGE_WRITABLE", "false").strip().lower() not in ("false", "0", "no")

# ── LLM provider（DB 设置为主，下面是建默认行时的初值 / env 兜底）──
# 文本 provider：stub | openai(兼容API) | minimax-m3 | claude-cli(订阅授权)
# 图像 provider：stub | codex(订阅授权) | minimax-m3
TEXT_PROVIDER = os.environ.get("TNALPHA_TEXT_PROVIDER", "claude-cli")  # 默认本机 Claude；无/失败回退 stub
IMAGE_PROVIDER = os.environ.get("TNALPHA_IMAGE_PROVIDER", "stub")
# openai 兼容（OpenAI/DeepSeek/Moonshot/MiniMax/Ollama…）
OPENAI_BASE_URL = os.environ.get("TNALPHA_OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_API_KEY = os.environ.get("TNALPHA_OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("TNALPHA_OPENAI_MODEL", "gpt-4o-mini")
# 图像 API 单独配置，避免图像 provider 覆盖文本模型的 key/base/model。
IMAGE_BASE_URL = os.environ.get("TNALPHA_IMAGE_BASE_URL", "https://api.minimax.chat/v1")
IMAGE_API_KEY = os.environ.get("TNALPHA_IMAGE_API_KEY", "")
IMAGE_PROVIDER_MODEL = os.environ.get("TNALPHA_IMAGE_PROVIDER_MODEL", "image-01")
# claude-cli（本机 claude CLI 订阅授权）
CLAUDE_MODEL = os.environ.get("TNALPHA_CLAUDE_MODEL", "sonnet")
# claude 二进制路径：默认按 PATH 找 "claude"；不在 PATH（如 launchd 受限环境）就设全路径
CLAUDE_BIN = os.environ.get("TNALPHA_CLAUDE_BIN", "claude")
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
