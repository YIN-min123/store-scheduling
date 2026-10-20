#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 daily_forecast_lite_xinqin.py 的输出写入 港惠店 HTML 的
   modelForecastHistory（回测预测，≠实际）与 modelForecastData（未来16天）。"""
import json, re, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT = os.path.join(HERE, 'forecast_result_xinqin.json')
HTML_CANDIDATES = [
    r'D:\TBSG\Desktop\order-forecast-scheduling-港惠店.html',
    os.path.join(HERE, '兴勤超市港惠店订单预估.html'),
]

res = json.load(open(RESULT, encoding='utf-8'))
history = {r['date']: int(r['forecast']) for r in res['history']}
future = {r['date']: int(r['forecast']) for r in res['future']}

def js_block(obj):
    lines = ',\n'.join(f"  '{k}': {v}" for k, v in sorted(obj.items()))
    return '{\n' + lines + '\n}'

hist_str = js_block(history)
fut_str = js_block(future)

for path in HTML_CANDIDATES:
    if not os.path.exists(path):
        print('[skip] 不存在:', path); continue
    c = open(path, encoding='utf-8').read()
    orig = c
    c, n1 = re.subn(r'const\s+modelForecastHistory\s*=\s*\{.*?\};',
                    'const modelForecastHistory = ' + hist_str + ';', c, count=1, flags=re.DOTALL)
    c, n2 = re.subn(r'const\s+modelForecastData\s*=\s*\{.*?\};',
                    'const modelForecastData = ' + fut_str + ';', c, count=1, flags=re.DOTALL)
    if n1 != 1 or n2 != 1:
        print(f'[ERR] {path} 替换失败 n_hist={n1} n_fut={n2}'); continue
    # 版本 v1.1 -> v1.2
    c = re.sub(r'>v1\.1<', '>v1.2<', c)
    open(path, 'w', encoding='utf-8').write(c)
    print(f'[OK] {os.path.basename(path)}: history={len(history)}天 future={len(future)}天 已写入 (changed={orig!=c})')

print('history:', json.dumps(history, ensure_ascii=False))
print('future :', json.dumps(future, ensure_ascii=False))
