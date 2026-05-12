#!/usr/bin/env python3
"""
代码发布前预检脚本
纯文件分析 + 语法检查，不依赖云托管Docker内的模块
"""
import sys
import re

errors = []
warnings = []

def check(label, ok, detail=""):
    if ok:
        print(f"  ✅ {label}")
    else:
        errors.append(label)
        print(f"  ❌ {label}  {detail}")

def warn(label, detail=""):
    warnings.append(label)
    print(f"  ⚠️  {label}  {detail}")

def read_f(path):
    with open(path) as f:
        return f.read()

print("\n🔍 发布前预检\n")

# 1. 语法检查
print("── 语法检查 ──")
files = ["wxcloudrun/views.py", "config.py", "run.py", "wxcloudrun/__init__.py"]
for f in files:
    try:
        compile(read_f(f), f, "exec")
        check(f"{f} 语法", True)
    except SyntaxError as e:
        check(f"{f} 语法", False, str(e))

# 2. 潜在风险检查
print("\n── 编码风险 ──")
views = read_f("wxcloudrun/views.py")
if "sys.exit" in views:
    warn("含 sys.exit 调用", "云托管环境可能导致worker退出")
if "os._exit" in views:
    warn("含 os._exit 调用", "可能导致worker强制退出")
if "while True" in views and "time.sleep" not in views:
    warn("无限循环缺少 sleep", "可能导致CPU 100%")

# 3. 配置完整性检查
print("\n── 配置检查 ──")
config = read_f("config.py")
cfg_vars = re.findall(r'^(\w+)\s*=\s*os\.environ\.get', config, re.MULTILINE)
check(f"环境变量配置项数: {len(cfg_vars)}", len(cfg_vars) > 0)
for var in cfg_vars:
    check(f"  {var} 已定义", True)

# 4. 关键函数检查
print("\n── 关键函数检查 ──")
funcs = [
    "build_xml_reply", "get_db", "get_user_role", "ensure_user_exists",
    "get_recent_chats", "save_chat_log", "cleanup_old_chats",
    "search_chat_history", "call_deepseek", "process_with_ai",
    "get_wechat_access_token", "push_custom_message",
    "create_repair_order", "query_orders_by_phone",
]
for fn in funcs:
    if f"def {fn}(" in views:
        check(f"函数 {fn} 已定义", True)
    else:
        check(f"函数 {fn} 已定义", False)

# 5. 路由检查
print("\n── 路由检查 ──")
if re.search(r"@app\.route\(['\"]/wechat['\"]", views):
    check("/wechat 路由（含GET+POST）", True)
else:
    check("/wechat 路由", False)

# 6. 核心功能模式检查
print("\n── 核心功能 ──")
if re.search(r"<ToUserName.*CDATA.*{from_user}", views):
    check("XML 回复模板", True)
else:
    check("XML 回复模板", False)

if "echostr" in views:
    check("微信服务器验证（echostr）", True)
else:
    check("微信服务器验证（echostr）", False)

checks_map = [
    ("客服消息推送接口", "push_custom_message"),
    ("后台单次AI调用(30s)", "background_ai"),
    ("快速调用(4s优先)", "fast_reply = process_with_ai"),
    ("轮询超时空值保护", "return_none_on_error"),
    ("品牌中立定位声明", "第三方技术服务商"),
    ("对比免责声明", "未经验证"),
    ("SQL参数化查询(%s)", "cursor.execute.*%s"),
]
for label, pattern in checks_map:
    if re.search(pattern, views):
        check(label, True)
    else:
        warn(label, "未检测到")

# 7. 文件统计
print(f"\n── 文件统计 ──")
total_lines = len(views.split("\n"))
total_bytes = len(views.encode())
check(f"views.py 行数: {total_lines}", total_lines < 2000)
check(f"views.py 大小: {total_bytes/1024:.0f}KB", total_bytes < 100000)

# 8. 配置读取方式
if "app.config.get" in views:
    check("使用 Flask app.config 读取配置", True)
else:
    warn("未使用 app.config.get 读取配置")

# 汇总
print(f"\n{'='*40}")
if errors:
    print(f"❌ 失败: {len(errors)} 个关键错误, {len(warnings)} 个警告")
    for e in errors:
        print(f"   - {e}")
    sys.exit(1)
elif warnings:
    print(f"⚠️  通过（有 {len(warnings)} 个警告，已记录）")
    sys.exit(0)
else:
    print("✅ 全部通过，可以发布！")
    sys.exit(0)
