#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
兴勤超市(港惠店) 日粒度轻量预估引擎
==================================
复刻成山 daily_forecast_lite 的日级口径：
  近3周按星期 recency 加权基准 × 周级衰减 × 天气系数(连雨/孤立雨/大雨/温度) × 需求惯性 × 节假日/调休。
独立于成山：单独常量、单独天气文件(weather_huizhou.json)、单独输入(xinqin_daily_full.json)。

两种输出：
  future   : 今天..今天+horizon-1 的系统预估（用截至昨天的全部实际数据 + 未来天气预报）
  history  : 回测——对过去每一天，只用该日之前的实际数据预测（leave-one-out），
             得到"模型当时会给出的预估"，用于 modelForecastHistory（≠实际订单）。

环境变量：
  ACTUALS_JSON  (默认 workspace/xinqin_daily_full.json)
  WEATHER_PATH  (默认 本目录/weather_huizhou.json)
  FC_HORIZON    (未来天数，默认 16)
  HIST_DAYS     (回测历史天数，默认 21)
"""
import os, json
from datetime import datetime, timedelta, date
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
STORE_NAME = '兴勤超市(港惠店)'

# ---- 系数（按兴勤/惠州实测校准，非成山口径）----
# 实测：雨天单量≈DOW中位1.04、晴≈0.885（雨仅小幅抬升即时零售需求，且周末/工作日无显著差异）；
# 连雨不做激进叠加，封顶1.08；大雨同样抬升需求而非抑制（惠州多雨，居民雨天转线上）。
CONSEC_RAIN_COEFF = {0: 1.0, 1: 1.02, 2: 1.04, 3: 1.06, 4: 1.08, 5: 1.08}
ISOLATED_RAIN_COEFF_WEEKDAY = 1.03
ISOLATED_RAIN_COEFF_WEEKEND = 1.03
HEAVY_RAIN_SUPPRESSION = 1.0          # 大雨不抑制（区别于成山）
INERTIA_SUPPRESSION = 0.97
WEEKEND_RAIN_UPLIFT = 1.0             # 周末雨无额外抬升（实测与工作日相近）
# 惠州 subtropical：极端高温阈值更高，≥38 记高温、≥35 记热
HOT_THRESHOLD = 35.0
EXTREME_HOT_THRESHOLD = 38.0

# ---- 节假日（全国统一，2026）----
HOLIDAYS = {
    '2026-09-25': {'type': 'festival', 'label': '中秋节'},
    '2026-09-26': {'type': 'festival', 'label': '中秋节'},
    '2026-09-27': {'type': 'festival', 'label': '中秋节'},
    '2026-10-01': {'type': 'travel', 'label': '国庆节'},
    '2026-10-02': {'type': 'travel', 'label': '国庆节'},
    '2026-10-03': {'type': 'travel', 'label': '国庆节'},
    '2026-10-04': {'type': 'travel', 'label': '国庆节'},
    '2026-10-05': {'type': 'travel', 'label': '国庆节'},
    '2026-10-06': {'type': 'travel', 'label': '国庆节'},
    '2026-10-07': {'type': 'travel', 'label': '国庆节'},
}
TIAOXIU_WORKDAYS = {
    '2026-09-20': '国庆节调休补班',
    '2026-10-10': '国庆节调休补班',
}
KEY_DATES = {}   # 兴勤暂无确认的异常缺货日

DOW_NAMES = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
RAIN_LABEL = {'none': '晴', 'light': '小雨', 'moderate': '中雨', 'heavy': '大雨'}


def load_actuals(path):
    with open(path, encoding='utf-8') as f:
        raw = json.load(f)
    anomaly = {d for d, i in KEY_DATES.items() if i.get('type') == 'anomaly'}
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
    return pd.DataFrame(rows).sort_values('date').reset_index(drop=True)


def load_weather_daily(path):
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
        max_precip=('precip', 'max'), total_precip=('precip', 'sum'),
        max_temp=('temp', 'max'), min_temp=('temp', 'min'),
    ).reset_index().sort_values('date').reset_index(drop=True)
    consec, run = [], 0
    for _, r in daily.iterrows():
        run = run + 1 if (r['max_precip'] or 0) >= 0.1 else 0
        consec.append(run)
    daily['consec_rain'] = consec
    return {r['date']: r for _, r in daily.iterrows()}


def classify_rain(mm):
    if mm is None or mm < 0.1: return 'none'
    if mm < 2.5: return 'light'
    if mm < 8.0: return 'moderate'
    return 'heavy'


def classify_temp(t):
    if t is None: return 'normal'
    if t >= EXTREME_HOT_THRESHOLD: return 'extreme_hot'
    if t >= HOT_THRESHOLD: return 'hot'
    if t <= 5: return 'cool'
    return 'normal'


def recency_weighted(values):
    vals = list(values)
    n = len(vals)
    if n == 0: return 0.0
    if n >= 3: take, w = vals[-3:], [0.2, 0.3, 0.5]
    elif n == 2: take, w = vals, [0.4, 0.6]
    else: take, w = vals, [1.0]
    return sum(v * wi for v, wi in zip(take, w)) / sum(w[:len(take)])


def _compute_baseline_and_decay(clean, asof_last):
    """只用 <= asof_last 的数据，算近3周DOW基准 + 周衰减。"""
    by_date = {r['date']: r['orders'] for _, r in clean.iterrows() if r['date'] <= asof_last}
    recent = clean[(clean['date'] >= asof_last - timedelta(days=20)) & (clean['date'] <= asof_last)]
    by_dow = {}
    for dow in range(7):
        sub = recent[recent['date'].apply(lambda d: d.weekday()) == dow].sort_values('date')
        if len(sub) >= 1:
            vals = list(sub['orders'])
            std = float(np.std(vals, ddof=1)) if len(vals) >= 2 else 40.0
            by_dow[dow] = {'weighted': recency_weighted(vals), 'std': std}
    weekly_means = []
    for w in range(4):
        ws = asof_last - timedelta(days=6 + w * 7)
        we = asof_last - timedelta(days=w * 7)
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
    return by_dow, decay, by_date


def predict_one(tdate, by_dow, decay, by_date, wx, prev_high):
    dow = tdate.weekday()
    date_str = str(tdate)
    week_ahead = 0  # 相对基准日，衰减在调用侧按 off 传入
    return None  # placeholder


def generate_series(clean, wx, start_date, days, asof_last):
    """从 start_date 起预测 days 天，基准数据截至 asof_last。"""
    by_dow, decay, by_date = _compute_baseline_and_decay(clean, asof_last)
    fallback = float(np.mean(list(by_date.values()))) if by_date else 450.0
    results = []
    prev_high = False
    for off in range(days):
        tdate = start_date + timedelta(days=off)
        dow = tdate.weekday()
        date_str = str(tdate)
        week_ahead = max(0, (tdate - asof_last).days) // 7

        is_tiaoxiu = date_str in TIAOXIU_WORKDAYS
        if is_tiaoxiu:
            eff_dow = 3 if dow == 6 else 4
            is_weekend = False
        else:
            eff_dow = dow
            is_weekend = dow >= 5

        if eff_dow in by_dow:
            base_daily = by_dow[eff_dow]['weighted']; std = by_dow[eff_dow]['std']
        elif dow in by_dow:
            base_daily = by_dow[dow]['weighted']; std = by_dow[dow]['std']
        else:
            base_daily = fallback; std = 40
        adjusted = base_daily * ((1 + decay) ** week_ahead)

        weather_coeff = 1.0; weather_desc = '晴'
        wr = wx.get(tdate)
        if wr is not None:
            max_precip = float(wr['max_precip'] or 0)
            max_temp = float(wr['max_temp']) if wr['max_temp'] is not None else None
            consec_days = int(wr['consec_rain']); rain_level = classify_rain(max_precip)
            temp_level = classify_temp(max_temp); is_rainy = consec_days > 0
            if is_rainy:
                weather_coeff *= CONSEC_RAIN_COEFF.get(min(consec_days, 5), 1.16)
            elif rain_level != 'none':
                weather_coeff *= ISOLATED_RAIN_COEFF_WEEKEND if is_weekend else ISOLATED_RAIN_COEFF_WEEKDAY
            if is_weekend and (is_rainy or rain_level != 'none'):
                weather_coeff *= WEEKEND_RAIN_UPLIFT
            if rain_level == 'heavy':
                weather_coeff *= HEAVY_RAIN_SUPPRESSION; weather_desc = f'大雨(连续{consec_days}天)'
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
            elif temp_level == 'cool':
                weather_coeff *= 1.06; weather_desc += '+凉'
        adjusted *= weather_coeff

        inertia_coeff = 1.0
        if prev_high:
            inertia_coeff = INERTIA_SUPPRESSION; adjusted *= inertia_coeff

        holiday_coeff = 1.0; holiday_label = ''
        if date_str in HOLIDAYS:
            h = HOLIDAYS[date_str]
            holiday_coeff = 1.05 if h['type'] == 'festival' else 1.00  # 本地超市国庆无出行 surge
            holiday_label = h['label']
        if is_tiaoxiu:
            holiday_label = TIAOXIU_WORKDAYS[date_str]
        adjusted *= holiday_coeff

        final = round(max(0, adjusted))
        prev_high = final > base_daily * 1.15
        results.append({
            'date': date_str, 'dow': dow, 'dow_name': DOW_NAMES[dow],
            'is_weekend': is_weekend, 'is_tiaoxiu': is_tiaoxiu,
            'base_daily': round(base_daily), 'weather_coeff': round(weather_coeff, 3),
            'inertia_coeff': round(inertia_coeff, 3), 'holiday_coeff': round(holiday_coeff, 3),
            'weekly_decay': round(decay, 4), 'weather_desc': weather_desc,
            'holiday_label': holiday_label, 'forecast': int(final),
            'confidence_low': int(max(0, adjusted - 1.5 * std)),
            'confidence_high': int(adjusted + 1.5 * std),
        })
    return results


def main():
    actuals_path = os.environ.get('ACTUALS_JSON', '').strip() or \
        r'C:\Users\TBSG\.qoderwork\workspace\mvg1a4pbc2196wre\xinqin_daily_full.json'
    wx_path = os.environ.get('WEATHER_PATH', '').strip() or os.path.join(HERE, 'weather_huizhou.json')
    horizon = int(os.environ.get('FC_HORIZON', '16'))
    hist_days = int(os.environ.get('HIST_DAYS', '21'))

    df = load_actuals(actuals_path)
    clean = df[~df['is_anomaly']].copy()
    last_actual = clean['date'].max()
    today = date.today()
    wx = load_weather_daily(wx_path)

    # 未来：基准截至最后实际日，预测 今天..今天+horizon-1
    future = generate_series(clean, wx, today, horizon, asof_last=last_actual)

    # 回测历史：对 [today-hist_days .. 昨天] 每一天，只用该日之前的数据预测该日
    history = []
    for k in range(hist_days, 0, -1):
        d = today - timedelta(days=k)
        if d > last_actual:
            # 该日尚无实际数据，无法回测校准，用"截至最后实际日"的模型给一个值
            pred = generate_series(clean, wx, d, 1, asof_last=last_actual)[0]
        else:
            pred = generate_series(clean, wx, d, 1, asof_last=d - timedelta(days=1))[0]
        pred['actual'] = int(clean[clean['date'] == d]['orders'].iloc[0]) if (clean['date'] == d).any() else None
        history.append(pred)

    out = {
        'meta': {
            'store': STORE_NAME, 'engine': 'lite-xinqin',
            'data_range': f"{clean['date'].min()} ~ {clean['date'].max()}",
            'actual_days': int(len(clean)),
            'forecast_start': str(today), 'forecast_days': horizon,
            'history_start': str(today - timedelta(days=hist_days)),
            'history_days': hist_days,
            'generated_at': datetime.now().isoformat(),
        },
        'future': future, 'history': history,
    }
    with open(os.path.join(HERE, 'forecast_result_xinqin.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)

    print(f"[xinqin-lite] 基准 {clean['date'].min()}~{last_actual} ({len(clean)}天)  周衰减 {future[0]['weekly_decay']}")
    print(f"[xinqin-lite] 回测历史 {out['meta']['history_start']}~昨天:")
    for r in history:
        print(f"  {r['date']} {r['dow_name']}: 预测{r['forecast']:>4}  实际{str(r['actual']):>4}  基准{r['base_daily']:>4}  天气{r['weather_coeff']:.2f}  {r['weather_desc']}")
    print(f"[xinqin-lite] 未来 {today}~{today + timedelta(days=horizon-1)}:")
    for r in future:
        hl = f" [{r['holiday_label']}]" if r['holiday_label'] else ''
        print(f"  {r['date']} {r['dow_name']}: {r['forecast']:>4} ({r['confidence_low']}-{r['confidence_high']}) 基准{r['base_daily']:>4} 天气{r['weather_coeff']:.2f}  {r['weather_desc']}{hl}")
    print('[xinqin-lite] 已写出 forecast_result_xinqin.json')


if __name__ == '__main__':
    main()
