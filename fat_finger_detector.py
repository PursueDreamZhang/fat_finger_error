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
        - window: 计算窗口，默认为20天（此参数保留以保持向后兼容性）
        - exclude_dates: 需要排除的日期列表，这些日期的数据不会参与计算
        
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
        
        # 计算当天的价格差值（最高价-最低价）
        df['price_spread'] = df['high'] - df['low']
        
        # 计算价格差值相对于最低价的百分比
        df['price_spread_pct'] = (df['price_spread'] / df['low']) * 100
        
        # 删除临时列
        if 'to_exclude' in df.columns:
            df.drop('to_exclude', axis=1, inplace=True)
        
        return df
    
    def calculate_all_spread_differences(self, target_data, reference_data_dict, target_code, reference_codes, exclude_dates=None):
        """
        计算目标品种与所有参考品种的当天价格差值差异
        
        参数:
        - target_data: 目标品种数据DataFrame
        - reference_data_dict: 参考品种数据字典，键为品种代码，值为DataFrame
        - target_code: 目标品种代码
        - reference_codes: 参考品种代码列表
        - exclude_dates: 需要排除的日期列表，这些日期的数据不会参与计算
        
        返回:
        - DataFrame: 包含价格差值差异的数据
        """
        # 计算目标品种的价格差值
        target_spread = self.calculate_price_spread(target_data, exclude_dates=exclude_dates)
        
        # 从目标品种的价格差值开始
        merged_data = target_spread[['date', 'price_spread', 'price_spread_pct']].copy()
        merged_data.rename(columns={
            'price_spread': f'{target_code}_spread',
            'price_spread_pct': f'{target_code}_spread_pct'
        }, inplace=True)
        
        # 为每个参考品种计算差值差异并合并
        for ref_code in reference_codes:
            if ref_code in reference_data_dict:
                # 计算参考品种的价格差值
                ref_spread = self.calculate_price_spread(reference_data_dict[ref_code], exclude_dates=exclude_dates)
                
                # 合并目标品种和参考品种的数据
                temp_data = pd.merge(
                    merged_data[['date', f'{target_code}_spread']],
                    ref_spread[['date', 'price_spread']],
                    on='date',
                    how='inner'
                )
                
                # 计算差值差异
                temp_data[f'spread_diff_{target_code}_{ref_code}'] = (
                    temp_data[f'{target_code}_spread'] - temp_data['price_spread']
                )
                
                # 计算差值差异的绝对值
                temp_data[f'spread_diff_abs_{target_code}_{ref_code}'] = temp_data[f'spread_diff_{target_code}_{ref_code}'].abs()
                
                # 将差值差异合并到主数据中
                if f'spread_diff_{target_code}_{ref_code}' not in merged_data.columns:
                    merged_data = pd.merge(
                        merged_data,
                        temp_data[['date', f'spread_diff_{target_code}_{ref_code}', f'spread_diff_abs_{target_code}_{ref_code}']],
                        on='date',
                        how='left'
                    )
                    
                    # 将NaN值替换为0，表示没有参考品种数据时差值差异为0
                    merged_data[f'spread_diff_{target_code}_{ref_code}'].fillna(0, inplace=True)
                    merged_data[f'spread_diff_abs_{target_code}_{ref_code}'].fillna(0, inplace=True)
        
        return merged_data
    
    def calculate_daily_spread_difference(self, target_data, reference_data, target_code, reference_code, exclude_dates=None):
        """
        计算目标品种与参考品种的当天价格差值差异
        
        参数:
        - target_data: 目标品种数据DataFrame
        - reference_data: 参考品种数据DataFrame
        - target_code: 目标品种代码
        - reference_code: 参考品种代码
        - exclude_dates: 需要排除的日期列表，这些日期的数据不会参与计算
        
        返回:
        - DataFrame: 包含价格差值差异的数据
        """
        # 计算目标品种和参考品种的价格差值
        target_spread = self.calculate_price_spread(target_data, exclude_dates=exclude_dates)
        reference_spread = self.calculate_price_spread(reference_data, exclude_dates=exclude_dates)
        
        # 合并数据
        merged_data = pd.merge(
            target_spread[['date', 'price_spread', 'price_spread_pct']],
            reference_spread[['date', 'price_spread', 'price_spread_pct']],
            on='date',
            how='inner',
            suffixes=(f'_{target_code}', f'_{reference_code}')
        )
        
        # 计算差值差异
        merged_data[f'spread_diff_{target_code}_{reference_code}'] = (
            merged_data[f'price_spread_{target_code}'] - merged_data[f'price_spread_{reference_code}']
        )
        
        # 计算差值差异的绝对值
        merged_data[f'spread_diff_abs_{target_code}_{reference_code}'] = (
            merged_data[f'spread_diff_{target_code}_{reference_code}'].abs()
        )
        
        return merged_data
    
    def calculate_historical_spread_difference_stats(self, spread_diff_data, window=20, exclude_dates=None):
        """
        计算历史差值差异的统计特征
        
        参数:
        - spread_diff_data: 包含价格差值差异的DataFrame
        - window: 计算历史统计特征的窗口，默认为20天
        - exclude_dates: 需要排除的日期列表，这些日期的数据不会参与历史统计计算
        
        返回:
        - DataFrame: 包含历史统计特征的数据
        """
        df = spread_diff_data.copy()
        
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
        
        # 找出所有差值差异列
        spread_diff_cols = [col for col in df.columns if 'spread_diff_' in col and 'abs' not in col]
        
        # 为每个差值差异列计算历史统计特征
        for col in spread_diff_cols:
            # 计算滚动窗口内的平均值
            df[f'{col}_hist_mean'] = self._calculate_rolling_mean(df[col], window, df['to_exclude'])
            
            # 计算滚动窗口内的标准差
            df[f'{col}_hist_std'] = self._calculate_rolling_std(df[col], window, df['to_exclude'])
            
            # 计算当前值与历史平均值的差异倍数（Z-score）
            # 避免除以0和NaN的情况
            hist_std = df[f'{col}_hist_std'].copy()
            hist_mean = df[f'{col}_hist_mean'].copy()
            
            # 处理历史平均值为NaN的情况
            valid_mask = ~(hist_mean.isna() | hist_std.isna())
            
            # 只对有效数据计算Z-score
            df[f'{col}_zscore'] = np.nan
            
            # 对于标准差为0或接近0的情况，Z-score设为0（表示没有变化）
            zero_std_mask = valid_mask & (hist_std < 1e-10)
            df.loc[zero_std_mask, f'{col}_zscore'] = 0.0
            
            # 对于标准差有效的情况，正常计算Z-score
            normal_std_mask = valid_mask & (hist_std >= 1e-10)
            df.loc[normal_std_mask, f'{col}_zscore'] = (df.loc[normal_std_mask, col] - df.loc[normal_std_mask, f'{col}_hist_mean']) / df.loc[normal_std_mask, f'{col}_hist_std']
            
            # 计算当前值与历史平均值的绝对差异
            df[f'{col}_abs_diff_from_mean'] = (df[col] - df[f'{col}_hist_mean']).abs()
            
            # 计算当前值相对于历史平均值的百分比差异
            # 避免除以0和NaN的情况
            hist_mean_abs = df[f'{col}_hist_mean'].abs()
            
            # 处理历史平均值为NaN的情况
            valid_mask = ~hist_mean_abs.isna()
            
            # 只对有效数据计算百分比差异
            df[f'{col}_pct_diff_from_mean'] = np.nan
            
            # 对于历史平均值为0或接近0的情况，百分比差异设为0（表示没有变化）
            zero_mean_mask = valid_mask & (hist_mean_abs < 1e-10)
            df.loc[zero_mean_mask, f'{col}_pct_diff_from_mean'] = 0.0
            
            # 对于历史平均值有效的情况，正常计算百分比差异
            normal_mean_mask = valid_mask & (hist_mean_abs >= 1e-10)
            df.loc[normal_mean_mask, f'{col}_pct_diff_from_mean'] = (df.loc[normal_mean_mask, f'{col}_abs_diff_from_mean'] / hist_mean_abs[normal_mean_mask]) * 100
        
        # 删除临时列
        if 'to_exclude' in df.columns:
            df.drop('to_exclude', axis=1, inplace=True)
        
        return df
    
    def _calculate_rolling_mean(self, series, window, exclude_mask):
        """
        计算滚动窗口平均值，排除指定日期的数据
        
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
            
            # 如果窗口内没有有效数据，使用0
            if len(window_data) == 0:
                result.append(0.0)
            else:
                result.append(window_data.mean())
        
        return pd.Series(result, index=series.index)
    
    def _calculate_rolling_std(self, series, window, exclude_mask):
        """
        计算滚动窗口标准差，排除指定日期的数据
        
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
            
            # 如果窗口内没有有效数据或只有一个数据点，使用0
            if len(window_data) <= 1:
                result.append(0.0)
            else:
                result.append(window_data.std())
        
        return pd.Series(result, index=series.index)
    
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
            
            # 如果窗口内没有有效数据，使用0.0
            if len(window_data) == 0:
                result.append(0.0)
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
            
            # 如果窗口内没有有效数据，使用0.0
            if len(window_data) == 0:
                result.append(0.0)
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
        - window: 计算历史统计特征的窗口，默认为20天
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
            
            # 计算目标品种与所有参考品种的当天价格差值差异
            daily_spread_diff = self.calculate_all_spread_differences(
                target_data, reference_data, target_code, reference_codes, exclude_dates=exclude_dates
            )
            
            # 计算历史差值差异的统计特征
            daily_spread_diff_stats = self.calculate_historical_spread_difference_stats(
                daily_spread_diff, window=window, exclude_dates=exclude_dates
            )
            
            # 基于历史统计特征标记异常事件
            merged_data = daily_spread_diff_stats.copy()
            
            # 找出所有差值差异列（只包含基础列名，不包含统计特征列）
            spread_diff_cols = [col for col in merged_data.columns 
                               if 'spread_diff_' in col and 'abs' not in col 
                               and '_hist_mean' not in col and '_hist_std' not in col 
                               and '_zscore' not in col and '_pct_diff_from_mean' not in col 
                               and '_abs_diff_from_mean' not in col]
            
            # 为每个差值差异列标记异常事件
            for col in spread_diff_cols:
                # 方法1：基于Z-score的异常检测（当前值与历史平均值的差异倍数）
                merged_data[f'is_anomaly_zscore_{col}'] = merged_data[f'{col}_zscore'].abs() > 2.0
                
                # 方法2：基于百分比差异的异常检测（当前值与历史平均值的百分比差异）
                merged_data[f'is_anomaly_pct_{col}'] = merged_data[f'{col}_pct_diff_from_mean'] > threshold_pct
                
                # 方法3：基于绝对差异的异常检测（当前值与历史平均值的绝对差异）
                # 这里使用历史标准差作为阈值
                merged_data[f'is_anomaly_abs_{col}'] = merged_data[f'{col}_abs_diff_from_mean'] > merged_data[f'{col}_hist_std']
            
            # 计算综合异常指标（任一参考品种出现异常即标记为异常）
            zscore_anomaly_cols = [f'is_anomaly_zscore_{col}' for col in spread_diff_cols]
            pct_anomaly_cols = [f'is_anomaly_pct_{col}' for col in spread_diff_cols]
            abs_anomaly_cols = [f'is_anomaly_abs_{col}' for col in spread_diff_cols]
            
            merged_data['is_fat_finger_zscore'] = merged_data[zscore_anomaly_cols].any(axis=1)
            merged_data['is_fat_finger_pct'] = merged_data[pct_anomaly_cols].any(axis=1)
            merged_data['is_fat_finger_abs'] = merged_data[abs_anomaly_cols].any(axis=1)
            
            # 综合三种方法的结果，任一方法检测到异常即标记为异常
            merged_data['is_fat_finger'] = (
                merged_data['is_fat_finger_zscore'] | 
                merged_data['is_fat_finger_pct'] | 
                merged_data['is_fat_finger_abs']
            )
            
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
        
        # 计算目标品种与所有参考品种的当天价格差值差异
        final_daily_spread_diff = self.calculate_all_spread_differences(
            target_data, reference_data, target_code, reference_codes, exclude_dates=exclude_dates
        )
        
        # 计算历史差值差异的统计特征
        final_daily_spread_diff_stats = self.calculate_historical_spread_difference_stats(
            final_daily_spread_diff, window=window, exclude_dates=exclude_dates
        )
        
        # 基于历史统计特征标记异常事件
        final_merged_data = final_daily_spread_diff_stats.copy()
        
        # 找出所有差值差异列（只包含基础列名，不包含统计特征列）
        spread_diff_cols = [col for col in final_merged_data.columns 
                           if 'spread_diff_' in col and 'abs' not in col 
                           and '_hist_mean' not in col and '_hist_std' not in col 
                           and '_zscore' not in col and '_pct_diff_from_mean' not in col 
                           and '_abs_diff_from_mean' not in col]
        
        # 为每个差值差异列标记异常事件
        for col in spread_diff_cols:
            # 方法1：基于Z-score的异常检测（当前值与历史平均值的差异倍数）
            final_merged_data[f'is_anomaly_zscore_{col}'] = final_merged_data[f'{col}_zscore'].abs() > 2.0
            
            # 方法2：基于百分比差异的异常检测（当前值与历史平均值的百分比差异）
            final_merged_data[f'is_anomaly_pct_{col}'] = final_merged_data[f'{col}_pct_diff_from_mean'] > threshold_pct
            
            # 方法3：基于绝对差异的异常检测（当前值与历史平均值的绝对差异）
            # 这里使用历史标准差作为阈值
            final_merged_data[f'is_anomaly_abs_{col}'] = final_merged_data[f'{col}_abs_diff_from_mean'] > final_merged_data[f'{col}_hist_std']
        
        # 计算综合异常指标（任一参考品种出现异常即标记为异常）
        zscore_anomaly_cols = [f'is_anomaly_zscore_{col}' for col in spread_diff_cols]
        pct_anomaly_cols = [f'is_anomaly_pct_{col}' for col in spread_diff_cols]
        abs_anomaly_cols = [f'is_anomaly_abs_{col}' for col in spread_diff_cols]
        
        final_merged_data['is_fat_finger_zscore'] = final_merged_data[zscore_anomaly_cols].any(axis=1)
        final_merged_data['is_fat_finger_pct'] = final_merged_data[pct_anomaly_cols].any(axis=1)
        final_merged_data['is_fat_finger_abs'] = final_merged_data[abs_anomaly_cols].any(axis=1)
        
        # 综合三种方法的结果，任一方法检测到异常即标记为异常
        final_merged_data['is_fat_finger'] = (
            final_merged_data['is_fat_finger_zscore'] | 
            final_merged_data['is_fat_finger_pct'] | 
            final_merged_data['is_fat_finger_abs']
        )
        
        # 筛选最终异常事件
        final_events_data = final_merged_data[final_merged_data['is_fat_finger']].copy()
        
        # 添加异常原因分析
        if not final_events_data.empty:
            for idx, row in final_events_data.iterrows():
                reasons = []
                for col in spread_diff_cols:
                    if row[f'is_anomaly_zscore_{col}']:
                        reasons.append(f"{col} Z-score异常({row[f'{col}_zscore']:.2f})")
                    if row[f'is_anomaly_pct_{col}']:
                        reasons.append(f"{col} 百分比差异({row[f'{col}_pct_diff_from_mean']:.2f}%)")
                    if row[f'is_anomaly_abs_{col}']:
                        reasons.append(f"{col} 绝对差异({row[f'{col}_abs_diff_from_mean']:.2f})")
                
                # 将原因列表合并为字符串
                final_events_data.at[idx, 'anomaly_reasons'] = "; ".join(reasons)
        
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
            
            # 显示目标品种和参考品种的当天价格差值
            if f'{target_code}_spread' in row:
                target_spread = row[f'{target_code}_spread']
                report.append(f"  {target_code} 当天价格差值: {target_spread:.2f}")
            
            for ref_code in reference_codes:
                if f'{ref_code}_spread' in row:
                    ref_spread = row[f'{ref_code}_spread']
                    report.append(f"  {ref_code} 当天价格差值: {ref_spread:.2f}")
                
                if f'spread_diff_{target_code}_{ref_code}' in row:
                    spread_diff = row[f'spread_diff_{target_code}_{ref_code}']
                    spread_diff_abs = row[f'spread_diff_abs_{target_code}_{ref_code}']
                    report.append(f"  差值差异: {spread_diff:.2f} (绝对值: {spread_diff_abs:.2f})")
                    
                    # 显示历史统计信息
                    if f'spread_diff_{target_code}_{ref_code}_hist_mean' in row:
                        hist_mean = row[f'spread_diff_{target_code}_{ref_code}_hist_mean']
                        hist_std = row[f'spread_diff_{target_code}_{ref_code}_hist_std']
                        zscore = row[f'spread_diff_{target_code}_{ref_code}_zscore']
                        pct_diff = row[f'spread_diff_{target_code}_{ref_code}_pct_diff_from_mean']
                        
                        report.append(f"  历史平均差值: {hist_mean:.2f} ± {hist_std:.2f}")
                        report.append(f"  当前差值与历史平均的差异: {pct_diff:.2f}%")
                        report.append(f"  Z-score: {zscore:.2f}")
            
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
        
        # 第一个子图：价格差值趋势
        ax1 = axes[0]
        ax1.plot(full_data['date'], full_data[f'{target_code}_spread'], 
                label=f'{target_code} 当天价格差值', linewidth=2)
        
        for ref_code in reference_codes:
            if f'{ref_code}_spread' in full_data.columns:
                ax1.plot(full_data['date'], full_data[f'{ref_code}_spread'], 
                        label=f'{ref_code} 当天价格差值', linestyle='--')
        
        ax1.set_title('期货品种当天价格差值趋势', fontsize=14)
        ax1.set_ylabel('价格差值', fontsize=12)
        ax1.legend()
        ax1.grid(True)
        
        # 第二个子图：差值差异
        ax2 = axes[1]
        for ref_code in reference_codes:
            if f'spread_diff_{target_code}_{ref_code}' in full_data.columns:
                ax2.plot(full_data['date'], full_data[f'spread_diff_{target_code}_{ref_code}'], 
                        label=f'{target_code} vs {ref_code} 差值差异', linestyle='-')
        
        # 添加零线
        ax2.axhline(y=0, color='gray', linestyle='-', alpha=0.5)
        
        # 标记异常事件
        if events_data is not None and not events_data.empty:
            for idx, row in events_data.iterrows():
                ax2.axvline(x=row['date'], color='red', alpha=0.3, linestyle='--')
        
        ax2.set_title('目标品种与参考品种的差值差异', fontsize=14)
        ax2.set_xlabel('日期', fontsize=12)
        ax2.set_ylabel('差值差异', fontsize=12)
        ax2.legend()
        ax2.grid(True)
        
        # 第三个子图：Z-score
        ax3 = axes[2]
        for ref_code in reference_codes:
            if f'spread_diff_{target_code}_{ref_code}_zscore' in full_data.columns:
                ax3.plot(full_data['date'], full_data[f'spread_diff_{target_code}_{ref_code}_zscore'], 
                        label=f'{target_code} vs {ref_code} Z-score', linestyle='-')
        
        # 添加Z-score阈值线
        ax3.axhline(y=2.0, color='r', linestyle=':', label='Z-score阈值(±2.0)')
        ax3.axhline(y=-2.0, color='r', linestyle=':')
        
        # 标记异常事件
        if events_data is not None and not events_data.empty:
            for idx, row in events_data.iterrows():
                ax3.axvline(x=row['date'], color='red', alpha=0.3, linestyle='--')
        
        ax3.set_title('差值差异的Z-score', fontsize=14)
        ax3.set_xlabel('日期', fontsize=12)
        ax3.set_ylabel('Z-score', fontsize=12)
        ax3.legend()
        ax3.grid(True)
        
        plt.tight_layout()
        
        # 保存图片
        if save_path:
            # 创建目录（如果不存在）
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            plt.savefig(save_path, dpi=300, bbox_inches='tight')
            print(f"可视化图表已保存到: {save_path}")
        
        plt.show()