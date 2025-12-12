# -*- coding: utf-8 -*-
"""
期货"乌龙指"异常交易识别指标系统 - 重构版

乌龙指是指由于操作失误（如价格输入错误、数量错误等）导致的异常交易，
通常表现为价格瞬间大幅偏离正常水平，但很快恢复。

本系统通过比较目标期货品种与多个其他期货品种在过去20天内的最高价和最低价差值，
识别潜在的乌龙指事件，帮助快速定位异常交易日。

核心逻辑：
1. 计算目标品种和参考品种在过去20天内的最高价和最低价差值
2. 比较目标品种与各参考品种的差值差异
3. 当差值差异超出预设阈值时，判定为乌龙指事件
"""

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, timedelta
import os
import warnings
import json
import hashlib
warnings.filterwarnings('ignore')

# 导入缓存管理器
try:
    from cache_manager import CacheManager
except ImportError:
    print("警告：无法导入缓存管理器，将使用内置缓存功能")
    CacheManager = None

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

class FatFingerDetector:
    """
    期货"乌龙指"异常交易识别系统 - 重构版
    
    核心逻辑：
    1. 获取目标期货品种和多个参考期货品种的历史数据
    2. 计算每个品种在过去20天内的最高价和最低价差值
    3. 比较目标品种与各参考品种的差值差异
    4. 当差值差异超出预设阈值时，判定为乌龙指事件
    """
    
    def __init__(self):
        """初始化乌龙指检测器"""
        self.results = {}
        
        # 初始化缓存管理器
        if CacheManager is not None:
            self.cache_manager = CacheManager()
        else:
            self.cache_manager = None
            print("警告：缓存管理器未初始化，将使用内置缓存功能")
        
    def get_future_data(self, future_code, start_date=None, end_date=None, save_to_csv=True, 
                        cache_days=10000, use_cache=True):
        """
        获取指定期货合约的历史数据并保存到本地，支持本地缓存机制
        
        参数:
        - future_code: 期货合约代码，如 'CU2401'（沪铜2401合约）
        - start_date: 开始日期，格式为 'YYYYMMDD'，默认为3年前
        - end_date: 结束日期，格式为 'YYYYMMDD'，默认为今天
        - save_to_csv: 是否保存为CSV文件，默认为True
        - cache_days: 缓存有效期（天数），默认为1天
        - use_cache: 是否使用缓存，默认为True
        
        返回:
        - DataFrame: 包含期货历史数据的数据框
        """
        
        # 导入akshare
        try:
            import akshare as ak
        except ImportError:
            print("错误：未安装akshare库，请先安装：pip install akshare")
            return None
        
        # 设置默认日期范围（过去3年）
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=3*365)).strftime('%Y%m%d')
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')
        
        # 创建data文件夹和csv_data子文件夹（如果不存在）
        if not os.path.exists('data'):
            os.makedirs('data')
        if not os.path.exists('data/csv_data/data'):
            os.makedirs('data/csv_data/data')
        
        # 创建参数字典用于生成哈希值
        params = {
            'future_code': future_code,
            'start_date': start_date,
            'end_date': end_date
        }
        
        # 生成参数哈希值（用于内部缓存追踪）
        params_str = str(sorted(params.items()))
        params_hash = hashlib.md5(params_str.encode()).hexdigest()[:8]
        
        # 生成仅包含参数的缓存文件名
        cache_file_path = f"data/csv_data/data/future_{future_code}_{start_date}_{end_date}.csv"
        
        # 创建缓存映射JSON文件路径
        cache_mapping_file = "data/csv_data/data/cache_mapping.json"
        
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
                    df = pd.read_csv(cache_file_path)
                    
                    # 确保日期列是datetime类型
                    if 'date' in df.columns:
                        df['date'] = pd.to_datetime(df['date'])
                    
                    # 验证数据有效性
                    if not df.empty and len(df) > 0:
                        print(f"从缓存加载 {len(df)} 条数据记录")
                        return df
                    else:
                        print("缓存数据无效，将重新获取")
                else:
                    print(f"缓存数据已过期（超过{cache_days}天），将重新获取")
            except Exception as e:
                print(f"读取缓存文件时出错: {e}，将重新获取数据")
        
        # 缓存不存在、已过期或无效，重新获取数据
        print(f"正在获取期货合约 {future_code} 从 {start_date} 到 {end_date} 的历史数据...")
        
        try:
            # 使用AKShare获取期货历史数据
            try:
                # 首先尝试使用futures_main_sina接口
                df = ak.futures_main_sina(symbol=future_code, start_date=start_date, end_date=end_date)
                if df.empty:
                    raise Exception("futures_main_sina返回空数据")
            except Exception as e:
                print(f"使用futures_main_sina接口失败: {e}")
                print("尝试使用futures_zh_daily_sina接口获取数据...")
                
                # 尝试使用futures_zh_daily_sina接口
                try:
                    # 添加"0"表示主力合约
                    df = ak.futures_zh_daily_sina(symbol=future_code + "0")
                    if df.empty:
                        raise Exception("futures_zh_daily_sina返回空数据")
                    
                    # 如果使用futures_zh_daily_sina接口，我们需要手动筛选日期范围
                    if 'date' in df.columns:
                        df['date'] = pd.to_datetime(df['date'])
                        # 尝试多种日期格式解析
                        try:
                            start_dt = pd.to_datetime(start_date, format='%Y%m%d')
                            end_dt = pd.to_datetime(end_date, format='%Y%m%d')
                        except:
                            start_dt = pd.to_datetime(start_date)
                            end_dt = pd.to_datetime(end_date)
                        df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]
                        print(f"从获取的数据中筛选出 {len(df)} 条符合日期范围的数据")
                except Exception as e2:
                    print(f"使用futures_zh_daily_sina接口也失败: {e2}")
                    raise Exception(f"所有接口都无法获取期货合约 {future_code} 的数据")
            
            if df.empty:
                print(f"未获取到期货合约 {future_code} 的数据，请检查期货代码是否正确")
                return None
            
            # 筛选日期范围内的数据
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                start_dt = pd.to_datetime(start_date)
                end_dt = pd.to_datetime(end_date)
                df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]
            
            # 添加计算字段
            df = self._add_future_calculated_fields(df)
            
            print(f"成功获取 {len(df)} 条数据记录")
            
            # 保存到CSV文件（更新缓存）
            if save_to_csv:
                # 保存数据
                df.to_csv(cache_file_path, index=False, encoding='utf-8-sig')
                print(f"数据已保存到: {cache_file_path}")
            
            return df
            
        except Exception as e:
            print(f"获取期货数据时出错: {e}")
            return None
    
    def _add_future_calculated_fields(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        添加期货数据计算字段
        
        Args:
            df: 原始期货数据
            
        Returns:
            添加计算字段后的数据
        """
        # 复制数据避免修改原始数据
        df = df.copy()
        
        # 处理日期列 - 标准化列名和格式
        date_col = None
        for col in ['日期', 'date', 'Date']:
            if col in df.columns:
                date_col = col
                break
        
        if date_col is not None:
            # 将日期列重命名为'date'并转换为datetime类型
            df['date'] = pd.to_datetime(df[date_col])
            # 如果原始列名不是'date'，删除原始列以避免重复
            if date_col != 'date':
                df.drop(columns=[date_col], inplace=True)
        else:
            # 如果没有日期列，使用索引创建
            df['date'] = pd.to_datetime(df.index)
        
        # 标准化价格列名
        price_columns = {
            '开盘价': 'open',
            '最高价': 'high',
            '最低价': 'low',
            '收盘价': 'close',
            '成交量': 'volume',
            '持仓量': 'open_interest'
        }
        
        for chinese_col, english_col in price_columns.items():
            if chinese_col in df.columns and english_col not in df.columns:
                df[english_col] = df[chinese_col]
                # 删除原始中文列以避免重复
                df.drop(columns=[chinese_col], inplace=True)
        
        # 确保数值列为float类型
        numeric_columns = ['open', 'high', 'low', 'close', 'volume', 'open_interest']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        # 添加"最高价-最低价差值"列
        if 'high' in df.columns and 'low' in df.columns:
            df['最高价-最低价差值'] = df['high'] - df['low']
        
        return df
    
    def calculate_price_spread(self, data):
        """
        计算期货品种在指定窗口期内的价格差值（最高价-最低价）
        
        参数:
        - data: 期货数据DataFrame
        返回:
        - DataFrame: 包含价格差值的数据
        """
        df = data.copy()
        
        # 确保数据按日期排序
        df = df.sort_values('date')
        
        # 如果已有'最高价-最低价差值'列，直接使用；否则计算
        if '最高价-最低价差值' not in df.columns and 'high' in df.columns and 'low' in df.columns:
            df['最高价-最低价差值'] = df['high'] - df['low']
        
        # 为了保持向后兼容性，同时创建price_spread列，指向相同的值
        df['price_spread'] = df['最高价-最低价差值']
        
        return df
    
    def calculate_historical_averages(self, full_data, target_code, reference_codes, window_size=20):
        """
        计算历史平均值（前N天的平均值）
        
        参数:
        - full_data: 包含差值数据的DataFrame源数据
        - target_code: 目标品种代码
        - reference_codes: 参考品种代码列表
        - window_size: 计算平均值的窗口大小，默认为20天
        
        返回:
        - DataFrame: 添加了历史平均值列的数据
        """

        # 确保数据按日期排序
        full_data = full_data.sort_values('date')

        
        # 为每个参考品种计算历史平均值
        for ref_code in reference_codes:
            # 计算最高值差异的历史平均值
            high_diff_col = f'high_diff_{target_code}_{ref_code}'
            if high_diff_col in full_data.columns:
                # 使用rolling窗口计算前N天的平均值
                hist_mean_col = f'{high_diff_col}_hist_mean'
                full_data[hist_mean_col] = full_data[high_diff_col].rolling(
                        window=window_size, min_periods=1
                    ).mean()
            
            # 计算最低值差异的历史平均值
            low_diff_col = f'low_diff_{target_code}_{ref_code}'
            if low_diff_col in full_data.columns:
                # 使用rolling窗口计算前N天的平均值
                hist_mean_col = f'{low_diff_col}_hist_mean'
                full_data[hist_mean_col] = full_data[low_diff_col].rolling(
                        window=window_size, min_periods=1
                    ).mean() 
        return full_data
    
    def calculate_min_max_differences(self, target_data, reference_data_dict, target_code, reference_codes):
        """
        计算目标品种与所有参考品种的最低值和最高值差值
        
        参数:
        - target_data: 目标品种数据DataFrame
        - reference_data_dict: 参考品种数据字典，键为品种代码，值为DataFrame
        - target_code: 目标品种代码
        - reference_codes: 参考品种代码列表
        
        返回:
        - DataFrame: 包含最低值和最高值差值的数据
        """
        # 确保目标数据包含必要的列
        if 'low' not in target_data.columns or 'high' not in target_data.columns:
            raise ValueError("目标数据必须包含'low'和'high'列")
        
        # 从目标品种的数据开始
        merged_data = target_data[['date', 'low', 'high']].copy()
        merged_data.rename(columns={
            'low': f'{target_code}_low',
            'high': f'{target_code}_high'
        }, inplace=True)
        
        # 为每个参考品种计算最低值和最高值差值并合并
        for ref_code in reference_codes:
            if ref_code in reference_data_dict:
                ref_data = reference_data_dict[ref_code]
                
                # 确保参考数据包含必要的列
                if 'low' not in ref_data.columns or 'high' not in ref_data.columns:
                    print(f"参考品种 {ref_code} 缺少'low'或'high'列，跳过")
                    continue
                
                # 合并目标品种和参考品种的数据
                temp_data = pd.merge(
                    merged_data[['date', f'{target_code}_low', f'{target_code}_high']],
                    ref_data[['date', 'low', 'high']],
                    on='date',
                    how='inner'
                )
                
                # 计算最低值差值
                temp_data[f'low_diff_{target_code}_{ref_code}'] = (
                    temp_data[f'{target_code}_low'] - temp_data['low']
                )
                
                # 计算最高值差值
                temp_data[f'high_diff_{target_code}_{ref_code}'] = (
                    temp_data[f'{target_code}_high'] - temp_data['high']
                )
                
                # 将差值合并到主数据中
                if f'low_diff_{target_code}_{ref_code}' not in merged_data.columns:
                    merged_data = pd.merge(
                        merged_data,
                        temp_data[['date', f'low_diff_{target_code}_{ref_code}', f'high_diff_{target_code}_{ref_code}']],
                        on='date',
                        how='left'
                    )
                    
                    # 将NaN值替换为0，表示没有参考品种数据时差值为0
                    merged_data[f'low_diff_{target_code}_{ref_code}'] = merged_data[f'low_diff_{target_code}_{ref_code}'].fillna(0)
                    merged_data[f'high_diff_{target_code}_{ref_code}'] = merged_data[f'high_diff_{target_code}_{ref_code}'].fillna(0)
        
        return merged_data

    def detect_fat_finger_events(self, target_code, reference_codes, start_date=None, end_date=None,
                                 difference_threshold=0.1, save_to_csv=True, window_size=20,
                                 amplitude_ratio_threshold=2.0):
        """
        检测乌龙指事件（基于价格差异阈值和振幅倍数）

        此函数实现了以下乌龙指检测逻辑：
        1.  **计算高/低价差值**: 对于目标期货品种和每个参考期货品种，计算它们每日的:
            - 最高价差异: (目标最高价 - 参考最高价) / 目标最低价
            - 最低价差异: (目标最低价 - 参考最低价) / 目标最高价
        2.  **计算振幅**: 计算目标品种每日的振幅（最高价-最低价）
        3.  **异常判定**: 
            - 如果任一差异的绝对值超过了 `difference_threshold`，判定为差异异常
            - 如果当前振幅超过历史平均振幅的 `amplitude_ratio_threshold` 倍，判定为振幅异常
            - 只有同时满足差异异常和振幅异常，才将该日期标记为乌龙指事件

        参数:
        - target_code: 目标期货品种代码，如 'CU2404'
        - reference_codes: 参考期货品种代码列表，如 ['CU2405', 'AL2404', 'ZN2404']
        - start_date: 开始日期，格式为 'YYYYMMDD'，默认为60天前
        - end_date: 结束日期，格式为 'YYYYMMDD'，默认为今天
        - difference_threshold: 差异阈值，用于判断异常。
        - save_to_csv: 是否保存结果到CSV，默认为True。
        - window_size: 计算历史平均值的窗口大小，默认为20天
        - amplitude_ratio_threshold: 振幅倍数阈值，当前振幅超过历史平均振幅的倍数，默认为2.0

        返回:
        - tuple: (full_data, events_data)
            - full_data (pd.DataFrame): 包含所有计算结果和异常标记的完整数据。
            - events_data (pd.DataFrame): 仅包含被识别为乌龙指事件的日期数据。
        """
        # 设置默认日期范围
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=60)).strftime('%Y%m%d')
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')
        
        # 获取目标品种数据
        print(f"获取目标品种 {target_code} 的数据...")
        target_data = self.get_future_data(
            future_code=target_code,
            start_date=start_date,
            end_date=end_date,
            save_to_csv=save_to_csv,
            cache_days=7,
            use_cache=True
        )
        
        if target_data is None or target_data.empty:
            print(f"无法获取目标品种 {target_code} 的数据")
            return None, None
        
        # 获取参考品种数据
        reference_data = {}
        for ref_code in reference_codes:
            print(f"获取参考品种 {ref_code} 的数据...")
            ref_data = self.get_future_data(
                future_code=ref_code,
                start_date=start_date,
                end_date=end_date,
                save_to_csv=save_to_csv,
                cache_days=7,
                use_cache=True
            )
            
            if ref_data is not None and not ref_data.empty:
                reference_data[ref_code] = ref_data
            else:
                print(f"无法获取参考品种 {ref_code} 的数据，将跳过")
        
        if not reference_data:
            print("没有获取到任何参考品种数据")
            return target_data, None
        
        print("\n--- 使用基于价格差异阈值的方法检测乌龙指事件 ---")
        
        # 步骤1: 计算目标品种与参考品种的最低值和最高值差值
        print("步骤1: 计算原始高低价差值...")
        full_data = self.calculate_min_max_differences(
            target_data, reference_data, target_code, reference_codes
        )
        
        # 将参考品种的原始最低价和最高价合并到 full_data 中
        for ref_code, ref_df in reference_data.items():
            if ref_code in reference_data:
                full_data = pd.merge(
                    full_data,
                    ref_df[['date', 'low', 'high']],
                    on='date',
                    how='left',
                    suffixes=('', f'_{ref_code}')
                )
                full_data.rename(columns={'low': f'{ref_code}_low', 'high': f'{ref_code}_high'}, inplace=True)
        
        # 步骤1.5: 计算历史平均值（前20天的平均值）
        print("步骤1.5: 计算历史平均值（前20天的平均值）...")
        full_data = self.calculate_historical_averages(full_data, target_code=target_code, reference_codes=reference_codes, window_size=window_size)
        
        # 计算目标品种的振幅（最高价-最低价）
        full_data[f'{target_code}_amplitude'] = full_data[f'{target_code}_high'] - full_data[f'{target_code}_low']
        
        # 计算目标品种的历史平均振幅（前N天的平均值）
        full_data[f'{target_code}_amplitude_avg'] = full_data[f'{target_code}_amplitude'].rolling(
            window=window_size, min_periods=1
        ).mean()

        # 步骤2: 基于 difference_threshold 检测异常
        print(f"步骤2: 使用差异阈值 {difference_threshold} 检测异常...")
        
        anomaly_indices = []
        anomaly_reasons_list = []

        # 初始化结果列
        full_data['is_fat_finger'] = False
        full_data['anomaly_reasons'] = ''

        for idx, row in full_data.iterrows():
            reasons = []
            is_diff_anomaly = False

            for ref_code in reference_codes:
                # 检查最高值差异异常
                high_diff = row.get(f'high_diff_{target_code}_{ref_code}')
                # 获取前20天最高值差异的平均值
                high_diff_hist_mean = row.get(f'high_diff_{target_code}_{ref_code}_hist_mean')
                
                # 使用历史平均值进行异常检测
                if pd.notna(high_diff) and pd.notna(high_diff_hist_mean) and high_diff_hist_mean != 0:
                    # 直接与阈值比较
                    if abs((high_diff - high_diff_hist_mean) / high_diff_hist_mean) > difference_threshold:
                        is_diff_anomaly = True
                        reasons.append(f"与{ref_code}最高值差异异常 (比率: {abs((high_diff - high_diff_hist_mean) / high_diff_hist_mean):.2%})")
                    
                # 检查最低值差异异常
                low_diff = row.get(f'low_diff_{target_code}_{ref_code}')
                # 获取前20天最低值差异的平均值
                low_diff_hist_mean = row.get(f'low_diff_{target_code}_{ref_code}_hist_mean')
                
                # 使用历史平均值进行异常检测
                if pd.notna(low_diff) and pd.notna(low_diff_hist_mean) and low_diff_hist_mean != 0:
                    # 直接与阈值比较
                    if abs((low_diff - low_diff_hist_mean) / low_diff_hist_mean) > difference_threshold:
                        is_diff_anomaly = True
                        reasons.append(f"与{ref_code}最低值差异异常 (比率: {abs((low_diff - low_diff_hist_mean) / low_diff_hist_mean):.2%})")
                
                    
           
            # 最终判定：同时满足差异异常和振幅异常
            is_anomaly = is_diff_anomaly
                  
            if is_anomaly:
                anomaly_indices.append(idx)
                anomaly_reasons_list.append("; ".join(reasons))
        
        # 更新 full_data 中的异常标记和原因
        if anomaly_indices:
            full_data.loc[anomaly_indices, 'is_fat_finger'] = True
            # Create a Series for reasons to align indices correctly
            reasons_series = pd.Series(anomaly_reasons_list, index=anomaly_indices)
            full_data.loc[anomaly_indices, 'anomaly_reasons'] = reasons_series

        # 筛选异常事件
        events_data = full_data[full_data['is_fat_finger']].copy()
        # 把异常数据的日期保存到 events_data 中
        events_data['date'] = full_data.loc[events_data.index, 'date']

        print(f"检测完成，共识别 {len(events_data)} 个乌龙指事件")
        
        # 保存结果
        if save_to_csv:
            # 创建data文件夹和csv_data子文件夹（如果不存在）
            if not os.path.exists('data'):
                os.makedirs('data')
            if not os.path.exists('data/csv_data'):
                os.makedirs('data/csv_data')
            
            # 保存完整数据
            full_data_path = f"data/csv_data/fat_finger_full_{target_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            full_data.to_csv(full_data_path, index=False, encoding='utf-8-sig')
            print(f"完整数据已保存到: {full_data_path}")
            
            # 保存异常事件数据
            if not events_data.empty:
                events_data_path = f"data/csv_data/fat_finger_events_{target_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                events_data.to_csv(events_data_path, index=False, encoding='utf-8-sig')
                print(f"异常事件数据已保存到: {events_data_path}")
        
        return full_data, events_data
    
    def generate_report(self, events_data, target_code, reference_codes, threshold_pct=50.0, start_date=None, end_date=None):
        """
        生成乌龙指检测报告
        
        参数:
        - events_data: 异常事件数据
        - target_code: 目标期货品种代码
        - reference_codes: 参考期货品种代码列表
        - threshold_pct: 价格差值差异阈值（百分比）
        - start_date: 检测开始日期，格式为 'YYYYMMDD'
        - end_date: 检测结束日期，格式为 'YYYYMMDD'
        
        返回:
        - str: 检测报告文本
        """
        if events_data is None or events_data.empty:
            date_range_str = f"{start_date} 至 {end_date}" if start_date and end_date else "指定时间段内"
            return f"在 {date_range_str} 未检测到 {target_code} 的乌龙指事件（阈值: {threshold_pct}%）"
        
        report = []
        report.append("=" * 60)
        report.append(f"期货乌龙指检测报告 - {target_code}")
        report.append("=" * 60)
        report.append(f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"目标品种: {target_code}")
        report.append(f"参考品种: {', '.join(reference_codes)}")
        if start_date and end_date:
            report.append(f"检测日期范围: {start_date} 至 {end_date}")
        report.append(f"检测阈值: {threshold_pct}%")
        report.append(f"检测到的异常事件数量: {len(events_data)}")
        report.append("")
        
        # 按日期排序
        events_data = events_data.sort_values('date')
        
        # 添加每个异常事件的详细信息
        for i, (idx, row) in enumerate(events_data.iterrows()):
            report.append(f"异常事件 #{i+1}:")
            report.append(f"  日期: {row['date'].strftime('%Y-%m-%d')}")
            
            # 显示目标品种的最低价和最高价
            if f'{target_code}_low' in row:
                target_low = row[f'{target_code}_low']
                report.append(f"  {target_code} 最低价: {target_low:.2f}")
            
            if f'{target_code}_high' in row:
                target_high = row[f'{target_code}_high']
                report.append(f"  {target_code} 最高价: {target_high:.2f}")
            
            # 显示与参考品种的最低值和最高值差值以及历史统计信息
            for ref_code in reference_codes:
                # 显示参考品种的最低价和最高价
                if f'{ref_code}_low' in row:
                    ref_low = row[f'{ref_code}_low']
                    report.append(f"  {ref_code} 最低价: {ref_low:.2f}")
                if f'{ref_code}_high' in row:
                    ref_high = row[f'{ref_code}_high']
                    report.append(f"  {ref_code} 最高价: {ref_high:.2f}")

                if f'low_diff_{target_code}_{ref_code}' in row:
                    low_diff = row[f'low_diff_{target_code}_{ref_code}']
                    report.append(f"  与{ref_code}最低值差值: {low_diff:.2f}")
                    
                    # 显示历史统计信息（如果存在）
                    if f'low_diff_{target_code}_{ref_code}_hist_mean' in row:
                        hist_mean = row[f'low_diff_{target_code}_{ref_code}_hist_mean']
                        report.append(f"    最低值差值历史平均: {hist_mean:.2f}")
                    if f'low_diff_{target_code}_{ref_code}_pct_diff_from_mean' in row:
                        pct_diff = row[f'low_diff_{target_code}_{ref_code}_pct_diff_from_mean']
                        report.append(f"    当前最低值差值与历史平均的差异: {pct_diff:.2f}%")
                
                if f'high_diff_{target_code}_{ref_code}' in row:
                    high_diff = row[f'high_diff_{target_code}_{ref_code}']
                    report.append(f"  与{ref_code}最高值差值: {high_diff:.2f}")
                    
                    # 显示历史统计信息（如果存在）
                    if f'high_diff_{target_code}_{ref_code}_hist_mean' in row:
                        hist_mean = row[f'high_diff_{target_code}_{ref_code}_hist_mean']
                        report.append(f"    最高值差值历史平均: {hist_mean:.2f}")
                    if f'high_diff_{target_code}_{ref_code}_pct_diff_from_mean' in row:
                        pct_diff = row[f'high_diff_{target_code}_{ref_code}_pct_diff_from_mean']
                        report.append(f"    当前最高值差值与历史平均的差异: {pct_diff:.2f}%")
            
            # 显示异常原因
            if 'anomaly_reasons' in row:
                report.append(f"  异常原因: {row['anomaly_reasons']}")
            
            report.append("")
        
        report.append("=" * 60)
        report.append("报告结束")
        report.append("=" * 60)
        
        return "\n".join(report)
    
    def visualize_fat_finger_events(self, target_code, reference_codes, full_data, events_data=None, 
                                   save_path=None):
        """
        可视化乌龙指检测结果
        
        参数:
        - target_code: 目标期货品种代码
        - reference_codes: 参考期货品种代码列表
        - full_data: 完整数据
        - events_data: 异常事件数据（可选）
        - save_path: 图片保存路径（可选）
        """
        plt.figure(figsize=(15, 10))
        
        # 创建子图
        fig, axes = plt.subplots(3, 1, figsize=(15, 12))
        
        # 第一个子图：最低价和最高价趋势
        ax1 = axes[0]
        if f'{target_code}_low' in full_data.columns:
            ax1.plot(full_data['date'], full_data[f'{target_code}_low'], 
                    label=f'{target_code} 最低价', linewidth=2)
        if f'{target_code}_high' in full_data.columns:
            ax1.plot(full_data['date'], full_data[f'{target_code}_high'], 
                    label=f'{target_code} 最高价', linewidth=2)
        
        ax1.set_title('目标品种价格趋势', fontsize=14)
        ax1.set_ylabel('价格', fontsize=12)
        ax1.legend()
        ax1.grid(True)
        
        # 第二个子图：最低值差值
        ax2 = axes[1]
        for ref_code in reference_codes:
            if f'low_diff_{target_code}_{ref_code}' in full_data.columns:
                ax2.plot(full_data['date'], full_data[f'low_diff_{target_code}_{ref_code}'], 
                        label=f'{target_code} vs {ref_code} 最低值差值', linestyle='-')
        
        # 添加零线
        ax2.axhline(y=0, color='gray', linestyle='-', alpha=0.5)
        
        # 标记异常事件
        if events_data is not None and not events_data.empty:
            for idx, row in events_data.iterrows():
                ax2.axvline(x=row['date'], color='red', alpha=0.3, linestyle='--')
        
        ax2.set_title('目标品种与参考品种的最低值差值', fontsize=14)
        ax2.set_xlabel('日期', fontsize=12)
        ax2.set_ylabel('最低值差值', fontsize=12)
        ax2.legend()
        ax2.grid(True)
        
        # 第三个子图：最高值差值
        ax3 = axes[2]
        for ref_code in reference_codes:
            if f'high_diff_{target_code}_{ref_code}' in full_data.columns:
                ax3.plot(full_data['date'], full_data[f'high_diff_{target_code}_{ref_code}'], 
                        label=f'{target_code} vs {ref_code} 最高值差值', linestyle='-')
        
        # 添加零线
        ax3.axhline(y=0, color='gray', linestyle='-', alpha=0.5)
        
        # 标记异常事件
        if events_data is not None and not events_data.empty:
            for idx, row in events_data.iterrows():
                ax3.axvline(x=row['date'], color='red', alpha=0.3, linestyle='--')
        
        ax3.set_title('目标品种与参考品种的最高值差值', fontsize=14)
        ax3.set_xlabel('日期', fontsize=12)
        ax3.set_ylabel('最高值差值', fontsize=12)
        ax3.legend()
        ax3.grid(True)
        
        plt.tight_layout()
        
        # 保存图片
        if save_path:
            # 创建目录（如果不存在）
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"可视化图表已保存到: {save_path}")
        
        #plt.show()

    def clean_generated_files(self, report_dir="data/report", pic_dir="data/pic", csv_dir="data/csv_data"):
        """
        清理生成的报告、图片和非缓存的CSV文件

        参数:
        - report_dir: 报告文件目录
        - pic_dir: 图片文件目录
        - csv_dir: CSV文件目录 (仅清理报告相关的CSV)
        """
        import glob

        cleaned_count = 0
        
        # 定义要清理的文件模式
        patterns = [
            os.path.join(report_dir, '*'),
            os.path.join(pic_dir, '*'),
            os.path.join(csv_dir, 'fat_finger_*.csv') # 仅匹配报告相关的CSV
        ]
        
        print("开始清理生成的文件...")

        # 确保目录存在，避免glob出错
        for dir_path in [report_dir, pic_dir, csv_dir]:
            if not os.path.exists(dir_path):
                print(f"目录不存在，跳过: {dir_path}")

        for pattern in patterns:
            files_to_delete = glob.glob(pattern)
            for file_path in files_to_delete:
                try:
                    if os.path.isfile(file_path):
                        os.remove(file_path)
                        print(f"已删除文件: {file_path}")
                        cleaned_count += 1
                except Exception as e:
                    print(f"删除文件失败: {file_path}, 错误: {e}")
        
        print(f"清理完成，共删除了 {cleaned_count} 个文件。")
        return cleaned_count
