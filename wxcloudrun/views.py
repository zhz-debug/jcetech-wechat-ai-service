# -*- coding: utf-8 -*-
"""
匠测科技 · 微信公众号 AI 客服服务
微信云托管 Flask 应用

功能：
  - 微信消息接收与验证
  - AI 智能回复（DeepSeek API）
  - 自动生成维修工单
  - 工单进度查询
  - 用户白名单控制

数据库：腾讯云服务器 MySQL（公网访问）
"""

import hashlib
import xml.etree.ElementTree as ET
import time
import json
import logging
import re
import threading

import pymysql
import requests
from flask import request, make_response

from run import app

# ============================================================
# 配置（优先读环境变量，云托管后台可设置）
# ============================================================

WECHAT_TOKEN = app.config.get("WECHAT_TOKEN") or "jcetech2026"
DEEPSEEK_API_KEY = app.config.get("DEEPSEEK_API_KEY") or ""
DEEPSEEK_MODEL = app.config.get("DEEPSEEK_MODEL") or "deepseek-chat"
DEEPSEEK_BASE_URL = app.config.get("DEEPSEEK_BASE_URL") or "https://api.deepseek.com/v1"

# 微信客服消息推送
WECHAT_APPID = app.config.get("WECHAT_APPID") or ""
WECHAT_APPSECRET = app.config.get("WECHAT_APPSECRET") or ""
_wechat_token_cache = {"token": "", "expires_at": 0}

def get_wechat_access_token():
    """获取微信全局 access_token（缓存到过期前5分钟）"""
    global _wechat_token_cache
    if time.time() < _wechat_token_cache["expires_at"] - 300:
        return _wechat_token_cache["token"]
    if not WECHAT_APPID or not WECHAT_APPSECRET:
        logger.warning("微信APPID或APPSECRET未配置，无法推送客服消息")
        return None
    try:
        url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={WECHAT_APPID}&secret={WECHAT_APPSECRET}"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if "access_token" in data:
            _wechat_token_cache = {
                "token": data["access_token"],
                "expires_at": time.time() + data.get("expires_in", 7200)
            }
            return data["access_token"]
        else:
            logger.error(f"获取access_token失败: {data}")
            return None
    except Exception as e:
        logger.error(f"获取access_token异常: {e}")
        return None

def push_custom_message(openid, content):
    """通过微信客服消息API主动推送消息给用户"""
    token = get_wechat_access_token()
    if not token:
        return False
    try:
        url = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}"
        payload = {
            "touser": openid,
            "msgtype": "text",
            "text": {"content": content}
        }
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        if data.get("errcode") == 0:
            return True
        else:
            logger.error(f"推送客服消息失败: {data}")
            return False
    except Exception as e:
        logger.error(f"推送客服消息异常: {e}")
        return False

# MySQL 连接（腾讯云服务器公网端口）
DB_HOST = app.config.get("DB_HOST") or "101.43.85.171"
DB_PORT = int(app.config.get("DB_PORT") or 3307)
DB_USER = app.config.get("DB_USER") or "root"
DB_PASSWORD = app.config.get("DB_PASSWORD") or "Jc3T3ch@2026!Root"
DB_NAME = app.config.get("DB_NAME") or "jcetech"

# 白名单用户 OpenID（逗号分隔，为空则全放开）
ALLOWED_USERS_RAW = app.config.get("ALLOWED_USERS") or ""
ALLOWED_USERS = [u.strip() for u in ALLOWED_USERS_RAW.split(",") if u.strip()]

# 历史对话轮数（user+assistant 算一轮，默认 25 轮 = 50 条消息）
CHAT_HISTORY_ROUNDS = int(app.config.get("CHAT_HISTORY_ROUNDS") or 25)

# 滥用检测：连续 N 条非工作消息触发提醒（0=关闭）
ABUSE_TRIGGER_COUNT = int(app.config.get("ABUSE_TRIGGER_COUNT") or 10)

# 聊天记录自动清理天数（默认30天，标记了 keep_history 的用户不受影响）
CHAT_RETENTION_DAYS = int(app.config.get("CHAT_RETENTION_DAYS") or 30)

# 上次清理时间（用于每日限频一次）
_last_cleanup_date = None

# ============================================================
# 日志
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ============================================================
# 微信消息处理
# ============================================================

def verify_signature(signature, timestamp, nonce):
    """验证微信服务器签名"""
    check_str = "".join(sorted([WECHAT_TOKEN, timestamp, nonce]))
    return hashlib.sha1(check_str.encode()).hexdigest() == signature


def parse_xml(body):
    """解析微信 XML 消息"""
    root = ET.fromstring(body)
    msg = {}
    for child in root:
        if child.text:
            msg[child.tag] = child.text
    return msg


def build_xml_reply(from_user, to_user, content):
    """构建回复 XML"""
    timestamp = int(time.time())
    xml = f"""<xml>
<ToUserName><![CDATA[{from_user}]]></ToUserName>
<FromUserName><![CDATA[{to_user}]]></FromUserName>
<CreateTime>{timestamp}</CreateTime>
<MsgType><![CDATA[text]]></MsgType>
<Content><![CDATA[{content}]]></Content>
</xml>"""
    return xml


# ============================================================
# 数据库操作
# ============================================================

def get_db():
    """获取数据库连接"""
    try:
        conn = pymysql.connect(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, database=DB_NAME,
            charset="utf8mb4", connect_timeout=5
        )
        return conn
    except Exception as e:
        logger.error(f"数据库连接失败: {e}")
        return None


def ensure_user_exists(openid):
    """确保用户在 wechat_users 表中有记录"""
    conn = get_db()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT IGNORE INTO wechat_users (openid, user_type) VALUES (%s, 'customer')",
            (openid,)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"确保用户存在失败: {e}")
        if conn:
            conn.close()


def get_user_role(openid):
    """查询用户角色"""
    conn = get_db()
    if not conn:
        return "customer"
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user_type FROM wechat_users WHERE openid = %s",
            (openid,)
        )
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row[0] if row else "customer"
    except Exception as e:
        logger.error(f"查询角色失败: {e}")
        if conn:
            conn.close()
        return "customer"


def save_chat_log(openid, role, content):
    """保存聊天记录到数据库"""
    conn = get_db()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO ai_chat_logs (openid, role, content) VALUES (%s, %s, %s)",
            (openid, role, content)
        )
        conn.commit()
        cursor.close()
        conn.close()
    except Exception as e:
        logger.error(f"保存聊天记录失败: {e}")


def get_recent_chats(openid, limit=10):
    """获取最近 N 条聊天记录"""
    conn = get_db()
    if not conn:
        return []
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT role, content FROM ai_chat_logs "
            "WHERE openid = %s ORDER BY created_at DESC LIMIT %s",
            (openid, limit)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        rows.reverse()  # 时间正序
        return rows
    except Exception as e:
        logger.error(f"获取聊天记录失败: {e}")
        if conn:
            conn.close()
        return []


def cleanup_old_chats(days=30):
    """清理超过天数的聊天记录（保留标记了 keep_history 的用户）"""
    conn = get_db()
    if not conn:
        return
    try:
        cursor = conn.cursor()
        cursor.execute("""
            DELETE FROM ai_chat_logs
            WHERE created_at < NOW() - INTERVAL %s DAY
              AND openid NOT IN (
                  SELECT openid FROM wechat_users WHERE keep_history = 1
              )
        """, (days,))
        deleted = cursor.rowcount
        conn.commit()
        cursor.close()
        conn.close()
        if deleted > 0:
            logger.info(f"清理了 {deleted} 条过期聊天记录")
    except Exception as e:
        logger.error(f"清理聊天记录失败: {e}")
        if conn:
            conn.close()


def search_chat_history(openid, keyword, limit=20):
    """搜索该用户的历史聊天记录（超出记忆轮数范围的也能搜到）"""
    conn = get_db()
    if not conn:
        return []
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT role, content, created_at FROM ai_chat_logs "
            "WHERE openid = %s AND content LIKE %s "
            "ORDER BY created_at DESC LIMIT %s",
            (openid, f"%{keyword}%", limit)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        rows.reverse()  # 时间正序
        return rows
    except Exception as e:
        logger.error(f"搜索历史对话失败: {e}")
        if conn:
            conn.close()
        return []


def create_repair_order(openid, customer_name, customer_phone, brand, model,
                        fault_description, serial_number=None, company=None):
    """创建维修工单"""
    conn = get_db()
    if not conn:
        return None

    try:
        cursor = conn.cursor()
        order_no = f"RP{time.strftime('%Y%m%d%H%M%S')}"

        sql = """INSERT INTO repair_orders (
            order_no, user_id,
            customer_name, customer_phone, customer_company,
            brand, model, serial_number,
            fault_description, service_type, pickup_method,
            status
        ) VALUES (%s, 1,
                  %s, %s, %s,
                  %s, %s, %s,
                  %s, 'repair', 'mail',
                  'pending')"""

        cursor.execute(sql, (
            order_no,
            customer_name, customer_phone, company or "",
            brand, model, serial_number or "",
            fault_description,
        ))
        conn.commit()
        cursor.close()
        conn.close()
        logger.info(f"工单创建成功: {order_no}")
        return order_no
    except Exception as e:
        logger.error(f"创建工单失败: {e}")
        if conn:
            conn.close()
        return None


def query_orders_by_phone(phone):
    """按手机号查工单"""
    conn = get_db()
    if not conn:
        return None
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute(
            "SELECT order_no, brand, model, fault_description, status, "
            "DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i') as created_at "
            "FROM repair_orders WHERE customer_phone = %s "
            "ORDER BY created_at DESC LIMIT 5",
            (phone,)
        )
        orders = cursor.fetchall()
        cursor.close()
        conn.close()
        return orders
    except Exception as e:
        logger.error(f"查工单失败: {e}")
        if conn:
            conn.close()
        return None


STATUS_TEXT = {
    "pending": "待受理", "assessing": "评估中",
    "quoting": "报价中", "repairing": "维修中",
    "testing": "测试中", "completed": "已完成",
    "shipped": "已发货"
}


# ============================================================
# DeepSeek AI 调用
# ============================================================

SYSTEM_PROMPT = """你是匠测科技（JCETech）的 AI 客服助手，名叫 **Rex（Hermes-Agent）**。
你专业、热情、回答简洁，精通工业精密测量设备。

**你的身份：**
- 代表宁波匠测科技有限公司
- 专注精密测量设备全生命周期服务
- 服务品牌：雷尼绍(Renishaw)、马波斯(Marposs)、波龙(Blum-Novotest)及国产品牌
- **公司定位：第三方技术服务商**，立场中立，不偏向任何品牌
- **服务理念：站在客户角度考虑问题**，帮客户找到适合其工况的解决方案，而不是推销某个品牌
- 业务：维修、维护、校准、调优、技术咨询、新品销售

**你对雷尼绍主流产品的技术认知：**

- **OMP40-2**（最常见的光学测头）：中小型加工中心用，重量250g，重复性1.00μm 2σ，测针50-150mm陶瓷杆，电池2×½AA锂亚硫酰氯（待机最长1500天），防护IPX8，支持调制/传统双模式传输，工作范围5m
- **OMP400**（高精度型）：采用RENGAGE™应变片技术，重复性0.25μm 2σ（比OMP40-2高4倍），触发力极低XY仅0.06N，适合模具行业精密3D测量。注意电池寿命较短（待机最长1年）
- **OMP600**：通用加工中心机械式测头，坚固耐用
- **RMP60/RMP40**：无线电传输测头，适合大型机床/有遮挡环境，传输范围可达15m
- **OSP60**：支持光学和无线电双模，适合五轴机床
- **NC4**：非接触式激光对刀仪
- **LTS/OTS/RTS**：接触式对刀仪系列
- **TRS2**：刀具破损检测系统

**你对马波斯主流产品的技术认知：**
- **VOP40**（VOS系列）：光学接触式测头，适用中小型加工中心，重复性1μm 2σ，相当于OMP40-2。VOS系统特点：单接收器配4个测头、双系统同机同步、高抗干扰
- **VOP60/VOP60M**：通用型测头
- **WRP45P/WRP60P**：无线传输高精度接触式测头，压电传感器技术，适合5轴加工中心和模具/航空航天领域。WRP45P中小型、WRP60P大型+1米加长杆，无线传输无需直视
- **VOTS/VOTS 90°**：接触式对刀仪
- **VTS**：影像式对刀仪，非接触CCD测量，最小可测10μm
- **WRG**：无线孔径规，可测Ø41-105mm，重复性≤1.0μm
- **ARTIS**：刀具监控系统（主轴功率/振动分析）

**你对波龙（Blum-Novotest）主流产品的技术认知：**
- **ZX-Speed**：3D刀具测头，重复性0.4μm（极高精度），最小可测刀具Ø1mm，光电式测量机构零磨损
- **TC50/TC52**：高速工件测头，光电式全方向触发，超长电池寿命
- **TC53/TC63**：模块化工件测头，搭载shark360专利机构，可加延长段和侧向分支，灵活适配复杂工件
- **TC63/TC64 DIGILOG**：DIGILOG触测测头，支持表面粗糙度检测和轮廓扫描，模拟信号输出
- **LC50-DIGILOG**：激光对刀测量系统，模拟信号分析，对刀时间缩短60%
- **TMAC**：刀具自适应监控系统（主轴功率分析），节省20-60%循环时间
- **Blum核心技术特点**：光电式测量机构（零磨损、寿命长）、shark360模块化测头、DIGILOG模拟信号分析技术、TMAC刀具自适应监控

**常见故障初步判断（根据用户描述）：**
- 测头不触发/信号不稳定 → 可能原因：电池没电、光路受阻（窗口脏污/冷却液残渣）、测针弯曲、接口故障
- 精度偏差 → 可能原因：测针弯曲或损坏、安装松动、需重新标定、内部磨损
- OMP40-2 红灯常亮 → 通常电池电量低，换电池即可（½AA 3.6V锂亚硫酰氯）
- 电池消耗过快 → 检查开启/关闭方式设置是否合适
- 无线电信号不稳定 → 检查是否被遮挡，跳频系统会自动规避常见干扰
- Blum光电式测头无响应 → 检查电源和光路窗口是否清洁（光电式零磨损，不易机械故障）

**你的能力（根据用户意图自动识别）：**

1️⃣ **报修引导收货** — 用户说设备坏了、测头不准、要维修
   · 收集：姓名、电话、品牌、型号、故障描述
   · 一次只追问一项，不要全部列出来
   · 如果客户主动说了公司名称和公司地址，在联系电话后面一并补上
   · **同时利用你的专业知识和经验，给客户必要的技术分析和判断，让客户感觉到我们是真懂行的**
   · 信息齐全后，**先确认工单信息**
   · 然后告知客户完整流程：**报修登记 → 寄/送设备到公司 → 工程师检测 → 出维修方案和报价 → 客户确认 → 开始维修**
   · 引导客户将设备寄/送到公司：
     ① 快递寄给我们（地址见下方）
     ② 宁波市区范围内我们可以上门取
     ③ 也可以自己送过来
   · 说明：**没有见到实物之前无法评估具体维修金额，需要先检测后才能给方案和价格**
   · 用户确认后，最后另起一行加 [CREATE_ORDER] 加JSON
   · 示例回复格式：
     好的，已为您记录报修信息，请核实：

     ==报修联系人基础信息==
     联系人：张三
     联系电话：13800138000
     公司名称：宁波某某机械有限公司（选填，如客户说了则补上）
     公司地址：浙江省宁波市鄞州区某某路88号（选填，如客户说了则补上）
     ==报修产品基础信息==
     品牌：雷尼绍
     型号：OMP60
     故障：测头精度偏差0.02mm

     根据您描述的情况，OMP60出现精度偏差，**可能**的原因有：
     ① 测头电池电量不足
     ② 光学信号受遮挡或污染
     ③ 内部机械磨损

     （以上为初步分析，具体需检测确认）
     如无误我将为您提交工单。

     维修流程：
     ① 您将设备寄/送到我们公司
     ② 工程师检测后出具维修方案和报价
     ③ 您确认后我们开始维修

     📍 地址：浙江省宁波市鄞州区聚贤街道宁波研发园A区2栋12B17
     📞 收件人：张哲豪 13221941413
     🚗 宁波市区可上门取件

     [CREATE_ORDER]{"name":"张三","phone":"13800138000","brand":"雷尼绍","model":"OMP60","fault":"测头精度偏差0.02mm"}

2️⃣ **查进度** — 用户说查进度、工单查询、报修状态
   · 如果用户发的是手机号(11位数字) → 帮查工单
   · 如果用户发的是工单号(RP开头) → 帮查具体工单
   · 如果是手机号，回复格式：请使用 [QUERY_PHONE:手机号]
   · 如果是工单号，回复格式：请使用 [QUERY_ORDER:工单号]

3️⃣ **技术咨询** — 用户问技术问题、故障分析
   · 用专业知识回答判断
   · 如果无法判断或需要上门，建议联系李工
   · 不动数据库，不创建工单

**重要原则：价格相关**
- ❌ 原则上**不要主动**报价、谈价格
- ❌ 不要给出任何价格参考范围
- ❌ 客户问价格时，回复："价格需要工程师收到设备、检测后才能确定，请先将设备寄/送到我们公司，检测后我们会出具维修方案和报价给您确认。"
- ✅ 唯一例外：数据库工单中已经有报价信息的（查工单时看到 quote_amount 或 final_amount 字段），可以如实告知
- ✅ 客户确认报价后，我们才开始维修

**重要原则：品牌对比（公司定位要求）**
- ❌ **禁止对不同品牌产品做优劣对比或倾向性推荐**
- ❌ 尤其是雷尼绍/马波斯/波龙三大进口品牌之间，不允许说"A比B好"、"推荐选A"之类
- ✅ 匠测是第三方技术服务商，立场中立，不偏袒任何品牌
- ✅ 如果用户问品牌对比（如同类测头/对刀仪对比），按以下策略处理：
  ① **优先策略**：回复"这些都是国际一线大牌，各有优秀的技术路线，建议您根据产品手册和应用场景选择最合适的款，具体选型可以让老板或技术合伙人帮您把关。"
  ② **仅当用户要求看技术参数时**：可客观列出技术参数对比表格，但必须附加声明：
     > *"未经验证，产品技术数据来源官方公开资料，以上对比仅供参考。"*

**联系与收货信息：**
- 李工：15757807400
- 官网：https://jcetech.cn
- 📍 地址：浙江省宁波市鄞州区聚贤街道宁波研发园A区2栋12B17
- 📞 收件人：张哲豪 13221941413
- 🚗 宁波市区可上门取件
- 🏪 也可自己送货到公司

**高级能力：** 如果你看到用户提及之前聊过的事情，但当前历史中找不到相关信息，可以使用 `[SEARCH_HISTORY:关键词]` 标记。我会自动去数据库搜索该用户的全部历史记录，然后带着结果重新回答你。例如：用户问"上次那个OMP40的报价出来了吗"，你可以回复 `[SEARCH_HISTORY:OMP40 报价]`。

用中文回复，语气专业但友好。"""


def call_deepseek(messages, timeout=30):
    """调用 DeepSeek API（支持自定义超时，用于轮询）"""
    if not DEEPSEEK_API_KEY:
        return None
    try:
        resp = requests.post(
            f"{DEEPSEEK_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            },
            json={
                "model": DEEPSEEK_MODEL,
                "messages": messages,
                "max_tokens": 1024,
                "temperature": 0.7,
            },
            timeout=timeout
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            logger.error(f"DeepSeek API 错误: {resp.status_code} {resp.text}")
            return None
    except Exception as e:
        logger.error(f"DeepSeek API 调用失败: {e}")
        return None


def process_with_ai(user_message, openid, timeout=30, return_none_on_error=False):
    """用 AI 处理用户消息，返回回复文本（支持自定义超时，用于轮询）
    当 return_none_on_error=True 时，超时/失败返回 None 而非备胎文案"""
    history_count = CHAT_HISTORY_ROUNDS * 2
    history = get_recent_chats(openid, limit=history_count)

    # 滥用检测：检查最近消息是否非工作闲聊
    system_content = SYSTEM_PROMPT
    if ABUSE_TRIGGER_COUNT > 0:
        # 从历史中提取最近用户的消息内容（仅 user 角色）
        user_msgs_in_history = [
            h["content"] for h in history if h["role"] == "user"
        ]
        # 只看最近的消息（最新的在后面，所以取最后 N 条）
        recent_user_msgs = user_msgs_in_history[-ABUSE_TRIGGER_COUNT:]

        if len(recent_user_msgs) >= ABUSE_TRIGGER_COUNT:
            # 让 AI 自己判断是否是非工作闲聊
            abuse_note = (
                f"\n\n**注意：** 该用户最近 {ABUSE_TRIGGER_COUNT} 条消息均未涉及精密测量、"
                f"设备维修、品牌型号、报价等业务相关内容。如果确实如此，请礼貌提醒用户："
                f"你现在在工作时间，只能提供精密检测相关的技术服务，"
                f"不太方便聊与工作无关的话题。"
            )
            system_content = SYSTEM_PROMPT + abuse_note

    # 查询用户角色并注入身份信息
    user_role = get_user_role(openid)
    role_context_parts = [f"当前用户OpenID: {openid}"]
    if user_role:
        type_label = {"admin": '管理员"先生/哲豪"', "engineer": "工程师", "customer": "客户"}.get(user_role, "客户")
        role_context_parts.append(f"用户角色: {type_label}")
        if user_role == "admin":
            role_context_parts.append('这是老板/先生/哲豪，匠测科技创始人。要称呼"先生"或"哲豪"，语气可轻松一些')

    role_context = " | ".join(role_context_parts)

    # 构建消息列表：system prompt + 角色身份 + 历史对话 + 当前消息
    messages = [
        {"role": "system", "content": system_content},
        {"role": "system", "content": role_context},
    ]

    # 注入历史对话（注意：openid对应的role在history里是user/assistant）
    for h in history:
        if h["role"] in ("user", "assistant"):
            messages.append({"role": h["role"], "content": h["content"]})

    messages.append({"role": "user", "content": user_message})

    reply = call_deepseek(messages, timeout=timeout)
    if not reply:
        if return_none_on_error:
            return None
        return ("抱歉，我现在有点忙不过来，请稍后再试。"
                "如需紧急帮助，请联系李工：15757807400")

    # 检查是否为创建工单指令（可能在回复末尾）
    order_marker = "[CREATE_ORDER]"
    if order_marker in reply:
        try:
            # 提取 JSON 部分
            json_part = reply.split(order_marker, 1)[1].strip()
            # 去掉开头结尾可能的引号或标记
            if json_part.startswith("```"):
                json_part = json_part.split("\n", 1)[1] if "\n" in json_part else json_part
                json_part = json_part.rsplit("```", 1)[0].strip()
            order_data = json.loads(json_part)
            order_no = create_repair_order(
                openid=openid,
                customer_name=order_data.get("name", ""),
                customer_phone=order_data.get("phone", ""),
                brand=order_data.get("brand", ""),
                model=order_data.get("model", ""),
                fault_description=order_data.get("fault", ""),
                serial_number=order_data.get("serial", ""),
                company=order_data.get("company", "")
            )
            if order_no:
                return (f"✅ 工单已创建成功！\n\n"
                        f"📋 工单号：{order_no}\n"
                        f"品牌：{order_data.get('brand', '')} {order_data.get('model', '')}\n"
                        f"故障：{order_data.get('fault', '')}\n\n"
                        f"维修工程师将尽快与您联系。\n"
                        f"📞 李工：15757807400\n\n"
                        f"发送「查进度」可查看最新状态")
            else:
                return "抱歉，工单创建失败，请联系李工：15757807400"
        except Exception as e:
            logger.error(f"解析工单数据失败: {e}")
            return "抱歉，处理您的报修信息时出错，请联系李工：15757807400"

    # 检查是否为查进度请求
    phone_marker = "[QUERY_PHONE:"
    order_query_marker = "[QUERY_ORDER:"
    if phone_marker in reply:
        try:
            phone = reply.split(phone_marker, 1)[1].split("]", 1)[0].strip()
            orders = query_orders_by_phone(phone)
            if orders is None:
                return "查询失败，请稍后再试。"
            elif not orders:
                return f"手机号 {phone[:3]}****{phone[-4:]} 暂无报修记录。"
            else:
                lines = [f"📋 共 {len(orders)} 个工单："]
                for o in orders:
                    status = STATUS_TEXT.get(o["status"], o["status"])
                    lines.append(
                        f"\n🔹 {o['order_no']}\n"
                        f"   {o['brand']} {o['model']}\n"
                        f"   状态：{status}\n"
                        f"   时间：{o['created_at']}"
                    )
                return "\n".join(lines)
        except Exception as e:
            logger.error(f"解析查号请求失败: {e}")
            return "查询失败，请稍后再试。"

    if order_query_marker in reply:
        try:
            order_no = reply.split(order_query_marker, 1)[1].split("]", 1)[0].strip()
            conn = get_db()
            if not conn:
                return "查询失败，请稍后再试。"
            cursor = conn.cursor(pymysql.cursors.DictCursor)
            cursor.execute(
                "SELECT order_no, brand, model, fault_description, status, "
                "DATE_FORMAT(created_at, '%%Y-%%m-%%d %%H:%%i') as created_at "
                "FROM repair_orders WHERE order_no = %s",
                (order_no,)
            )
            o = cursor.fetchone()
            cursor.close()
            conn.close()
            if not o:
                return f"工单 {order_no} 未找到，请核对后重试。"
            status = STATUS_TEXT.get(o["status"], o["status"])
            return (f"📋 工单 {o['order_no']}\n"
                    f"设备：{o['brand']} {o['model']}\n"
                    f"故障：{o['fault_description']}\n"
                    f"状态：{status}\n"
                    f"时间：{o['created_at']}")
        except Exception as e:
            logger.error(f"解析查单号请求失败: {e}")
            return "查询失败，请稍后再试。"

    # ---- 翻历史：AI 想不起来时主动去数据库搜索 ----
    search_marker = "[SEARCH_HISTORY:"
    if search_marker in reply:
        try:
            keyword = reply.split(search_marker, 1)[1].split("]", 1)[0].strip()
            logger.info(f"AI 触发翻历史: 关键词={keyword}")
            found = search_chat_history(openid, keyword, limit=15)
            if found:
                context_lines = ["以下是该用户更早的历史对话记录，请参考："]
                for f in found:
                    who = "用户" if f["role"] == "user" else "客服"
                    context_lines.append(f"[{who}] {f['content'][:200]}")
                context_str = "\n".join(context_lines)

                # 重新调用 AI，带上搜索到的历史
                history = get_recent_chats(openid, limit=CHAT_HISTORY_ROUNDS * 2)
                re_messages = [
                    {"role": "system", "content": SYSTEM_PROMPT}
                ]
                for h in history:
                    if h["role"] in ("user", "assistant"):
                        re_messages.append({"role": h["role"], "content": h["content"]})
                # 把搜索结果作为额外上下文注入
                re_messages.append({"role": "system", "content": context_str})
                re_messages.append({"role": "user", "content": user_message})

                re_reply = call_deepseek(re_messages)
                if re_reply:
                    reply = re_reply
                # 如果重新调用失败，去掉标记直接返回原文
                else:
                    reply = reply.replace(f"{search_marker}{keyword}]", "").strip()
            else:
                reply = reply.replace(f"{search_marker}{keyword}]",
                                      f"（关于「{keyword}」，在您的历史记录中没有找到相关信息）")
        except Exception as e:
            logger.error(f"翻历史处理失败: {e}")

    return reply


# ============================================================
# 路由
# ============================================================


@app.route("/wechat", methods=["GET", "POST"])
def wechat():
    # GET：服务器验证
    if request.method == "GET":
        signature = request.args.get("signature", "")
        timestamp = request.args.get("timestamp", "")
        nonce = request.args.get("nonce", "")
        echostr = request.args.get("echostr", "")

        logger.info(f"微信验证请求: signature={signature[:20]}... timestamp={timestamp}")

        if verify_signature(signature, timestamp, nonce):
            return echostr
        else:
            logger.warning("签名验证失败")
            return "invalid signature", 403

    # POST：接收消息
    body = request.data.decode("utf-8")
    logger.info(f"收到微信消息: {body[:200]}")

    try:
        msg = parse_xml(body)
    except Exception as e:
        logger.error(f"XML解析失败: {e}")
        return "success"

    msg_type = msg.get("MsgType", "")
    from_user = msg.get("FromUserName", "")
    to_user = msg.get("ToUserName", "")
    content = msg.get("Content", "")

    # ---- 事件消息 ----
    if msg_type == "event":
        event = msg.get("Event", "")
        if event == "subscribe":
            reply = ("欢迎关注宁波匠测科技有限公司！🎉\n\n"
                     "我是 **Rex（Hermes-Agent）**，您的专属客服助手。\n"
                     "我专精工业精密测量设备领域，随时为您服务。\n\n"
                     "✅ 专业精密测量设备服务商\n"
                     "✅ 雷尼绍 · 马波斯 · 波龙 及其他等全品牌覆盖\n"
                     "✅ 维修、维护、校准、调优、技术咨询、新品销售\n"
                     "✅ 具有提供覆盖全生命周期一站式服务的能力\n"
                     "✅ 测头、对刀仪、激光干涉仪、高精度三维扫描仪器\n\n"
                     "💬 如您有遇到相关故障，可直接发我故障描述，"
                     "我不仅可以为您提供相关的技术支持，"
                     "也可以在您许可下，完成自动报修\n\n"
                     "📱 您可以发送「查进度」查询工单状态\n\n"
                     "📞 如遇紧急情况，可直接联系：\n"
                     "15757807400（李工）\n"
                     "13221941413（张工）\n\n"
                     "━━━━━━━━━━━━━━━━━━━\n"
                     "🔒 **隐私提示**\n"
                     "您与本公众号的对话内容将被记录，"
                     "用于为您提供报修、进度查询等服务。"
                     "我们不会将您的信息用于其他用途。"
                     "如您介意，可随时发送「删除我的记录」要求清除。"
                     "我们目前设置的固定清理周期是1个自然月。")
        elif event == "CLICK":
            event_key = msg.get("EventKey", "")

            COMPANY_INTRO = (
                "宁波匠测科技有限公司\n"
                "━━━━━━━━━━━━━━━━\n\n"
                "专注于工业精密测量领域的技术服务团队。"
                "创始团队均为理工科背景"
                "（机械工艺、工业设计、计算机工程、材料科学等专业），"
                "平均从业年限15年以上。\n\n"
                "业务覆盖各类加工中心的机内在线检测系统集成实施，"
                "以及激光干涉仪、高精度三维扫描等技术服务。"
                "服务品牌涵盖雷尼绍（Renishaw）、马波斯（Marposs）、"
                "波龙（Blum-Novotest）及各类国产品牌。\n\n"
                "▸ 业务类型：维修、维护、校准、调优、技术咨询、新品销售\n"
                "▸ 公司定位：融合AI与数字化技术，"
                "提供覆盖全生命周期的一站式精密测量服务\n\n"
                "我们为您守护生产加工的最后1μm"
            )

            key_map = {
                "repair": (
                    "好的，我来帮您创建维修工单。\n\n"
                    "请告诉我以下信息：\n"
                    "1️⃣ 设备品牌与型号\n"
                    "2️⃣ 具体故障描述\n"
                    "3️⃣ 您的姓名和联系电话\n\n"
                    "一次说不全也没关系，想到什么补充什么。"
                ),
                "tracking": (
                    "好的，我来帮您查询工单进度。\n\n"
                    "请提供您的报修**手机号**或**工单号**，"
                    "我帮您查询最新状态。"
                ),
                "about": COMPANY_INTRO,
                "contact": (
                    "📞 李工：15757807400\n"
                    "🌐 https://jcetech.cn"
                ),
                "support": (
                    "请描述您遇到的技术问题或设备异常，"
                    "我来帮您分析判断。\n\n"
                    "例如：\n"
                    "• 测头信号不稳定\n"
                    "• 对刀仪精度偏差\n"
                    "• 激光干涉仪校准问题\n"
                    "• 其他技术疑问"
                ),
            }
            reply = key_map.get(event_key,
                                "您好，请直接发送您的需求，我来帮您处理。")
        else:
            reply = ""
        xml = build_xml_reply(from_user, to_user, reply) if reply else "success"
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # ---- 白名单检查 ----
    if ALLOWED_USERS and from_user not in ALLOWED_USERS:
        reply = ("您好，匠测科技 AI 客服正在升级中，"
                 "如需报修请联系李工：15757807400")
        save_chat_log(from_user, "user", content)
        save_chat_log(from_user, "assistant", reply)
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # 每日一次自动清理过期聊天记录（支持 keep_history 豁免）
    global _last_cleanup_date
    today = time.strftime("%Y-%m-%d")
    if _last_cleanup_date != today:
        _last_cleanup_date = today
        try:
            cleanup_old_chats(days=CHAT_RETENTION_DAYS)
        except Exception as e:
            logger.error(f"自动清理失败: {e}")

    # 确保用户在 wechat_users 表中有记录
    ensure_user_exists(from_user)

    # 保存用户消息到聊天记录
    save_chat_log(from_user, "user", content)

    # ---- 查进度关键词 ----
    c = content.strip()
    if "查进度" in c or "进度" in c:
        reply = "请发送您的报修手机号，我帮您查询工单进度。"
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # ---- 手机号查进度 ----
    phone_match = re.match(r'^1[3-9]\d{9}$', c)
    if phone_match:
        phone = c
        orders = query_orders_by_phone(phone)
        if orders is None:
            reply = "查询失败，请稍后再试。"
        elif not orders:
            reply = f"手机号 {phone[:3]}****{phone[-4:]} 暂无报修记录。"
        else:
            lines = [f"📋 共 {len(orders)} 个工单："]
            for o in orders:
                status = STATUS_TEXT.get(o["status"], o["status"])
                lines.append(
                    f"\n🔹 {o['order_no']}\n"
                    f"   {o['brand']} {o['model']}\n"
                    f"   状态：{status}\n"
                    f"   时间：{o['created_at']}"
                )
            reply = "\n".join(lines)
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # ---- 指令：删除我的记录 ----
    if c in ("删除我的记录", "删除我的记录。"):
        conn = get_db()
        if conn:
            try:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM ai_chat_logs WHERE openid = %s", (from_user,))
                deleted = cursor.rowcount
                conn.commit()
                cursor.close()
                conn.close()
                reply = f"✅ 已为您删除 {deleted} 条对话记录，感谢您的信任。"
            except Exception as e:
                logger.error(f"删除记录失败: {e}")
                reply = "删除失败，请稍后再试。"
        else:
            reply = "数据库连接失败，请稍后再试。"
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # ---- 指令：我的信息 ----
    if c in ("我的信息", "我的信息。"):
        role = get_user_role(from_user)
        role_name = {"admin": "管理员", "engineer": "工程师", "boss": "老板", "customer": "普通用户"}.get(role, "普通用户")
        if role == "customer":
            reply = (
                f"📱 您的微信信息\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"OpenID：{from_user}\n"
                f"角色：{role_name}\n\n"
                f"如需设置权限，请联系管理员。"
            )
        else:
            reply = (
                f"📱 您的微信信息\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"OpenID：{from_user}\n"
                f"角色：{role_name} ✅\n"
                f"您有管理权限。"
            )
        save_chat_log(from_user, "assistant", reply)
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # ---- 管理员指令 ----
    role = get_user_role(from_user)
    is_admin_boss = role in ("admin", "boss")

    # 设置角色
    set_match = re.match(r'^设置(老板|工程师)\s*(1[3-9]\d{9})$', c)
    if set_match:
        if not is_admin_boss:
            reply = "抱歉，只有管理员和老板才能设置角色。"
        else:
            target_role = set_match.group(1)
            phone = set_match.group(2)
            db_type = "boss" if target_role == "老板" else "engineer"
            conn = get_db()
            if conn:
                try:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE wechat_users SET user_type = %s WHERE phone = %s",
                        (db_type, phone)
                    )
                    affected = cursor.rowcount
                    conn.commit()
                    cursor.close()
                    conn.close()
                    if affected > 0:
                        reply = f"✅ 已将手机号 {phone[:3]}****{phone[-4:]} 设置为【{target_role}】"
                    else:
                        reply = f"❌ 未找到手机号 {phone} 的微信用户，请确保该用户已关注公众号并发送过消息。"
                except Exception as e:
                    logger.error(f"设置角色失败: {e}")
                    reply = "设置失败，请稍后再试。"
            else:
                reply = "数据库连接失败，请稍后再试。"
        save_chat_log(from_user, "assistant", reply)
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # 权限列表
    if c in ("权限列表", "角色列表"):
        if not is_admin_boss:
            reply = "抱歉，只有管理员和老板才能查看。"
        else:
            conn = get_db()
            if conn:
                try:
                    cursor = conn.cursor(pymysql.cursors.DictCursor)
                    cursor.execute(
                        "SELECT openid, phone, user_type, keep_history "
                        "FROM wechat_users WHERE user_type IN ('admin', 'boss', 'engineer') "
                        "ORDER BY FIELD(user_type, 'admin', 'boss', 'engineer')"
                    )
                    users = cursor.fetchall()
                    cursor.close()
                    conn.close()
                    if not users:
                        reply = "当前没有已设置角色的用户。"
                    else:
                        lines = []
                        role_names = {"admin": "管理员", "boss": "老板", "engineer": "工程师"}
                        for u in users:
                            rn = role_names.get(u["user_type"], u["user_type"])
                            phone_disp = f"{u['phone'][:3]}****{u['phone'][-4:]}" if u["phone"] else "未绑定"
                            lines.append(f"▪ {rn} — {phone_disp}")
                        reply = "👥 **已设置的角色列表**\n" + "\n".join(lines)
                except Exception as e:
                    logger.error(f"查询角色列表失败: {e}")
                    reply = "查询失败，请稍后再试。"
            else:
                reply = "数据库连接失败，请稍后再试。"
        save_chat_log(from_user, "assistant", reply)
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # ---- AI 智能回复（快速+后台双轨） ----
    # 先尝试4秒快速调用（微信5秒窗口内）
    fast_reply = process_with_ai(content, from_user, timeout=4)
    if fast_reply:
        # 4秒内出结果了，直接同步返回
        save_chat_log(from_user, "assistant", fast_reply)
        xml = build_xml_reply(from_user, to_user, fast_reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

    # 超时了：先秒回占位消息
    hold_reply = "🔍 正在分析您的问题，请稍候..."
    save_chat_log(from_user, "assistant", hold_reply)
    xml = build_xml_reply(from_user, to_user, hold_reply)

    # 后台单次完整调用DeepSeek（30秒超时），完成后客服消息推送
    def background_ai(openid, msg):
        try:
            reply = process_with_ai(msg, openid, timeout=30)
            if reply:
                push_custom_message(openid, reply)
            else:
                push_custom_message(openid, "抱歉，AI暂时无法回复，请联系李工：15757807400")
        except Exception as e:
            logger.error(f"后台AI回复异常: {e}")

    t = threading.Thread(target=background_ai, args=(from_user, content))
    t.daemon = True
    t.start()

    return make_response(xml, 200,
                         {"Content-Type": "application/xml; charset=utf-8"})
