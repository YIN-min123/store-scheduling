#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""兴勤港惠店 全量口径同步：
   1) storeDailyData '港惠店' -> 鲸品云 orderNum 全量序列
   2) modelForecastHistory(回测预测) / modelForecastData(未来) -> forecast_result_xinqin.json
   3) 版本 v1.4 -> v1.5
   同时写桌面副本与 repo 副本，二者保持一致。"""
import json, re, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
FULL = os.path.join(HERE, 'xinqin_whale_full.json')
RESULT = os.path.join(HERE, 'forecast_result_xinqin.json')
HTML_CANDIDATES = [
    r'D:\TBSG\Desktop\order-forecast-scheduling-港惠店.html',
    os.path.join(HERE, '兴勤超市港惠店订单预估.html'),
]

full = json.load(open(FULL, encoding='utf-8'))
res = json.load(open(RESULT, encoding='utf-8'))
history = {r['date']: int(r['forecast']) for r in res['history']}
future = {r['date']: int(r['forecast']) for r in res['future']}


def js_flat(obj):
    return '{' + ','.join(f"'{k}': {int(obj[k])}" for k in sorted(obj)) + '}'


def js_block(obj):
    lines = ',\n'.join(f"  '{k}': {int(v)}" for k, v in sorted(obj.items()))
    return '{\n' + lines + '\n}'


store_js = js_flat(full)
hist_str = js_block(history)
fut_str = js_block(future)

for path in HTML_CANDIDATES:
    if not os.path.exists(path):
        print('[skip] 不存在:', path)
        continue
    c = open(path, encoding='utf-8').read()
    orig = c

    # 1) storeDailyData 内层 '港惠店': {...}
    c, n_sdd = re.subn(
        r"(let\s+storeDailyData\s*=\s*\{\s*'港惠店'\s*:\s*)\{[^}]*\}",
        lambda m: m.group(1) + store_js,
        c, count=1
    )
    # 注释更新（严格限制在单行内，[^\\n\\r] 用单反斜杠让正则把 \\n 解析为换行，避免跨行吞掉代码）
    c, _ = re.subn(r'[ \t]*//[^\n]*兴勤超市港惠店每日订单汇总[^\n]*',
                   f'    // 兴勤超市港惠店每日订单汇总（全渠道, 鲸品云履约效率报表, {min(full)}~{max(full)}）',
                   c, count=1)
    c, _ = re.subn(r'[ \t]*//[^\n]*FBI看板1796001 门店级每日订单[^\n]*',
                   '    // 数据源: 鲸品云 整体效率分析 orderNum(订单数) 全量', c, count=1)

    # 2) 预估两块
    c, n1 = re.subn(r'const\s+modelForecastHistory\s*=\s*\{.*?\};',
                    'const modelForecastHistory = ' + hist_str + ';',
                    c, count=1, flags=re.DOTALL)
    c, n2 = re.subn(r'const\s+modelForecastData\s*=\s*\{.*?\};',
                    'const modelForecastData = ' + fut_str + ';',
                    c, count=1, flags=re.DOTALL)

    # 3) 版本自动 +0.1（找当前 >vX.Y< 标签）
    vm = re.search(r'>v(\d+)\.(\d+)<', c)
    nv = 0
    if vm:
        major, minor = int(vm.group(1)), int(vm.group(2)) + 1
        newver = f'v{major}.{minor}'
        c, nv = re.subn(r'>v\d+\.\d+<', f'>{newver}<', c, count=1)
        print(f'[VER] {vm.group(0)} -> >{newver}<')

    if n_sdd != 1 or n1 != 1 or n2 != 1:
        print(f'[ERR] {path} 替换异常 sdd={n_sdd} hist={n1} fut={n2}')
        continue
    open(path, 'w', encoding='utf-8').write(c)
    print(f'[OK] {os.path.basename(path)}: sdd={n_sdd} hist={n1} fut={n2} ver={nv} changed={orig!=c}')

print(f'[INFO] storeDailyData {len(full)}天; history {len(history)}天; future {len(future)}天')
print('[INFO] future 范围:', min(future), '~', max(future))
