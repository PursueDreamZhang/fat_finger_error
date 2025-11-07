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
        if not os.path.exists('data/csv_data'):
            os.makedirs('data/csv_data')
        
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
        
        # 使用缓存管理器（如果可用）
        if self.cache_manager is not None:
            # 创建参数字典
            params = {
                'exchange_symbol': exchange_symbol,
                'start_date': start_date,
                'end_date': end_date
            }
            
            # 检查缓存是否有效
            if use_cache and self.cache_manager.is_cache_valid('future_main', exchange_symbol, params, cache_days):
                cache_info = self.cache_manager.get_cache_info('future_main', exchange_symbol, params)
                if cache_info and os.path.exists(cache_info['file_path']):
                    print(f"使用缓存数据: {cache_info['file_path']}")
                    df = pd.read_csv(cache_info['file_path'])
                    
                    # 确保日期列是datetime类型
                    if 'date' in df.columns:
                        df['date'] = pd.to_datetime(df['date'])
                    
                    # 验证数据有效性
                    if not df.empty and len(df) > 0:
                        print(f"从缓存加载 {len(df)} 条数据记录")
                        return df
                    else:
                        print("缓存数据无效，将重新获取")
            
            # 获取缓存文件路径
            cache_file_path = self.cache_manager.get_cache_file_path('future_main', exchange_symbol, params)
        else:
            # 使用内置缓存逻辑
            # 创建参数字典用于生成哈希值
            params = {
                'exchange_symbol': exchange_symbol,
                'start_date': start_date,
                'end_date': end_date
            }
            
            # 生成参数哈希值
            params_str = str(sorted(params.items()))
            params_hash = hashlib.md5(params_str.encode()).hexdigest()[:8]
            
            # 生成仅包含参数的缓存文件名
            cache_file_path = f"data/csv_data/future_main_{exchange_symbol}_{start_date}_{end_date}.csv"
            
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
        
            # 创建缓存元数据文件路径
            cache_metadata_file = "data/csv_data/cache_metadata.json"
            
            # 初始化缓存元数据
            cache_metadata = {}
            if os.path.exists(cache_metadata_file):
                try:
                    with open(cache_metadata_file, 'r', encoding='utf-8') as f:
                        cache_metadata = json.load(f)
                except Exception as e:
                    print(f"读取缓存元数据文件失败: {e}")
                    cache_metadata = {}
            
            # 检查缓存
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
                    
                    # 由于futures_zh_daily_sina不支持日期参数，我们需要手动筛选日期范围
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
                    print(f"使用主力合约代码 {main_contract_code} 失败: {e2}")
                    print(f"尝试使用品种代码 {exchange_symbol} 直接获取...")
                    
                    try:
                        df = ak.futures_zh_daily_sina(symbol=exchange_symbol)
                        if df.empty:
                            raise Exception(f"使用品种代码 {exchange_symbol} 获取数据为空")
                        
                        # 由于futures_zh_daily_sina不支持日期参数，我们需要手动筛选日期范围
                        if 'date' in df.columns:
                            df['date'] = pd.to_datetime(df['date'])
                            start_dt = pd.to_datetime(start_date, format='%Y%m%d')
                            end_dt = pd.to_datetime(end_date, format='%Y%m%d')
                            df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)]
                            print(f"从获取的数据中筛选出 {len(df)} 条符合日期范围的数据")
                    except Exception as e3:
                        print(f"使用品种代码 {exchange_symbol} 也失败: {e3}")
                        raise Exception(f"所有接口都无法获取期货主力合约 {exchange_symbol} 的数据")
            
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
                df.to_csv(cache_file_path, index=False, encoding='utf-8-sig')
                print(f"数据已保存到: {cache_file_path}")
                
                # 使用缓存管理器注册缓存（如果可用）
                if self.cache_manager is not None:
                    self.cache_manager.register_cache('future_main', exchange_symbol, params, cache_file_path, len(df))
                else:
                    # 更新缓存元数据
                    try:
                        # 使用文件路径作为键，而不是哈希值
                        cache_key = cache_file_path
                        cache_metadata[cache_key] = {
                            'params': params,
                            'params_hash': params_hash,
                            'file_path': cache_file_path,
                            'created_time': datetime.now().isoformat(),
                            'data_count': len(df)
                        }
                        
                        with open(cache_metadata_file, 'w', encoding='utf-8') as f:
                            json.dump(cache_metadata, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        print(f"更新缓存元数据失败: {e}")
            
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

    def detect_min_max_anomalies(self, min_max_data, target_code, reference_codes, window=20, threshold_pct=50.0):
        """
        检测最低值和最高值的异常
        
        参数:
        - min_max_data: 包含最低值和最高值差值的DataFrame
        - target_code: 目标品种代码
        - reference_codes: 参考品种代码列表
        - window: 历史统计窗口（天数）
        - threshold_pct: 异常检测阈值（百分比）
        
        返回:
        - tuple: (完整数据, 异常事件数据)
        """
        # 确保数据按日期排序
        data = min_max_data.sort_values('date').copy()
        
        # 找出所有差值列
        low_diff_cols = [col for col in data.columns if 'low_diff_' in col]
        high_diff_cols = [col for col in data.columns if 'high_diff_' in col]
        
        # 初始化异常日期集合
        exclude_dates = set()
        
        # 为每个差值列初始化统计特征列
        for col in low_diff_cols + high_diff_cols:
            data[f'{col}_hist_mean'] = np.nan
            data[f'{col}_pct_diff_from_mean'] = np.nan
        
        # 单次遍历处理每个日期
        for i, row in data.iterrows():
            current_date = row['date']
            row_idx = data.index.get_loc(i)
            
            # 为每个差值列计算历史统计特征
            for col in low_diff_cols + high_diff_cols:
                # 确定窗口范围
                start_idx = max(0, row_idx - window + 1)
                end_idx = row_idx
                
                # 获取窗口内的数据，排除已标记的异常日期
                window_data = data.iloc[start_idx:end_idx]
                window_mask = ~window_data['date'].isin(exclude_dates)
                valid_window_data = window_data[window_mask]
                
                if len(valid_window_data) > 0:
                    # 计算历史平均值
                    hist_mean = valid_window_data[col].mean()
                    
                    # 计算当前值与历史平均值的差异
                    current_value = row[col]
                    abs_diff_from_mean = abs(current_value - hist_mean)
                    
                    # 计算百分比差异
                    if abs(hist_mean) > 1e-10:
                        pct_diff_from_mean = (abs_diff_from_mean / abs(hist_mean)) * 100
                    else:
                        pct_diff_from_mean = 0.0
                    
                    # 更新统计特征列
                    data.at[i, f'{col}_hist_mean'] = hist_mean
                    data.at[i, f'{col}_pct_diff_from_mean'] = pct_diff_from_mean
                    
                    # 检测异常
                    is_anomaly = pct_diff_from_mean > threshold_pct
                    
                    # 如果检测到异常，则标记为异常日期
                    if is_anomaly:
                        exclude_dates.add(current_date)
        
        # 为每个差值列标记异常事件
        for col in low_diff_cols + high_diff_cols:
            data[f'is_anomaly_{col}'] = data[f'{col}_pct_diff_from_mean'] > threshold_pct
        
        # 计算综合异常指标（任一参考品种出现异常即标记为异常）
        low_anomaly_cols = [f'is_anomaly_{col}' for col in low_diff_cols]
        high_anomaly_cols = [f'is_anomaly_{col}' for col in high_diff_cols]
        
        data['is_low_anomaly'] = data[low_anomaly_cols].any(axis=1)
        data['is_high_anomaly'] = data[high_anomaly_cols].any(axis=1)
        
        # 综合最低值和最高值的异常结果，任一出现异常即标记为异常
        data['is_fat_finger'] = data['is_low_anomaly'] | data['is_high_anomaly']
        
        # 筛选异常事件
        events_data = data[data['is_fat_finger']].copy()
        
        # 添加异常原因分析
        if not events_data.empty:
            for idx, row in events_data.iterrows():
                reasons = []
                
                # 检查最低值异常原因
                for col in low_diff_cols:
                    if row[f'is_anomaly_{col}']:
                        reasons.append(f"{col} 最低值差异({row[f'{col}_pct_diff_from_mean']:.2f}%)")
                
                # 检查最高值异常原因
                for col in high_diff_cols:
                    if row[f'is_anomaly_{col}']:
                        reasons.append(f"{col} 最高值差异({row[f'{col}_pct_diff_from_mean']:.2f}%)")
                
                # 将原因列表合并为字符串
                events_data.at[idx, 'anomaly_reasons'] = "; ".join(reasons)
        
        return data, events_data

    def calculate_all_spread_differences(self, target_data, reference_data_dict, target_code, reference_codes):
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
        target_spread = self.calculate_price_spread(target_data)
        
        # 从目标品种的价格差值开始
        merged_data = target_spread[['date', 'price_spread']].copy()
        merged_data.rename(columns={
            'price_spread': f'{target_code}_spread'
        }, inplace=True)
        
        # 为每个参考品种计算差值差异并合并
        for ref_code in reference_codes:
            if ref_code in reference_data_dict:
                # 计算参考品种的价格差值
                ref_spread = self.calculate_price_spread(reference_data_dict[ref_code])
                
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
                    merged_data[f'spread_diff_{target_code}_{ref_code}'] = merged_data[f'spread_diff_{target_code}_{ref_code}'].fillna(0)
                    merged_data[f'spread_diff_abs_{target_code}_{ref_code}'] = merged_data[f'spread_diff_abs_{target_code}_{ref_code}'].fillna(0)
        
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
        target_spread = self.calculate_price_spread(target_data)
        reference_spread = self.calculate_price_spread(reference_data)
        
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
            
            # 计算当前值与历史平均值的差异倍数
            # 避免除以0和NaN的情况
            hist_std = df[f'{col}_hist_std'].copy()
            hist_mean = df[f'{col}_hist_mean'].copy()
            
            # 处理历史平均值为NaN的情况
            valid_mask = ~(hist_mean.isna() | hist_std.isna())
            
            # 只对有效数据计算差异倍数
            df[f'{col}_zscore'] = np.nan
            
            # 对于标准差为0或接近0的情况，差异倍数设为0（表示没有变化）
            zero_std_mask = valid_mask & (hist_std < 1e-10)
            df.loc[zero_std_mask, f'{col}_zscore'] = 0.0
            
            # 对于标准差有效的情况，正常计算差异倍数
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
    
    def calculate_spread_differences_single_pass(self, target_data, reference_data, target_code, 
                                                reference_codes, window=20, threshold_pct=50.0, use_absolute_diff=True):
        """
        单次遍历计算价格差值差异并检测异常
        
        参数:
        - target_data: 目标品种数据DataFrame
        - reference_data: 参考品种数据字典
        - target_code: 目标品种代码
        - reference_codes: 参考品种代码列表
        - window: 计算历史统计特征的窗口，默认为20天
        - threshold_pct: 价格差值差异阈值（百分比），默认为50%
        - use_absolute_diff: 是否使用绝对差异检测，默认为True
        
        返回:
        - tuple: (完整数据, 异常事件数据)
        """
        # 计算目标品种与所有参考品种的当天价格差值差异
        daily_spread_diff = self.calculate_all_spread_differences(
            target_data, reference_data, target_code, reference_codes
        )
        
        # 确保数据按日期排序
        daily_spread_diff = daily_spread_diff.sort_values('date')
        
        # 找出所有差值差异列
        spread_diff_cols = [col for col in daily_spread_diff.columns 
                           if 'spread_diff_' in col and 'abs' not in col 
                           and '_hist_mean' not in col and '_hist_std' not in col 
                           and '_zscore' not in col and '_pct_diff_from_mean' not in col 
                           and '_abs_diff_from_mean' not in col]
        
        # 初始化异常日期集合
        exclude_dates = set()
        
        # 为每个差值差异列初始化统计特征列
        for col in spread_diff_cols:
            daily_spread_diff[f'{col}_hist_mean'] = np.nan
            daily_spread_diff[f'{col}_hist_std'] = np.nan
            daily_spread_diff[f'{col}_abs_diff_from_mean'] = np.nan
            daily_spread_diff[f'{col}_pct_diff_from_mean'] = np.nan
        
        # 单次遍历处理每个日期
        for i, row in daily_spread_diff.iterrows():
            current_date = row['date']
            row_idx = daily_spread_diff.index.get_loc(i)
            
            # 为每个差值差异列计算历史统计特征
            for col in spread_diff_cols:
                # 确定窗口范围
                start_idx = max(0, row_idx - window + 1)
                end_idx = row_idx
                
                # 获取窗口内的数据，排除已标记的异常日期
                window_data = daily_spread_diff.iloc[start_idx:end_idx]
                window_mask = ~window_data['date'].isin(exclude_dates)
                valid_window_data = window_data[window_mask]
                
                if len(valid_window_data) > 0:
                    # 计算历史平均值和标准差
                    hist_mean = valid_window_data[col].mean()
                    hist_std = valid_window_data[col].std()
                    
                    # 处理标准差为0或NaN的情况
                    if pd.isna(hist_std) or hist_std < 1e-10:
                        hist_std = 0.0
                    
                    # 计算当前值与历史平均值的差异
                    current_value = row[col]
                    abs_diff_from_mean = abs(current_value - hist_mean)
                    
                    # 计算百分比差异
                    if abs(hist_mean) > 1e-10:
                        pct_diff_from_mean = (abs_diff_from_mean / abs(hist_mean)) * 100
                    else:
                        pct_diff_from_mean = 0.0
                    
                    # 更新统计特征列
                    daily_spread_diff.at[i, f'{col}_hist_mean'] = hist_mean
                    daily_spread_diff.at[i, f'{col}_hist_std'] = hist_std
                    daily_spread_diff.at[i, f'{col}_abs_diff_from_mean'] = abs_diff_from_mean
                    daily_spread_diff.at[i, f'{col}_pct_diff_from_mean'] = pct_diff_from_mean
                    
                    # 检测异常（不使用差异倍数）
                    is_anomaly_pct = pct_diff_from_mean > threshold_pct
                    is_anomaly_abs = use_absolute_diff and (abs_diff_from_mean > hist_std*2)
                    
                    # 如果任一方法检测到异常，则标记为异常
                    if is_anomaly_pct or is_anomaly_abs:
                        exclude_dates.add(current_date)
        
        # 为每个差值差异列标记异常事件
        for col in spread_diff_cols:
            # 方法1：基于百分比差异的异常检测（当前值与历史平均值的百分比差异）
            daily_spread_diff[f'is_anomaly_pct_{col}'] = daily_spread_diff[f'{col}_pct_diff_from_mean'] > threshold_pct
            
            # 方法2：基于绝对差异的异常检测（当前值与历史平均值的绝对差异）
            # 这里使用历史标准差的倍数作为阈值，默认使用2倍标准差
            if use_absolute_diff:
                daily_spread_diff[f'is_anomaly_abs_{col}'] = daily_spread_diff[f'{col}_abs_diff_from_mean'] > (daily_spread_diff[f'{col}_hist_std'] * 2)
            else:
                daily_spread_diff[f'is_anomaly_abs_{col}'] = False
        
        # 计算综合异常指标（任一参考品种出现异常即标记为异常）
        pct_anomaly_cols = [f'is_anomaly_pct_{col}' for col in spread_diff_cols]
        abs_anomaly_cols = [f'is_anomaly_abs_{col}' for col in spread_diff_cols]
        
        daily_spread_diff['is_fat_finger_pct'] = daily_spread_diff[pct_anomaly_cols].any(axis=1)
        if use_absolute_diff:
            daily_spread_diff['is_fat_finger_abs'] = daily_spread_diff[abs_anomaly_cols].any(axis=1)
            # 综合两种方法的结果，任一方法检测到异常即标记为异常
            daily_spread_diff['is_fat_finger'] = (
                daily_spread_diff['is_fat_finger_pct'] | 
                daily_spread_diff['is_fat_finger_abs']
            )
        else:
            daily_spread_diff['is_fat_finger_abs'] = False
            # 只使用百分比差异方法
            daily_spread_diff['is_fat_finger'] = daily_spread_diff['is_fat_finger_pct']
        
        # 筛选异常事件
        events_data = daily_spread_diff[daily_spread_diff['is_fat_finger']].copy()
        
        # 添加异常原因分析
        if not events_data.empty:
            for idx, row in events_data.iterrows():
                reasons = []
                for col in spread_diff_cols:
                    if row[f'is_anomaly_pct_{col}']:
                        reasons.append(f"{col} 百分比差异({row[f'{col}_pct_diff_from_mean']:.2f}%)")
                    if row[f'is_anomaly_abs_{col}']:
                        reasons.append(f"{col} 绝对差异({row[f'{col}_abs_diff_from_mean']:.2f})")
                
                # 将原因列表合并为字符串
                events_data.at[idx, 'anomaly_reasons'] = "; ".join(reasons)
        
        return daily_spread_diff, events_data

    def detect_fat_finger_events(self, target_code, reference_codes, start_date=None, end_date=None, 
                                threshold_pct=50.0, window=20, save_to_csv=True, max_iterations=3, use_absolute_diff=True):
        """
        检测乌龙指事件（基于最低值和最高值差值的新方法）
        
        参数:
        - target_code: 目标期货品种代码，如 'CU2404'
        - reference_codes: 参考期货品种代码列表，如 ['CU2405', 'AL2404', 'ZN2404']
        - start_date: 开始日期，格式为 'YYYYMMDD'，默认为60天前
        - end_date: 结束日期，格式为 'YYYYMMDD'，默认为今天
        - threshold_pct: 价格差值差异阈值（百分比），默认为50%
        - window: 历史统计窗口（天数），默认为20天
        - save_to_csv: 是否保存结果到CSV，默认为True
        - max_iterations: 最大迭代次数（保留参数以保持向后兼容，但不再使用）
        - use_absolute_diff: 是否使用绝对差异检测（保留参数以保持向后兼容，但不再使用）
        
        返回:
        - tuple: (完整数据, 异常事件数据)
        
        新的检测逻辑:
        1. 计算目标期货品种的最低值与参考期货品种的最低值之间的差值
        2. 获取过去历史统计窗口(天数)内该差值的平均值
        3. 如果当前差值超过设定的阈值，则判定为数据异常
        4. 使用相同的计算方式处理最高值的情况：计算目标期货品种的最高值与参考期货品种的最高值之间的差值，
           与对应历史统计窗口内的平均值进行比较，超过阈值则判定为数据异常
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
        
        # 使用新的基于最低值和最高值差值的方法检测乌龙指事件
        print("\n--- 使用基于最低值和最高值差值的方法检测乌龙指事件 ---")
        
        # 步骤1: 计算目标品种与参考品种的最低值和最高值差值
        print("步骤1: 计算目标品种与参考品种的最低值和最高值差值...")
        min_max_data = self.calculate_min_max_differences(
            target_data, reference_data, target_code, reference_codes
        )
        
        # 步骤2: 检测最低值和最高值的异常
        print("步骤2: 检测最低值和最高值的异常...")
        full_data, events_data = self.detect_min_max_anomalies(
            min_max_data, target_code, reference_codes, window, threshold_pct
        )
        
        print(f"检测完成，共识别 {len(events_data) if events_data is not None else 0} 个乌龙指事件")
        
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
            if events_data is not None and not events_data.empty:
                events_data_path = f"data/csv_data/fat_finger_events_{target_code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
                events_data.to_csv(events_data_path, index=False, encoding='utf-8-sig')
                print(f"异常事件数据已保存到: {events_data_path}")
        
        return full_data, events_data
    
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
            
            # 显示与参考品种的最低值和最高值差值
            for ref_code in reference_codes:
                if f'low_diff_{target_code}_{ref_code}' in row:
                    low_diff = row[f'low_diff_{target_code}_{ref_code}']
                    report.append(f"  与{ref_code}最低值差值: {low_diff:.2f}")
                    
                    # 显示历史统计信息（如果存在）
                    if f'low_diff_{target_code}_{ref_code}_hist_mean' in row and f'low_diff_{target_code}_{ref_code}_hist_std' in row:
                        hist_mean = row[f'low_diff_{target_code}_{ref_code}_hist_mean']
                        hist_std = row[f'low_diff_{target_code}_{ref_code}_hist_std']
                        pct_diff = row[f'low_diff_{target_code}_{ref_code}_pct_diff_from_mean'] if f'low_diff_{target_code}_{ref_code}_pct_diff_from_mean' in row else "N/A"
                        
                        report.append(f"    最低值差值历史平均: {hist_mean:.2f} ± {hist_std:.2f}")
                        if pct_diff != "N/A":
                            report.append(f"    当前最低值差值与历史平均的差异: {pct_diff:.2f}%")
                
                if f'high_diff_{target_code}_{ref_code}' in row:
                    high_diff = row[f'high_diff_{target_code}_{ref_code}']
                    report.append(f"  与{ref_code}最高值差值: {high_diff:.2f}")
                    
                    # 显示历史统计信息（如果存在）
                    if f'high_diff_{target_code}_{ref_code}_hist_mean' in row and f'high_diff_{target_code}_{ref_code}_hist_std' in row:
                        hist_mean = row[f'high_diff_{target_code}_{ref_code}_hist_mean']
                        hist_std = row[f'high_diff_{target_code}_{ref_code}_hist_std']
                        pct_diff = row[f'high_diff_{target_code}_{ref_code}_pct_diff_from_mean'] if f'high_diff_{target_code}_{ref_code}_pct_diff_from_mean' in row else "N/A"
                        
                        report.append(f"    最高值差值历史平均: {hist_mean:.2f} ± {hist_std:.2f}")
                        if pct_diff != "N/A":
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