#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
兴勤超市港惠店订单预估 - 每日推送脚本 (GitHub Pages + 钉钉链接)
流程: 更新HTML数据 → 更新天气 → git push到GitHub Pages → 钉钉推送链接
注意: 本脚本与成山农场 dingtalk_push.py 完全独立，互不影响。
"""
import re
import json
import time
import hmac
import hashlib
import base64
import urllib.parse
import urllib.request
import subprocess
import sys
import os
import shutil
from datetime import datetime, timedelta

# ==================== 配置 ====================
# 钉钉 Webhook (兴勤超市群)
WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send"
ACCESS_TOKEN = "9543110798e243c59078fc69507e4ec20b4e1e7158b8be3238b061bc4f2ef8bb"
SECRET = "SEC621256ec403f62bdb330ced57067088ed984865abb854a614cd1b471102362f8"

# 文件路径
HTML_PATH = r"D:\TBSG\Desktop\order-forecast-scheduling-港惠店.html"
REPO_DIR = r"C:\Users\TBSG\.qoderwork\workspace\store-scheduling-repo"
PAGES_FILENAME = "兴勤超市港惠店订单预估.html"
PAGES_URL = f"https://yin-min123.github.io/store-scheduling/{urllib.parse.quote(PAGES_FILENAME)}"

# Open-Meteo (惠州)
WEATHER_LAT = 23.11
WEATHER_LON = 114.42

STORE_NAME = '港惠店'
BRAND_NAME = '兴勤超市'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = SCRIPT_DIR
ACCUMULATED_DATA_PATH = os.path.join(SCRIPT_DIR, 'daily_actuals_xinqin.json')

DRY_RUN = False


# ==================== 天气获取 ====================
def fetch_weather():
    """从 Open-Meteo 获取惠州7天天气预报"""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        f"&daily=weathercode,temperature_2m_max,temperature_2m_min,"
        f"precipitation_probability_max,precipitation_sum"
        f"&forecast_days=7&timezone=Asia/Shanghai"
    )
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))

        daily = data.get('daily', {})
        dates = daily.get('time', [])
        codes = daily.get('weathercode', [])
        t_max = daily.get('temperature_2m_max', [])
        t_min = daily.get('temperature_2m_min', [])
        precip_sum = daily.get('precipitation_sum', [])

        wmo_desc = {
            0: '晴', 1: '晴', 2: '多云', 3: '阴',
            45: '雾', 48: '雾凇',
            51: '小雨', 53: '小雨', 55: '小雨',
            61: '小雨', 63: '中雨', 65: '大雨',
            71: '小雪', 73: '中雪', 75: '大雪',
            80: '小阵雨', 81: '阵雨', 82: '强阵雨',
            95: '雷暴', 96: '雷暴冰雹', 99: '强雷暴'
        }

        entries = []
        for i, d in enumerate(dates):
            code = codes[i] if i < len(codes) else 0
            desc = wmo_desc.get(code, f'code{code}')
            hi = round(t_max[i]) if i < len(t_max) else '-'
            lo = round(t_min[i]) if i < len(t_min) else '-'
            mm = precip_sum[i] if i < len(precip_sum) else 0
            if mm and mm > 0:
                desc += f' {mm}mm'
            entries.append({'date': d, 'desc': desc, 'temp': f'{lo}~{hi}°C'})
        return entries
    except Exception as e:
        print(f"[WARN] Open-Meteo天气获取失败: {e}")
        return []


# ==================== HTML更新 ====================
def update_html_data(content, target_date_str, orders, warehouse_t):
    """更新 storeDailyData / warehouseTData"""
    is_today = (target_date_str == datetime.now().strftime('%Y-%m-%d'))
    if is_today:
        return content

    # storeDailyData
    sdm = re.search(
        rf"let\s+storeDailyData\s*=\s*\{{.*?'{STORE_NAME}'\s*:\s*\{{[^}}]*\}}",
        content, re.DOTALL
    )
    if sdm:
        old = sdm.group(0)
        date_pat = rf"'{re.escape(target_date_str)}'\s*:\s*\d+"
        if re.search(date_pat, old):
            new_block = re.sub(date_pat, f"'{target_date_str}': {orders}", old)
        else:
            new_block, n = re.subn(r",\s*\}\s*$", f", '{target_date_str}': {orders}}}", old)
            if n == 0:
                new_block = re.sub(r"\}\s*$", f", '{target_date_str}': {orders}}}", old)
        content = content.replace(old, new_block)

    # warehouseTData
    if warehouse_t > 0:
        wtm = re.search(
            rf"let\s+warehouseTData\s*=\s*\{{.*?'{STORE_NAME}'\s*:\s*\{{[^}}]*\}}",
            content, re.DOTALL
        )
        if wtm:
            old = wtm.group(0)
            wt_pat = rf"'{re.escape(target_date_str)}'\s*:\s*[\d.]+"
            if re.search(wt_pat, old):
                new_block = re.sub(wt_pat, f"'{target_date_str}': {warehouse_t}", old)
            else:
                new_block, n = re.subn(r",\s*\}\s*$", f", '{target_date_str}': {warehouse_t}}}", old)
                if n == 0:
                    new_block = re.sub(r"\}\s*$", f", '{target_date_str}': {warehouse_t}}}", old)
            content = content.replace(old, new_block)

    return content


def update_html_weather(content, weather_entries):
    """更新HTML中的 staticWeatherData"""
    if not weather_entries:
        return content
    lines = []
    for w in weather_entries:
        lines.append(f"        {{date:'{w['date']}',desc:'{w['desc']}',temp:'{w['temp']}'}}")
    js_block = (
        f"// Open-Meteo 天气预报 (惠州 {WEATHER_LAT},{WEATHER_LON}) "
        f"更新于 {datetime.now().strftime('%Y-%m-%d')}\n"
        f"    const staticWeatherData = {{\n"
        f"      '{BRAND_NAME}港惠店': [\n"
        f"{',\n'.join(lines)}\n"
        f"      ]\n"
        f"    }};"
    )
    if 'const staticWeatherData' in content:
        content = re.sub(
            r'// Open-Meteo 天气预报.*?const staticWeatherData\s*=\s*\{.*?\};',
            js_block,
            content,
            flags=re.DOTALL
        )
    return content


# ==================== 模型预估更新 ====================
def _build_js_dict(obj):
    if not obj:
        return '{}'
    items = ',\n  '.join(f"'{k}': {int(v)}" for k, v in sorted(obj.items()))
    return '{\n  ' + items + '\n}'


def _replace_js_object(content, var_name, new_body):
    """用花括号计数法定位并整体替换 const/let var_name = {...};"""
    m = re.search(r'(?:const|let)\s+' + re.escape(var_name) + r'\s*=\s*\{', content)
    if not m:
        return content, False
    start = m.start()
    brace = 0
    end = -1
    for i in range(m.end() - 1, len(content)):
        if content[i] == '{':
            brace += 1
        elif content[i] == '}':
            brace -= 1
            if brace == 0:
                end = i + 1
                if end < len(content) and content[end] == ';':
                    end += 1
                break
    if end < 0:
        return content, False
    keyword = 'const' if content[start] == 'c' else 'let'
    replacement = f"{keyword} {var_name} = {new_body};"
    return content[:start] + replacement + content[end:], True


def _read_js_int_map(content, var_name):
    m = re.search(r'(?:const|let)\s+' + re.escape(var_name) + r'\s*=\s*\{', content)
    if not m:
        return {}
    brace = 0
    end = -1
    for i in range(m.end() - 1, len(content)):
        if content[i] == '{':
            brace += 1
        elif content[i] == '}':
            brace -= 1
            if brace == 0:
                end = i + 1
                break
    if end < 0:
        return {}
    body = content[m.end():end - 1]
    entries = re.findall(r"'(\d{4}-\d{2}-\d{2})'\s*:\s*(\d+)", body)
    return {d: int(v) for d, v in entries}


def update_model_forecast(content, horizon=16):
    """冻结过期预估进历史 + 调用轻量引擎刷新未来窗口 + 陈旧校验（对齐成山修复）。"""
    today = datetime.now().strftime('%Y-%m-%d')

    # 1. 读取现有预估块
    current_mf = _read_js_int_map(content, 'modelForecastData')
    history = _read_js_int_map(content, 'modelForecastHistory')

    # 2. 冻结所有 <= 今天且近60天内的过期/当日预估进历史，
    #    防止日期滚出预估窗口后被 generateDailyForecasts 静默回退成上周同期实际。
    freeze_cutoff = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=60)).strftime('%Y-%m-%d')
    newly_frozen = [d for d in current_mf if freeze_cutoff < d <= today and d not in history]
    for d in sorted(newly_frozen):
        history[d] = current_mf[d]
    if newly_frozen:
        print(f"[INFO] 冻结{len(newly_frozen)}个已过期/当日预估进历史: {min(newly_frozen)}~{max(newly_frozen)}")

    # 3. 运行轻量引擎（回测历史 + 未来窗口），写出 forecast_result_xinqin.json
    model_script = os.path.join(MODEL_DIR, 'daily_forecast_lite_xinqin.py')
    result_file = os.path.join(MODEL_DIR, 'forecast_result_xinqin.json')
    if not os.path.exists(model_script):
        print(f"[WARN] 预估引擎不存在: {model_script}，仅冻结历史、不改未来")
        return content
    model_env = dict(os.environ)
    model_env['PYTHONIOENCODING'] = 'utf-8'
    model_env['FC_HORIZON'] = str(horizon)
    try:
        r = subprocess.run([sys.executable, '-X', 'utf8', model_script],
                           capture_output=True, text=True, timeout=180,
                           cwd=MODEL_DIR, env=model_env)
        if r.returncode != 0:
            print(f"[WARN] 预估引擎异常:\n{r.stderr[-500:]}")
            return content
    except Exception as e:
        print(f"[WARN] 预估引擎运行失败: {e}")
        return content

    if not os.path.exists(result_file):
        print("[WARN] forecast_result_xinqin.json 不存在")
        return content
    with open(result_file, 'r', encoding='utf-8') as f:
        res = json.load(f)

    # 4. 陈旧校验：数据不足3周或预估窗口整体已过期 → 拒绝覆盖
    meta = res.get('meta', {}) or {}
    dr = str(meta.get('data_range', ''))
    dr_dates = re.findall(r'\d{4}-\d{2}-\d{2}', dr)
    span_days = None
    if len(dr_dates) >= 2:
        try:
            span_days = (datetime.strptime(dr_dates[-1], '%Y-%m-%d')
                         - datetime.strptime(dr_dates[0], '%Y-%m-%d')).days + 1
        except Exception:
            span_days = None
    actual_days = meta.get('actual_days')
    if (actual_days is not None and actual_days < 21) or (span_days is not None and span_days < 21):
        print(f"[WARN] 引擎输入数据不足3周(actual_days={actual_days}, 跨度={span_days}天)，拒绝覆盖现有预估。")
        return content
    future_list = res.get('future', [])
    fc_dates = [x.get('date') for x in future_list if x.get('date')]
    if fc_dates and max(fc_dates) <= today:
        print(f"[WARN] 预估窗口({fc_dates[0]}~{max(fc_dates)})整体已过期，疑似陈旧运行，拒绝覆盖。")
        return content

    # 5. 构建新 modelForecastData：今天若在历史则锁定，未来天用模型值
    new_mf = {}
    for x in future_list:
        d = x['date']
        v = int(round(x.get('forecast', 0)))
        if d == today and d in history:
            new_mf[d] = history[d]
        elif d > today:
            new_mf[d] = v
        elif d not in history:
            new_mf[d] = v
    if not new_mf:
        print("[WARN] 引擎无有效未来预估输出")
        return content

    # 6. 写回两个块
    content, _ = _replace_js_object(content, 'modelForecastData', _build_js_dict(new_mf))
    content, _ = _replace_js_object(content, 'modelForecastHistory', _build_js_dict(history))

    locked = len(history)
    future_count = sum(1 for d in new_mf if d > today)
    print(f"[OK] 系统预估更新: 冻结{locked}天历史, {future_count}天未来预估 "
          f"({min(new_mf.values())}~{max(new_mf.values())}单)")
    return content


# ==================== GitHub Pages ====================
def push_to_github_pages(target_date_str):
    """复制HTML到repo，git push"""
    repo_html_path = os.path.join(REPO_DIR, PAGES_FILENAME)
    shutil.copy2(HTML_PATH, repo_html_path)
    print(f"[INFO] 已复制HTML到repo: {repo_html_path}")

    os.chdir(REPO_DIR)
    subprocess.run(['git', 'add', PAGES_FILENAME], check=True)
    commit_msg = f"Update xinqin store forecast ({target_date_str})"
    result = subprocess.run(
        ['git', 'commit', '-m', commit_msg],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).lower()
        if 'nothing to commit' in combined:
            print("[INFO] HTML无变化，跳过commit")
            return True
        print(f"[ERROR] git commit失败: {result.stderr}")
        return False

    result = subprocess.run(['git', 'push'], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"[ERROR] git push失败: {result.stderr}")
        return False
    print(f"[OK] GitHub Pages 推送成功")
    return True


# ==================== 钉钉推送 ====================
def format_message(target_date_str, orders, warehouse_t, weather_entries):
    """格式化钉钉消息"""
    dt = datetime.strptime(target_date_str, '%Y-%m-%d')
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    weekday = weekday_names[dt.weekday()]

    lines = []
    lines.append(f"## 兴勤超市·港惠店 排班工具 {target_date_str} {weekday}")
    lines.append("")
    lines.append(f"**当日订单**: {orders}单 / 仓T {warehouse_t:.1f}min{' ⚠️' if warehouse_t > 15 else ''}")
    lines.append("")
    if weather_entries:
        lines.append("**惠州天气预报**:")
        for w in weather_entries[:4]:
            d = datetime.strptime(w['date'], '%Y-%m-%d')
            wd = weekday_names[d.weekday()]
            lines.append(f"- {w['date'][5:]} {wd}: {w['desc']} {w['temp']}")
        lines.append("")
    lines.append(f"[点击打开排班工具]({PAGES_URL})")
    lines.append("")
    lines.append("> 手机可直接打开，选择日期自动切换工作日/周末模式")
    return "\n".join(lines)


def sign_and_send(message):
    """签名并发送钉钉消息"""
    timestamp = str(round(time.time() * 1000))
    string_to_sign = f'{timestamp}\n{SECRET}'
    hmac_code = hmac.new(
        SECRET.encode('utf-8'),
        string_to_sign.encode('utf-8'),
        digestmod=hashlib.sha256
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
    url = f"{WEBHOOK_URL}?access_token={ACCESS_TOKEN}&timestamp={timestamp}&sign={sign}"
    payload = {
        "msgtype": "markdown",
        "markdown": {"title": "兴勤超市港惠店排班", "text": message}
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {"errcode": -1, "errmsg": str(e)}


# ==================== 主流程 ====================
def main():
    if any(a in ('--refresh-weekly', '--weekly') for a in sys.argv):
        global DRY_RUN
        if '--dry-run' in sys.argv:
            DRY_RUN = True
        run_weekly_refresh()
        return

    if len(sys.argv) >= 3:
        target_date = sys.argv[1]
        orders = int(sys.argv[2])
        warehouse_t = float(sys.argv[3]) if len(sys.argv) >= 4 else 5.0
    else:
        print("用法: python dingtalk_push_xinqin.py <日期> <门店订单数> [仓T]")
        print("示例: python dingtalk_push_xinqin.py 2026-10-19 490 5.2")
        sys.exit(1)

    print(f"[INFO] 目标: {target_date} | 订单: {orders} | 仓T: {warehouse_t}")

    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        content = f.read()

    # 更新数据
    content = update_html_data(content, target_date, orders, warehouse_t)
    weather = fetch_weather()
    if weather:
        content = update_html_weather(content, weather)

    # 更新预估
    content = update_model_forecast(content)

    # 写回
    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(content)

    # Push Pages
    if not DRY_RUN:
        push_to_github_pages(target_date)

    # 钉钉
    message = format_message(target_date, orders, warehouse_t, weather)
    if DRY_RUN:
        print("[DRY-RUN] 推送内容:\n" + message)
    else:
        result = sign_and_send(message)
        print(f"[INFO] 钉钉: {result}")


def run_weekly_refresh():
    """每周刷新预估"""
    today = datetime.now().strftime('%Y-%m-%d')
    print(f"=== 兴勤超市港惠店 周刷新 {today} ===")

    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        content = f.read()

    # 刷新天气
    weather = fetch_weather()
    if weather:
        content = update_html_weather(content, weather)

    # 刷新预估
    content = update_model_forecast(content)

    # 写回
    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(content)

    if not DRY_RUN:
        push_to_github_pages(today)
        # 推送周预估摘要
        weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        lines = []
        lines.append(f"## 兴勤超市·港惠店 系统预估周刷新 {today}")
        lines.append("")
        lines.append(f"[点击打开排班工具]({PAGES_URL})")
        lines.append("")
        lines.append("> 每周五自动刷新未来16天预估")
        message = "\n".join(lines)
        result = sign_and_send(message)
        print(f"[OK] 周刷新完成 | 钉钉: {result}")


if __name__ == '__main__':
    main()
