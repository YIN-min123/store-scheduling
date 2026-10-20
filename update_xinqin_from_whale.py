#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""兴勤港惠店 全量口径日常更新编排（单一入口）
   前置：xinqin_whale_full.json 已由浏览器步骤从鲸品云「整体效率分析」拉取最新 orderNum 序列覆盖。
   本脚本：
     1) 覆盖引擎输入 xinqin_daily_full.json（全量真值）
     2) 重跑预估引擎 daily_forecast_lite_xinqin.py
     3) 运行 sync_xinqin_full.py 写回 storeDailyData/预估块并自增版本
     4) 仅提交 兴勤 HTML（绝不碰成山），git push 部署 Pages
   用法: python update_xinqin_from_whale.py [--no-push]
"""
import os, sys, json, subprocess, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, 'daily_forecast_lite_xinqin.py')
SYNC = os.path.join(HERE, 'sync_xinqin_full.py')
WHALE = os.path.join(HERE, 'xinqin_whale_full.json')
# 引擎默认输入落在 scratchpad
ACTUALS = r'C:\Users\TBSG\.qoderwork\workspace\mvg1a4pbc2196wre\xinqin_daily_full.json'
PAGES_FILENAME = '兴勤超市港惠店订单预估.html'

do_push = '--no-push' not in sys.argv

# 1) 全量真值 -> 引擎输入
full = json.load(open(WHALE, encoding='utf-8'))
json.dump(full, open(ACTUALS, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print(f'[1/4] 引擎输入已更新为全量 {len(full)}天 ({min(full)}~{max(full)})')

env = dict(os.environ)
env['PYTHONIOENCODING'] = 'utf-8'

# 2) 重跑引擎
r = subprocess.run([sys.executable, '-X', 'utf8', ENGINE], cwd=HERE, env=env,
                   capture_output=True, text=True, timeout=180)
if r.returncode != 0:
    print('[2/4][ERR] 引擎失败:\n' + (r.stderr or '')[-600:]); sys.exit(2)
print('[2/4] 引擎完成')

# 3) 写回 HTML + 版本自增
r = subprocess.run([sys.executable, '-X', 'utf8', SYNC], cwd=HERE, env=env,
                   capture_output=True, text=True, timeout=120)
print(r.stdout[-800:])
if r.returncode != 0 or '[ERR]' in r.stdout:
    print('[3/4][ERR] 写回失败:\n' + (r.stderr or '')[-600:]); sys.exit(3)
print('[3/4] HTML 已写回')

# 4) 仅提交 兴勤 HTML 并推送
if do_push:
    os.chdir(HERE)
    subprocess.run(['git', 'add', PAGES_FILENAME], check=True)
    res = subprocess.run(['git', 'commit', '-m', f'Update xinqin 全量预估 ({max(full)})'],
                         capture_output=True, text=True)
    combined = (res.stdout + res.stderr).lower()
    if res.returncode != 0 and 'nothing to commit' in combined:
        print('[4/4] HTML 无变化，跳过推送')
    else:
        pr = subprocess.run(['git', 'push'], capture_output=True, text=True, timeout=60)
        print('[4/4] push:', 'OK' if pr.returncode == 0 else pr.stderr[-300:])
else:
    print('[4/4] --no-push 跳过部署')
