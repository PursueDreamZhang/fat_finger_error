# -*- coding: utf-8 -*-
"""
期货"乌龙指"异常交易识别示例 - 基于历史统计的检测版

本示例展示如何使用基于历史统计的FatFingerDetector类检测乌龙指事件。
新方案通过计算目标期货品种与参考品种当天的最高价和最低价差值，
然后将两者差值与历史平均水平进行比较，当差值显著大于历史平均水平时判定为乌龙指。
"""

from datetime import datetime
import os
import warnings
warnings.filterwarnings('ignore')

# 导入重构后的FatFingerDetector类
from fat_finger_detector import FatFingerDetector

def main():
    """
    主函数：演示如何使用基于历史统计的FatFingerDetector检测乌龙指事件
    """
    print("=" * 60)
    print("期货乌龙指检测系统 - 基于历史统计的检测版示例")
    print("=" * 60)
    print()
    
    # 1. 创建检测器实例
    detector = FatFingerDetector()
    
    # 2. 设置参数
    target_code = "C2409"  # 目标期货品种代码（玉米2403合约）
    reference_codes = ["C2407", "C2411"]  # 参考期货品种代码列表
    threshold_pct = 4  # 价格差值差异阈值（百分比）

    window = 20  # 历史统计窗口（天数）
    
    # 设置日期范围（使用固定日期范围测试）
    end_date = "20241231"  # 固定结束日期
    start_date = "20220101"  # 固定开始日期
    
    print(f"目标品种: {target_code}")
    print(f"参考品种: {', '.join(reference_codes)}")
    print(f"检测阈值: {threshold_pct}%")
    print(f"历史统计窗口: {window}天")
    print(f"数据范围: {start_date} 至 {end_date}")
    print()
    print("检测逻辑说明：")
    print("1. 计算目标品种和参考品种当天的最高价与最低价差值")
    print("2. 计算两者差值，并与历史平均水平进行比较")
    print("3. 当差值显著大于历史平均水平时，判定为乌龙指事件")
    print()
    
    # 3. 检测乌龙指事件
    print("开始检测乌龙指事件...")
    full_data, events_data = detector.detect_fat_finger_events(
        target_code=target_code,
        reference_codes=reference_codes,
        start_date=start_date,
        end_date=end_date,
        save_to_csv=True,
        difference_threshold=threshold_pct
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
            threshold_pct=threshold_pct,
            start_date=start_date,
            end_date=end_date
        )
        
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

    print("\n示例运行完成！")

    # 清理生成的文件
def clean_generated_files():

    # 1. 创建检测器实例
    detector = FatFingerDetector()
    """
    清理生成的报告、图片和非缓存的CSV文件
    """
    detector.clean_generated_files()

if __name__ == "__main__":
    # 运行主示例
    main()
   #clean_generated_files()
