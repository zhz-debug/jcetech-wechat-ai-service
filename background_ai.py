#!/usr/bin/env python3
"""
后台AI处理脚本（独立进程，不受gunicorn生命周期影响）
由 views.py 通过 subprocess.Popen 调用
参数: openid msg
"""
import sys
import os
import json
import traceback

# 日志（同时写文件 + 标准错误输出到云托管控制台）
LOG_FILE = "/tmp/background_ai.log"

def log(msg):
    timestamp = __import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{line}\n")
    except:
        pass
    # 输出到stderr → 显示在云托管日志中
    print(line, file=__import__('sys').stderr, flush=True)

log(f"=== 后台AI启动 args={sys.argv} len={len(sys.argv)}")
log(f"Python可执行路径: {sys.executable}")
log(f"工作目录: {os.getcwd()}")
log(f"PATH环境变量: {os.environ.get('PATH', 'N/A')[:200]}")

try:
    import requests
    log("✓ requests导入成功")
except Exception as e:
    log(f"✗ 导入requests失败: {e}")
    sys.exit(1)

openid = sys.argv[1]
msg = sys.argv[2]
log(f"openid={openid[:15]}... msg前50字={msg[:50]}")

# 读取配置
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from config import (DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL,
        DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
        WECHAT_APPID, WECHAT_APPSECRET)
    log(f"config: KEY={'有' if DEEPSEEK_API_KEY else '空'}(前5={DEEPSEEK_API_KEY[:5]}), MODEL={DEEPSEEK_MODEL}")
    log(f"config: DB_HOST={DB_HOST}:{DB_PORT} DB_USER={DB_USER}")
    log(f"config: WECHAT_APPID={'有' if WECHAT_APPID else '空'}({WECHAT_APPID[:10]}...) APPSECRET={'有' if WECHAT_APPSECRET else '空'}")
except Exception as e:
    log(f"✗ 导入config失败: {e}")
    log(traceback.format_exc())
    sys.exit(1)

def get_db():
    try:
        import pymysql
        return pymysql.connect(
            host=DB_HOST, port=int(DB_PORT) if DB_PORT else 3306,
            user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
            charset="utf8mb4", connect_timeout=5, cursorclass=pymysql.cursors.DictCursor
        )
    except Exception as e:
        log(f"数据库连接失败: {e}")
        return None

def call_deepseek():
    """调用DeepSeek并返回回复"""
    if not DEEPSEEK_API_KEY:
        log("DeepSeek API Key 为空")
        return "抱歉，AI服务暂不可用，请联系李工：15757807400"
    
    try:
        # 获取最近聊天历史
        import pymysql
        conn = get_db()
        history = []
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT role, content FROM ai_chat_logs WHERE openid=%s ORDER BY id ASC LIMIT 50",
                    (openid,)
                )
                history = cursor.fetchall()
                cursor.close()
                conn.close()
                log(f"获取到 {len(history)} 条历史记录")
            except Exception as e:
                log(f"查询历史失败: {e}")
                try: conn.close()
                except: pass
        
        # 构建消息
        messages = [{"role": "system", "content": ""}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": msg})
        
        # 调用DeepSeek
        url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
        log(f"调用DeepSeek: model={DEEPSEEK_MODEL}, url={url}")
        
        resp = requests.post(
            url,
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
            result = resp.json()["choices"][0]["message"]["content"]
            log(f"DeepSeek返回成功: {len(result)}字")
            return result
        else:
            log(f"DeepSeek错误: {resp.status_code} {resp.text[:200]}")
            return "抱歉，AI服务暂不可用，请联系李工：15757807400"
    except requests.exceptions.Timeout:
        log("DeepSeek超时(60s)")
        return "抱歉，AI服务暂不可用，请联系李工：15757807400"
    except Exception as e:
        log(f"DeepSeek异常: {e}\n{traceback.format_exc()}")
        return "抱歉，AI服务暂不可用，请联系李工：15757807400"

def push_result(content):
    """通过微信客服消息API推送"""
    if not WECHAT_APPID or not WECHAT_APPSECRET:
        log(f"✗ 微信APPID或APPSECRET为空: appid={'有' if WECHAT_APPID else '空'} secret={'有' if WECHAT_APPSECRET else '空'}")
        return
    try:
        # 获取access_token
        log("获取access_token...")
        secret_url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={WECHAT_APPID}&secret={WECHAT_APPSECRET}"
        log(f"请求token URL(secret掩码): {secret_url[:60]}...{secret_url[-10:]}")
        r = requests.get(secret_url, timeout=10)
        data = r.json()
        token = data.get("access_token")
        if not token:
            log(f"获取token失败: {data}")
            return
        log("token获取成功")
        
        # 推送消息
        push_resp = requests.post(
            f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}",
            json={"touser": openid, "msgtype": "text", "text": {"content": content}},
            timeout=10
        )
        push_data = push_resp.json()
        log(f"推送结果: {push_data}")
    except Exception as e:
        log(f"推送异常: {e}")

if __name__ == "__main__":
    try:
        result = call_deepseek()
        log(f"AI回复: {result[:100]}...")
        
        # 更新数据库占位记录
        try:
            import pymysql
            conn = get_db()
            if conn:
                cursor = conn.cursor()
                cursor.execute(
                    "UPDATE ai_chat_logs SET content=%s WHERE openid=%s AND role='assistant' AND content LIKE '🔍 正在分析%' ORDER BY id DESC LIMIT 1",
                    (result, openid)
                )
                affected = cursor.rowcount
                conn.commit()
                cursor.close()
                conn.close()
                log(f"数据库更新: {affected}行")
        except Exception as e:
            log(f"数据库更新失败: {e}")
        
        # 推送
        push_result(result)
        log("=== 后台AI完成 ===")
    except Exception as e:
        log(f"主流程异常: {e}\n{traceback.format_exc()}")
