#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""月度汇总生成器：合并某月所有日级 -par 目录的候选事件 CSV，并生成月度 HTML 报告。

用法:
  PYTHONPATH=. ./venv/bin/python scripts/generate_monthly_summary.py --month 202601
  PYTHONPATH=. ./venv/bin/python scripts/generate_monthly_summary.py --month 202601 --label "2026年1月"

输出（默认 output/{month}_monthly/）:
  - tick_candidate_events_{month}.csv  合并后的整月事件（保留全部列）
  - monthly_summary.html              月度报告（品种/按天分布、top 缺口事件，相对链接下钻到日级/品种级）
"""
import argparse
import csv
import glob
import os
import html as H
from collections import Counter, defaultdict


def fnum(s, d=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return d


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--month', required=True, help='月份前缀，如 202601')
    ap.add_argument('--output-root', default='output')
    ap.add_argument('--label', default=None, help='报告标题，默认取 month')
    ap.add_argument('--out-dir', default=None)
    args = ap.parse_args()
    month, root = args.month, args.output_root
    label = args.label or month
    out_dir = args.out_dir or f'{root}/{month}_monthly'
    os.makedirs(out_dir, exist_ok=True)

    files = sorted(glob.glob(f'{root}/{month}*-par/tick_candidate_events.csv'))
    rows, fset = [], []
    for f in files:
        with open(f, newline='', encoding='utf-8') as fh:
            rd = csv.DictReader(fh)
            for r in rd:
                rows.append(r)
            for k in rd.fieldnames or []:
                if k not in fset:
                    fset.append(k)
    days = sorted({os.path.basename(os.path.dirname(f)).replace('-par', '') for f in files})

    if not rows:
        print(f'[skip] {month}: 没有找到事件 CSV（{len(files)} 个 -par 目录，0 事件）')
        return

    # 合并整月 CSV（保留全部列）
    merged = f'{out_dir}/tick_candidate_events_{month}.csv'
    with open(merged, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=fset)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, '') for k in fset})

    # 统计
    by_comm = Counter(r.get('品种', '') for r in rows)
    comm_gap = defaultdict(float)
    for r in rows:
        comm_gap[r.get('品种', '')] += fnum(r.get('名义成交额缺口'))
    by_day = Counter(r.get('交易日', '') for r in rows)
    day_comms = defaultdict(set)
    for r in rows:
        day_comms[r.get('交易日', '')].add(r.get('品种', ''))
    gap_total = sum(fnum(r.get('名义成交额缺口')) for r in rows)
    agsn = sum(by_comm.get(c, 0) for c in ['AG', 'SN', 'NI'])
    n, ndays = len(rows), len(days)
    top = sorted(rows, key=lambda r: fnum(r.get('名义成交额缺口')), reverse=True)[:10]

    e = H.escape

    def comm_rows():
        return '\n'.join(
            f'<tr><td>{e(c)}</td><td>{cnt}</td><td>{cnt / n * 100:.1f}%</td><td>{comm_gap[c]:,.0f}</td></tr>'
            for c, cnt in by_comm.most_common())

    def day_rows():
        return '\n'.join(
            f'<tr><td>{e(d)}</td><td>{by_day[d]}</td><td>{len(day_comms[d])}</td>'
            f'<td><a href="../{d}-par/batch_summary.html">当日汇总</a></td></tr>'
            for d in sorted(by_day))

    def top_rows():
        out = []
        for r in top:
            d = r.get('交易日', '')
            c = r.get('品种', '')
            out.append(
                f'<tr><td>{e(d)}</td><td>{e(r.get("合约"))}</td><td>{e(r.get("事件时间"))}</td>'
                f'<td>{fnum(r.get("名义成交额缺口")):,.0f}</td><td>{e(r.get("末笔向下偏离_跳"))}</td>'
                f'<td>{e(r.get("触发原因"))}</td>'
                f'<td><a href="../{d}-par/{c}/event_replay_{c}.html">复盘</a></td></tr>')
        return '\n'.join(out)

    CSS = (
        'body{font-family:-apple-system,"PingFang SC",sans-serif;margin:20px;color:#222;background:#fafafa;max-width:1100px}'
        'h1{font-size:20px}h2{font-size:15px;margin-top:24px;border-left:4px solid #2b6cb0;padding-left:8px}'
        '.kpis{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}'
        '.kpi{background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:10px 14px;min-width:110px}'
        '.kpi .v{font-size:22px;font-weight:700;color:#2b6cb0}.kpi .l{font-size:12px;color:#666}'
        'table{border-collapse:collapse;width:100%;background:#fff;margin:8px 0;font-size:13px}'
        'th,td{border:1px solid #e2e8f0;padding:6px 8px;text-align:left}'
        'th{background:#edf2f7}tr:nth-child(even){background:#f7fafc}'
        'a{color:#2b6cb0;text-decoration:none}a:hover{text-decoration:underline}'
        '.note{color:#666;font-size:12px;margin-top:18px}code{background:#edf2f7;padding:1px 4px;border-radius:3px}'
    )

    doc = f'''<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><title>{label} tick 乌龙指候选 月度汇总</title>
<style>{CSS}</style></head><body>
<h1>{label} tick 乌龙指候选 · 月度汇总</h1>
<div class="note">初筛候选，非交易所错单确认。交易日 {ndays} 天 · 合并自 {len(files)} 个日级 -par 目录 · 合并 CSV：<code>tick_candidate_events_{month}.csv</code></div>
<div class="kpis">
<div class="kpi"><div class="v">{n}</div><div class="l">候选事件</div></div>
<div class="kpi"><div class="v">{len(by_comm)}</div><div class="l">命中品种</div></div>
<div class="kpi"><div class="v">{n / ndays:.0f}</div><div class="l">日均事件</div></div>
<div class="kpi"><div class="v">{gap_total / 10000:.0f}万</div><div class="l">名义缺口总额</div></div>
<div class="kpi"><div class="v">{agsn / n * 100:.0f}%</div><div class="l">AG/SN/NI 占比</div></div>
</div>
<h2>按品种分布</h2>
<table><tr><th>品种</th><th>事件数</th><th>占比</th><th>名义缺口合计(元)</th></tr>
{comm_rows()}</table>
<h2>按交易日分布</h2>
<table><tr><th>交易日</th><th>事件数</th><th>命中品种数</th><th>当日明细</th></tr>
{day_rows()}</table>
<h2>名义缺口 top10 候选事件</h2>
<table><tr><th>交易日</th><th>合约</th><th>时间</th><th>名义缺口(元)</th><th>末笔偏离(跳)</th><th>触发原因</th><th>复盘</th></tr>
{top_rows()}</table>
<div class="note">生成自 scripts/generate_monthly_summary.py · 链接为相对路径，在 output/ 下用浏览器打开本文件即可点击跳转到当日/品种复盘。</div>
</body></html>'''

    hp = f'{out_dir}/monthly_summary.html'
    with open(hp, 'w', encoding='utf-8') as f:
        f.write(doc)
    print(f'[merge] {merged}  ({n} 事件, {len(fset)} 列)')
    print(f'[html]  {hp}')
    print(f'  交易日{ndays}  事件{n}  品种{len(by_comm)}  缺口总额{gap_total / 10000:.0f}万  AG/SN/NI占{agsn / n * 100:.0f}%')


if __name__ == '__main__':
    main()
