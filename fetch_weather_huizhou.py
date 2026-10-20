#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""抓取惠州 Open-Meteo 天气（过去92天实况 + 未来16天预报），hourly 结构，供 lite 引擎使用。"""
import json, urllib.request, urllib.parse, sys

LAT, LON = 23.1100, 114.4200
URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&hourly=precipitation,temperature_2m"
    "&past_days=92&forecast_days=16&timezone=Asia/Shanghai"
)

def main():
    req = urllib.request.Request(URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    out = 'C:/Users/TBSG/.qoderwork/workspace/store-scheduling-repo/weather_huizhou.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False)
    h = data['hourly']
    days = sorted(set(t[:10] for t in h['time']))
    print(f"[OK] 惠州天气: {len(h['time'])} 小时, {days[0]} ~ {days[-1]} ({len(days)}天) -> {out}")

if __name__ == '__main__':
    main()
