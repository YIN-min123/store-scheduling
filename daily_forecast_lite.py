#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
日粒度轻量预估引擎 (daily_forecast_lite)
=======================================
用途：无人值守「每周五系统预估刷新」——不加载订单级拣货单Excel（动辄 200MB、10+ 分钟），
直接从累积的「每日实际订单」序列 + 天气预报，复刻 order_forecast_model 的日级预估逻辑：
  近3周按星期 recency 加权基准 × 周级衰减 × 天气系数（连雨/孤立雨/大雨/温度）× 需求惯性 × 节假日/调休。
复用主模型的常量（节假日/调休/异常日/门店），避免口径漂移。

输入（环境变量）：
  ACTUALS_JSON  : {date: orders} 的日实际订单 JSON（默认 latest_actuals.json）
  FC_START_DATE : 预估起始日 YYYY-MM-DD（默认 最后实际日+1）
  FC_DAYS       : 预估天数（默认 16）
  WEATHER_PATH  : 天气 JSON（默认 weather_xian.json，Open-Meteo hourly 结构）
输出：
  forecast_result.json （与主模型同 schema 的 forecast 列表，供 dingtalk_push 读取）
"""
import os
import json
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

# 复用主模型常量（同目录）——导入不会触发重活（load_orders/main 都在函数内 + __main__ 保护）
import order_forecast_model as M

DOW_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
RAIN_LABEL = {'none': '晴', 'light': '小雨', 'moderate': '中雨', 'heavy': '大雨'}

# ---- 与主模型对齐的系数 ----
CONSEC_RAIN_COEFF = {0: 1.0, 1: 1.00, 2: 1.05, 3: 1.13, 4: 1.09, 5: 1.16}
ISOLATED_RAIN_COEFF_WEEKDAY = 1.08
ISOLATED_RAIN_COEFF_WEEKEND = 0.96
HEAVY_RAIN_SUPPRESSION = 0.857
INERTIA_SUPPRESSION = 0.95


def _env(name, default=None):
    v = os.environ.get(name, '')
    v = v.strip()
    return v if v else default


def load_daily_actuals(path):
    """{date_str: orders} → 有序 DataFrame[date(date), orders(int)]，剔除异常日"""
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    anomaly = set()
    for d, info in M.KEY_DATES.items():
        if info.get('type') == 'anomaly':
            anomaly.add(d)
    rows = []
    for d, v in raw.items():
        try:
            orders = int(v)
        except (TypeError, ValueError):
            continue
        if orders <= 0:
            continue
        rows.append({'date': datetime.strptime(d, '%Y-%m-%d').date(),
                     'orders': orders, 'is_anomaly': d in anomaly})
    df = pd.DataFrame(rows).sort_values('date').reset_index(drop=True)
    return df


def load_weather_daily(path):
    """Open-Meteo hourly JSON → 日级 [{date, max_precip, total_precip, max_temp, consec_rain}]"""
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    h = raw['hourly']
    recs = []
    for i, t in enumerate(h['time']):
        dt = pd.Timestamp(t)
        recs.append({'date': dt.date(),
                     'precip': h['precipitation'][i] or 0.0,
                     'temp': h['temperature_2m'][i]})
    wdf = pd.DataFrame(recs)
    daily = wdf.groupby('date').agg(
        max_precip=('precip', 'max'),
        total_precip=('precip', 'sum'),
        max_temp=('temp', 'max'),
        min_temp=('temp', 'min'),
    ).reset_index().sort_values('date').reset_index(drop=True)
    # 连续降雨天数：日最大降水>=0.1mm 视为雨天，向未来累积
    consec = []
    run = 0
    for _, r in daily.iterrows():
        if (r['max_precip'] or 0) >= 0.1:
            run += 1
        else:
            run = 0
        consec.append(run)
    daily['consec_rain'] = consec
    return {r['date']: r for _, r in daily.iterrows()}


def classify_rain(precip_mm):
    if precip_mm is None or precip_mm < 0.1:
        return 'none'
    if precip_mm < 2.5:
        return 'light'
    if precip_mm < 8.0:
        return 'moderate'
    return 'heavy'


def classify_temp(temp_c):
    if temp_c is None:
        return 'normal'
    if temp_c >= 38:
        return 'extreme_hot'
    if temp_c >= 36:
        return 'hot'
    if temp_c <= 0:
        return 'cold'
    return 'normal'


def recency_weighted(values):
    """同主模型：新周0.5/中周0.3/远周0.2"""
    vals = list(values)
    n = len(vals)
    if n == 0:
        return 0.0
    if n >= 3:
        take, w = vals[-3:], [0.2, 0.3, 0.5]
    elif n == 2:
        take, w = vals, [0.4, 0.6]
    else:
        take, w = vals, [1.0]
    return sum(v * wi for v, wi in zip(take, w)) / sum(w[:len(take)])


def generate(df, wx, start_date, days):
    clean = df[~df['is_anomaly']].copy()
    last_date = clean['date'].max()
    by_date = {r['date']: r['orders'] for _, r in clean.iterrows()}

    # ---- 近3周按星期 recency 加权基准 ----
    recent = clean[clean['date'] >= last_date - timedelta(days=20)]
    by_dow = {}
    for dow in range(7):
        sub = recent[recent['date'].apply(lambda d: d.weekday()) == dow].sort_values('date')
        if len(sub) >= 1:
            vals = list(sub['orders'])
            m = float(np.mean(vals))
            std = float(np.std(vals, ddof=1)) if len(vals) >= 2 else 40.0
            by_dow[dow] = {'weighted': recency_weighted(sub['orders']),
                           'mean': m, 'std': std}

    # ---- 周级衰减 ----
    weekly_means = []
    for w in range(4):
        ws = last_date - timedelta(days=6 + w * 7)
        we = last_date - timedelta(days=w * 7)
        wv = [o for d, o in by_date.items() if ws <= d <= we]
        if wv:
            weekly_means.append(float(np.mean(wv)))
    if len(weekly_means) >= 3:
        x = np.arange(len(weekly_means))
        y_chrono = np.array(weekly_means)[::-1]
        slope = np.polyfit(x, y_chrono, 1)[0]
        decay = 0.5 * slope / np.mean(y_chrono)
        decay = float(np.clip(decay, -0.05, 0.02))
    else:
        decay = 0.0

    results = []
    prev_high = False
    # 起始日前一天的实际订单，用于首日的惯性判断
    for off in range(days):
        tdate = start_date + timedelta(days=off)
        dow = tdate.weekday()
        date_str = str(tdate)
        week_ahead = off // 7

        is_tiaoxiu = date_str in M.TIAOXIU_WORKDAYS
        if is_tiaoxiu:
            eff_dow = 3 if dow == 6 else 4
            is_weekend = False
        else:
            eff_dow = dow
            is_weekend = dow >= 5
        lookup = eff_dow

        if lookup in by_dow:
            base_daily = by_dow[lookup]['weighted']
            std = by_dow[lookup].get('std', 40)
        elif dow in by_dow:
            base_daily = by_dow[dow]['weighted']
            std = by_dow[dow].get('std', 40)
        else:
            base_daily = float(np.mean(list(by_date.values()))) if by_date else 350
            std = 40

        adjusted = base_daily * ((1 + decay) ** max(0, week_ahead))

        # ---- 天气 ----
        weather_coeff = 1.0
        weather_desc = '晴'
        wr = wx.get(tdate)
        if wr is not None:
            max_precip = float(wr['max_precip'] or 0)
            max_temp = float(wr['max_temp']) if wr['max_temp'] is not None else None
            consec_days = int(wr['consec_rain'])
            rain_level = classify_rain(max_precip)
            temp_level = classify_temp(max_temp)
            is_rainy = consec_days > 0
            if is_rainy:
                weather_coeff *= CONSEC_RAIN_COEFF.get(min(consec_days, 5), 1.16)
            elif rain_level != 'none':
                weather_coeff *= ISOLATED_RAIN_COEFF_WEEKEND if is_weekend else ISOLATED_RAIN_COEFF_WEEKDAY
            if rain_level == 'heavy':
                weather_coeff *= HEAVY_RAIN_SUPPRESSION
                weather_desc = f'大雨(连续{consec_days}天)'
            elif rain_level == 'moderate':
                weather_desc = f'中雨(连续{consec_days}天)' if consec_days else '中雨'
            elif rain_level == 'light':
                weather_desc = f'小雨(连续{consec_days}天)' if consec_days else '小雨'
            if consec_days == 0 and rain_level == 'none':
                weather_desc = RAIN_LABEL.get(rain_level, '晴')
            if temp_level == 'extreme_hot':
                weather_coeff *= 1.10; weather_desc += '+极端高温'
            elif temp_level == 'hot':
                weather_coeff *= 1.05; weather_desc += '+高温'
            elif temp_level == 'cold':
                weather_coeff *= 1.08; weather_desc += '+低温'
        adjusted *= weather_coeff

        # ---- 需求惯性（高需求日次日微降）----
        inertia_coeff = 1.0
        if prev_high:
            inertia_coeff = INERTIA_SUPPRESSION
            adjusted *= inertia_coeff

        # ---- 节假日 / 调休 ----
        holiday_coeff = 1.0
        holiday_label = ''
        if date_str in M.HOLIDAYS:
            h = M.HOLIDAYS[date_str]
            holiday_coeff = 1.25 if h['type'] == 'travel' else 1.15
            holiday_label = h['label']
        if is_tiaoxiu:
            holiday_label = M.TIAOXIU_WORKDAYS[date_str]
        adjusted *= holiday_coeff

        final = round(max(0, adjusted), 0)
        is_high = final > base_daily * 1.15
        prev_high = is_high

        results.append({
            'date': date_str,
            'dow': dow,
            'dow_name': DOW_NAMES[dow],
            'is_weekend': is_weekend,
            'is_tiaoxiu': is_tiaoxiu,
            'base_daily': round(base_daily, 0),
            'future_decay': round((1 + decay) ** max(0, week_ahead), 3),
            'weather_coeff': round(weather_coeff, 3),
            'inertia_coeff': round(inertia_coeff, 3),
            'holiday_coeff': round(holiday_coeff, 3),
            'weather_desc': weather_desc,
            'holiday_label': holiday_label,
            'forecast': int(final),           # 供 push 读取
            'forecast_total': int(final),
            'confidence_low': int(max(0, adjusted - 1.5 * std)),
            'confidence_high': int(adjusted + 1.5 * std),
        })

    return results, {'weekly_decay_rate': round(decay, 4),
                     'weekly_means': [round(w, 1) for w in weekly_means],
                     'engine': 'lite'}


def main():
    actuals_path = _env('ACTUALS_JSON', 'latest_actuals.json')
    if not os.path.exists(actuals_path):
        alt = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'latest_actuals.json')
        actuals_path = alt
    wx_path = _env('WEATHER_PATH', 'weather_xian.json')
    days = int(_env('FC_DAYS', '16'))
    start_env = _env('FC_START_DATE')

    df = load_daily_actuals(actuals_path)
    if df.empty:
        raise SystemExit(f'[ERR] 无可用实际订单数据: {actuals_path}')
    last_date = df[~df['is_anomaly']]['date'].max()
    start_date = datetime.strptime(start_env, '%Y-%m-%d').date() if start_env else last_date + timedelta(days=1)

    wx = load_weather_daily(wx_path) if os.path.exists(wx_path) else {}

    forecast, meta = generate(df, wx, start_date, days)

    output = {
        'meta': {
            'store': M.STORE_NAME,
            'engine': 'lite',
            'data_range': f"{df['date'].min()} ~ {df['date'].max()}",
            'actual_days': int((~df['is_anomaly']).sum()),
            'forecast_start': str(start_date),
            'forecast_days': days,
            'generated_at': datetime.now().isoformat(),
        },
        'forecast': forecast,
        'forecast_meta': meta,
    }
    with open('forecast_result.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)

    print(f"[lite] 基准数据: {df['date'].min()} ~ {last_date} (有效{int((~df['is_anomaly']).sum())}天)")
    print(f"[lite] 预估窗口: {start_date} ~ {start_date + timedelta(days=days-1)} ({days}天)")
    print(f"[lite] 周衰减率: {meta['weekly_decay_rate']}")
    for r in forecast:
        tx = ' [调休]' if r['is_tiaoxiu'] else ''
        hl = f" [{r['holiday_label']}]" if r['holiday_label'] else ''
        print(f"  {r['date']} {r['dow_name']}: {r['forecast']}单 ({r['confidence_low']}-{r['confidence_high']}) {r['weather_desc']}{tx}{hl}")
    print('[lite] 已写出 forecast_result.json')


if __name__ == '__main__':
    main()
