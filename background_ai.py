#!/usr/bin/env python3
"""
后台AI处理脚本（独立进程，不受gunicorn生命周期影响）
由 views.py 通过 subprocess.Popen 调用
"""
import sys
import os
import json
import requests

# 接收参数
openid = sys.argv[1]
msg = sys.argv[2]
config_path = sys.argv[3] if len(sys.argv) > 3 else None

# 读取配置
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL, \
    DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME, \
    WECHAT_APPID, WECHAT_APPSECRET

# 获取数据库连接
def get_db():
    try:
        import pymysql
        return pymysql.connect(
            host=DB_HOST, port=int(DB_PORT) if DB_PORT else 3306,
            user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
            charset="utf8mb4", connect_timeout=5, cursorclass=pymysql.cursors.DictCursor
        )
    except Exception as e:
        return None

# 调用DeepSeek
def call_deepseek():
    if not DEEPSEEK_API_KEY:
        return "抱歉，AI服务暂不可用，请联系李工：15757807400"
    try:
        # 获取最近历史（简化）
        import pymysql
        conn = get_db()
        history = []
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT role, content FROM ai_chat_logs WHERE openid=%s ORDER BY id DESC LIMIT 50",
                    (openid,)
                )
                history = cursor.fetchall()
                cursor.close()
                conn.close()
            except:
                conn.close()
        
        messages = [{"role": "system", "content": ""}]
        for h in reversed(history):
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": msg})

        resp = requests.post(
            f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions" if DEEPSEEK_BASE_URL else "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": DEEPSEEK_MODEL or "deepseek-chat",
                "messages": messages,
                "max_tokens": 1024,
                "temperature": 0.7,
            },
            timeout=60
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        return "抱歉，AI服务暂不可用，请联系李工：15757807400"
    except Exception as e:
        return "抱歉，AI服务暂不可用，请联系李工：15757807400"

# 推送客服消息
def push_result(content):
    if not WECHAT_APPID or not WECHAT_APPSECRET:
        return
    try:
        # 获取token
        r = requests.get(
            f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={WECHAT_APPID}&secret={WECHAT_APPSECRET}",
            timeout=10
        )
        data = r.json()
        token = data.get("access_token")
        if not token:
            return
        
        # 推送
        requests.post(
            f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}",
            json={"touser": openid, "msgtype": "text", "text": {"content": content}},
            timeout=10
        )
    except:
        pass

if __name__ == "__main__":
    result = call_deepseek()
    # 保存到数据库
    import pymysql
    conn = get_db()
    if conn:
        try:
            cursor = conn.cursor()
            # 更新占位记录为真实回复
            cursor.execute(
                "UPDATE ai_chat_logs SET content=%s WHERE openid=%s AND role='assistant' AND content LIKE '🔍 正在分析%' ORDER BY id DESC LIMIT 1",
                (result, openid)
            )
            conn.commit()
            cursor.close()
        except:
            pass
        finally:
            conn.close()
    # 推送
    push_result(result)
