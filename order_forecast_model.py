#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
订单预估分析模型 v3.0
基于拣货单数据 + 天气数据，多因素分析生成15天订单预估
v3改进：集成4店降雨占比分析结论（工作日/周末分化、大雨抑制、连续降雨累积效应）
"""
import pandas as pd
import numpy as np
import json
import os
import glob
from datetime import datetime, timedelta
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

# ===== CONFIG =====
def _resolve_excel_path():
    """优先环境变量 EXCEL_PATH；否则取 Download 目录最新的 拣货单导出*.xlsx（按修改时间）。"""
    env_path = os.environ.get('EXCEL_PATH', '').strip()
    if env_path and os.path.exists(env_path):
        return env_path
    cand = sorted(glob.glob(r'D:\TBSG\Download\拣货单导出*.xlsx'),
                  key=lambda p: os.path.getmtime(p), reverse=True)
    if cand:
        return cand[0]
    return r'D:\TBSG\Download\拣货单导出-20260928.xlsx'

EXCEL_PATH = _resolve_excel_path()
WEATHER_PATH = 'weather_xian.json'
STORE_NAME = '成山农场(龙湖天街店)'
# FORECAST_DAYS / forecast_start 可被环境变量覆盖（每周刷新锚定次日）
FORECAST_DAYS = int(os.environ.get('FC_DAYS', '15'))  # 默认15天
FC_START_OVERRIDE = os.environ.get('FC_START_DATE', '').strip() or None  # 'YYYY-MM-DD'
SLOT_MINUTES = 30
OPEN_HOUR = 7
CLOSE_HOUR = 22

KEY_DATES = {
    '2026-09-17': {'type': 'anomaly', 'label': '鸡蛋30枚缺货'},
}
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
# 调休/单休补班日：虽是周末但实际为工作日，用工作日基准+时段占比
TIAOXIU_WORKDAYS = {
    '2026-09-20': '国庆节调休补班',  # 周日→工作日
    '2026-10-10': '国庆节调休补班',  # 周六→工作日
}
SCHOOL_OPEN_DATE = '2026-09-01'

# ===== DATA LOADING =====
def load_orders(path):
    """新全年导出：真实数据在'拣货单明细'sheet(item级,多行/单)，首个sheet'拣货单列表'仅有拣货单号列，
    且明细无'履约方式'列。此处流式读取明细、过滤目标门店、按拣货单号去重到订单级。
    履约方式缺失时全部视为即时单(该字段不影响预估总量)。"""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheet = '拣货单明细' if '拣货单明细' in wb.sheetnames else wb.sheetnames[0]
    ws = wb[sheet]
    it = ws.iter_rows(values_only=True)
    hdr = list(next(it))

    def col(name):
        return hdr.index(name) if name in hdr else None

    c_gno = col('拣货单号'); c_store = col('门店名称')
    c_status = col('拣货单状态'); c_disp = col('拣货下发时间')
    c_done = col('完成拣货时间'); c_cancel = col('拣货取消时间')
    c_perf = col('履约方式')
    if c_gno is None or c_disp is None:
        raise ValueError(f'明细sheet缺少必需列(拣货单号/拣货下发时间): sheet={sheet}')

    orders = {}  # 拣货单号 -> dict
    for row in it:
        if c_store is not None:
            store = row[c_store]
            if store is None or '龙湖天街' not in str(store):
                continue
        gno = row[c_gno]
        if gno is None or gno in orders:
            continue  # 订单级去重：保留首个item
        orders[gno] = {
            '拣货单号': gno,
            '拣货下发时间': row[c_disp],
            '完成拣货时间': row[c_done] if c_done is not None else None,
            '拣货取消时间': row[c_cancel] if c_cancel is not None else None,
            '拣货单状态': row[c_status] if c_status is not None else '',
            '履约方式': (row[c_perf] if c_perf is not None else '即时单'),
            '门店名称': STORE_NAME,
        }
    wb.close()

    df = pd.DataFrame(list(orders.values()))
    df['拣货下发时间'] = pd.to_datetime(df['拣货下发时间'])
    df['完成拣货时间'] = pd.to_datetime(df['完成拣货时间'], errors='coerce')
    df['拣货取消时间'] = pd.to_datetime(df['拣货取消时间'], errors='coerce')
    df['is_cancelled'] = df['拣货单状态'].astype(str).str.contains('取消', na=False)
    df['is_valid'] = ~df['is_cancelled']
    df['date'] = df['拣货下发时间'].dt.date
    df['hour'] = df['拣货下发时间'].dt.hour
    df['minute'] = df['拣货下发时间'].dt.minute
    df['slot'] = df['hour'] * 2 + df['minute'] // SLOT_MINUTES
    df['dow'] = df['拣货下发时间'].dt.dayofweek
    df['is_weekend'] = df['dow'] >= 5
    df['is_instant'] = df['履约方式'].astype(str) == '即时单'
    df['warehouse_t'] = (df['完成拣货时间'] - df['拣货下发时间']).dt.total_seconds() / 60
    df.loc[~df['is_instant'] | df['is_cancelled'], 'warehouse_t'] = np.nan
    return df

def load_weather(path):
    with open(path) as f:
        raw = json.load(f)
    h = raw['hourly']
    records = []
    for i, t in enumerate(h['time']):
        records.append({
            'datetime': pd.Timestamp(t),
            'temp': h['temperature_2m'][i],
            'precip': h['precipitation'][i],
            'weathercode': h['weathercode'][i],
            'wind': h['windspeed_10m'][i]
        })
    wdf = pd.DataFrame(records)
    wdf['date'] = wdf['datetime'].dt.date
    wdf['hour'] = wdf['datetime'].dt.hour
    wdf['slot'] = wdf['hour'] * 2
    return wdf

def classify_rain(precip_mm):
    if precip_mm is None or precip_mm < 0.1:
        return 'none'
    elif precip_mm < 2.5:
        return 'light'
    elif precip_mm < 8.0:
        return 'moderate'
    return 'heavy'

def classify_temp(temp_c):
    if temp_c is None:
        return 'normal'
    elif temp_c >= 38:
        return 'extreme_hot'
    elif temp_c >= 36:
        return 'hot'
    elif temp_c <= 0:
        return 'cold'
    return 'normal'

RAIN_LABEL = {'none': '晴', 'light': '小雨', 'moderate': '中雨', 'heavy': '大雨'}
DOW_NAMES = ['周一','周二','周三','周四','周五','周六','周日']

def recency_weighted(daily_counts):
    """按 新周0.5/中周0.3/远周0.2 对同星期日订单(索引按日期升序)加权，让当前水平主导、兼顾平滑。"""
    vals = list(daily_counts.values)
    n = len(vals)
    if n == 0:
        return 0.0
    if n >= 3:
        take, w = vals[-3:], [0.2, 0.3, 0.5]
    elif n == 2:
        take, w = vals, [0.4, 0.6]
    else:
        take, w = vals, [1.0]
    return sum(t * wi for t, wi in zip(take, w)) / sum(w)

# ===== FACTOR 1: BASE PATTERN =====
def compute_base_pattern(orders_df):
    df = orders_df[orders_df['is_valid']].copy()
    daily_orders = df.groupby('date').size()
    mean_daily = daily_orders.mean()
    std_daily = daily_orders.std()
    anomaly_dates = set(daily_orders[daily_orders > mean_daily + 2 * std_daily].index)
    df_clean = df[~df['date'].isin(anomaly_dates)]
    
    slot_dow = df_clean.groupby(['slot', 'dow']).size().reset_index(name='count')
    slot_dow_days = df_clean.groupby(['slot', 'dow'])['date'].nunique().reset_index(name='days')
    base = slot_dow.merge(slot_dow_days, on=['slot', 'dow'])
    base['avg_per_slot'] = base['count'] / base['days']
    
    dow_daily = df_clean.groupby(['date', 'dow']).size().reset_index(name='daily_total')
    dow_avg = dow_daily.groupby('dow')['daily_total'].agg(['mean', 'median', 'std', 'count']).reset_index()
    dow_avg.columns = ['dow', 'mean', 'median', 'std', 'sample_days']
    return base, dow_avg, anomaly_dates

# ===== FACTOR 2: WEATHER IMPACT =====
def compute_weather_impact(orders_df, weather_df):
    df = orders_df[orders_df['is_valid']].copy()
    df['weather_hour'] = df['slot'] // 2
    weather_hourly = weather_df[['date', 'hour', 'temp', 'precip', 'weathercode', 'wind']].copy()
    weather_hourly = weather_hourly.rename(columns={'hour': 'weather_hour'})
    merged = df.merge(weather_hourly, on=['date', 'weather_hour'], how='left')
    merged['rain_level'] = merged['precip'].apply(classify_rain)
    merged['temp_level'] = merged['temp'].apply(classify_temp)
    
    slot_daily = merged.groupby(['date', 'slot']).agg(
        orders=('拣货单号', 'size'),
        rain_level=('rain_level', lambda x: x.mode().iloc[0] if len(x) > 0 else 'none'),
        temp=('temp', 'mean'),
        precip=('precip', 'max'),
        temp_level=('temp_level', lambda x: x.mode().iloc[0] if len(x) > 0 else 'normal'),
        dow=('dow', 'first'),
        is_weekend=('is_weekend', 'first')
    ).reset_index()
    slot_daily = slot_daily[slot_daily['temp'].notna()]
    
    rain_impact = {}
    for slot in range(OPEN_HOUR * 2, CLOSE_HOUR * 2):
        slot_data = slot_daily[slot_daily['slot'] == slot]
        if len(slot_data) < 3:
            continue
        baseline = slot_data[slot_data['rain_level'] == 'none']['orders'].median()
        if baseline == 0 or pd.isna(baseline):
            continue
        for level in ['light', 'moderate', 'heavy']:
            level_data = slot_data[slot_data['rain_level'] == level]['orders']
            if len(level_data) >= 2:
                coeff = level_data.median() / baseline
                rain_impact[(slot, level)] = {
                    'coefficient': round(coeff, 3),
                    'sample_size': len(level_data),
                    'baseline_median': round(baseline, 1),
                    'rain_median': round(level_data.median(), 1)
                }
    
    temp_impact = {}
    for slot in range(OPEN_HOUR * 2, CLOSE_HOUR * 2):
        slot_data = slot_daily[slot_daily['slot'] == slot]
        if len(slot_data) < 3:
            continue
        normal = slot_data[slot_data['temp_level'] == 'normal']['orders'].median()
        if normal == 0 or pd.isna(normal):
            continue
        for level in ['hot', 'extreme_hot']:
            level_data = slot_data[slot_data['temp_level'] == level]['orders']
            if len(level_data) >= 2:
                coeff = level_data.median() / normal
                temp_impact[(slot, level)] = {
                    'coefficient': round(coeff, 3),
                    'sample_size': len(level_data),
                    'normal_median': round(normal, 1),
                    'level_median': round(level_data.median(), 1)
                }
    
    # 日级降雨汇总
    daily_rain_summary = {}
    for level in ['none', 'light', 'moderate', 'heavy']:
        for is_we in [False, True]:
            subset = slot_daily[(slot_daily['rain_level'] == level) & (slot_daily['is_weekend'] == is_we)]
            if len(subset) >= 2:
                daily_rain_summary[(level, is_we)] = {
                    'mean_orders': round(subset['orders'].sum().mean() if len(subset) > 0 else 0, 1),
                    'sample_days': len(subset)
                }
    
    return rain_impact, temp_impact, daily_rain_summary, slot_daily

# ===== FACTOR 3: SCHOOL OPENING =====
def compute_school_impact(orders_df):
    df = orders_df[orders_df['is_valid']].copy()
    school_date = pd.to_datetime(SCHOOL_OPEN_DATE).date()
    pre_start, pre_end = school_date - timedelta(days=14), school_date - timedelta(days=1)
    post_start, post_end = school_date, school_date + timedelta(days=14)
    pre = df[(df['date'] >= pre_start) & (df['date'] <= pre_end)]
    post = df[(df['date'] >= post_start) & (df['date'] <= post_end)]
    pre_daily = pre.groupby('date').size()
    post_daily = post.groupby('date').size()
    
    pre_slot = pre.groupby('slot').size() / max(pre['date'].nunique(), 1)
    post_slot = post.groupby('slot').size() / max(post['date'].nunique(), 1)
    
    slot_change = {}
    for slot in range(OPEN_HOUR * 2, CLOSE_HOUR * 2):
        pv = pre_slot.get(slot, 0)
        ptv = post_slot.get(slot, 0)
        if pv > 0:
            slot_change[str(slot)] = round((ptv / pv - 1) * 100, 1)
    
    return {
        'pre_mean': round(pre_daily.mean(), 1) if len(pre_daily) > 0 else 0,
        'post_mean': round(post_daily.mean(), 1) if len(post_daily) > 0 else 0,
        'change_pct': round((post_daily.mean() / pre_daily.mean() - 1) * 100, 1) if len(pre_daily) > 0 and pre_daily.mean() > 0 else 0,
        'slot_change': slot_change
    }

# ===== FACTOR 4: HOLIDAY / WEEKEND =====
def compute_holiday_pattern(orders_df):
    df = orders_df[orders_df['is_valid']].copy()
    we_daily = df[df['is_weekend']].groupby('date').size()
    wd_daily = df[~df['is_weekend']].groupby('date').size()
    return {
        'weekend_mean': round(we_daily.mean(), 1),
        'weekday_mean': round(wd_daily.mean(), 1),
        'weekend_weekday_ratio': round(we_daily.mean() / wd_daily.mean(), 3),
    }

# ===== FACTOR 5: PROMOTION DETECTION =====
def detect_promotions(orders_df):
    df = orders_df[orders_df['is_valid']].copy()
    daily = df.groupby('date').size().reset_index(name='orders')
    daily['dow'] = pd.to_datetime(daily['date']).dt.dayofweek
    dow_mean = daily.groupby('dow')['orders'].transform('mean')
    dow_std = daily.groupby('dow')['orders'].transform('std')
    daily['zscore'] = (daily['orders'] - dow_mean) / dow_std
    daily['is_anomaly'] = daily['zscore'] > 1.5
    
    promotions = []
    for _, row in daily[daily['is_anomaly']].iterrows():
        promotions.append({
            'date': str(row['date']),
            'orders': int(row['orders']),
            'zscore': round(row['zscore'], 2),
            'dow': int(row['dow'])
        })
    
    promo_dates = set(daily[daily['is_anomaly']]['date'])
    normal_dates = set(daily[~daily['is_anomaly']]['date'])
    promo_slot = df[df['date'].isin(promo_dates)].groupby('slot').size() / max(len(promo_dates), 1)
    normal_slot = df[df['date'].isin(normal_dates)].groupby('slot').size() / max(len(normal_dates), 1)
    slot_promo_ratio = {}
    for slot in range(OPEN_HOUR * 2, CLOSE_HOUR * 2):
        n = normal_slot.get(slot, 0)
        p = promo_slot.get(slot, 0)
        if n > 0:
            slot_promo_ratio[str(slot)] = round(p / n, 3)
    
    return promotions, slot_promo_ratio

# ===== FACTOR 6: FULFILLMENT =====
def compute_fulfillment_impact(orders_df):
    df = orders_df[(orders_df['is_valid']) & (orders_df['is_instant'])].copy()
    df = df[df['warehouse_t'].notna() & (df['warehouse_t'] > 0) & (df['warehouse_t'] < 120)]
    
    slot_t = df.groupby('slot')['warehouse_t'].agg(['mean', 'median', 'count']).reset_index()
    slot_t.columns = ['slot', 'mean_t', 'median_t', 'sample']
    
    daily_slot_t = df.groupby(['date', 'slot']).agg(
        mean_t=('warehouse_t', 'mean'),
        orders=('拣货单号', 'size')
    ).reset_index()
    corr = daily_slot_t[['mean_t', 'orders']].corr().iloc[0, 1] if len(daily_slot_t) > 10 else 0
    
    high_t = daily_slot_t[daily_slot_t['mean_t'] > 15]
    low_t = daily_slot_t[daily_slot_t['mean_t'] <= 10]
    
    return {
        'overall_mean_t': round(df['warehouse_t'].mean(), 1),
        'overall_median_t': round(df['warehouse_t'].median(), 1),
        'slot_stats': slot_t.to_dict('records'),
        'correlation': round(corr, 3),
        'high_t_avg_orders': round(high_t['orders'].mean(), 1) if len(high_t) > 0 else 0,
        'low_t_avg_orders': round(low_t['orders'].mean(), 1) if len(low_t) > 0 else 0,
        't_impact_ratio': round(high_t['orders'].mean() / low_t['orders'].mean(), 3) if len(high_t) > 0 and len(low_t) > 0 and low_t['orders'].mean() > 0 else 1.0
    }

# ===== FORECAST MODEL v2 =====
def generate_forecast(base_pattern, dow_avg, weather_df, rain_impact, temp_impact,
                      school_impact, holiday_pattern, promotions, slot_promo_ratio,
                      fulfillment, orders_df, anomaly_dates=None):
    df_valid = orders_df[orders_df['is_valid']]
    last_date = orders_df['date'].max()
    if FC_START_OVERRIDE:
        forecast_start = datetime.strptime(FC_START_OVERRIDE, '%Y-%m-%d').date()
    else:
        forecast_start = last_date + timedelta(days=1)
    weather_future = weather_df[weather_df['date'] >= forecast_start].copy()
    
    if anomaly_dates is None:
        anomaly_dates = set()
    
    # === 近期基准（最近3周，按星期，排除大促异常日） ===
    recent_3w = df_valid[df_valid['date'] >= last_date - timedelta(days=20)]
    recent_3w_clean = recent_3w[~recent_3w['date'].isin(anomaly_dates)]
    recent_3w_copy = recent_3w_clean.copy()
    recent_3w_copy['dow_col'] = recent_3w_copy['date'].apply(lambda d: d.weekday())
    
    recent_by_dow = {}
    for dow in range(7):
        dow_dates = recent_3w_copy[recent_3w_copy['dow_col'] == dow]
        daily_counts = dow_dates.groupby('date').size()
        if len(daily_counts) >= 1:
            recent_by_dow[dow] = {
                'mean': daily_counts.mean(),
                'median': daily_counts.median(),
                'weighted': recency_weighted(daily_counts),
                'values': list(daily_counts.values),
                'dates': [str(d) for d in daily_counts.index],
                'std': daily_counts.std() if len(daily_counts) >= 2 else 40
            }
    
    # === 周级趋势 ===
    weekly_means = []
    for w in range(4):
        w_start = last_date - timedelta(days=6 + w * 7)
        w_end = last_date - timedelta(days=w * 7)
        w_data = df_valid[(df_valid['date'] >= w_start) & (df_valid['date'] <= w_end)]
        if len(w_data) > 0:
            w_daily = w_data.groupby('date').size()
            weekly_means.append(w_daily.mean())
    
    week_wow = weekly_means[0] / weekly_means[1] if len(weekly_means) >= 2 and weekly_means[1] > 0 else 1.0
    
    if len(weekly_means) >= 3:
        # weekly_means[0]=最近周, 索引越大越早。polyfit 需按时间正序(早→晚)，
        # 否则下降序列会得到正斜率并把未来周错误放大。反转后阻尼0.5并限幅。
        x = np.arange(len(weekly_means))
        y_chrono = np.array(weekly_means)[::-1]   # 早→晚(时间正序)
        slope = np.polyfit(x, y_chrono, 1)[0]
        weekly_decay_rate = 0.5 * slope / np.mean(y_chrono)
        weekly_decay_rate = float(np.clip(weekly_decay_rate, -0.05, 0.02))
    else:
        weekly_decay_rate = 0
    
    # 按星期趋势
    dow_trend = {}
    for dow in range(7):
        r = df_valid[(df_valid['date'] >= last_date - timedelta(days=13)) & 
                     (pd.to_datetime(df_valid['date']).dt.dayofweek == dow)]
        p = df_valid[(df_valid['date'] >= last_date - timedelta(days=27)) & 
                     (df_valid['date'] < last_date - timedelta(days=13)) &
                     (pd.to_datetime(df_valid['date']).dt.dayofweek == dow)]
        r_m = r.groupby('date').size().mean() if len(r) > 0 else 0
        p_m = p.groupby('date').size().mean() if len(p) > 0 else 0
        dow_trend[dow] = r_m / p_m if p_m > 0 else 1.0
    
    trend_ratio = (df_valid[df_valid['date'] >= last_date - timedelta(days=13)].groupby('date').size().mean() /
                   df_valid[(df_valid['date'] >= last_date - timedelta(days=27)) & 
                            (df_valid['date'] < last_date - timedelta(days=13))].groupby('date').size().mean()) \
                  if df_valid[(df_valid['date'] >= last_date - timedelta(days=27)) & 
                              (df_valid['date'] < last_date - timedelta(days=13))].groupby('date').size().mean() > 0 else 1.0
    
    # 时段级近期基准
    recent_slot_dow = recent_3w_copy.groupby(['slot', 'dow_col']).size().reset_index(name='count')
    recent_slot_days = recent_3w_copy.groupby(['slot', 'dow_col'])['date'].nunique().reset_index(name='days')
    recent_slot_dow = recent_slot_dow.merge(recent_slot_days, on=['slot', 'dow_col'])
    recent_slot_dow['avg'] = recent_slot_dow['count'] / recent_slot_dow['days']
    
    # === 历史连续降雨追踪 ===
    # 合并历史+未来天气，计算连续降雨天数
    weather_all = weather_df.copy()
    weather_all['rainy'] = weather_all.groupby('date')['precip'].transform('sum') > 2
    weather_all_daily = weather_all.groupby('date').agg(
        total_rain=('precip', 'sum'), max_rain=('precip', 'max')
    ).reset_index()
    weather_all_daily['rainy'] = weather_all_daily['total_rain'] > 2
    weather_all_daily = weather_all_daily.sort_values('date').reset_index(drop=True)
    
    consec = []
    c = 0
    for _, row in weather_all_daily.iterrows():
        c = c + 1 if row['rainy'] else 0
        consec.append(c)
    weather_all_daily['consec_rain'] = consec
    
    # 历史连续降雨系数（4店占比分析结论）
    # Day 0: baseline, Day 1: -0.3%, Day 2: +5.3%, Day 3: +12.6%, Day 4: +9.3%, Day 5: +16.1%
    CONSEC_RAIN_COEFF = {0: 1.0, 1: 1.00, 2: 1.05, 3: 1.13, 4: 1.09, 5: 1.16}
    
    # 工作日/周末降雨分化（4店占比分析）
    # 仅用于非连雨的孤立降雨日；连雨日用连雨累积系数
    # 整体降雨增幅+5.2%（4店均值），周末略低
    ISOLATED_RAIN_COEFF_WEEKDAY = 1.08   # 工作日孤立降雨+8%
    ISOLATED_RAIN_COEFF_WEEKEND = 0.96   # 周末孤立降雨-4%（周末降雨反而抑制）
    
    # 大雨抑制效应（4店占比分析：大雨日-14.3%）
    HEAVY_RAIN_SUPPRESSION = 0.857
    
    # 时段级降雨占比调整（4店slot占比分析）
    # 正值=降雨天该时段占比上升，负值=下降
    SLOT_RAIN_PROPORTION = {
        15: -0.045, 16: -0.046, 17: -0.044,  # 07:30-08:30 早高峰下降
        18: 0.017, 19: -0.027, 20: -0.071, 21: -0.037,  # 上午混合
        22: 0.003, 23: -0.070, 24: -0.030, 25: -0.058,  # 午间下降
        26: 0.012, 27: 0.032, 28: 0.017, 29: -0.021,    # 下午早段混合
        30: 0.045, 31: 0.084, 32: 0.016, 33: 0.042,     # 下午茶时段上升
        34: 0.006, 35: 0.049, 36: -0.004, 37: 0.044,    # 晚餐时段上升
        38: -0.027, 39: -0.079, 40: 0.179, 41: -0.085   # 晚间混合
    }
    
    # === 需求惯性系数（前一天高需求→次日微降） ===
    # 分析：当一天订单>同星期均值*1.2时，次日通常回落约5%
    df_valid_daily = df_valid.groupby('date').size()
    df_valid_dow_mean = df_valid.groupby(df_valid['date'].apply(lambda d: d.weekday())).size()
    # 按星期归一化
    high_demand_dates = []
    for d, cnt in df_valid_daily.items():
        dow = d.weekday()
        dow_mean = df_valid[df_valid['date'].apply(lambda dd: dd.weekday()) == dow].groupby('date').size().mean()
        if cnt > dow_mean * 1.2:
            high_demand_dates.append(d)
    
    # 高需求日的次日效应
    inertia_suppression = 0.95  # 高需求日次日下降约5%
    
    forecast_results = []
    prev_day_high_demand = False  # 追踪前一天是否高需求
    
    for day_offset in range(FORECAST_DAYS):
        target_date = forecast_start + timedelta(days=day_offset)
        target_dow = target_date.weekday()
        is_weekend = target_dow >= 5
        date_str = str(target_date)
        week_ahead = day_offset // 7
        
        # 调休补班日处理：周末→视为工作日
        is_tiaoxiu = date_str in TIAOXIU_WORKDAYS
        if is_tiaoxiu:
            is_weekend = False  # 视为工作日
            # 周日补班→用周四(3)基准，周六补班→用周五(4)基准
            effective_dow = 3 if target_dow == 6 else 4
        else:
            effective_dow = target_dow
        
        # 1. 基准（调休日用effective_dow查基准）
        lookup_dow = effective_dow if is_tiaoxiu else target_dow
        if lookup_dow in recent_by_dow:
            base_daily = recent_by_dow[lookup_dow]['weighted']
        else:
            dow_row = dow_avg[dow_avg['dow'] == lookup_dow]
            base_daily = dow_row['median'].iloc[0] if len(dow_row) > 0 else 350
        
        # 2. 周级衰减外推
        future_decay = (1 + weekly_decay_rate) ** max(0, week_ahead)
        adjusted = base_daily * future_decay
        
        # 3. 天气（连续降雨 + 降雨强度 + 温度）
        day_weather = weather_future[weather_future['date'] == target_date]
        weather_coeff = 1.0
        weather_desc = '晴'
        weather_detail = {}
        
        # 获取连续降雨天数
        consec_row = weather_all_daily[weather_all_daily['date'] == target_date]
        consec_days = int(consec_row['consec_rain'].iloc[0]) if len(consec_row) > 0 else 0
        is_rainy = consec_days > 0
        
        if len(day_weather) > 0:
            max_precip = day_weather['precip'].max()
            max_temp = day_weather['temp'].max()
            min_temp = day_weather['temp'].min()
            total_precip = day_weather['precip'].sum()
            rain_level = classify_rain(max_precip if max_precip else 0)
            temp_level = classify_temp(max_temp)
            
            weather_detail = {
                'max_temp': round(max_temp, 1) if max_temp else None,
                'min_temp': round(min_temp, 1) if min_temp else None,
                'max_precip': round(max_precip, 1) if max_precip else 0,
                'total_precip': round(total_precip, 1),
                'rain_level': rain_level,
                'temp_level': temp_level,
                'consec_rain_days': consec_days
            }
            
            # === 降雨系数（v3: 4店占比分析结论） ===
            # 核心逻辑：连续降雨系数为主驱动（已含累积效应），
            # 仅非连雨的孤立降雨日才用星期分化系数
            if is_rainy:
                # 连续降雨天：用连雨累积系数（已包含星期平均效应）
                consec_key = min(consec_days, 5)
                consec_coeff = CONSEC_RAIN_COEFF.get(consec_key, 1.16)
                weather_coeff *= consec_coeff
            elif rain_level != 'none':
                # 孤立降雨（非连续）：用简化工作日/周末系数
                if is_weekend:
                    weather_coeff *= ISOLATED_RAIN_COEFF_WEEKEND
                else:
                    weather_coeff *= ISOLATED_RAIN_COEFF_WEEKDAY
            
            # 大雨抑制效应（任何降雨天，≥8mm触发）
            if rain_level == 'heavy':
                weather_coeff *= HEAVY_RAIN_SUPPRESSION
                weather_desc = f'大雨(连续{consec_days}天)'
            elif rain_level == 'moderate':
                weather_desc = f'中雨(连续{consec_days}天)'
            elif rain_level == 'light':
                weather_desc = f'小雨(连续{consec_days}天)' if consec_days > 0 else '小雨'
            
            if consec_days == 0 and rain_level != 'none':
                weather_desc = RAIN_LABEL.get(rain_level, '晴')
            
            # 温度影响
            if temp_level == 'extreme_hot':
                weather_coeff *= 1.10
                weather_desc += '+极端高温'
            elif temp_level == 'hot':
                weather_coeff *= 1.05
                weather_desc += '+高温'
            elif temp_level == 'cold':
                weather_coeff *= 1.08
                weather_desc += '+低温'
        
        adjusted *= weather_coeff
        
        # 4. 需求惯性（高需求日次日微降）
        inertia_coeff = 1.0
        if prev_day_high_demand:
            inertia_coeff = inertia_suppression
            adjusted *= inertia_coeff
        
        # 5. 节假日 + 调休
        holiday_coeff = 1.0
        holiday_label = ''
        if date_str in HOLIDAYS:
            h = HOLIDAYS[date_str]
            holiday_coeff = 1.25 if h['type'] == 'travel' else 1.15
            holiday_label = h['label']
        if is_tiaoxiu:
            holiday_label = TIAOXIU_WORKDAYS[date_str]
        adjusted *= holiday_coeff
        
        # 6. 手动关键日
        if date_str in KEY_DATES:
            kd = KEY_DATES[date_str]
            if kd['type'] == 'promotion':
                adjusted *= 1.25
            elif kd['type'] == 'anomaly':
                pass  # 异常日不做系数调整，仅标记
            holiday_label += f" [{kd['label']}]"
        
        # 判断当日是否高需求（用于次日惯性）
        is_high_demand = adjusted > base_daily * 1.15
        prev_day_high_demand = is_high_demand
        
        # 7. 时段分布（调休日用effective_dow的时段占比）
        slot_lookup_dow = effective_dow if is_tiaoxiu else target_dow
        slot_forecast = {}
        for slot in range(OPEN_HOUR * 2, CLOSE_HOUR * 2):
            slot_row = recent_slot_dow[
                (recent_slot_dow['slot'] == slot) & (recent_slot_dow['dow_col'] == slot_lookup_dow)
            ]
            if len(slot_row) > 0:
                slot_base = slot_row['avg'].iloc[0]
            else:
                base_slot = base_pattern[
                    (base_pattern['slot'] == slot) & (base_pattern['dow'] == slot_lookup_dow)
                ]
                slot_base = base_slot['avg_per_slot'].iloc[0] if len(base_slot) > 0 else 0
            
            slot_weather_coeff = 1.0
            if len(day_weather) > 0:
                slot_hour = slot // 2
                slot_wx = day_weather[day_weather['hour'] == slot_hour]
                if len(slot_wx) > 0:
                    s_precip = slot_wx['precip'].iloc[0]
                    s_temp = slot_wx['temp'].iloc[0]
                    s_rain = classify_rain(s_precip if s_precip else 0)
                    s_temp_l = classify_temp(s_temp)
                    
                    # 降雨天使用时段占比调整（v3: 4店分析结论）
                    if s_rain != 'none' and slot in SLOT_RAIN_PROPORTION:
                        slot_weather_coeff *= (1 + SLOT_RAIN_PROPORTION[slot])
                    
                    # 温度影响保留历史系数
                    if (slot, s_temp_l) in temp_impact and s_temp_l != 'normal':
                        slot_weather_coeff *= temp_impact[(slot, s_temp_l)]['coefficient']
            
            slot_forecast[str(slot)] = round(slot_base * future_decay * slot_weather_coeff, 1)
        
        total_slot = sum(slot_forecast.values())
        if total_slot > 0 and adjusted > 0:
            scale = adjusted / total_slot
            slot_forecast = {k: round(v * scale, 1) for k, v in slot_forecast.items()}
        
        # 置信区间
        if lookup_dow in recent_by_dow:
            std = recent_by_dow[lookup_dow].get('std', 40)
        else:
            dow_row = dow_avg[dow_avg['dow'] == lookup_dow]
            std = dow_row['std'].iloc[0] if len(dow_row) > 0 else 40
        
        forecast_results.append({
            'date': date_str,
            'dow': target_dow,
            'dow_name': DOW_NAMES[target_dow],
            'is_weekend': is_weekend,
            'is_tiaoxiu': is_tiaoxiu,
            'base_daily': round(base_daily, 0),
            'dow_trend': round(dow_trend.get(target_dow, trend_ratio), 3),
            'future_decay': round(future_decay, 3),
            'weather_coeff': round(weather_coeff, 3),
            'inertia_coeff': round(inertia_coeff, 3),
            'holiday_coeff': round(holiday_coeff, 3),
            'weather_desc': weather_desc,
            'weather_detail': weather_detail,
            'holiday_label': holiday_label,
            'forecast_total': round(adjusted, 0),
            'slot_forecast': slot_forecast,
            'confidence_low': round(max(0, adjusted - 1.5 * std), 0),
            'confidence_high': round(adjusted + 1.5 * std, 0)
        })
    
    return forecast_results, {
        'trend_ratio': round(trend_ratio, 3),
        'week_wow': round(week_wow, 3),
        'weekly_decay_rate': round(weekly_decay_rate, 4),
        'dow_trend': {str(k): round(v, 3) for k, v in dow_trend.items()},
        'weekly_means': [round(w, 1) for w in weekly_means],
        'recent_by_dow': {str(k): {'mean': round(v['mean'],1), 'dates': v['dates']} for k, v in recent_by_dow.items()}
    }

# ===== BACKTEST =====
def backtest(base_pattern, dow_avg, weather_df, rain_impact, temp_impact, orders_df):
    df_valid = orders_df[orders_df['is_valid']]
    last_date = orders_df['date'].max()
    backtest_start = last_date - timedelta(days=13)
    backtest_dates = [backtest_start + timedelta(days=i) for i in range(14)]
    
    # 回测基准：backtest前3周
    bt_base_start = backtest_start - timedelta(days=20)
    bt_base = df_valid[(df_valid['date'] >= bt_base_start) & (df_valid['date'] < backtest_start)]
    bt_base_copy = bt_base.copy()
    bt_base_copy['dow_col'] = bt_base_copy['date'].apply(lambda d: d.weekday())
    
    recent_by_dow_bt = {}
    for dow in range(7):
        d = bt_base_copy[bt_base_copy['dow_col'] == dow]
        dc = d.groupby('date').size()
        if len(dc) >= 1:
            recent_by_dow_bt[dow] = {'mean': dc.mean(), 'median': dc.median(), 'weighted': recency_weighted(dc), 'std': dc.std() if len(dc) >= 2 else 40}
    
    # 周衰减
    bt_weekly = []
    for w in range(4):
        ws = backtest_start - timedelta(days=6 + w * 7)
        we = backtest_start - timedelta(days=w * 7)
        wd = df_valid[(df_valid['date'] >= ws) & (df_valid['date'] < we)]
        if len(wd) > 0:
            bt_weekly.append(wd.groupby('date').size().mean())
    
    if len(bt_weekly) >= 3:
        x = np.arange(len(bt_weekly))
        y_bt = np.array(bt_weekly)[::-1]   # 早→晚(时间正序)
        slope = np.polyfit(x, y_bt, 1)[0]
        bt_decay = 0.5 * slope / np.mean(y_bt)
        bt_decay = float(np.clip(bt_decay, -0.05, 0.02))
    else:
        bt_decay = 0
    
    # 连续降雨追踪（v3: 4店占比分析结论）
    CONSEC_RAIN_COEFF = {0: 1.0, 1: 1.00, 2: 1.05, 3: 1.13, 4: 1.09, 5: 1.16}
    ISOLATED_RAIN_COEFF_WEEKDAY = 1.08
    ISOLATED_RAIN_COEFF_WEEKEND = 0.96
    HEAVY_RAIN_SUPPRESSION = 0.857
    wx_daily = weather_df.groupby('date').agg(total_rain=('precip', 'sum')).reset_index()
    wx_daily['rainy'] = wx_daily['total_rain'] > 2
    wx_daily = wx_daily.sort_values('date').reset_index(drop=True)
    consec = []
    c = 0
    for _, row in wx_daily.iterrows():
        c = c + 1 if row['rainy'] else 0
        consec.append(c)
    wx_daily['consec_rain'] = consec
    
    results = []
    prev_high_demand = False
    for target_date in backtest_dates:
        actual = len(df_valid[df_valid['date'] == target_date])
        target_dow = target_date.weekday()
        days_from_bt = (target_date - backtest_start).days
        week_ahead = days_from_bt // 7
        
        base = recent_by_dow_bt.get(target_dow, {}).get('weighted', 350)
        decay = (1 + bt_decay) ** max(0, week_ahead)
        
        # 连续降雨系数
        consec_row = wx_daily[wx_daily['date'] == target_date]
        consec_days = int(consec_row['consec_rain'].iloc[0]) if len(consec_row) > 0 else 0
        is_rainy_bt = consec_days > 0
        is_weekend_bt = target_dow >= 5
        
        day_wx = weather_df[weather_df['date'] == target_date]
        wx_coeff = 1.0
        
        if len(day_wx) > 0:
            max_p = day_wx['precip'].max()
            max_t = day_wx['temp'].max()
            rl = classify_rain(max_p if max_p else 0)
            tl = classify_temp(max_t)
            
            if is_rainy_bt:
                # 连续降雨：用连雨累积系数
                consec_key = min(consec_days, 5)
                wx_coeff = CONSEC_RAIN_COEFF.get(consec_key, 1.16)
            elif rl != 'none':
                # 孤立降雨：用简化系数
                if is_weekend_bt:
                    wx_coeff = ISOLATED_RAIN_COEFF_WEEKEND
                else:
                    wx_coeff = ISOLATED_RAIN_COEFF_WEEKDAY
            
            # 大雨抑制
            if rl == 'heavy':
                wx_coeff *= HEAVY_RAIN_SUPPRESSION
            
            if tl == 'extreme_hot': wx_coeff *= 1.10
            elif tl == 'hot': wx_coeff *= 1.05
        
        # 需求惯性
        inertia = 0.95 if prev_high_demand else 1.0
        
        predicted = base * decay * wx_coeff * inertia
        accuracy = max(0, 1 - abs(predicted - actual) / actual) if actual > 0 else 0
        
        # 追踪高需求日
        is_high = actual > base * 1.15
        prev_high_demand = is_high
        
        results.append({
            'date': str(target_date),
            'dow': target_dow,
            'dow_name': DOW_NAMES[target_dow],
            'actual': actual,
            'predicted': round(predicted, 0),
            'error': round(predicted - actual, 0),
            'error_pct': round((predicted - actual) / actual * 100, 1) if actual > 0 else 0,
            'accuracy': round(accuracy * 100, 1),
            'consec_rain': consec_days
        })
    
    avg_accuracy = np.mean([r['accuracy'] for r in results])
    mape = np.mean([abs(r['error_pct']) for r in results])
    
    return results, {
        'avg_accuracy': round(avg_accuracy, 1),
        'mape': round(mape, 1),
        'within_10pct': sum(1 for r in results if abs(r['error_pct']) <= 10),
        'total_days': len(results)
    }

# ===== AGGREGATIONS =====
def compute_aggregations(orders_df, weather_df):
    df = orders_df.copy()
    valid = df[df['is_valid']]
    
    daily = valid.groupby('date').size().reset_index(name='total_orders')
    daily['date_str'] = daily['date'].astype(str)
    daily['dow'] = pd.to_datetime(daily['date']).dt.dayofweek
    daily['dow_name'] = pd.to_datetime(daily['date']).dt.strftime('%a')
    
    # 即时单 vs 预订单
    instant_daily = valid[valid['is_instant']].groupby('date').size().reset_index(name='instant')
    pre_daily = valid[~valid['is_instant']].groupby('date').size().reset_index(name='preorder')
    daily = daily.merge(instant_daily, on='date', how='left').merge(pre_daily, on='date', how='left')
    daily['instant'] = daily['instant'].fillna(0)
    daily['preorder'] = daily['preorder'].fillna(0)
    
    # 天气合并
    weather_daily = weather_df.groupby('date').agg(
        max_temp=('temp', 'max'),
        min_temp=('temp', 'min'),
        total_precip=('precip', 'sum'),
        max_precip=('precip', 'max')
    ).reset_index()
    weather_daily['date_str'] = weather_daily['date'].astype(str)
    weather_daily['rain_level'] = weather_daily['max_precip'].apply(classify_rain)
    daily = daily.merge(weather_daily[['date_str', 'max_temp', 'min_temp', 'total_precip', 'rain_level']], 
                        on='date_str', how='left')
    
    # 时段×星期热力图
    heatmap = valid.groupby(['slot', 'dow']).size().reset_index(name='count')
    slot_days = valid.groupby(['slot', 'dow'])['date'].nunique().reset_index(name='days')
    heatmap = heatmap.merge(slot_days, on=['slot', 'dow'])
    heatmap['avg'] = heatmap['count'] / heatmap['days']
    
    # 取消率
    all_daily = df.groupby('date').size().reset_index(name='all_orders')
    cancel_daily = df[df['is_cancelled']].groupby('date').size().reset_index(name='cancelled')
    cancel_rate = all_daily.merge(cancel_daily, on='date', how='left')
    cancel_rate['cancelled'] = cancel_rate['cancelled'].fillna(0)
    cancel_rate['date_str'] = cancel_rate['date'].astype(str)
    cancel_rate['cancel_rate'] = cancel_rate['cancelled'] / cancel_rate['all_orders'] * 100
    
    return {
        'daily': daily.to_dict('records'),
        'heatmap': heatmap[['slot', 'dow', 'avg']].to_dict('records'),
        'cancel_rate': cancel_rate[['date_str', 'cancel_rate']].to_dict('records')
    }

# ===== MAIN =====
def main():
    print("=" * 60)
    print("订单预估分析模型 v3.0 (多店降雨占比分析版)")
    print("=" * 60)
    
    print("\n[1/8] 加载订单数据...")
    orders = load_orders(EXCEL_PATH)
    print(f"  总记录: {len(orders)}, 有效: {orders['is_valid'].sum()}, 取消: {orders['is_cancelled'].sum()}")
    print(f"  日期范围: {orders['date'].min()} ~ {orders['date'].max()}")
    
    print("\n[2/8] 加载天气数据...")
    weather = load_weather(WEATHER_PATH)
    print(f"  天气记录: {len(weather)}, 有效: {weather['temp'].notna().sum()}")
    
    print("\n[3/8] 基准模式分析...")
    base_pattern, dow_avg, anomaly_dates = compute_base_pattern(orders)
    print(f"  异常日: {[str(d) for d in sorted(anomaly_dates)]}")
    print(dow_avg.to_string())
    
    print("\n[4/8] 天气影响分析...")
    rain_impact, temp_impact, daily_rain_summary, slot_weather = compute_weather_impact(orders, weather)
    print(f"  降雨系数: {len(rain_impact)} 组合, 温度系数: {len(temp_impact)} 组合")
    for level in ['light', 'moderate', 'heavy']:
        coeffs = [(s, v['coefficient']) for (s, l), v in rain_impact.items() if l == level]
        if coeffs:
            print(f"  {level}: avg={np.mean([c for _,c in coeffs]):.3f} ({len(coeffs)} slots)")
    
    print("\n[5/8] 开学影响...")
    school_impact = compute_school_impact(orders)
    print(f"  前: {school_impact['pre_mean']} → 后: {school_impact['post_mean']} ({school_impact['change_pct']}%)")
    
    print("\n[6/8] 节假日/周末模式...")
    holiday_pattern = compute_holiday_pattern(orders)
    print(f"  周末: {holiday_pattern['weekend_mean']}, 工作日: {holiday_pattern['weekday_mean']}, 比值: {holiday_pattern['weekend_weekday_ratio']}")
    
    print("\n[7/8] 大促检测 & 履约分析...")
    promotions, slot_promo_ratio = detect_promotions(orders)
    print(f"  检测 {len(promotions)} 个异常日:")
    for p in promotions:
        print(f"    {p['date']}: {p['orders']}单 z={p['zscore']}")
    
    fulfillment = compute_fulfillment_impact(orders)
    print(f"  仓T: mean={fulfillment['overall_mean_t']}min, median={fulfillment['overall_median_t']}min, corr={fulfillment['correlation']}")
    
    print("\n[8/8] 生成15天预估...")
    # 合并所有异常日：基准分析异常 + 大促检测异常 + KEY_DATES异常标记
    all_anomaly_dates = set(anomaly_dates)
    for p in promotions:
        all_anomaly_dates.add(pd.to_datetime(p['date']).date())
    for kd_date, kd_info in KEY_DATES.items():
        if kd_info['type'] == 'anomaly':
            all_anomaly_dates.add(pd.to_datetime(kd_date).date())
    
    forecast, meta = generate_forecast(
        base_pattern, dow_avg, weather, rain_impact, temp_impact,
        school_impact, holiday_pattern, promotions, slot_promo_ratio,
        fulfillment, orders, all_anomaly_dates
    )
    print(f"  趋势: {meta['trend_ratio']}, 周环比: {meta['week_wow']}, 周衰减率: {meta['weekly_decay_rate']}")
    print(f"  按星期趋势: {meta['dow_trend']}")
    print(f"  周均值序列(近→远): {meta['weekly_means']}")
    print(f"\n  预估结果:")
    for f in forecast:
        tx = " [调休]" if f.get('is_tiaoxiu') else ""
        print(f"    {f['date']} ({f['dow_name']}): {int(f['forecast_total'])}单 "
              f"[{int(f['confidence_low'])}-{int(f['confidence_high'])}] "
              f"天气:{f['weather_desc']}{tx} decay:{f['future_decay']}")
    
    print("\n[回测] 最近14天验证...")
    bt_results, bt_summary = backtest(base_pattern, dow_avg, weather, rain_impact, temp_impact, orders)
    print(f"  平均准确率: {bt_summary['avg_accuracy']}%")
    print(f"  MAPE: {bt_summary['mape']}%")
    print(f"  ±10%内: {bt_summary['within_10pct']}/{bt_summary['total_days']}天")
    for r in bt_results:
        mark = "✓" if abs(r['error_pct']) <= 10 else "✗"
        rain_info = f" 连雨{r['consec_rain']}天" if r['consec_rain'] > 0 else ""
        print(f"    {mark} {r['date']} ({r['dow_name']}): 预估{r['predicted']:.0f} 实际{r['actual']} 偏差{r['error_pct']:+.1f}%{rain_info}")
    
    print("\n[输出] 生成可视化数据...")
    aggs = compute_aggregations(orders, weather)
    
    output = {
        'meta': {
            'store': STORE_NAME,
            'data_range': f"{orders['date'].min()} ~ {orders['date'].max()}",
            'total_orders': int(orders['is_valid'].sum()),
            'cancelled_orders': int(orders['is_cancelled'].sum()),
            'generated_at': datetime.now().isoformat()
        },
        'analysis': {
            'dow_avg': dow_avg.to_dict('records'),
            'rain_impact': {f"{s}_{l}": v for (s, l), v in rain_impact.items()},
            'temp_impact': {f"{s}_{l}": v for (s, l), v in temp_impact.items()},
            'school_impact': school_impact,
            'holiday_pattern': holiday_pattern,
            'promotions': promotions,
            'slot_promo_ratio': slot_promo_ratio,
            'fulfillment': fulfillment,
            'anomaly_dates': [str(d) for d in sorted(anomaly_dates)]
        },
        'forecast': forecast,
        'forecast_meta': meta,
        'backtest': {'results': bt_results, 'summary': bt_summary},
        'aggregations': aggs,
        'config': {
            'key_dates': KEY_DATES,
            'holidays': HOLIDAYS,
            'school_open_date': SCHOOL_OPEN_DATE
        }
    }
    
    with open('forecast_result.json', 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    
    print(f"\n结果已保存到 forecast_result.json")
    return output

if __name__ == '__main__':
    main()
