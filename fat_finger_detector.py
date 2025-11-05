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
import seaborn as sns
from datetime import datetime, timedelta
import os
import warnings
warnings.filterwarnings('ignore')

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
        
    def get_future_data(self, future_code, start_date=None, end_date=None, save_to_csv=True, 
                        cache_days=1, use_cache=True):
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
        if not os.path.exists('data/csv_data'):
            os.makedirs('data/csv_data')
        
        # 生成缓存文件名
        cache_file_name = f"data/csv_data/future_{future_code}_{start_date}_{end_date}.csv"
        
        # 检查缓存
        if use_cache and os.path.exists(cache_file_name):
            try:
                # 获取文件修改时间
                file_mod_time = datetime.fromtimestamp(os.path.getmtime(cache_file_name))
                current_time = datetime.now()
                
                # 检查缓存是否过期
                if (current_time - file_mod_time).days < cache_days:
                    print(f"使用缓存数据: {cache_file_name}")
                    df = pd.read_csv(cache_file_name)
                    
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
            df = ak.futures_main_sina(symbol=future_code, start_date=start_date, end_date=end_date)
            
            # 如果获取不到数据，尝试其他接口
            if df.empty:
                print(f"使用第一个接口未获取到数据，尝试其他接口...")
                # 尝试使用另一个接口
                df = ak.futures_zh_daily_sina(symbol=future_code)
            
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
                df.to_csv(cache_file_name, index=False, encoding='utf-8-sig')
                print(f"数据已保存到: {cache_file_name}")
            
            return df
            
        except Exception as e:
            print(f"获取期货数据时出错: {e}")
            return None
    
    def get_future_main_contract_data(self, exchange_symbol, start_date=None, end_date=None, 
                                     save_to_csv=True, cache_days=1, use_cache=True):
        """
        获取期货主力合约的历史数据
        
        参数:
        - exchange_symbol: 交易所品种代码，如 'CU'（沪铜）、'IF'（沪深300股指期货）
        - start_date: 开始日期，格式为 'YYYYMMDD'，默认为3年前
        - end_date: 结束日期，格式为 'YYYYMMDD'，默认为今天
        - save_to_csv: 是否保存为CSV文件，默认为True
        - cache_days: 缓存有效期（天数），默认为1天
        - use_cache: 是否使用缓存，默认为True
        
        返回:
        - DataFrame: 包含期货主力合约历史数据的数据框
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
        if not os.path.exists('data/csv_data'):
            os.makedirs('data/csv_data')
        
        # 生成缓存文件名
        cache_file_name = f"data/csv_data/future_main_{exchange_symbol}_{start_date}_{end_date}.csv"
        
        # 检查缓存
        if use_cache and os.path.exists(cache_file_name):
            try:
                # 获取文件修改时间
                file_mod_time = datetime.fromtimestamp(os.path.getmtime(cache_file_name))
                current_time = datetime.now()
                
                # 检查缓存是否过期
                if (current_time - file_mod_time).days < cache_days:
                    print(f"使用缓存数据: {cache_file_name}")
                    df = pd.read_csv(cache_file_name)
                    
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
        print(f"正在获取期货主力合约 {exchange_symbol} 从 {start_date} 到 {end_date} 的历史数据...")
        
        try:
            # 使用AKShare获取期货主力合约数据
            try:
                df = ak.futures_main_sina(symbol=exchange_symbol, start_date=start_date, end_date=end_date)
                if df.empty:
                    raise Exception("futures_main_sina返回空数据")
            except Exception as e:
                print(f"使用futures_main_sina接口失败: {e}")
                print("尝试使用futures_zh_daily_sina接口获取主力合约数据...")
                
                # 尝试使用品种代码加0后缀获取主力合约数据
                main_contract_code = f"{exchange_symbol}0"
                print(f"尝试使用主力合约代码: {main_contract_code}")
                
                try:
                    df = ak.futures_zh_daily_sina(symbol=main_contract_code)
                    if df.empty:
                        raise Exception(f"使用主力合约代码 {main_contract_code} 获取数据为空")
                except Exception as e2:
                    print(f"使用主力合约代码 {main_contract_code} 失败: {e2}")
                    print(f"尝试使用品种代码 {exchange_symbol} 直接获取...")
                    df = ak.futures_zh_daily_sina(symbol=exchange_symbol)
            
            if df.empty:
                print(f"未获取到期货主力合约 {exchange_symbol} 的数据，请检查品种代码是否正确")
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
                df.to_csv(cache_file_name, index=False, encoding='utf-8-sig')
                print(f"数据已保存到: {cache_file_name}")
            
            return df
            
        except Exception as e:
            print(f"获取期货主力合约数据时出错: {e}")
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
        
        # 确保数值列为float类型
        numeric_columns = ['open', 'high', 'low', 'close', 'volume', 'open_interest']
        for col in numeric_columns:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')
        
        return df
    
    def calculate_price_spread(self, data, window=20, exclude_dates=None):
        """
        计算期货品种在指定窗口期内的价格差值（最高价-最低价）
        
        参数:
        - data: 期货数据DataFrame
        - window: 计算窗口，默认为20天
        - exclude_dates: 需要排除的日期列表，这些日期的数据不会参与滑动窗口计算
        
        返回:
        - DataFrame: 包含价格差值的数据
        """
        df = data.copy()
        
        # 确保数据按日期排序
        df = df.sort_values('date')
        
        # 如果提供了排除日期列表，将这些日期的数据标记为需要排除
        if exclude_dates is not None:
            # 将exclude_dates转换为datetime类型（如果是字符串）
            exclude_dates_dt = []
            for date in exclude_dates:
                if isinstance(date, str):
                    exclude_dates_dt.append(pd.to_datetime(date))
                else:
                    exclude_dates_dt.append(date)
            
            # 标记需要排除的日期
            df['to_exclude'] = df['date'].isin(exclude_dates_dt)
        else:
            df['to_exclude'] = False
        
        # 计算滚动窗口内的最高价和最低价，排除指定日期的数据
        # 使用自定义函数计算滚动窗口，排除特定日期
        df['rolling_high'] = self._calculate_rolling_max(df['high'], window, df['to_exclude'])
        df['rolling_low'] = self._calculate_rolling_min(df['low'], window, df['to_exclude'])
        
        # 计算价格差值
        df['price_spread'] = df['rolling_high'] - df['rolling_low']
        
        # 计算价格差值相对于最低价的百分比
        df['price_spread_pct'] = (df['price_spread'] / df['rolling_low']) * 100
        
        # 删除临时列
        df.drop('to_exclude', axis=1, inplace=True)
        
        return df
    
    def _calculate_rolling_max(self, series, window, exclude_mask):
        """
        计算滚动窗口最大值，排除指定日期的数据
        
        参数:
        - series: 数据序列
        - window: 窗口大小
        - exclude_mask: 布尔掩码，True表示需要排除的日期
        
        返回:
        - Series: 计算结果
        """
        result = []
        for i in range(len(series)):
            # 确定窗口范围
            start_idx = max(0, i - window + 1)
            end_idx = i + 1
            
            # 获取窗口内的数据，排除指定日期
            window_data = series[start_idx:end_idx][~exclude_mask[start_idx:end_idx]]
            
            # 如果窗口内没有有效数据，使用NaN
            if len(window_data) == 0:
                result.append(np.nan)
            else:
                result.append(window_data.max())
        
        return pd.Series(result, index=series.index)
    
    def _calculate_rolling_min(self, series, window, exclude_mask):
        """
        计算滚动窗口最小值，排除指定日期的数据
        
        参数:
        - series: 数据序列
        - window: 窗口大小
        - exclude_mask: 布尔掩码，True表示需要排除的日期
        
        返回:
        - Series: 计算结果
        """
        result = []
        for i in range(len(series)):
            # 确定窗口范围
            start_idx = max(0, i - window + 1)
            end_idx = i + 1
            
            # 获取窗口内的数据，排除指定日期
            window_data = series[start_idx:end_idx][~exclude_mask[start_idx:end_idx]]
            
            # 如果窗口内没有有效数据，使用NaN
            if len(window_data) == 0:
                result.append(np.nan)
            else:
                result.append(window_data.min())
        
        return pd.Series(result, index=series.index)
    
    def detect_fat_finger_events(self, target_code, reference_codes, start_date=None, end_date=None, 
                                threshold_pct=50.0, window=20, save_to_csv=True, max_iterations=3):
        """
        检测乌龙指事件
        
        参数:
        - target_code: 目标期货品种代码，如 'CU2404'
        - reference_codes: 参考期货品种代码列表，如 ['CU2405', 'AL2404', 'ZN2404']
        - start_date: 开始日期，格式为 'YYYYMMDD'，默认为60天前
        - end_date: 结束日期，格式为 'YYYYMMDD'，默认为今天
        - threshold_pct: 价格差值差异阈值（百分比），默认为50%
        - window: 计算窗口，默认为20天
        - save_to_csv: 是否保存结果到CSV，默认为True
        - max_iterations: 最大迭代次数，默认为3次
        
        返回:
        - tuple: (完整数据, 异常事件数据)
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
        
        # 迭代检测乌龙指事件
        exclude_dates = []  # 存储已识别的乌龙指日期
        iteration = 0
        final_events_data = None
        
        while iteration < max_iterations:
            print(f"\n--- 第 {iteration + 1} 次迭代检测 ---")
            print(f"当前排除的乌龙指日期数量: {len(exclude_dates)}")
            
            # 计算目标品种的价格差值（排除已识别的乌龙指日期）
            target_data_spread = self.calculate_price_spread(
                target_data, window=window, exclude_dates=exclude_dates
            )
            
            # 计算参考品种的价格差值（排除已识别的乌龙指日期）
            reference_data_spread = {}
            for ref_code, ref_data in reference_data.items():
                reference_data_spread[ref_code] = self.calculate_price_spread(
                    ref_data, window=window, exclude_dates=exclude_dates
                )
            
            # 合并所有品种的数据
            merged_data = target_data_spread[['date', 'price_spread', 'price_spread_pct']].copy()
            merged_data.rename(columns={
                'price_spread': f'{target_code}_spread',
                'price_spread_pct': f'{target_code}_spread_pct'
            }, inplace=True)
            
            for ref_code, ref_data_spread in reference_data_spread.items():
                merged_data = pd.merge(
                    merged_data,
                    ref_data_spread[['date', 'price_spread', 'price_spread_pct']],
                    on='date',
                    how='left',
                    suffixes=('', f'_{ref_code}')
                )
                merged_data.rename(columns={
                    'price_spread': f'{ref_code}_spread',
                    'price_spread_pct': f'{ref_code}_spread_pct'
                }, inplace=True)
            
            # 计算目标品种与各参考品种的差值差异
            for ref_code in reference_data.keys():
                # 计算绝对差值差异
                merged_data[f'diff_abs_{ref_code}'] = (
                    merged_data[f'{target_code}_spread'] - merged_data[f'{ref_code}_spread']
                )
                
                # 计算相对差值差异（百分比）
                merged_data[f'diff_pct_{ref_code}'] = (
                    merged_data[f'diff_abs_{ref_code}'] / merged_data[f'{ref_code}_spread'] * 100
                )
                
                # 标记异常事件（差值差异超过阈值）
                merged_data[f'is_anomaly_{ref_code}'] = (
                    merged_data[f'diff_pct_{ref_code}'].abs() > threshold_pct
                )
            
            # 计算综合异常指标（任一参考品种出现异常即标记为异常）
            anomaly_cols = [f'is_anomaly_{ref_code}' for ref_code in reference_data.keys()]
            merged_data['is_fat_finger'] = merged_data[anomaly_cols].any(axis=1)
            
            # 筛选异常事件
            current_events_data = merged_data[merged_data['is_fat_finger']].copy()
            
            # 获取新识别的乌龙指日期（不在排除列表中的）
            if not current_events_data.empty:
                new_fat_finger_dates = current_events_data['date'].tolist()
                # 过滤掉已经在排除列表中的日期
                new_dates_to_exclude = [date for date in new_fat_finger_dates 
                                       if date not in exclude_dates]
                
                print(f"本次检测到 {len(new_fat_finger_dates)} 个异常事件")
                print(f"其中 {len(new_dates_to_exclude)} 个是新的乌龙指日期")
                
                # 如果没有新的乌龙指日期，结束迭代
                if not new_dates_to_exclude:
                    print("没有检测到新的乌龙指日期，迭代结束")
                    final_events_data = current_events_data
                    break
                
                # 将新的乌龙指日期添加到排除列表
                exclude_dates.extend(new_dates_to_exclude)
                final_events_data = current_events_data
            else:
                print("未检测到异常事件，迭代结束")
                final_events_data = current_events_data
                break
            
            iteration += 1
        
        print(f"\n迭代检测完成，共识别 {len(exclude_dates)} 个乌龙指日期")
        
        # 最终使用所有已识别的乌龙指日期重新计算价格差值
        print("使用最终识别的乌龙指日期重新计算价格差值...")
        target_data_spread = self.calculate_price_spread(
            target_data, window=window, exclude_dates=exclude_dates
        )
        
        reference_data_spread = {}
        for ref_code, ref_data in reference_data.items():
            reference_data_spread[ref_code] = self.calculate_price_spread(
                ref_data, window=window, exclude_dates=exclude_dates
            )
        
        # 合并最终结果
        final_merged_data = target_data_spread[['date', 'price_spread', 'price_spread_pct']].copy()
        final_merged_data.rename(columns={
            'price_spread': f'{target_code}_spread',
            'price_spread_pct': f'{target_code}_spread_pct'
        }, inplace=True)
        
        for ref_code, ref_data_spread in reference_data_spread.items():
            final_merged_data = pd.merge(
                final_merged_data,
                ref_data_spread[['date', 'price_spread', 'price_spread_pct']],
                on='date',
                how='left',
                suffixes=('', f'_{ref_code}')
            )
            final_merged_data.rename(columns={
                'price_spread': f'{ref_code}_spread',
                'price_spread_pct': f'{ref_code}_spread_pct'
            }, inplace=True)
        
        # 重新计算差值差异和异常标记
        for ref_code in reference_data.keys():
            final_merged_data[f'diff_abs_{ref_code}'] = (
                final_merged_data[f'{target_code}_spread'] - final_merged_data[f'{ref_code}_spread']
            )
            final_merged_data[f'diff_pct_{ref_code}'] = (
                final_merged_data[f'diff_abs_{ref_code}'] / final_merged_data[f'{ref_code}_spread'] * 100
            )
            final_merged_data[f'is_anomaly_{ref_code}'] = (
                final_merged_data[f'diff_pct_{ref_code}'].abs() > threshold_pct
            )
        
        anomaly_cols = [f'is_anomaly_{ref_code}' for ref_code in reference_data.keys()]
        final_merged_data['is_fat_finger'] = final_merged_data[anomaly_cols].any(axis=1)
        
        # 筛选最终异常事件
        final_events_data = final_merged_data[final_merged_data['is_fat_finger']].copy()
        
        # 保存结果
        if save_to_csv:
            # 创建data文件夹和csv_data子文件夹（如果不存在）
            if not os.path.exists('data'):
                os.makedirs('data')
            if not os.path.exists('data/csv_data'):
                os.makedirs('data/csv_data')
            
            # 保存完整数据
            full_data_path = f"data/csv_data/fat_finger_full_{target_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            final_merged_data.to_csv(full_data_path, index=False, encoding='utf-8-sig')
            print(f"完整数据已保存到: {full_data_path}")
            
            # 保存异常事件数据
            if not final_events_data.empty:
                events_data_path = f"data/csv_data/fat_finger_events_{target_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                final_events_data.to_csv(events_data_path, index=False, encoding='utf-8-sig')
                print(f"异常事件数据已保存到: {events_data_path}")
        
        return final_merged_data, final_events_data
    
    def generate_report(self, events_data, target_code, reference_codes, threshold_pct=50.0):
        """
        生成乌龙指检测报告
        
        参数:
        - events_data: 异常事件数据
        - target_code: 目标期货品种代码
        - reference_codes: 参考期货品种代码列表
        - threshold_pct: 价格差值差异阈值（百分比）
        
        返回:
        - str: 检测报告文本
        """
        if events_data is None or events_data.empty:
            return f"在指定时间段内未检测到 {target_code} 的乌龙指事件（阈值: {threshold_pct}%）"
        
        report = []
        report.append("=" * 60)
        report.append(f"期货乌龙指检测报告 - {target_code}")
        report.append("=" * 60)
        report.append(f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        report.append(f"目标品种: {target_code}")
        report.append(f"参考品种: {', '.join(reference_codes)}")
        report.append(f"检测阈值: {threshold_pct}%")
        report.append(f"检测到的异常事件数量: {len(events_data)}")
        report.append("")
        
        # 按日期排序
        events_data = events_data.sort_values('date')
        
        # 添加每个异常事件的详细信息
        for idx, row in events_data.iterrows():
            report.append(f"异常事件 #{idx+1}:")
            report.append(f"  日期: {row['date'].strftime('%Y-%m-%d')}")
            report.append(f"  {target_code} 价格差值: {row[f'{target_code}_spread']:.2f}")
            
            for ref_code in reference_codes:
                if f'diff_pct_{ref_code}' in row and not pd.isna(row[f'diff_pct_{ref_code}']):
                    report.append(f"  与 {ref_code} 差值差异: {row[f'diff_pct_{ref_code}']:.2f}%")
            
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
        fig, axes = plt.subplots(2, 1, figsize=(15, 10))
        
        # 第一个子图：价格差值趋势
        ax1 = axes[0]
        ax1.plot(full_data['date'], full_data[f'{target_code}_spread'], 
                label=f'{target_code} 价格差值', linewidth=2)
        
        for ref_code in reference_codes:
            if f'{ref_code}_spread' in full_data.columns:
                ax1.plot(full_data['date'], full_data[f'{ref_code}_spread'], 
                        label=f'{ref_code} 价格差值', linestyle='--')
        
        ax1.set_title('期货品种价格差值趋势（20日滚动窗口）', fontsize=14)
        ax1.set_ylabel('价格差值', fontsize=12)
        ax1.legend()
        ax1.grid(True)
        
        # 第二个子图：差值差异
        ax2 = axes[1]
        for ref_code in reference_codes:
            if f'diff_pct_{ref_code}' in full_data.columns:
                ax2.plot(full_data['date'], full_data[f'diff_pct_{ref_code}'], 
                        label=f'{target_code} vs {ref_code} 差值差异(%)', linestyle='-')
        
        # 添加阈值线
        ax2.axhline(y=50, color='r', linestyle=':', label='异常阈值(50%)')
        ax2.axhline(y=-50, color='r', linestyle=':')
        
        # 标记异常事件
        if events_data is not None and not events_data.empty:
            for idx, row in events_data.iterrows():
                ax2.axvline(x=row['date'], color='red', alpha=0.3, linestyle='--')
        
        ax2.set_title('目标品种与参考品种的差值差异', fontsize=14)
        ax2.set_xlabel('日期', fontsize=12)
        ax2.set_ylabel('差值差异(%)', fontsize=12)
        ax2.legend()
        ax2.grid(True)
        
        plt.tight_layout()
        
        # 保存图片
        if save_path:
            # 创建目录（如果不存在）
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"可视化图表已保存到: {save_path}")
        
        plt.show()