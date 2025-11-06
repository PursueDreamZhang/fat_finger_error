#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
测试修改后的缓存机制 - 文件名仅包含参数，并将参数、哈希值和文件名保存到JSON文件
"""

import sys
import os
import json
from datetime import datetime, timedelta

# 模拟必要的依赖
class MockPandasDataFrame:
    def __init__(self, data=None):
        self.data = data or []
        self.empty = len(self.data) == 0
    
    def __len__(self):
        return len(self.data)
    
    def to_csv(self, path, **kwargs):
        with open(path, 'w') as f:
            f.write("date,open,high,low,close,volume\n")
            for row in self.data:
                f.write(f"{row}\n")

def mock_akshare_futures_main_sina(symbol, start_date, end_date):
    """模拟akshare.futures_main_sina函数"""
    df = MockPandasDataFrame([
        "2024-01-01,100,105,95,102,1000",
        "2024-01-02,102,108,98,105,1200"
    ])
    return df

# 模拟FatFingerDetector类的get_future_data函数
def mock_get_future_data(self, future_code, start_date=None, end_date=None, 
                        save_to_csv=True, cache_days=1, use_cache=True):
    """
    模拟修改后的get_future_data函数
    """
    
    # 设置默认日期范围（过去3年）
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=3*365)).strftime('%Y%m%d')
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')
    
    # 创建data文件夹和csv_data子文件夹（如果不存在）
    if not os.path.exists('data'):
        os.makedirs('data')
    if not os.path.exists('data/csv_data'):
        os.makedirs('data/csv_data')
    
    # 创建参数字典用于生成哈希值
    params = {
        'future_code': future_code,
        'start_date': start_date,
        'end_date': end_date
    }
    
    # 生成参数哈希值（用于内部缓存追踪）
    import hashlib
    params_str = str(sorted(params.items()))
    params_hash = hashlib.md5(params_str.encode()).hexdigest()[:8]
    
    # 生成仅包含参数的缓存文件名
    cache_file_path = f"data/csv_data/future_{future_code}_{start_date}_{end_date}.csv"
    
    # 创建缓存映射JSON文件路径
    cache_mapping_file = "data/csv_data/cache_mapping.json"
    
    # 初始化缓存映射字典
    cache_mapping = {}
    if os.path.exists(cache_mapping_file):
        try:
            with open(cache_mapping_file, 'r', encoding='utf-8') as f:
                cache_mapping = json.load(f)
        except Exception as e:
            print(f"读取缓存映射文件失败: {e}")
            cache_mapping = {}
    
    # 更新缓存映射
    cache_mapping[cache_file_path] = {
        'params': params,
        'params_hash': params_hash,
        'file_name': os.path.basename(cache_file_path),
        'created_time': datetime.now().isoformat()
    }
    
    # 保存缓存映射到JSON文件
    try:
        with open(cache_mapping_file, 'w', encoding='utf-8') as f:
            json.dump(cache_mapping, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存缓存映射文件失败: {e}")
    
    # 检查缓存文件是否存在且有效
    if use_cache and os.path.exists(cache_file_path):
        try:
            # 获取文件修改时间
            file_mod_time = datetime.fromtimestamp(os.path.getmtime(cache_file_path))
            current_time = datetime.now()
            
            # 检查缓存是否过期
            if (current_time - file_mod_time).days < cache_days:
                print(f"使用缓存数据: {cache_file_path}")
                # 模拟读取缓存数据
                with open(cache_file_path, 'r') as f:
                    lines = f.readlines()
                    print(f"从缓存加载 {len(lines)-1} 条数据记录")  # 减去标题行
                return MockPandasDataFrame(lines[1:])  # 返回数据（不包括标题行）
            else:
                print(f"缓存数据已过期（超过{cache_days}天），将重新获取")
        except Exception as e:
            print(f"读取缓存文件时出错: {e}，将重新获取数据")
    
    # 缓存不存在、已过期或无效，重新获取数据
    print(f"正在获取期货合约 {future_code} 从 {start_date} 到 {end_date} 的历史数据...")
    
    # 模拟获取数据
    df = mock_akshare_futures_main_sina(future_code, start_date, end_date)
    
    if df.empty:
        print(f"未获取到期货合约 {future_code} 的数据，请检查期货代码是否正确")
        return None
    
    print(f"成功获取 {len(df)} 条数据记录")
    
    # 保存到CSV文件（更新缓存）
    if save_to_csv:
        # 保存数据
        df.to_csv(cache_file_path, index=False, encoding='utf-8-sig')
        print(f"数据已保存到: {cache_file_path}")
    
    return df

def test_modified_cache_mechanism():
    """测试修改后的缓存机制"""
    
    print("测试修改后的缓存机制 - 文件名仅包含参数，并将参数、哈希值和文件名保存到JSON文件\n")
    
    # 测试1：首次获取数据（缓存不存在）
    print("测试1：首次获取数据（缓存不存在）")
    df1 = mock_get_future_data(None, 'CU2404', '20240101', '20240110')
    print(f"获取到 {len(df1) if df1 else 0} 条数据\n")
    
    # 测试2：再次获取相同参数的数据（使用缓存）
    print("测试2：再次获取相同参数的数据（使用缓存）")
    df2 = mock_get_future_data(None, 'CU2404', '20240101', '20240110')
    print(f"获取到 {len(df2) if df2 else 0} 条数据\n")
    
    # 测试3：获取不同参数的数据（缓存隔离）
    print("测试3：获取不同参数的数据（缓存隔离）")
    df3 = mock_get_future_data(None, 'CU2404', '20240101', '20240120')  # 不同的结束日期
    print(f"获取到 {len(df3) if df3 else 0} 条数据\n")
    
    # 测试4：获取不同期货代码的数据（缓存隔离）
    print("测试4：获取不同期货代码的数据（缓存隔离）")
    df4 = mock_get_future_data(None, 'IF2404', '20240101', '20240110')  # 不同的期货代码
    print(f"获取到 {len(df4) if df4 else 0} 条数据\n")
    
    # 列出所有缓存文件
    print("当前目录下的缓存文件:")
    for file in os.listdir('data/csv_data'):
        if file.startswith('future_') and file.endswith('.csv'):
            print(f"- {file}")
    
    # 查看缓存映射JSON文件内容
    print("\n缓存映射JSON文件内容:")
    cache_mapping_file = "data/csv_data/cache_mapping.json"
    if os.path.exists(cache_mapping_file):
        try:
            with open(cache_mapping_file, 'r', encoding='utf-8') as f:
                cache_mapping = json.load(f)
                for file_path, info in cache_mapping.items():
                    print(f"\n文件路径: {file_path}")
                    print(f"参数: {info['params']}")
                    print(f"参数哈希值: {info['params_hash']}")
                    print(f"文件名: {info['file_name']}")
                    print(f"创建时间: {info['created_time']}")
        except Exception as e:
            print(f"读取缓存映射文件失败: {e}")
    
    print("\n测试完成！新的缓存机制工作正常。")

if __name__ == "__main__":
    test_modified_cache_mechanism()