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

import os
import hashlib
import xml.etree.ElementTree as ET
import time
import json
import logging
import re
import pymysql

import requests
from flask import Flask, request, make_response

# ============================================================
# 配置
# ============================================================

WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "jcetech2026")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# MySQL 连接（腾讯云服务器公网端口）
DB_HOST = os.getenv("DB_HOST", "101.43.85.171")
DB_PORT = int(os.getenv("DB_PORT", "3307"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "Jc3T3ch@2026!Root")
DB_NAME = os.getenv("DB_NAME", "jcetech")

# 白名单用户 OpenID（为空则全放开）
ALLOWED_USERS = os.getenv("ALLOWED_USERS", "").split(",") if os.getenv("ALLOWED_USERS") else []

# ============================================================
# 日志
# ============================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# Flask App
# ============================================================

app = Flask(__name__)


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


def get_wechat_user(openid):
    """根据OpenID查询微信用户信息"""
    conn = get_db()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT openid, phone, user_type, nickname, company_name, keep_history "
                "FROM wechat_users WHERE openid = %s", (openid,)
            )
            row = cur.fetchone()
            return row
    except Exception as e:
        logger.error(f"查询用户失败: {e}")
        return None
    finally:
        conn.close()


def query_wechat_user_by_phone(phone):
    """根据手机号查询微信用户"""
    conn = get_db()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT openid, phone, user_type, nickname, company_name "
                "FROM wechat_users WHERE phone = %s LIMIT 1", (phone,)
            )
            row = cur.fetchone()
            return row
    except Exception as e:
        logger.error(f"按手机号查询用户失败: {e}")
        return None
    finally:
        conn.close()


def upsert_wechat_user(openid, phone=None, user_type=None, nickname=None, company_name=None):
    """创建或更新微信用户信息"""
    conn = get_db()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            # 先查是否存在
            cur.execute("SELECT id FROM wechat_users WHERE openid = %s", (openid,))
            exists = cur.fetchone()
            if exists:
                updates = []
                params = []
                if phone is not None:
                    updates.append("phone = %s")
                    params.append(phone)
                if user_type is not None:
                    updates.append("user_type = %s")
                    params.append(user_type)
                if nickname is not None:
                    updates.append("nickname = %s")
                    params.append(nickname)
                if company_name is not None:
                    updates.append("company_name = %s")
                    params.append(company_name)
                if updates:
                    params.append(openid)
                    cur.execute(f"UPDATE wechat_users SET {', '.join(updates)} WHERE openid = %s", params)
            else:
                cur.execute(
                    "INSERT INTO wechat_users (openid, phone, user_type, nickname, company_name) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (openid, phone or "", user_type or "customer", nickname or "", company_name or "")
                )
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"更新用户失败: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


ADMIN_OPENID = "omSRr243pGE1j-gAzWOrWhBDQoGg"


def handle_set_role_command(openid, content):
    """处理管理员设置角色/电话指令"""
    if openid != ADMIN_OPENID:
        return None  # 非管理员不处理
    # 格式: 设置角色 [openid或手机号] admin/engineer/customer
    match = re.match(r'设置角色\s+(\S+)\s+(admin|engineer|customer)', content)
    if match:
        target = match.group(1)
        role = match.group(2)
        # 判断目标是openid还是手机号
        if re.match(r'^1[3-9]\d{9}$', target):
            user = query_wechat_user_by_phone(target)
            if user:
                upsert_wechat_user(user[0], user_type=role)
                return f"已将手机号 {target[:3]}****{target[-4:]} 的角色设为 [{role}]"
            else:
                return f"未找到手机号 {target} 对应的微信用户"
        else:
            upsert_wechat_user(target, user_type=role)
            return f"已将OpenID {target[:10]}... 的角色设为 [{role}]"
    # 格式: 绑定电话 [手机号]
    match = re.match(r'绑定电话\s+(1[3-9]\d{9})', content)
    if match:
        phone = match.group(1)
        upsert_wechat_user(openid, phone=phone)
        return f"已绑定手机号 {phone[:3]}****{phone[-4:]}"
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
            order_no, openid,
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
你专业、热情、回答简洁。经先生确认过的事情要及时执行不拖延。

**你的身份：**
- 代表宁波匠测科技有限公司
- 专注精密测量设备维修服务
- 服务品牌：雷尼绍(Renishaw)、马波斯(Marposs)、波龙(Blum-Novotest)、发格(Fagor)、海德汉(Heidenhain)等
- 精通Blum核心技术：光电式测量机构（零磨损）、shark360模块化测头、DIGILOG模拟信号分析、TMAC刀具自适应监控

**核心能力：**
1. 回答客户关于维修服务、价格、品牌的咨询
2. 识别客户的报修意图，收集必要信息
3. 帮助客户查询工单进度

**产品知识速查（常见型号）：**
- **OMP40-2**：小型光学测头，¥800-1500维修，重复性1.00μm，电池2×½AA锂亚硫酰氯，待机1500天
- **OMP60**：标准光学测头（大机床版），¥800-1500维修，重复性1.00μm，电池2×AA，待机1767天
- **OMP400**：RENGAGE™应变片高精度测头，¥1200-2000维修，重复性0.25μm，极低触发力XY仅0.06N
- **OMP600**：RENGAGE™应变片高精度测头（非机械触发），¥1500-2500维修，重复性0.25μm
- **RMP40/RMP60**：无线电测头，适合大型机床/五轴，¥1000-2000维修，范围15m
- **NC4**：激光对刀仪，¥1500-3000维修，非接触式
- **OTS/RTS**：对刀仪，光学/无线电传输，¥1000-2000维修
- **TRS2**：刀具破损检测，¥1200-2500维修
- **Blum ZX-Speed**：3D刀具测头，重复性0.4μm（极高），最小刀具Ø1mm，光电式零磨损
- **Blum TC50/TC52**：高速工件测头，光电式全方向，超长电池寿命
- **Blum TC53/TC63**：模块化工件测头，shark360专利机构，可加延长段/侧向分支
- **Blum TC63/TC64 DIGILOG**：DIGILOG触测测头，可测表面粗糙度/扫描轮廓
- **Blum LC50-DIGILOG**：激光测量系统，模拟信号分析，时间缩短60%
- **Blum TMAC**：刀具自适应监控（主轴功率分析），节省20-60%循环时间
- **Marposs VOP40**：光学测头，重复性1μm，相当于雷尼绍OMP40-2
- **Marposs WRP60P**：无线电测头，模块化可加长杆，压电传感器
- **Marposs VTS**：影像式对刀仪，非接触CCD，最小可测10μm
- **Marposs WRG**：无线孔径规，可测Ø41-105mm，重复性≤1.0μm
- **Marposs ARTIS**：刀具监控系统（主轴功率分析）

**故障排查常识：**
1. 测头不触发→先查电池（½AA 3.6V锂亚硫酰氯），再查光路是否清洁
2. 红灯常亮→OMP40-2电池电量低，换电池即可
3. 精度偏差→测针是否弯曲/磨损？是否需重新标定？
4. 电池消耗过快→检查开启/关闭方式设置
5. 无线电信号不稳定→检查是否被遮挡，跳频系统会自动规避干扰

**报修需要收集的信息：**
- 客户姓名
- 联系电话
- 设备品牌（必填）
- 设备型号（必填）
- 故障描述（必填）
- 设备编号（可选）

**信息收集规则：**
- 如果客户一次性提供了所有信息，直接汇总确认
- 如果缺少信息，一次只追问一项，不要一次性问太多
- 确认完毕后回复格式：[CREATE_ORDER] 开头，后面跟JSON格式的工单数据

**价格说明：** Rex不负责报价。客户问价格时，统一回复"需要收到设备检测后才能确定报价"。
- 检测费用由工程师根据具体故障情况评估

**联系方式：**
- 李工：15757807400（24小时）
- 地址：宁波研发园A区

用中文回复，语气专业但友好。"""


def call_deepseek(messages):
    """调用 DeepSeek API"""
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
    # 先检查管理员指令
    admin_reply = handle_set_role_command(openid, user_message)
    if admin_reply:
        return admin_reply

    # 查询用户身份信息
    user_info = get_wechat_user(openid)
    user_desc_parts = [f"当前用户OpenID: {openid}"]
    if user_info:
        phone = user_info[2]  # phone
        user_type = user_info[3] or "customer"   # user_type
        nickname = user_info[4] or ""     # nickname
        company = user_info[5] or ""      # company_name
        type_label = {"admin": "管理员(老板/先生)", "engineer": "工程师", "customer": "客户"}.get(user_type, "客户")
        user_desc_parts.append(f"用户角色: {type_label}")
        if phone:
            user_desc_parts.append(f"绑定手机: {phone[:3]}****{phone[-4:]}")
        if nickname:
            user_desc_parts.append(f"昵称: {nickname}")
        if company:
            user_desc_parts.append(f"公司: {company}")
        if user_type == "admin":
            user_desc_parts.append('注意：这是管理员，要称呼"先生"或"哲豪"，语气可轻松一些')

    identity_context = " | ".join(user_desc_parts)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": identity_context},
        {"role": "user", "content": user_message}
    ]

    reply = call_deepseek(messages)
    if not reply:
        return "抱歉，我现在有点忙不过来，请稍后再试。如需紧急帮助，请联系李工：15757807400"

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
                return (
                    f"✅ 工单已创建成功！\n\n"
                    f"📋 工单号：{order_no}\n"
                    f"品牌：{order_data.get('brand', '')} {order_data.get('model', '')}\n"
                    f"故障：{order_data.get('fault', '')}\n\n"
                    f"维修工程师将尽快与您联系。\n"
                    f"📞 李工：15757807400\n\n"
                    f"发送「查进度」可查看最新状态"
                )
            else:
                return "抱歉，工单创建失败，请联系李工：15757807400"
        except Exception as e:
            logger.error(f"解析工单数据失败: {e}")
            return "抱歉，处理您的报修信息时出错，请联系李工：15757807400"

    return reply


# ============================================================
# 非 AI 回复（关键词快捷匹配）
# ============================================================

def quick_reply(content):
    """关键词快捷回复"""
    c = content.strip()
    if "查进度" in c or "进度" in c:
        return "PROMPT_PHONE"  # 让用户输手机号
    return None


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
            reply = (
                "欢迎关注宁波匠测科技有限公司！🎉\n\n"
                "✅ 专业精密测量设备维修服务\n"
                "✅ 雷尼绍 · 马波斯 · 波龙 全品牌覆盖\n\n"
                "💬 直接发送故障描述即可在线报修\n"
                "📱 发送「查进度」查询工单状态\n"
                "📞 紧急联系：李工 15757807400"
            )
        elif event == "CLICK":
            event_key = msg.get("EventKey", "")
            key_map = {
                "repair": "请直接发送您的设备品牌、型号和故障描述，我帮您创建维修工单。",
                "tracking": "请发送您的报修手机号，我帮您查询工单进度。",
                "contact": "📞 李工：15757807400（24小时）\n📍 宁波研发园A区",
                "price": "📋 维修价格参考：\n测头维修：¥800-3000\n对刀仪维修：¥1200-3500\n激光干涉仪校准：¥2000-5000\n具体需根据故障情况报价",
            }
            reply = key_map.get(event_key, "感谢您的关注，请发送故障描述在线报修。")
        else:
            reply = ""
        xml = build_xml_reply(from_user, to_user, reply) if reply else "success"
        return make_response(xml, 200, {"Content-Type": "application/xml; charset=utf-8"})

    # ---- 白名单检查 ----
    if ALLOWED_USERS and from_user not in ALLOWED_USERS:
        reply = "您好，匠测科技 AI 客服正在升级中，如需报修请联系李工：15757807400"
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200, {"Content-Type": "application/xml; charset=utf-8"})

    # ---- 快捷匹配 ----
    quick = quick_reply(content)
    if quick == "PROMPT_PHONE":
        reply = "请发送您的报修手机号，我帮您查询工单进度。"
        xml = build_xml_reply(from_user, to_user, reply)
        return make_response(xml, 200, {"Content-Type": "application/xml; charset=utf-8"})

    # ---- 手机号查进度 ----
    phone_match = re.match(r'^1[3-9]\d{9}$', content.strip())
    if phone_match:
        phone = content.strip()
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
        return make_response(xml, 200, {"Content-Type": "application/xml; charset=utf-8"})

    # ---- AI 智能回复 ----
    reply = process_with_ai(content, from_user)
    xml = build_xml_reply(from_user, to_user, reply)
    return make_response(xml, 200, {"Content-Type": "application/xml; charset=utf-8"})


# ============================================================
# 健康检查
# ============================================================

@app.route("/")
def index():
    return {"status": "ok", "service": "jcetech-wechat-ai"}


@app.route("/health")
def health():
    return {"status": "ok"}


# ============================================================
# 启动
# ============================================================

if __name__ == "__main__":
    port = int(os.getenv("PORT", 80))
    app.run(host="0.0.0.0", port=port)
