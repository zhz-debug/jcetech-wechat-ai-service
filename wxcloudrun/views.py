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

# MySQL 连接（腾讯云服务器公网端口）
DB_HOST = app.config.get("DB_HOST") or "101.43.85.171"
DB_PORT = int(app.config.get("DB_PORT") or 3307)
DB_USER = app.config.get("DB_USER") or "root"
DB_PASSWORD = app.config.get("DB_PASSWORD") or "Jc3T3ch@2026!Root"
DB_NAME = app.config.get("DB_NAME") or "jcetech"

# 白名单用户 OpenID（逗号分隔，为空则全放开）
ALLOWED_USERS_RAW = app.config.get("ALLOWED_USERS") or ""
ALLOWED_USERS = [u.strip() for u in ALLOWED_USERS_RAW.split(",") if u.strip()]

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

SYSTEM_PROMPT = """你是匠测科技（JCETech）的 AI 客服助手，名叫 Rex。
你专业、热情、回答简洁。

**你的身份：**
- 代表宁波匠测科技有限公司
- 专注精密测量设备维修服务
- 服务品牌：雷尼绍(Renishaw)、马波斯(Marposs)、波龙(Blum)及国产品牌

**你的能力（根据用户意图自动识别）：**

1️⃣ **报修** — 用户说设备坏了、测头不准、要维修
   · 收集：姓名、电话、品牌、型号、故障描述
   · 一次只追问一项，不要全部列出来
   · 信息齐全后确认汇总，回复 [CREATE_ORDER] 加JSON

2️⃣ **查进度** — 用户说查进度、工单查询、报修状态
   · 如果用户发的是手机号(11位数字) → 帮查工单
   · 如果用户发的是工单号(RP开头) → 帮查具体工单
   · 如果是手机号，回复格式：请使用 [QUERY_PHONE:手机号]
   · 如果是工单号，回复格式：请使用 [QUERY_ORDER:工单号]

3️⃣ **技术咨询** — 用户问技术问题、故障分析
   · 用专业知识回答判断
   · 如果无法判断或需要上门，建议联系李工
   · 不动数据库，不创建工单

**价格参考（客户询价时回答）：**
- 测头维修：¥800-3000
- 对刀仪维修：¥1200-3500
- 激光干涉仪校准：¥2000-5000
- 具体需根据故障情况报价

**联系方式：**
- 李工：15757807400
- 官网：https://jcetech.cn

用中文回复，语气专业但友好。"""


def call_deepseek(messages):
    """调用 DeepSeek API"""
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
            timeout=30
        )
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            logger.error(f"DeepSeek API 错误: {resp.status_code} {resp.text}")
            return None
    except Exception as e:
        logger.error(f"DeepSeek API 调用失败: {e}")
        return None


def process_with_ai(user_message, openid):
    """用 AI 处理用户消息，返回回复文本"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_message}
    ]

    reply = call_deepseek(messages)
    if not reply:
        return ("抱歉，我现在有点忙不过来，请稍后再试。"
                "如需紧急帮助，请联系李工：15757807400")

    # 检查是否为创建工单指令
    if reply.startswith("[CREATE_ORDER]"):
        try:
            order_data = json.loads(reply[len("[CREATE_ORDER]"):].strip())
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
                     "✅ 专业精密测量设备维修服务\n"
                     "✅ 雷尼绍 · 马波斯 · 波龙 全品牌覆盖\n\n"
                     "💬 直接发送故障描述即可在线报修\n"
                     "📱 发送「查进度」查询工单状态\n"
                     "📞 李工：15757807400")
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
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200,
                             {"Content-Type": "application/xml; charset=utf-8"})

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

    # ---- AI 智能回复 ----
    reply = process_with_ai(content, from_user)
    xml = build_xml_reply(from_user, to_user, reply)
    return make_response(xml, 200,
                         {"Content-Type": "application/xml; charset=utf-8"})
