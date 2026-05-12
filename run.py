# 创建应用实例
import sys

from wxcloudrun import app

# 启动Flask Web服务
if __name__ == '__main__':
    # threaded=True: 多线程模式，后台线程安全存活
    # Flask dev server + threading 在云托管缩容模式下，容器有5-15分钟优雅关闭期
    # 后台线程在此期间可完成DeepSeek调用并推送客服消息
    app.run(host=sys.argv[1], port=sys.argv[2], threaded=True)
