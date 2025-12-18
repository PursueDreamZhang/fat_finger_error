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

def main(futures_commodity="TA", year=2024, difference_threshold=1.0, window_size=3,
         amplitude_ratio_threshold=2.0, volume_threshold=10, min_absolute_difference_pct=1.0):
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
    """
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
    
    print("开始检测乌龙指事件...")
    
    for i, contract_code in enumerate(contracts, 1):
        print(f"  处理合约 {i}/{len(contracts)}: {contract_code}")
        
        try:
            # 检测乌龙指事件（不保存到CSV，提高效率）
            full_data, events_data = detector.detect_fat_finger_events(
                target_code=contract_code,
                reference_codes=reference_codes,
                start_date=start_date,
                end_date=end_date,
                save_to_csv=False,
                difference_threshold=difference_threshold,
                window_size=window_size,
                amplitude_ratio_threshold=amplitude_ratio_threshold,
                min_absolute_difference_pct=min_absolute_difference_pct,
                volume_threshold=volume_threshold
            )
            
            if events_data is not None and not events_data.empty:
                print(f"    检测到 {len(events_data)} 起事件")
                
                # 提取关键信息，避免复杂的合并操作
                for _, event_row in events_data.iterrows():
                    event_info = {
                        'date': event_row.get('date', ''),
                        'contract_code': contract_code,
                        'target_code': contract_code
                    }
                    all_events_info.append(event_info)
        except Exception as e:
            print(f"    处理合约 {contract_code} 时出错: {str(e)}")
            continue
    
    print()
    
    # 构建事件数据帧
    if all_events_info:
        total_incidents = len(all_events_info)
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
        print("月度摘要报告：")
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
        print("未检测到乌龙指事件")
    
    print()
    print("分析完成！")

if __name__ == "__main__":
    # 运行主分析
    main()
