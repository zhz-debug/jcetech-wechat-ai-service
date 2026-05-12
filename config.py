import os

# 是否开启debug模式
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# 微信配置
WECHAT_TOKEN = os.environ.get("WECHAT_TOKEN", "jcetech2026")

# DeepSeek AI 配置
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# MySQL 配置（腾讯云服务器公网）
DB_HOST = os.environ.get("DB_HOST", "101.43.85.171")
DB_PORT = os.environ.get("DB_PORT", "3308")
DB_USER = os.environ.get("DB_USER", "jcetech")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "Jc3T3ch@2026!User")
DB_NAME = os.environ.get("DB_NAME", "jcetech")

# 白名单（逗号分隔，留空则全放开）
ALLOWED_USERS = os.environ.get("ALLOWED_USERS", "")
