# 选择基础镜像
FROM alpine:3.20

# 设置上海时区
RUN apk add tzdata && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo Asia/Shanghai > /etc/timezone

# 使用 HTTPS 协议访问容器云调用证书安装
RUN apk add ca-certificates

# 安装依赖包，选用国内镜像源以提高下载速度
RUN sed -i 's/dl-cdn.alpinelinux.org/mirrors.tencent.com/g' /etc/apk/repositories \
    && apk add --update --no-cache python3 py3-pip \
    && rm -rf /var/cache/apk/*

# 拷贝当前项目到/app目录下
COPY . /app

# 设定当前的工作目录
WORKDIR /app

# 安装依赖到指定的/install文件夹
# 选用国内镜像源以提高下载速度
RUN pip config set global.index-url http://mirrors.cloud.tencent.com/pypi/simple \
    && pip config set global.trusted-host mirrors.cloud.tencent.com \
    && pip install --upgrade pip \
    && pip install --user -r requirements.txt

# 暴露端口（需与云托管服务设置一致）
EXPOSE 80

# 执行启动命令
CMD ["python3", "run.py", "0.0.0.0", "80"]
