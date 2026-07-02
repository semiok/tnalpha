"""全局配置。开发用 SQLite（零安装），生产用 Postgres（设 DATABASE_URL）。"""
import os

# 数据库：默认 SQLite 文件（clone 即跑），生产用 env 覆盖为 postgresql://…
DATABASE_URL = os.environ.get("TNALPHA_DATABASE_URL", "sqlite:///./tnalpha.db")

# cookie 签名密钥（生产必须用 env 覆盖，否则可伪造）
SECRET_KEY = os.environ.get("TNALPHA_SECRET_KEY", "tnalpha-dev-secret-change-me")

# 文件上传根目录
DATA_DIR = os.environ.get("TNALPHA_DATA_DIR", "data")

# LLM provider：stub（默认，返回确定性假数据，无需密钥/网络）/ 后接 claude(文) / codex(图)
LLM_PROVIDER = os.environ.get("TNALPHA_LLM_PROVIDER", "stub")

# 三角色账号（密码默认 admin@123，env 可覆盖）。role: owner/editor/publisher
USERS = {
    os.environ.get("TNALPHA_OWNER_USER", "admin"):      (os.environ.get("TNALPHA_OWNER_PASS", "admin@123"), "owner"),
    os.environ.get("TNALPHA_EDITOR_USER", "admin1"):    (os.environ.get("TNALPHA_EDITOR_PASS", "admin@123"), "editor"),
    os.environ.get("TNALPHA_PUBLISHER_USER", "admin2"): (os.environ.get("TNALPHA_PUBLISHER_PASS", "admin@123"), "publisher"),
}
