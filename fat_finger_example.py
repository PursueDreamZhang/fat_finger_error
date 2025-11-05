# -*- coding: utf-8 -*-
"""
期货"乌龙指"异常交易识别示例 - 重构版

本示例展示如何使用重构后的FatFingerDetector类检测乌龙指事件。
新方案通过比较目标期货品种与多个其他期货品种在过去20天内的最高价和最低价差值，
当差值超出预设范围时，系统判定为发生了乌龙指事件。
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import os
import warnings
warnings.filterwarnings('ignore')

# 导入重构后的FatFingerDetector类
from fat_finger_detector import FatFingerDetector

def main():
    """
    主函数：演示如何使用重构后的FatFingerDetector检测乌龙指事件
    """
    print("=" * 60)
    print("期货乌龙指检测系统 - 重构版示例")
    print("=" * 60)
    print()
    
    # 1. 创建检测器实例
    detector = FatFingerDetector()
    
    # 2. 设置参数
    target_code = "CU2404"  # 目标期货品种代码（沪铜2404合约）
    reference_codes = ["CU2405", "CU2403", "CU0"]  # 参考期货品种代码列表
    threshold_pct = 50.0  # 价格差值差异阈值（百分比）
    window = 20  # 计算窗口（天数）
    
    # 设置日期范围（使用固定日期范围测试）
    end_date = "20231231"  # 固定结束日期
    start_date = "20230105"  # 固定开始日期
    
    print(f"目标品种: {target_code}")
    print(f"参考品种: {', '.join(reference_codes)}")
    print(f"检测阈值: {threshold_pct}%")
    print(f"计算窗口: {window}天")
    print(f"数据范围: {start_date} 至 {end_date}")
    print()
    
    # 3. 检测乌龙指事件
    print("开始检测乌龙指事件...")
    full_data, events_data = detector.detect_fat_finger_events(
        target_code=target_code,
        reference_codes=reference_codes,
        start_date=start_date,
        end_date=end_date,
        threshold_pct=threshold_pct,
        window=window,
        save_to_csv=True
    )
    
    # 4. 检查检测结果
    if full_data is None:
        print("获取数据失败，请检查网络连接或期货代码是否正确")
        return
    
    print(f"获取到 {len(full_data)} 天的数据")
    
    if events_data is None or events_data.empty:
        print("未检测到乌龙指事件")
    else:
        print(f"检测到 {len(events_data)} 起潜在的乌龙指事件")
        print()
        
        # 5. 生成检测报告
        report = detector.generate_report(
            events_data=events_data,
            target_code=target_code,
            reference_codes=reference_codes,
            threshold_pct=threshold_pct
        )
        
        print(report)
        
        # 保存报告到文件
        if not os.path.exists('data/report'):
            os.makedirs('data/report')
        
        report_path = f"data/report/fat_finger_report_{target_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(report)
        
        print(f"报告已保存到: {report_path}")
    
    # 6. 可视化结果
    print("\n生成可视化图表...")
    if not os.path.exists('data/pic'):
        os.makedirs('data/pic')
    
    plot_path = f"data/pic/fat_finger_analysis_{target_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    
    detector.visualize_fat_finger_events(
        target_code=target_code,
        reference_codes=reference_codes,
        full_data=full_data,
        events_data=events_data,
        save_path=plot_path
    )
    
    print("\n分析完成！")

def test_with_different_parameters():
    """
    测试不同参数下的检测结果
    """
    print("\n" + "=" * 60)
    print("测试不同参数下的检测结果")
    print("=" * 60)
    
    # 创建检测器实例
    detector = FatFingerDetector()
    
    # 测试参数组合
    test_cases = [
        {
            "target_code": "CU2404",
            "reference_codes": ["CU2405", "AL2404", "ZN2404"],
            "threshold_pct": 30.0,
            "window": 20,
            "description": "低阈值测试"
        },
        {
            "target_code": "CU2404",
            "reference_codes": ["CU2405", "AL2404", "ZN2404"],
            "threshold_pct": 70.0,
            "window": 20,
            "description": "高阈值测试"
        },
        {
            "target_code": "CU2404",
            "reference_codes": ["CU2405", "AL2404", "ZN2404"],
            "threshold_pct": 50.0,
            "window": 10,
            "description": "短窗口测试"
        },
        {
            "target_code": "AL2404",
            "reference_codes": ["AL2405", "CU2404", "ZN2404"],
            "threshold_pct": 50.0,
            "window": 20,
            "description": "不同目标品种测试"
        }
    ]
    
    # 设置日期范围
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n测试案例 {i}: {test_case['description']}")
        print(f"目标品种: {test_case['target_code']}")
        print(f"参考品种: {', '.join(test_case['reference_codes'])}")
        print(f"阈值: {test_case['threshold_pct']}%")
        print(f"窗口: {test_case['window']}天")
        
        # 检测乌龙指事件
        full_data, events_data = detector.detect_fat_finger_events(
            target_code=test_case['target_code'],
            reference_codes=test_case['reference_codes'],
            start_date=start_date,
            end_date=end_date,
            threshold_pct=test_case['threshold_pct'],
            window=test_case['window'],
            save_to_csv=False
        )
        
        # 输出结果
        if events_data is None or events_data.empty:
            print("结果: 未检测到乌龙指事件")
        else:
            print(f"结果: 检测到 {len(events_data)} 起潜在的乌龙指事件")

def analyze_price_spread_patterns():
    """
    分析不同期货品种的价格差值模式
    """
    print("\n" + "=" * 60)
    print("分析不同期货品种的价格差值模式")
    print("=" * 60)
    
    # 创建检测器实例
    detector = FatFingerDetector()
    
    # 要分析的期货品种
    future_codes = ["CU2404", "CU2405", "AL2404", "AL2405", "ZN2404", "ZN2405"]
    
    # 设置日期范围
    end_date = datetime.now().strftime('%Y%m%d')
    start_date = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')
    
    # 存储各品种的价格差值数据
    spread_data = {}
    
    for code in future_codes:
        print(f"\n获取 {code} 的数据...")
        data = detector.get_future_data(
            future_code=code,
            start_date=start_date,
            end_date=end_date,
            save_to_csv=False,
            cache_days=7,
            use_cache=True
        )
        
        if data is not None and not data.empty:
            # 计算价格差值
            data = detector.calculate_price_spread(data, window=20)
            spread_data[code] = data
            print(f"获取到 {len(data)} 天的数据")
        else:
            print(f"无法获取 {code} 的数据")
    
    # 分析价格差值统计特征
    if spread_data:
        print("\n价格差值统计特征:")
        print("-" * 40)
        print(f"{'品种代码':<10} {'平均差值':<10} {'最大差值':<10} {'最小差值':<10} {'标准差':<10}")
        print("-" * 40)
        
        for code, data in spread_data.items():
            avg_spread = data['price_spread'].mean()
            max_spread = data['price_spread'].max()
            min_spread = data['price_spread'].min()
            std_spread = data['price_spread'].std()
            
            print(f"{code:<10} {avg_spread:<10.2f} {max_spread:<10.2f} {min_spread:<10.2f} {std_spread:<10.2f}")

if __name__ == "__main__":
    # 运行主示例
    main()
    
    # 测试不同参数
    #test_with_different_parameters()
    
    # 分析价格差值模式
    #analyze_price_spread_patterns()