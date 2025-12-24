#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
期货"乌龙指"异常交易年度分析 - 基于历史统计的检测版

本脚本对特定期货品种的一整年数据进行乌龙指检测，生成月度摘要报告。
使用与fat_finger_example.py相同的检测算法，确保检测精度一致。
"""

from datetime import datetime
import warnings
import pandas as pd

# 导入FatFingerDetector类
from fat_finger_detector import FatFingerDetector

# 忽略警告
warnings.filterwarnings('ignore')

def main(futures_commodity="PP", year=2023, difference_threshold=1.0, window_size=3,
         amplitude_ratio_threshold=2.0, volume_threshold=100, min_absolute_difference_pct=2.0, verbose=True):
    """
    主函数：对特定期货品种的一整年数据进行乌龙指检测，并生成月度摘要
    
    参数：
    - futures_commodity: 期货品种基础代码（如"TA"表示PTA）
    - year: 要分析的年份
    - difference_threshold: 差异阈值（百分比）
    - window_size: 历史统计窗口（天数）
    - amplitude_ratio_threshold: 振幅倍数阈值
    - volume_threshold: 成交量阈值（单位：手）
    - min_absolute_difference_pct: 最小绝对差异百分比阈值
    - verbose: 是否打印详细信息
    
    返回：
    - 检测结果字典，包含：
      - total_incidents: 总事件数
      - monthly_stats: 月度统计数据
      - all_events_info: 所有事件详细信息
      - futures_commodity: 期货品种
      - year: 年份
    """
    if verbose:
        print("=" * 60)
        print(f"期货乌龙指年度分析 - {futures_commodity} {year}")
        print("=" * 60)
        print()
    
    # 创建检测器实例
    detector = FatFingerDetector()
    
    # 设置日期范围：开始时间为前一年1月1日，结束时间为当年12月31日
    # 这是因为期货合约在前一年就开始交易后一年的品种，比如2023年1月就开始交易TA2401
    start_date = f"{year - 1}0101"
    end_date = f"{year}1231"
    
    # 参考合约为品种连续合约
    reference_codes = [futures_commodity]
    
    # 生成该年份的所有合约代码（如TA2401, TA2402, ..., TA2412）
    contracts = []
    year_suffix = str(year)[-2:]
    for month in range(1, 13):
        contract_code = f"{futures_commodity}{year_suffix}{month:02d}"
        contracts.append(contract_code)
    
    if verbose:
        print(f"分析参数：")
        print(f"- 期货品种: {futures_commodity}")
        print(f"- 年份: {year}")
        print(f"- 数据范围: {start_date} 至 {end_date}")
        print(f"- 差异阈值: {difference_threshold}%")
        print(f"- 历史统计窗口: {window_size}天")
        print(f"- 振幅倍数阈值: {amplitude_ratio_threshold}")
        print(f"- 成交量阈值: {volume_threshold}手")
        print(f"- 最小绝对差异百分比: {min_absolute_difference_pct}%")
        print(f"- 分析合约: {', '.join(contracts)}")
        print()
    
    # 收集所有乌龙指事件的关键信息
    all_events_info = []
    
    if verbose:
        print("开始检测乌龙指事件...")
    
    for i, contract_code in enumerate(contracts, 1):
        if verbose:
            print(f"  处理合约 {i}/{len(contracts)}: {contract_code}")
        
        try:
            # 检测乌龙指事件（不保存到CSV，提高效率）
            full_data, events_data = detector.detect_fat_finger_events(
                target_code=contract_code,
                reference_codes=reference_codes,
                start_date=start_date,
                end_date=end_date,
                save_to_csv=True,
                difference_threshold=difference_threshold,
                window_size=window_size,
                amplitude_ratio_threshold=amplitude_ratio_threshold,
                min_absolute_difference_pct=min_absolute_difference_pct,
                volume_threshold=volume_threshold
            )
            
            if events_data is not None and not events_data.empty:
                if verbose:
                    print(f"    检测到 {len(events_data)} 起事件")
                
                # 提取关键信息，避免复杂的合并操作
                for _, event_row in events_data.iterrows():
                    event_info = {
                        'date': event_row.get('date', ''),
                        'contract_code': contract_code,
                        'target_code': contract_code,
                        'futures_commodity': futures_commodity,
                        'year': year
                    }
                    all_events_info.append(event_info)
        except Exception as e:
            if verbose:
                print(f"    处理合约 {contract_code} 时出错: {str(e)}")
            continue
    
    if verbose:
        print()
    
    # 构建事件数据帧
    total_incidents = len(all_events_info)
    combined_events = None
    monthly_stats_dict = {}
    
    if all_events_info:
        if verbose:
            print(f"共检测到 {total_incidents} 起潜在的乌龙指事件")
            print()
        
        # 创建一个新的数据帧，包含所有事件信息
        combined_events = pd.DataFrame(all_events_info)
        
        # 转换日期格式，便于按月分组
        combined_events['date_dt'] = pd.to_datetime(combined_events['date'], format='%Y%m%d')
        
        # 按年月分组统计
        monthly_stats_dict = {}
        
        # 获取所有唯一的年份和月份组合
        unique_year_months = combined_events['date_dt'].dt.strftime('%Y-%m').unique().tolist()
        unique_year_months.sort()
        
        # 初始化所有月份（包括前一年1月到当年12月）
        for year_val in [year - 1, year]:
            for month in range(1, 13):
                year_month_key = f"{year_val}-{month:02d}"
                monthly_stats_dict[year_month_key] = {
                    'year': year_val,
                    'month': month,
                    'incident_count': 0,
                    'involved_contracts': [],
                    'event_dates': []
                }
        
        # 计算每个年月的统计信息
        for _, event_row in combined_events.iterrows():
            year_month_key = event_row['date_dt'].strftime('%Y-%m')
            year_val = event_row['date_dt'].year
            month_val = event_row['date_dt'].month
            
            # 更新事件数量
            monthly_stats_dict[year_month_key]['incident_count'] += 1
            
            # 更新涉及合约
            contract_code = event_row['contract_code']
            if contract_code not in monthly_stats_dict[year_month_key]['involved_contracts']:
                monthly_stats_dict[year_month_key]['involved_contracts'].append(contract_code)
            
            # 更新事件日期
            monthly_stats_dict[year_month_key]['event_dates'].append(event_row['date'])
        
        # 对涉及合约和事件日期进行排序
        for year_month_key in monthly_stats_dict:
            monthly_stats_dict[year_month_key]['involved_contracts'].sort()
            monthly_stats_dict[year_month_key]['event_dates'].sort()
        
        # 生成月度摘要报告
        if verbose:
            print(f"{year} 年月度摘要报告：")
            print("-" * 90)
            print(f"{'年月':<10} {'事件数量':<12} {'涉及合约':<25} {'事件日期':<40}")
            print("-" * 90)
            
            # 按年月顺序遍历所有月份（从年前1月到当年12月）
            for year_val in [year - 1, year]:
                for month in range(1, 13):
                    year_month_key = f"{year_val}-{month:02d}"
                    stats = monthly_stats_dict[year_month_key]
                    
                    year_month_name = f"{year_val}-{month:02d}"
                    count = stats['incident_count']
                    
                    # 处理涉及合约
                    if stats['involved_contracts']:
                        contracts = ",".join(stats['involved_contracts'])
                    else:
                        contracts = "-"
                    
                    # 处理事件日期
                    if stats['event_dates']:
                        # 确保所有日期都是字符串格式
                        date_strings = []
                        for dt in stats['event_dates']:
                            if isinstance(dt, pd.Timestamp):
                                date_strings.append(dt.strftime('%Y%m%d'))
                            elif isinstance(dt, str):
                                date_strings.append(dt)
                            else:
                                date_strings.append(str(dt))
                        dates = ",".join(date_strings)
                    else:
                        dates = "-"
                    
                    print(f"{year_month_name:<10} {count:<12} {contracts:<25} {dates:<40}")
            print("-" * 90)
            print()
            print("交叉引用说明：")
            print("如需查看详细数据，请运行fat_finger_example.py并设置：")
            print(f"  - target_code='具体合约代码'（如上述'involved_contracts'列所示）")
            print(f"  - reference_codes=['{futures_commodity}']")
            print(f"  - start_date和end_date包含上述事件日期")
            print(f"  - difference_threshold={difference_threshold}")
            print(f"  - window_size={window_size}")
            print(f"  - min_absolute_difference_pct={min_absolute_difference_pct}")
            print(f"  - amplitude_ratio_threshold={amplitude_ratio_threshold}")
            print(f"  - volume_threshold={volume_threshold}")
    else:
        if verbose:
            print("未检测到乌龙指事件")
    
    if verbose:
        print()
        print("分析完成！")
    
    # 返回结果
    return {
        'total_incidents': total_incidents,
        'monthly_stats': monthly_stats_dict,
        'all_events_info': all_events_info,
        'futures_commodity': futures_commodity,
        'year': year
    }

def analyze_multiple_commodities(commodities, years, difference_threshold=1.0, window_size=3,
                                 amplitude_ratio_threshold=2.0, volume_threshold=100, min_absolute_difference_pct=2.0):
    """
    分析多个期货品种的乌龙指事件，并汇总结果
    
    参数：
    - commodities: 期货品种数组（如["PP", "TA", "jd"]）
    - years: 要分析的年份数组（如[2022, 2023, 2024]）
    - difference_threshold: 差异阈值（百分比）
    - window_size: 历史统计窗口（天数）
    - amplitude_ratio_threshold: 振幅倍数阈值
    - volume_threshold: 成交量阈值（单位：手）
    - min_absolute_difference_pct: 最小绝对差异百分比阈值
    """
    print("=" * 80)
    print("期货乌龙指多品种年度分析汇总")
    print("=" * 80)
    print()
    
    # 收集所有分析结果
    all_results = []
    all_events = []
    
    # 遍历所有年份和品种
    for year in years:
        for commodity in commodities:
            print(f"正在分析 {commodity} {year}...")
            result = main(
                futures_commodity=commodity,
                year=year,
                difference_threshold=difference_threshold,
                window_size=window_size,
                amplitude_ratio_threshold=amplitude_ratio_threshold,
                volume_threshold=volume_threshold,
                min_absolute_difference_pct=min_absolute_difference_pct,
                verbose=False  # 关闭详细输出，避免中间过程过多信息
            )
            all_results.append(result)
            all_events.extend(result['all_events_info'])
    
    print()
    print("=" * 80)
    print("分析完成！开始生成汇总报告")
    print("=" * 80)
    print()
    
    # 生成品种-年份汇总
    print("1. 品种-年份统计汇总")
    print("-" * 60)
    print(f"{'品种':<8} {'年份':<8} {'事件数量':<12}")
    print("-" * 60)
    
    total_total_incidents = 0
    for result in all_results:
        commodity = result['futures_commodity']
        year = result['year']
        incidents = result['total_incidents']
        total_total_incidents += incidents
        print(f"{commodity:<8} {year:<8} {incidents:<12}")
    
    print("-" * 60)
    print(f"{'总计':<16} {total_total_incidents:<12}")
    print()
    
    # 生成总体月度汇总
    print("2. 总体月度统计汇总")
    print("-" * 90)
    print(f"{'年月':<10} {'事件数量':<12} {'涉及合约':<25} {'事件日期':<40}")
    print("-" * 90)
    
    if all_events:
        # 创建包含所有事件的数据帧
        all_events_df = pd.DataFrame(all_events)
        all_events_df['date_dt'] = pd.to_datetime(all_events_df['date'], format='%Y%m%d')
        
        # 获取所有涉及的年份
        involved_years = sorted(all_events_df['date_dt'].dt.year.unique().tolist())
        
        # 初始化总体月度统计字典
        overall_monthly_stats = {}
        
        # 初始化所有月份
        for year_val in involved_years:
            for month in range(1, 13):
                year_month_key = f"{year_val}-{month:02d}"
                overall_monthly_stats[year_month_key] = {
                    'year': year_val,
                    'month': month,
                    'incident_count': 0,
                    'involved_contracts': [],
                    'event_dates': []
                }
        
        # 计算总体月度统计
        for _, event_row in all_events_df.iterrows():
            year_month_key = event_row['date_dt'].strftime('%Y-%m')
            contract_code = event_row['contract_code']
            event_date = event_row['date']
            
            # 更新事件数量
            overall_monthly_stats[year_month_key]['incident_count'] += 1
            
            # 更新涉及合约
            if contract_code not in overall_monthly_stats[year_month_key]['involved_contracts']:
                overall_monthly_stats[year_month_key]['involved_contracts'].append(contract_code)
            
            # 更新事件日期
            overall_monthly_stats[year_month_key]['event_dates'].append(event_date)
        
        # 对涉及合约和事件日期进行排序
        for year_month_key in overall_monthly_stats:
            overall_monthly_stats[year_month_key]['involved_contracts'].sort()
            overall_monthly_stats[year_month_key]['event_dates'].sort()
        
        # 按年月顺序打印
        for year_val in involved_years:
            for month in range(1, 13):
                year_month_key = f"{year_val}-{month:02d}"
                if year_month_key in overall_monthly_stats:
                    stats = overall_monthly_stats[year_month_key]
                    year_month_name = f"{year_val}-{month:02d}"
                    count = stats['incident_count']
                    
                    # 处理涉及合约
                    if stats['involved_contracts']:
                        contracts = ",".join(stats['involved_contracts'])
                    else:
                        contracts = "-"
                    
                    # 处理事件日期
                    if stats['event_dates']:
                        # 确保所有日期都是字符串格式
                        date_strings = []
                        for dt in stats['event_dates']:
                            if isinstance(dt, pd.Timestamp):
                                date_strings.append(dt.strftime('%Y%m%d'))
                            elif isinstance(dt, str):
                                date_strings.append(dt)
                            else:
                                date_strings.append(str(dt))
                        dates = ",".join(date_strings)
                    else:
                        dates = "-"
                    
                    # 只打印有事件的月份
                    if count > 0:
                        print(f"{year_month_name:<10} {count:<12} {contracts:<25} {dates:<40}")
    else:
        print("未检测到任何乌龙指事件")
    
    print("-" * 90)
    print()
    
    print("3. 品种总体统计")
    print("-" * 60)
    print(f"{'品种':<8} {'总事件数':<12}")
    print("-" * 60)
    
    # 按品种统计
    if all_events:
        all_events_df = pd.DataFrame(all_events)
        commodity_stats = all_events_df.groupby('futures_commodity')['contract_code'].count().reset_index()
        commodity_stats.columns = ['futures_commodity', 'total_incidents']
        
        for _, row in commodity_stats.iterrows():
            print(f"{row['futures_commodity']:<8} {row['total_incidents']:<12}")
    
    print("-" * 60)
    print()
    
    print("分析结束！")
    print("=" * 80)
    
    # 返回所有结果，方便进一步处理
    return all_results


if __name__ == "__main__":
    # 运行主分析 - 单品种多年份测试
    # futures_commodity = "fu"
    # main(futures_commodity=futures_commodity, year=2022)
    # main(futures_commodity=futures_commodity, year=2023)
    # main(futures_commodity=futures_commodity, year=2024)
    # main(futures_commodity=futures_commodity, year=2025)
    
    # 使用多品种分析函数进行测试
    # 示例1：分析单个品种的多个年份
    # analyze_multiple_commodities(commodities=["jd"], years=[2022, 2023, 2024, 2025])
    
    # 示例2：分析多个品种的单个年份
    # analyze_multiple_commodities(commodities=["PP", "TA", "jd"], years=[2023]) 
    # ["CS", "TA", "C","M","RM","FG","PP","SF","SM","RB","HC","BU","FU","V", "JD", "EG", "SR", "SA", "Y", "P", "CF"]
    
    # 示例3：分析多个品种的多个年份
    analyze_multiple_commodities(commodities=["CS", "TA", "C","M","RM","FG","PP","SF","SM","RB","HC","BU","FU","V", "JD", "EG", "SR", "SA", "Y", "P", "CF"], years=[2022, 2023, 2024, 2025])
    
