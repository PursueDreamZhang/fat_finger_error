#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分析乌龙指事件数据
1. 统计各品种的事件数量并筛选出数量排名前五的品种
2. 提取这五个品种的完整事件数据
3. 按照品种和年份两个维度进行汇总统计
"""

import pandas as pd
from collections import defaultdict

data = """
品种       年份       事件数量        
CS       2022     26          
TA       2022     26          
C        2022     4           
M        2022     24          
RM       2022     10          
FG       2022     59          
PP       2022     17          
SF       2022     20          
SM       2022     23          
RB       2022     16          
HC       2022     15          
BU       2022     30          
FU       2022     42          
V        2022     24          
JD       2022     19          
EG       2022     17          
SR       2022     3           
SA       2022     50          
Y        2022     11          
P        2022     13          
CF       2022     7           
CS       2023     6           
TA       2023     7           
C        2023     8           
M        2023     10          
RM       2023     12          
FG       2023     28          
PP       2023     7           
SF       2023     22          
SM       2023     15          
RB       2023     12          
HC       2023     8           
BU       2023     17          
FU       2023     36          
V        2023     22          
JD       2023     13          
EG       2023     8           
SR       2023     1           
SA       2023     21          
Y        2023     10          
P        2023     20          
CF       2023     4           
CS       2024     7           
TA       2024     7           
C        2024     2           
M        2024     10          
RM       2024     2           
FG       2024     33          
PP       2024     3           
SF       2024     6           
SM       2024     21          
RB       2024     5           
HC       2024     12          
BU       2024     7           
FU       2024     17          
V        2024     7           
JD       2024     31          
EG       2024     14          
SR       2024     1           
SA       2024     24          
Y        2024     5           
P        2024     9           
CF       2024     2           
CS       2025     9           
TA       2025     4           
C        2025     4           
M        2025     9           
RM       2025     0           
FG       2025     0           
PP       2025     2           
SF       2025     0           
SM       2025     0           
RB       2025     11          
HC       2025     5           
BU       2025     13          
FU       2025     27          
V        2025     14          
JD       2025     28          
EG       2025     9           
SR       2025     2           
SA       2025     13          
Y        2025     1           
P        2025     11          
CF       2025     0           
"""

def parse_data(data_str):
    """解析数据字符串"""
    lines = data_str.strip().split('\n')
    records = []
    
    for line in lines[1:]:
        parts = line.split()
        if len(parts) >= 3:
            variety = parts[0]
            year = parts[1]
            count = int(parts[2])
            records.append({
                'variety': variety,
                'year': year,
                'count': count
            })
    
    return pd.DataFrame(records)

def analyze_data():
    """分析数据"""
    print("=" * 80)
    print("乌龙指事件数据分析报告")
    print("=" * 80)
    
    df = parse_data(data)
    
    print("\n" + "=" * 80)
    print("1. 各品种事件总数量统计及排名（全部年份）")
    print("=" * 80)
    
    variety_total = df.groupby('variety')['count'].sum().sort_values(ascending=False)
    print("\n排名 | 品种 | 总事件数")
    print("-" * 30)
    for rank, (variety, count) in enumerate(variety_total.items(), 1):
        print(f" {rank}   | {variety}  |   {count}")
    
    top5_varieties = variety_total.head(5).index.tolist()
    print(f"\n全部年份前5名品种: {', '.join(top5_varieties)}")
    
    print("\n" + "=" * 80)
    print("2. 2024-2025年前五品种专项分析")
    print("=" * 80)
    
    df_24_25 = df[df['year'].isin(['2024', '2025'])]
    variety_total_24_25 = df_24_25.groupby('variety')['count'].sum().sort_values(ascending=False)
    
    print("\n排名 | 品种 | 2024-2025事件总数")
    print("-" * 35)
    for rank, (variety, count) in enumerate(variety_total_24_25.items(), 1):
        print(f" {rank}   | {variety}  |   {count}")
    
    top5_24_25 = variety_total_24_25.head(5).index.tolist()
    print(f"\n2024-2025年前5名品种: {', '.join(top5_24_25)}")
    
    print("\n" + "=" * 80)
    print("3. 全部年份前5品种按年份分布")
    print("=" * 80)
    
    top5_df = df[df['variety'].isin(top5_varieties)]
    
    print("\n品种 | 年份 | 事件数量")
    print("-" * 30)
    for _, row in top5_df.sort_values(['variety', 'year']).iterrows():
        marker = " ★" if row['variety'] in top5_varieties else ""
        print(f" {row['variety']}  | {row['year']} |   {row['count']}{marker}")
    
    print("\n" + "=" * 80)
    print("4. 2024-2025年前5品种按年份分布")
    print("=" * 80)
    
    top5_24_25_df = df[df['variety'].isin(top5_24_25)]
    
    print("\n品种 | 年份 | 事件数量")
    print("-" * 30)
    for _, row in top5_24_25_df.sort_values(['variety', 'year']).iterrows():
        marker = " ★" if row['variety'] in top5_24_25 else ""
        print(f" {row['variety']}  | {row['year']} |   {row['count']}{marker}")
    
    print("\n" + "=" * 80)
    print("5. 2024-2025年前5品种按年份维度汇总统计")
    print("=" * 80)
    
    top5_24_25_df = df[df['variety'].isin(top5_24_25)]
    df_24_25_only = df_24_25[df_24_25['variety'].isin(top5_24_25)]
    
    pivot_table_24_25 = df_24_25_only.pivot_table(
        index='variety', 
        columns='year', 
        values='count', 
        aggfunc='sum',
        fill_value=0
    )
    
    pivot_table_24_25['两年合计'] = pivot_table_24_25.sum(axis=1)
    pivot_table_24_25 = pivot_table_24_25.sort_values('两年合计', ascending=False)
    
    print("\n品种 | 2024 | 2025 | 两年合计")
    print("-" * 40)
    for variety in pivot_table_24_25.index:
        row = pivot_table_24_25.loc[variety]
        count_2024 = int(row['2024']) if '2024' in row.index else 0
        count_2025 = int(row['2025']) if '2025' in row.index else 0
        total = int(row['两年合计'])
        print(f"  {variety}  |   {count_2024}   |   {count_2025}   |    {total}")
    
    print("\n" + "=" * 80)
    print("6. 2024-2025年年度对比统计")
    print("=" * 80)
    
    year_total_24_25 = df_24_25.groupby('year')['count'].sum()
    print("\n年份 | 事件数量 | 占比")
    print("-" * 35)
    grand_total_24_25 = year_total_24_25.sum()
    for year, count in year_total_24_25.items():
        pct = (count / grand_total_24_25) * 100 if grand_total_24_25 > 0 else 0
        print(f" {year} |    {count}   | {pct:.1f}%")
    print("-" * 35)
    print(f" 合计 |    {grand_total_24_25}   | 100.0%")
    
    yoy_change = ((year_total_24_25.get('2025', 0) - year_total_24_25.get('2024', 0)) 
                  / year_total_24_25.get('2024', 1) * 100)
    print(f"\n同比变化: {yoy_change:.1f}%")
    
    print("\n" + "=" * 80)
    print("7. 全部年份年度汇总统计")
    print("=" * 80)
    
    year_total = df.groupby('year')['count'].sum()
    print("\n年份 | 事件数量 | 占比")
    print("-" * 35)
    grand_total = year_total.sum()
    for year, count in year_total.items():
        pct = (count / grand_total) * 100 if grand_total > 0 else 0
        print(f" {year} |    {count}   | {pct:.1f}%")
    print("-" * 35)
    print(f" 合计 |    {grand_total}   | 100.0%")
    
    print("\n" + "=" * 80)
    print("8. 关键发现")
    print("=" * 80)
    
    max_variety = variety_total.idxmax()
    max_count = variety_total.max()
    min_variety = variety_total.idxmin()
    min_count = variety_total.min()
    
    max_year = year_total.idxmax()
    max_year_count = year_total.max()
    
    max_variety_24_25 = variety_total_24_25.idxmax()
    max_count_24_25 = variety_total_24_25.max()
    
    best_year = None
    best_year_variety = None
    best_year_count = 0
    
    for variety in top5_varieties:
        variety_data = top5_df[top5_df['variety'] == variety]
        for _, row in variety_data.iterrows():
            if row['count'] > best_year_count:
                best_year_count = row['count']
                best_year = row['year']
                best_year_variety = variety
    
    print(f"""
【全部年份统计】
• 事件数量最多的品种: {max_variety}，共 {max_count} 次
• 事件数量最少的品种: {min_variety}，共 {min_count} 次
• 事件数量最多的年份: {max_year}，共 {max_year_count} 次
• 单品种单年份最多事件: {best_year_variety}（{best_year}年），共 {best_year_count} 次

【2024-2025年统计】
• 2024-2025事件最多的品种: {max_variety_24_25}，共 {max_count_24_25} 次
• 2024-2025年事件数量: {grand_total_24_25} 次
• 同比变化: {yoy_change:+.1f}%

• 全部年份前5品种事件占比: {top5_df['count'].sum()} / {df['count'].sum()} = {top5_df['count'].sum()/df['count'].sum()*100:.1f}%
• 2024-2025前5品种事件占比: {top5_24_25_df['count'].sum()} / {df_24_25['count'].sum()} = {top5_24_25_df['count'].sum()/df_24_25['count'].sum()*100:.1f}%
""")
    
    print("=" * 80)
    print("分析完成！")
    print("=" * 80)

if __name__ == "__main__":
    analyze_data()
