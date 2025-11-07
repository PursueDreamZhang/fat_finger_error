#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
单元测试：验证基于最低值和最高值差值的乌龙指检测逻辑
"""

import unittest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import sys
import os

# 添加父目录到系统路径，以便导入fat_finger_detector模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from fat_finger_detector import FatFingerDetector


class TestFatFingerDetection(unittest.TestCase):
    """测试基于最低值和最高值差值的乌龙指检测逻辑"""
    
    def setUp(self):
        """设置测试数据"""
        self.detector = FatFingerDetector()
        
        # 创建测试用的目标品种数据
        dates = [datetime.now() - timedelta(days=i) for i in range(30, 0, -1)]
        
        # 正常情况下的目标品种数据
        self.target_data_normal = pd.DataFrame({
            'date': dates,
            'low': [100 + i * 0.1 for i in range(30)],
            'high': [110 + i * 0.1 for i in range(30)],
            'open': [102 + i * 0.1 for i in range(30)],
            'close': [108 + i * 0.1 for i in range(30)],
            'volume': [1000 + i * 10 for i in range(30)],
            '持仓量': [5000 + i * 10 for i in range(30)]
        })
        
        # 包含异常的目标品种数据（在第15天有一个异常低的最低值）
        self.target_data_anomaly = self.target_data_normal.copy()
        self.target_data_anomaly.loc[14, 'low'] = 80  # 异常低的最低值
        
        # 包含异常的目标品种数据（在第20天有一个异常高的最高值）
        self.target_data_anomaly2 = self.target_data_normal.copy()
        self.target_data_anomaly2.loc[19, 'high'] = 150  # 异常高的最高值
        
        # 参考品种数据
        self.reference_data = {
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
    
    def test_calculate_min_max_differences(self):
        """测试计算最低值和最高值差值的函数"""
        # 测试正常情况
        result = self.detector.calculate_min_max_differences(
            self.target_data_normal, self.reference_data, 'TARGET', ['REF1', 'REF2']
        )
        
        # 检查结果是否包含预期的列
        self.assertIn('date', result.columns)
        self.assertIn('TARGET_low', result.columns)
        self.assertIn('TARGET_high', result.columns)
        self.assertIn('low_diff_TARGET_REF1', result.columns)
        self.assertIn('high_diff_TARGET_REF1', result.columns)
        self.assertIn('low_diff_TARGET_REF2', result.columns)
        self.assertIn('high_diff_TARGET_REF2', result.columns)
        
        # 检查差值计算是否正确
        # 对于第0天，TARGET_low - REF1_low = 100 - 95 = 5
        self.assertAlmostEqual(result.loc[0, 'low_diff_TARGET_REF1'], 5.0, places=1)
        # 对于第0天，TARGET_high - REF1_high = 110 - 105 = 5
        self.assertAlmostEqual(result.loc[0, 'high_diff_TARGET_REF1'], 5.0, places=1)
    
    def test_detect_min_max_anomalies(self):
        """测试检测最低值和最高值异常的函数"""
        # 计算最低值和最高值差值
        min_max_data = self.detector.calculate_min_max_differences(
            self.target_data_anomaly, self.reference_data, 'TARGET', ['REF1', 'REF2']
        )
        
        # 检测异常
        full_data, events_data = self.detector.detect_min_max_anomalies(
            min_max_data, 'TARGET', ['REF1', 'REF2'], window=10, threshold_pct=50.0
        )
        
        # 检查结果
        self.assertIsNotNone(full_data)
        self.assertIsNotNone(events_data)
        
        # 检查是否检测到了异常
        self.assertTrue(len(events_data) > 0)
        
        # 检查异常事件是否包含异常原因
        self.assertIn('anomaly_reasons', events_data.columns)
        
        # 检查异常事件是否包含最低值异常
        anomaly_reasons = events_data.iloc[0]['anomaly_reasons']
        self.assertIn('最低值差异', anomaly_reasons)
    
    def test_detect_fat_finger_events_with_low_anomaly(self):
        """测试检测包含最低值异常的乌龙指事件"""
        # 使用模拟数据进行测试，直接调用内部方法
        min_max_data = self.detector.calculate_min_max_differences(
            self.target_data_anomaly, self.reference_data, 'TARGET', ['REF1', 'REF2']
        )
        
        full_data, events_data = self.detector.detect_min_max_anomalies(
            min_max_data, 'TARGET', ['REF1', 'REF2'], window=10, threshold_pct=50.0
        )
        
        # 检查结果
        self.assertIsNotNone(full_data)
        self.assertIsNotNone(events_data)
        
        # 检查是否检测到了异常
        self.assertTrue(len(events_data) > 0)
        
        # 检查异常事件是否包含异常原因
        self.assertIn('anomaly_reasons', events_data.columns)
        
        # 检查异常事件是否包含最低值异常
        anomaly_reasons = events_data.iloc[0]['anomaly_reasons']
        self.assertIn('最低值差异', anomaly_reasons)
    
    def test_detect_fat_finger_events_with_high_anomaly(self):
        """测试检测包含最高值异常的乌龙指事件"""
        # 使用模拟数据进行测试，直接调用内部方法
        min_max_data = self.detector.calculate_min_max_differences(
            self.target_data_anomaly2, self.reference_data, 'TARGET', ['REF1', 'REF2']
        )
        
        full_data, events_data = self.detector.detect_min_max_anomalies(
            min_max_data, 'TARGET', ['REF1', 'REF2'], window=10, threshold_pct=50.0
        )
        
        # 检查结果
        self.assertIsNotNone(full_data)
        self.assertIsNotNone(events_data)
        
        # 检查是否检测到了异常
        self.assertTrue(len(events_data) > 0)
        
        # 检查异常事件是否包含异常原因
        self.assertIn('anomaly_reasons', events_data.columns)
        
        # 检查异常事件是否包含最高值异常
        anomaly_reasons = events_data.iloc[0]['anomaly_reasons']
        self.assertIn('最高值差异', anomaly_reasons)
    
    def test_integration_with_mock_data(self):
        """使用模拟数据进行集成测试"""
        # 直接调用内部方法，绕过数据获取部分
        min_max_data = self.detector.calculate_min_max_differences(
            self.target_data_anomaly, self.reference_data, 'TARGET', ['REF1', 'REF2']
        )
        
        full_data, events_data = self.detector.detect_min_max_anomalies(
            min_max_data, 'TARGET', ['REF1', 'REF2'], window=10, threshold_pct=50.0
        )
        
        # 验证结果
        self.assertIsNotNone(full_data)
        self.assertIsNotNone(events_data)
        
        # 检查是否检测到了异常
        self.assertTrue(len(events_data) > 0)
        
        # 检查异常事件是否包含异常原因
        self.assertIn('anomaly_reasons', events_data.columns)
        
        # 检查异常事件是否包含最低值异常
        anomaly_reasons = events_data.iloc[0]['anomaly_reasons']
        self.assertIn('最低值差异', anomaly_reasons)


if __name__ == '__main__':
    # 运行单元测试
    unittest.main(verbosity=2)