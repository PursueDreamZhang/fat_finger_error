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
    
    # 设置日期范围为整个年份
    start_date = f"{year}0101"
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
    
    # 收集所有合约的乌龙指事件
    all_events = []
    
    print("开始检测乌龙指事件...")
    for contract_code in contracts:
        print(f"  处理合约: {contract_code}")
        
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
            # 添加合约代码到事件数据中，便于交叉引用
            events_data['contract_code'] = contract_code
            all_events.append(events_data)
    
    print()
    
    # 合并所有事件数据
    if all_events:
        combined_events = pd.concat(all_events, ignore_index=True)
        total_incidents = len(combined_events)
        print(f"共检测到 {total_incidents} 起潜在的乌龙指事件")
        print()
        
        # 转换日期格式，便于按月分组
        combined_events['date_dt'] = pd.to_datetime(combined_events['date'], format='%Y%m%d')
        
        # 按月份分组统计
        monthly_stats = combined_events.groupby(combined_events['date_dt'].dt.month).agg({
            'date': ['count', lambda x: sorted(x.tolist())],
            'contract_code': lambda x: sorted(set(x.tolist()))
        })
        monthly_stats.columns = ['incident_count', 'event_dates', 'involved_contracts']
        monthly_stats = monthly_stats.sort_index()
        
        # 生成月度摘要报告
        print("月度摘要报告：")
        print("-" * 80)
        print(f"{'月份':<8} {'事件数量':<12} {'涉及合约':<20} {'事件日期':<30}")
        print("-" * 80)
        
        for month in range(1, 13):
            month_name = f"{month:02d}月"
            
            if month in monthly_stats.index:
                stats = monthly_stats.loc[month]
                count = stats['incident_count']
                contracts = ",".join(stats['involved_contracts'])
                dates = ",".join(stats['event_dates'])
            else:
                count = 0
                contracts = "-"
                dates = "-"
            
            print(f"{month_name:<8} {count:<12} {contracts:<20} {dates:<30}")
        
        print("-" * 80)
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
