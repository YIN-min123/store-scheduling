#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
成山农场订单预估 - 每日推送脚本 (GitHub Pages + 钉钉链接)
流程: 更新HTML数据 → 更新天气 → git push到GitHub Pages → 钉钉推送链接
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
import glob
from datetime import datetime, timedelta

# ==================== 配置 ====================
# 钉钉 Webhook (成山农场群)
WEBHOOK_URL = "https://oapi.dingtalk.com/robot/send"
ACCESS_TOKEN = "5f4be4907a3035777cb4a548e30f96ae2149748777286b7b0dafe478683b1a66"
SECRET = "SECccc7a40fcd824188559c9469192249095114182d64634b251859243a9e6a8999"

# 文件路径
HTML_PATH = r"D:\TBSG\Desktop\order-forecast-scheduling.html"
REPO_DIR = r"C:\Users\TBSG\.qoderwork\workspace\store-scheduling-repo"
PAGES_FILENAME = "成山农场龙湖天街店订单预估.html"
PAGES_URL = f"https://yin-min123.github.io/store-scheduling/{urllib.parse.quote(PAGES_FILENAME)}"
ONEDAY_URL = "https://1d-static.alibaba-inc.com/oneday/source/37359bce-e9a7-4fcf-8686-7b2f7317bb6e.html"

# Open-Meteo (西安)
WEATHER_LAT = 34.26
WEATHER_LON = 108.94

STORES = ['龙湖天街店']
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = r"C:\Users\TBSG\.qoderwork\workspace\mu3wgi8swufexcje"


# ==================== 天气获取 ====================
def fetch_weather():
    """从 Open-Meteo 获取西安7天天气预报（含降水量，标签对齐中国气象局）"""
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
        precip_prob = daily.get('precipitation_probability_max', [])
        precip_sum = daily.get('precipitation_sum', [])

        # CMA 对齐标签：不区分毛雨/微雨，统一为"小雨"
        wmo_desc = {
            0: '晴', 1: '晴', 2: '多云', 3: '阴',
            45: '雾', 48: '雾凇',
            51: '小雨', 53: '小雨', 55: '小雨',
            56: '冻雨', 57: '冻雨',
            61: '小雨', 63: '中雨', 65: '大雨',
            66: '冻雨', 67: '冻雨',
            71: '小雪', 73: '中雪', 75: '大雪', 77: '雪粒',
            80: '小阵雨', 81: '阵雨', 82: '强阵雨',
            85: '阵雪', 86: '强阵雪',
            95: '雷暴', 96: '雷暴冰雹', 99: '强雷暴'
        }

        entries = []
        for i, d in enumerate(dates):
            code = codes[i] if i < len(codes) else 0
            desc = wmo_desc.get(code, f'code{code}')
            hi = round(t_max[i]) if i < len(t_max) else '-'
            lo = round(t_min[i]) if i < len(t_min) else '-'
            # 降水量和概率
            mm = precip_sum[i] if i < len(precip_sum) else 0
            prob = precip_prob[i] if i < len(precip_prob) else 0
            # 有降水量时显示具体数值，否则显示概率
            if mm and mm > 0:
                desc += f' {mm}mm'
            elif prob and prob > 0:
                desc += f' {prob}%'
            entries.append({'date': d, 'desc': desc, 'temp': f'{lo}~{hi}°C'})
        return entries
    except Exception as e:
        print(f"[WARN] Open-Meteo天气获取失败: {e}")
        return []


# ==================== HTML更新 ====================
def update_html_data(content, target_date_str, orders, warehouse_t, brand_orders=None):
    """在HTML中更新 storeDailyData / warehouseTData / brandDailyData
    orders: 单店订单数（用于storeDailyData，仅非今天写入）
    brand_orders: 品牌合计订单数（用于brandDailyData，仅非今天写入）
    今天的数据不写入实际订单行（等次日从FBI取真实数据后再补），
    预估值通过 savedForecastsData 独立嵌入。
    """
    is_today = (target_date_str == datetime.now().strftime('%Y-%m-%d'))

    # 1. storeDailyData - 仅非今天才写入实际订单（今天留空，等次日FBI取真实数据）
    if not is_today:
        def replace_daily_data(match, store_name, new_date, new_val):
            block = match.group(0)
            date_pattern = rf"'{re.escape(new_date)}'\s*:\s*\d+"
            if re.search(date_pattern, block):
                block = re.sub(date_pattern, f"'{new_date}': {new_val}", block)
            else:
                last_date_match = list(re.finditer(r"'[\d-]+'\s*:\s*\d+", block))
                if last_date_match:
                    last = last_date_match[-1]
                    insert_pos = last.end()
                    block = block[:insert_pos] + f", '{new_date}': {new_val}" + block[insert_pos:]
            return block

        sdm = re.search(
            r"let\s+storeDailyData\s*=\s*\{.*?'龙湖天街店'\s*:\s*\{[^}]*\}",
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

    # 2. warehouseTData - 仅非今天才写入（当天仓T未完结，等次日补）
    if not is_today:
        wtm = re.search(
            r"let\s+warehouseTData\s*=\s*\{.*?'龙湖天街店'\s*:\s*\{[^}]*\}",
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

    # 3. brandDailyData - 仅非今天才写入品牌合计
    if not is_today and brand_orders is not None:
        bdm = re.search(
            r"let\s+brandDailyData\s*=\s*\{.*?'成山农场'\s*:\s*\{[^}]*\}",
            content, re.DOTALL
        )
        if bdm:
            old = bdm.group(0)
            bd_pat = rf"'{re.escape(target_date_str)}'\s*:\s*\d+"
            if re.search(bd_pat, old):
                new_block = re.sub(bd_pat, f"'{target_date_str}': {brand_orders}", old)
            else:
                new_block, n = re.subn(r",\s*\}\s*$", f", '{target_date_str}': {brand_orders}}}", old)
                if n == 0:
                    new_block = re.sub(r"\}\s*$", f", '{target_date_str}': {brand_orders}}}", old)
            content = content.replace(old, new_block)

    return content


def update_html_weather(content, weather_entries):
    """更新HTML中的 staticWeatherData"""
    if not weather_entries:
        return content

    # Build JS block
    lines = []
    for w in weather_entries:
        lines.append(f"    {{date:'{w['date']}',desc:'{w['desc']}',temp:'{w['temp']}'}}")
    js_block = (
        f"// Open-Meteo 天气预报 (西安 {WEATHER_LAT},{WEATHER_LON}) "
        f"更新于 {datetime.now().strftime('%Y-%m-%d')}\n"
        f"const staticWeatherData = {{\n"
        f"  '西安': [\n"
        f"{',\n'.join(lines)}\n"
        f"  ]\n"
        f"}};"
    )

    # Replace existing staticWeatherData or add before brands
    if 'const staticWeatherData' in content:
        content = re.sub(
            r'// Open-Meteo 天气预报.*?const staticWeatherData\s*=\s*\{[^}]*\};',
            js_block,
            content,
            flags=re.DOTALL
        )
    else:
        # Insert before "const brands"
        content = content.replace('const brands', js_block + '\n\n    const brands')

    return content


def fetch_deployed_forecasts():
    """从GitHub Pages已部署的HTML中提取savedForecastsData，作为多人合并的基准"""
    try:
        req = urllib.request.Request(PAGES_URL, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            deployed_html = resp.read().decode('utf-8')
    except Exception as e:
        print(f"[WARN] 无法获取已部署HTML: {e}")
        return {}

    # 用同样的花括号计数法提取 savedForecastsData 的JSON文本
    pattern = r'let\s+savedForecastsData\s*=\s*\{'
    match = re.search(pattern, deployed_html)
    if not match:
        print("[WARN] 已部署HTML中未找到savedForecastsData")
        return {}

    start_pos = match.start()
    brace_count = 0
    end_pos = start_pos
    in_string = False
    escape_next = False
    for i in range(start_pos, len(deployed_html)):
        char = deployed_html[i]
        if escape_next:
            escape_next = False
            continue
        if char == '\\':
            escape_next = True
            continue
        if char == '"' and not in_string:
            in_string = True
        elif char == '"' and in_string:
            in_string = False
        elif not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    end_pos = i + 1
                    break

    if end_pos <= start_pos:
        return {}

    js_text = deployed_html[start_pos:end_pos]
    # 把 let savedForecastsData = {...} 转成纯JSON
    json_text = re.sub(r'^let\s+savedForecastsData\s*=\s*', '', js_text)
    json_text = re.sub(r';\s*$', '', json_text)
    # JS单键名 → JSON双引号键名
    json_text = re.sub(r"'([^']+)'\s*:", r'"\1":', json_text)

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        print(f"[WARN] 解析已部署savedForecastsData失败: {e}")
        return {}


def update_html_forecasts(content):
    """读取forecasts JSON文件，与已部署数据合并后嵌入到HTML的savedForecastsData中"""
    # 查找forecasts JSON文件（在HTML同目录或脚本同目录）
    html_dir = os.path.dirname(HTML_PATH)
    script_dir = os.path.dirname(os.path.abspath(__file__))

    forecast_file = None
    for search_dir in [html_dir, script_dir]:
        pattern = os.path.join(search_dir, 'forecasts_*.json')
        files = glob.glob(pattern)
        if files:
            files.sort(key=os.path.getmtime, reverse=True)
            forecast_file = files[0]
            break

    if not forecast_file:
        print("[WARN] 未找到forecasts JSON文件，跳过预估数据嵌入")
        return content

    print(f"[INFO] 读取预估数据: {forecast_file}")
    try:
        with open(forecast_file, 'r', encoding='utf-8') as f:
            local_data = json.load(f)
    except Exception as e:
        print(f"[ERROR] 读取forecasts文件失败: {e}")
        return content

    # 从已部署HTML获取基准数据（多人多设备合并的关键）
    print("[INFO] 从GitHub Pages获取已部署预估数据...")
    deployed_data = fetch_deployed_forecasts()
    if deployed_data:
        deployed_count = sum(len(v) for v in deployed_data.values())
        print(f"[INFO] 已部署数据: {deployed_count} 条")
    else:
        deployed_count = 0
        print("[INFO] 无已部署数据，使用本地数据作为基准")

    # 合并：以已部署数据为底，本地JSON覆盖同键值
    merged = {}
    for store, dates in deployed_data.items():
        merged[store] = dict(dates)
    for store, dates in local_data.items():
        if store not in merged:
            merged[store] = {}
        for date, val in dates.items():
            merged[store][date] = val

    merged_count = sum(len(v) for v in merged.values())
    local_count = sum(len(v) for v in local_data.values())
    print(f"[OK] 合并完成: 已部署{deployed_count}条 + 本地{local_count}条 → 共{merged_count}条")

    # 构建新的savedForecastsData JS代码
    js_data = json.dumps(merged, ensure_ascii=False, indent=2)
    js_lines = js_data.split('\n')
    indented = '\n'.join(['      ' + line if i > 0 else line for i, line in enumerate(js_lines)])

    new_block = f"let savedForecastsData = {indented};"
    
    # 替换现有的savedForecastsData（使用更宽松的正则匹配多行嵌套结构）
    if 'let savedForecastsData' in content:
        # 找到 let savedForecastsData = 开始，到下一个 }; 结束（考虑嵌套）
        pattern = r'let\s+savedForecastsData\s*=\s*\{'
        match = re.search(pattern, content)
        if match:
            start_pos = match.start()
            # 从匹配位置开始，找到对应的结束 };
            brace_count = 0
            end_pos = start_pos
            in_string = False
            escape_next = False
            for i in range(start_pos, len(content)):
                char = content[i]
                if escape_next:
                    escape_next = False
                    continue
                if char == '\\':
                    escape_next = True
                    continue
                if char == "'" and not in_string:
                    in_string = True
                elif char == "'" and in_string:
                    in_string = False
                elif not in_string:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            # 找到匹配的结束括号，继续找分号
                            end_pos = i + 1
                            if end_pos < len(content) and content[end_pos] == ';':
                                end_pos += 1
                            break
            if end_pos > start_pos:
                content = content[:start_pos] + new_block + content[end_pos:]
                print(f"[OK] 已嵌入预估数据 ({len(merged)} 个门店)")
            else:
                print("[WARN] 无法找到savedForecastsData的结束位置")
        else:
            print("[WARN] HTML中未找到savedForecastsData声明")
    else:
        print("[WARN] HTML中未找到savedForecastsData，跳过嵌入")
    
    return content


# ==================== 模型预估动态更新 ====================
def fetch_hourly_weather():
    """从Open-Meteo获取小时级天气数据，供预估模型使用"""
    url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={WEATHER_LAT}&longitude={WEATHER_LON}"
        f"&hourly=temperature_2m,precipitation,weather_code,windspeed_10m"
        f"&forecast_days=16&timezone=Asia/Shanghai"
    )
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        h = data.get('hourly', {})
        # 模型期望字段名 weathercode (无下划线), API返回 weather_code
        if 'weather_code' in h and 'weathercode' not in h:
            h['weathercode'] = h.pop('weather_code')
        return {'hourly': h}
    except Exception as e:
        print(f"[WARN] 小时天气获取失败: {e}")
        return None


def extract_store_actuals(content):
    """从HTML的storeDailyData中提取龙湖天街店实际订单"""
    m = re.search(r"'龙湖天街店'\s*:\s*\{([^}]+)\}", content)
    if not m:
        return {}
    entries = re.findall(r"'(\d{4}-\d{2}-\d{2})'\s*:\s*(\d+)", m.group(1))
    return {d: int(v) for d, v in entries}


def _replace_js_object(content, var_name, new_value_str):
    """替换HTML中的 const/let varName = {...}; 块（支持花括号嵌套）"""
    pattern = rf'(const|let)\s+{re.escape(var_name)}\s*=\s*\{{'
    match = re.search(pattern, content)
    if match:
        start = match.start()
        brace = 0
        end = start
        for i in range(match.start() + len(match.group(0)) - 1, len(content)):
            if content[i] == '{':
                brace += 1
            elif content[i] == '}':
                brace -= 1
                if brace == 0:
                    end = i + 1
                    # skip optional semicolon
                    if end < len(content) and content[end] == ';':
                        end += 1
                    break
        if end > start:
            new_block = f"{match.group(1)} {var_name} = {new_value_str};"
            return content[:start] + new_block + content[end:]
    return content


def _build_js_dict(d):
    """将Python dict构建为JS对象字面量字符串"""
    items = []
    for k, v in sorted(d.items()):
        items.append(f"  '{k}': {v}")
    return "{\n" + ",\n".join(items) + "\n}"


def update_model_forecast(content):
    """运行预估模型并更新HTML中的modelForecastData（当日锁定，未来动态校准）"""
    today = datetime.now().strftime('%Y-%m-%d')

    # 1. 读取当前modelForecastData（今天还未锁定的值）
    current_mf = {}
    m = re.search(r"const\s+modelForecastData\s*=\s*\{([^}]*)\}", content)
    if m:
        entries = re.findall(r"'(\d{4}-\d{2}-\d{2})'\s*:\s*(\d+)", m.group(1))
        current_mf = {d: int(v) for d, v in entries}

    # 2. 读取已锁定的历史记录
    history = {}
    m = re.search(r"const\s+modelForecastHistory\s*=\s*\{([^}]*)\}", content)
    if m:
        entries = re.findall(r"'(\d{4}-\d{2}-\d{2})'\s*:\s*(\d+)", m.group(1))
        history = {d: int(v) for d, v in entries}

    # 3. 锁定今天的值到历史（仅今天有预估且尚未锁定时）
    if today in current_mf and today not in history:
        history[today] = current_mf[today]
        print(f"[INFO] 锁定{today}系统预估: {history[today]}单")

    # 4. 刷新天气数据
    print("[INFO] 刷新模型天气数据...")
    hourly_wx = fetch_hourly_weather()
    if hourly_wx:
        weather_path = os.path.join(MODEL_DIR, 'weather_xian.json')
        with open(weather_path, 'w', encoding='utf-8') as f:
            json.dump(hourly_wx, f, ensure_ascii=False)
        print(f"[OK] 天气数据已更新: {weather_path}")

    # 5. 提取实际订单并写入latest_actuals.json（供模型未来使用）
    actuals = extract_store_actuals(content)
    if actuals:
        actuals_path = os.path.join(MODEL_DIR, 'latest_actuals.json')
        with open(actuals_path, 'w', encoding='utf-8') as f:
            json.dump(actuals, f, ensure_ascii=False, indent=2)

    # 6. 运行预估模型
    model_script = os.path.join(MODEL_DIR, 'order_forecast_model.py')
    forecast_file = os.path.join(MODEL_DIR, 'forecast_result.json')
    if os.path.exists(model_script):
        print("[INFO] 运行预估模型...")
        try:
            result = subprocess.run(
                [sys.executable, '-X', 'utf8', model_script],
                capture_output=True, text=True, timeout=180,
                cwd=MODEL_DIR
            )
            if result.returncode != 0:
                print(f"[WARN] 模型运行异常:\n{result.stderr[-500:]}")
                return content
            print("[OK] 模型运行完成")
        except Exception as e:
            print(f"[WARN] 模型运行失败: {e}")
            return content
    else:
        print(f"[WARN] 模型脚本不存在: {model_script}")
        return content

    # 7. 读取模型输出
    if not os.path.exists(forecast_file):
        print("[WARN] forecast_result.json 不存在")
        return content
    with open(forecast_file, 'r', encoding='utf-8') as f:
        forecast_data = json.load(f)

    # 8. 构建新modelForecastData（今天锁定，明天起用模型新值）
    new_mf = {}
    for fc in forecast_data.get('forecast', []):
        d = fc['date']
        v = round(fc['forecast'])
        if d == today and d in history:
            new_mf[d] = history[d]  # 锁定值
        elif d > today:
            new_mf[d] = v           # 模型最新值
        elif d not in history:
            new_mf[d] = v           # 过去的日期且未锁定→用模型值

    if not new_mf:
        print("[WARN] 模型无有效预估输出")
        return content

    # 9. 写入HTML
    mf_str = _build_js_dict(new_mf)
    content = _replace_js_object(content, 'modelForecastData', mf_str)

    # 10. 更新历史记录块
    hist_str = _build_js_dict(history) if history else '{}'
    if 'modelForecastHistory' in content:
        content = _replace_js_object(content, 'modelForecastHistory', hist_str)
    else:
        # 在modelForecastData之前插入
        hist_block = f"    const modelForecastHistory = {hist_str};\n"
        content = content.replace(
            '    const modelForecastData',
            hist_block + '    const modelForecastData',
            1
        )

    locked_count = len(history)
    future_count = sum(1 for d in new_mf if d > today)
    print(f"[OK] 系统预估更新: 锁定{locked_count}天历史, {future_count}天未来预估")
    return content


# ==================== GitHub Pages 推送 ====================
def push_to_github_pages(target_date_str):
    """复制HTML到repo，git push到GitHub Pages"""
    repo_html_path = os.path.join(REPO_DIR, PAGES_FILENAME)

    # Copy HTML to repo
    shutil.copy2(HTML_PATH, repo_html_path)
    print(f"[INFO] 已复制HTML到repo: {repo_html_path}")

    # Git operations
    os.chdir(REPO_DIR)
    subprocess.run(['git', 'add', PAGES_FILENAME], check=True)
    commit_msg = f"Update order forecast data ({target_date_str})"
    result = subprocess.run(
        ['git', 'commit', '-m', commit_msg],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        combined = (result.stdout + result.stderr).lower()
        if 'nothing to commit' in combined or 'no changes added to commit' in combined:
            print("[INFO] HTML无变化，跳过commit")
            return True
        else:
            print(f"[ERROR] git commit失败: {result.stderr}")
            return False

    result = subprocess.run(['git', 'push'], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"[ERROR] git push失败: {result.stderr}")
        return False

    print(f"[OK] GitHub Pages 推送成功")
    return True


# ==================== 钉钉推送 ====================
def format_dingtalk_message(target_date_str, orders, warehouse_t, weather_entries):
    """格式化钉钉消息（包含数据摘要 + GitHub Pages链接）"""
    dt = datetime.strptime(target_date_str, '%Y-%m-%d')
    weekday_names = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    weekday = weekday_names[dt.weekday()]

    lines = []
    lines.append(f"## 成山农场日报 {target_date_str} {weekday}")
    lines.append("")
    lines.append(f"**龙湖天街店**: {orders}单 / 仓T {warehouse_t:.1f}min{' ⚠️' if warehouse_t > 15 else ''}")
    lines.append("")

    # 天气预报
    if weather_entries:
        lines.append("**天气预报**:")
        for w in weather_entries[:3]:
            d = datetime.strptime(w['date'], '%Y-%m-%d')
            wd = weekday_names[d.weekday()]
            lines.append(f"- {w['date']} {wd}: {w['desc']} {w['temp']}")
        lines.append("")

    # 链接
    lines.append(f"[点击打开排班工具]({PAGES_URL})")
    lines.append("")
    lines.append("> 数据来源: FBI看板1796001 + Open-Meteo")

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
        "markdown": {
            "title": "成山农场日报",
            "text": message
        }
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            return result
    except Exception as e:
        return {"errcode": -1, "errmsg": str(e)}


# ==================== 主流程 ====================
def main():
    # 参数: target_date orders warehouse_t [brand_orders]
    if len(sys.argv) >= 4:
        target_date = sys.argv[1]
        orders = int(sys.argv[2])
        warehouse_t = float(sys.argv[3])
        brand_orders = int(sys.argv[4]) if len(sys.argv) >= 5 else None
    else:
        print("用法: python dingtalk_push.py <日期> <门店订单数> <仓T> [品牌合计订单数]")
        print("示例: python dingtalk_push.py 2026-09-16 306 9.31 1405")
        sys.exit(1)

    print(f"[INFO] 目标日期: {target_date}")
    print(f"[INFO] 门店订单: {orders}, 仓T: {warehouse_t}, 品牌合计: {brand_orders or '未指定'}")
    print(f"[INFO] HTML路径: {HTML_PATH}")

    # Step 1: 读取HTML
    with open(HTML_PATH, 'r', encoding='utf-8') as f:
        content = f.read()

    # Step 2: 获取天气
    print("[INFO] 获取天气预报...")
    weather = fetch_weather()
    if weather:
        print(f"[OK] 获取到 {len(weather)} 天天气")

    # Step 3: 更新HTML数据
    print("[INFO] 更新HTML数据...")
    content = update_html_data(content, target_date, orders, warehouse_t, brand_orders)
    content = update_html_weather(content, weather)
    content = update_html_forecasts(content)

    # Step 3.5: 运行预估模型，动态更新系统预估（当日锁定，未来校准）
    print("[INFO] 更新模型系统预估...")
    content = update_model_forecast(content)

    # Step 4: 写回HTML
    with open(HTML_PATH, 'w', encoding='utf-8') as f:
        f.write(content)
    print("[OK] HTML已更新")

    # Step 5: 推送到GitHub Pages
    print("[INFO] 推送到GitHub Pages...")
    if not push_to_github_pages(target_date):
        print("[ERROR] GitHub Pages推送失败，终止")
        sys.exit(1)

    # Step 6: 发送钉钉消息
    print("[INFO] 发送钉钉消息...")
    message = format_dingtalk_message(target_date, orders, warehouse_t, weather)

    print("\n--- 推送内容 ---")
    print(message)
    print("--- 结束 ---\n")

    result = sign_and_send(message)
    print(f"[INFO] 钉钉返回: {result}")

    if result.get("errcode") == 0:
        print("[OK] 推送成功!")
    else:
        print(f"[FAIL] 推送失败: {result.get('errmsg', 'unknown error')}")
        sys.exit(1)


if __name__ == '__main__':
    main()
