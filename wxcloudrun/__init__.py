from flask import Flask

# 初始化web应用
app = Flask(__name__)

# 加载配置
app.config.from_object('config')

# 加载控制器
from wxcloudrun import views
