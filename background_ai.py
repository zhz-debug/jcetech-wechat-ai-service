#!/usr/bin/env python3
"""
后台AI处理脚本（独立进程）
由 views.py 通过 subprocess.Popen 调用
参数: openid msg

调试方式：每一步写入 MySQL ai_debug_logs 表
在 VM 上通过 mysql 客户端直接查询进度
"""

import sys
import os
import json
import traceback
import datetime

# ============================================================
# 配置导入
# ============================================================
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from config import (DEEPSEEK_API_KEY, DEEPSEEK_MODEL, DEEPSEEK_BASE_URL,
        DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME,
        WECHAT_APPID, WECHAT_APPSECRET)
except Exception as e:
    print(f"FATAL: 导入config失败: {e}\n{traceback.format_exc()}", file=sys.stderr, flush=True)
    sys.exit(1)

openid = sys.argv[1] if len(sys.argv) > 1 else "unknown"
msg = sys.argv[2] if len(sys.argv) > 2 else ""

# ============================================================
# 数据库调试日志
# ============================================================

def get_conn():
    """获取MySQL连接"""
    try:
        import pymysql
        return pymysql.connect(
            host=DB_HOST, port=int(DB_PORT) if DB_PORT else 3306,
            user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
            charset="utf8mb4", connect_timeout=10,
            cursorclass=pymysql.cursors.DictCursor
        )
    except Exception as e:
        print(f"DB_CONNECT_FAIL: {e}", file=sys.stderr, flush=True)
        return None

def db_log(step, detail=""):
    """写一步调试日志到 MySQL，同时输出到 stderr"""
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] [{step}] {detail}"
    print(line, file=sys.stderr, flush=True)
    try:
        import pymysql
        conn = get_conn()
        if conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO ai_debug_logs (openid, step, detail) VALUES (%s, %s, %s)",
                (openid, step, detail[:500])
            )
            conn.commit()
            cursor.close()
            conn.close()
    except Exception as e:
        print(f"DB_LOG_FAIL: {e}", file=sys.stderr, flush=True)

def ensure_debug_table():
    """确保调试日志表存在"""
    conn = get_conn()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_debug_logs (
                id INT AUTO_INCREMENT PRIMARY KEY,
                openid VARCHAR(64) DEFAULT '',
                step VARCHAR(64) DEFAULT '',
                detail TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                KEY idx_openid (openid),
                KEY idx_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """)
        conn.commit()
        cursor.close()
    except Exception as e:
        print(f"CREATE_TABLE_FAIL: {e}", file=sys.stderr, flush=True)
    finally:
        conn.close()

# ============================================================
# 主逻辑
# ============================================================

# 0. 初始化调试表
ensure_debug_table()
db_log("START", f"args={sys.argv} cwd={os.getcwd()}")

# 1. 导入依赖
try:
    import requests
    import pymysql
    db_log("IMPORT_OK", "requests+pymysql导入成功")
except Exception as e:
    db_log("IMPORT_FAIL", str(e))
    sys.exit(1)

# 2. 环境信息
db_log("ENV", f"python={sys.executable} path={os.environ.get('PATH','')[:100]}")
db_log("MSG", f"openid={openid[:20] if openid else '空'} msg={msg[:80] if msg else '空'}")

# 3. 验证配置
db_log("CONFIG",
    f"DeepSeek: KEY={'有' if DEEPSEEK_API_KEY else '空'}({DEEPSEEK_API_KEY[:5]}) "
    f"MODEL={DEEPSEEK_MODEL} URL={DEEPSEEK_BASE_URL}")
db_log("CONFIG",
    f"DB: {DB_HOST}:{DB_PORT}/{DB_NAME} user={DB_USER}")
db_log("CONFIG",
    f"WECHAT: appid={'有' if WECHAT_APPID else '空'} secret={'有' if WECHAT_APPSECRET else '空'}")

# 4. 调用DeepSeek
db_log("AI_CALL_START", f"正在调用DeepSeek model={DEEPSEEK_MODEL}")

if not DEEPSEEK_API_KEY:
    db_log("AI_SKIP", "API Key为空")
    result = "抱歉，AI服务暂不可用，请联系李工：15757807400"
else:
    try:
        # 获取历史
        conn = get_conn()
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
                db_log("HISTORY", f"获取{len(history)}条历史")
            except Exception as e:
                db_log("HISTORY_FAIL", str(e))
            finally:
                conn.close()

        # 构建消息
        messages = [{"role": "system", "content": ""}]
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": msg})

        # 调用DeepSeek
        url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
        db_log("AI_REQUEST", f"POST {url}")
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
        db_log("AI_RESP", f"status={resp.status_code}")

        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"]
            db_log("AI_OK", f"回复{len(result)}字")
        else:
            db_log("AI_ERR", f"{resp.status_code} {resp.text[:200]}")
            result = "抱歉，AI服务暂不可用，请联系李工：15757807400"

    except requests.exceptions.Timeout:
        db_log("AI_TIMEOUT", "DeepSeek超时60s")
        result = "抱歉，AI服务暂不可用，请联系李工：15757807400"
    except requests.exceptions.ConnectionError as e:
        db_log("AI_CONN_ERR", f"连接失败: {e}")
        result = "抱歉，AI服务暂不可用，请联系李工：15757807400"
    except Exception as e:
        db_log("AI_EXCEPTION", f"{e}\n{traceback.format_exc()}")
        result = "抱歉，AI服务暂不可用，请联系李工：15757807400"

# 5. 更新数据库占位记录
db_log("UPDATE_DB", f"回复前100字: {result[:100]}")
try:
    conn = get_conn()
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
        db_log("UPDATE_DB_OK", f"更新{affected}条记录")
except Exception as e:
    db_log("UPDATE_DB_FAIL", str(e))

# 6. 通过客服消息API推送
db_log("PUSH_START", "开始推送客服消息")
if not WECHAT_APPID or not WECHAT_APPSECRET:
    db_log("PUSH_SKIP", f"微信配置不全: appid={'有' if WECHAT_APPID else '空'} secret={'有' if WECHAT_APPSECRET else '空'}")
else:
    try:
        # 获取 access_token
        db_log("TOKEN_REQ", "请求access_token...")
        token_url = (f"https://api.weixin.qq.com/cgi-bin/token"
                     f"?grant_type=client_credential"
                     f"&appid={WECHAT_APPID}"
                     f"&secret={WECHAT_APPSECRET}")
        r = requests.get(token_url, timeout=10)
        data = r.json()
        token = data.get("access_token")
        if not token:
            db_log("TOKEN_FAIL", f"获取失败: {data}")
        else:
            db_log("TOKEN_OK", "access_token获取成功")

            # 推送消息
            push_url = (f"https://api.weixin.qq.com/cgi-bin/message/custom/send"
                       f"?access_token={token}")
            push_resp = requests.post(
                push_url,
                json={"touser": openid, "msgtype": "text", "text": {"content": result}},
                timeout=10
            )
            push_data = push_resp.json()
            db_log("PUSH_RESULT", f"推送结果: {push_data}")
    except Exception as e:
        db_log("PUSH_EXCEPTION", f"{e}\n{traceback.format_exc()}")

db_log("DONE", "后台AI处理完成")
