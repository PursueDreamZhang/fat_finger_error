#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
演示基于最低值和最高值差值的乌龙指检测逻辑
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys
import os

# 添加父目录到系统路径，以便导入fat_finger_detector模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fat_finger_detector import FatFingerDetector


def main():
    """
    演示基于最低值和最高值差值的乌龙指检测逻辑
    """
    print("=" * 60)
    print("基于最低值和最高值差值的乌龙指检测逻辑演示")
    print("=" * 60)
    print()
    
    # 创建检测器实例
    detector = FatFingerDetector()
    
    # 创建测试用的目标品种数据
    dates = [datetime.now() - timedelta(days=i) for i in range(30, 0, -1)]
    
    # 正常情况下的目标品种数据
    target_data = pd.DataFrame({
        'date': dates,
        'low': [100 + i * 0.1 for i in range(30)],
        'high': [110 + i * 0.1 for i in range(30)],
        'open': [102 + i * 0.1 for i in range(30)],
        'close': [108 + i * 0.1 for i in range(30)],
        'volume': [1000 + i * 10 for i in range(30)],
        '持仓量': [5000 + i * 10 for i in range(30)]
    })
    
    # 在第15天添加一个异常低的最低值
    target_data.loc[14, 'low'] = 80  # 异常低的最低值
    
    # 在第20天添加一个异常高的最高值
    target_data.loc[19, 'high'] = 150  # 异常高的最高值
    
    # 参考品种数据
    reference_data = {
        'REF1': pd.DataFrame({
            'date': dates,
            'low': [95 + i * 0.1 for i in range(30)],
            'high': [105 + i * 0.1 for i in range(30)],
            'open': [97 + i * 0.1 for i in range(30)],
            'close': [103 + i * 0.1 for i in range(30)],
            'volume': [900 + i * 10 for i in range(30)],
            '持仓量': [4500 + i * 10 for i in range(30)]
        }),
        'REF2': pd.DataFrame({
            'date': dates,
            'low': [98 + i * 0.1 for i in range(30)],
            'high': [108 + i * 0.1 for i in range(30)],
            'open': [100 + i * 0.1 for i in range(30)],
            'close': [106 + i * 0.1 for i in range(30)],
            'volume': [950 + i * 10 for i in range(30)],
            '持仓量': [4800 + i * 10 for i in range(30)]
        })
    }
    
    print("目标品种数据示例:")
    print(target_data[['date', 'low', 'high']].head())
    print()
    print("参考品种1数据示例:")
    print(reference_data['REF1'][['date', 'low', 'high']].head())
    print()
    
    # 计算最低值和最高值差值
    print("计算最低值和最高值差值...")
    min_max_data = detector.calculate_min_max_differences(
        target_data, reference_data, 'TARGET', ['REF1', 'REF2']
    )
    
    print("最低值和最高值差值数据示例:")
    print(min_max_data[['date', 'TARGET_low', 'TARGET_high', 'low_diff_TARGET_REF1', 'high_diff_TARGET_REF1']].head())
    print()
    
    # 检测异常
    print("检测异常...")
    full_data, events_data = detector.detect_min_max_anomalies(
        min_max_data, 'TARGET', ['REF1', 'REF2'], window=10, threshold_pct=50.0
    )
    
    # 输出结果
    print(f"检测到 {len(events_data)} 起异常事件")
    print()
    
    if not events_data.empty:
        print("异常事件详情:")
        for idx, event in events_data.iterrows():
            print(f"日期: {event['date'].strftime('%Y-%m-%d')}")
            print(f"异常原因: {event['anomaly_reasons']}")
            print()
    
    print("演示完成！")


if __name__ == '__main__':
    main()