# 基于 Debian slim 的 Python 镜像，更稳定
FROM python:3.11-slim

# 设置上海时区 + 安装CA证书（必须完整安装，否则SSL验证失败）
RUN apt-get update && apt-get install -y tzdata ca-certificates openssl \
    && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone \
    && update-ca-certificates --fresh \
    && rm -rf /var/lib/apt/lists/*

# 确保SSL证书环境变量正确
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# 拷贝项目到/app目录
COPY . /app

# 设定工作目录
WORKDIR /app

# 安装依赖（使用腾讯云镜像加速）
RUN pip config set global.index-url http://mirrors.cloud.tencent.com/pypi/simple \
    && pip config set global.trusted-host mirrors.cloud.tencent.com \
    && pip install --upgrade pip \
    && pip install -r requirements.txt

# 暴露端口（需与云托管服务设置一致）
EXPOSE 80

# 执行启动命令
CMD ["python3", "run.py", "0.0.0.0", "80"]
