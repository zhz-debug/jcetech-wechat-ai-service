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

# 历史聊天记录追忆轮数（每轮=用户1条+AI1条，留空默认15轮）
CHAT_HISTORY_ROUNDS = int(os.environ.get("CHAT_HISTORY_ROUNDS", "25"))

# 滥用检测：连续多少条非工作消息触发提醒（0=关闭）
ABUSE_TRIGGER_COUNT = int(os.environ.get("ABUSE_TRIGGER_COUNT", "10"))

# 聊天记录自动清理天数（默认30天）
CHAT_RETENTION_DAYS = int(os.environ.get("CHAT_RETENTION_DAYS", "30"))
