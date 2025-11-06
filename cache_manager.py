#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
缓存管理工具模块

提供缓存文件的创建、查询、清理等功能
"""

import os
import json
import hashlib
import glob
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any


class CacheManager:
    """
    缓存管理器类
    
    负责管理期货数据的缓存文件，包括：
    - 缓存元数据的维护
    - 缓存文件的查询和清理
    - 参数组合与缓存文件的映射关系管理
    """
    
    def __init__(self, cache_dir="data/csv_data", metadata_file="cache_metadata.json"):
        """
        初始化缓存管理器
        
        参数:
        - cache_dir: 缓存目录路径
        - metadata_file: 缓存元数据文件名
        """
        self.cache_dir = cache_dir
        self.metadata_file = os.path.join(cache_dir, metadata_file)
        
        # 确保缓存目录存在
        if not os.path.exists(cache_dir):
            os.makedirs(cache_dir)
        
        # 加载缓存元数据
        self.metadata = self._load_metadata()
    
    def _load_metadata(self) -> Dict[str, Any]:
        """
        加载缓存元数据
        
        返回:
        - 缓存元数据字典
        """
        if os.path.exists(self.metadata_file):
            try:
                with open(self.metadata_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"加载缓存元数据失败: {e}")
                return {}
        return {}
    
    def _save_metadata(self) -> bool:
        """
        保存缓存元数据
        
        返回:
        - 是否保存成功
        """
        try:
            with open(self.metadata_file, 'w', encoding='utf-8') as f:
                json.dump(self.metadata, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"保存缓存元数据失败: {e}")
            return False
    
    def _generate_params_hash(self, params: Dict[str, Any]) -> str:
        """
        根据参数生成哈希值
        
        参数:
        - params: 参数字典
        
        返回:
        - 参数哈希值（8位MD5）
        """
        params_str = str(sorted(params.items()))
        return hashlib.md5(params_str.encode()).hexdigest()[:8]
    
    def get_cache_key(self, data_type: str, symbol: str, params: Dict[str, Any]) -> str:
        """
        根据数据类型、品种代码和参数生成缓存键
        
        参数:
        - data_type: 数据类型（如 'future' 或 'future_main'）
        - symbol: 品种代码
        - params: 参数字典
        
        返回:
        - 缓存键
        """
        params_hash = self._generate_params_hash(params)
        return f"{data_type}_{symbol}_{params_hash}"
    
    def get_cache_file_path(self, data_type: str, symbol: str, params: Dict[str, Any]) -> str:
        """
        根据数据类型、品种代码和参数生成缓存文件路径
        
        参数:
        - data_type: 数据类型（如 'future' 或 'future_main'）
        - symbol: 品种代码
        - params: 参数字典
        
        返回:
        - 缓存文件路径
        """
        params_hash = self._generate_params_hash(params)
        return os.path.join(self.cache_dir, f"{data_type}_{symbol}_{params_hash}.csv")
    
    def register_cache(self, data_type: str, symbol: str, params: Dict[str, Any], 
                      file_path: str, data_count: int = 0) -> bool:
        """
        注册缓存文件
        
        参数:
        - data_type: 数据类型（如 'future' 或 'future_main'）
        - symbol: 品种代码
        - params: 参数字典
        - file_path: 缓存文件路径
        - data_count: 数据记录数
        
        返回:
        - 是否注册成功
        """
        cache_key = self.get_cache_key(data_type, symbol, params)
        
        self.metadata[cache_key] = {
            'data_type': data_type,
            'symbol': symbol,
            'params': params,
            'file_path': file_path,
            'created_time': datetime.now().isoformat(),
            'data_count': data_count
        }
        
        return self._save_metadata()
    
    def get_cache_info(self, data_type: str, symbol: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        获取缓存信息
        
        参数:
        - data_type: 数据类型（如 'future' 或 'future_main'）
        - symbol: 品种代码
        - params: 参数字典
        
        返回:
        - 缓存信息字典，如果不存在则返回None
        """
        cache_key = self.get_cache_key(data_type, symbol, params)
        return self.metadata.get(cache_key)
    
    def is_cache_valid(self, data_type: str, symbol: str, params: Dict[str, Any], 
                      cache_days: int = 1) -> bool:
        """
        检查缓存是否有效
        
        参数:
        - data_type: 数据类型（如 'future' 或 'future_main'）
        - symbol: 品种代码
        - params: 参数字典
        - cache_days: 缓存有效期（天数）
        
        返回:
        - 缓存是否有效
        """
        cache_info = self.get_cache_info(data_type, symbol, params)
        if not cache_info:
            return False
        
        file_path = cache_info['file_path']
        if not os.path.exists(file_path):
            return False
        
        # 检查文件修改时间
        file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
        current_time = datetime.now()
        
        return (current_time - file_mod_time).days < cache_days
    
    def clean_expired_cache(self, cache_days: int = 7) -> int:
        """
        清理过期的缓存文件
        
        参数:
        - cache_days: 缓存保留天数
        
        返回:
        - 清理的文件数量
        """
        cleaned_count = 0
        expired_keys = []
        
        # 查找过期的缓存
        for cache_key, cache_info in self.metadata.items():
            file_path = cache_info['file_path']
            if os.path.exists(file_path):
                file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                if (datetime.now() - file_mod_time).days > cache_days:
                    expired_keys.append(cache_key)
                    try:
                        os.remove(file_path)
                        cleaned_count += 1
                        print(f"已删除过期缓存文件: {file_path}")
                    except Exception as e:
                        print(f"删除缓存文件失败: {file_path}, 错误: {e}")
        
        # 从元数据中移除过期的缓存
        for key in expired_keys:
            if key in self.metadata:
                del self.metadata[key]
        
        # 保存更新后的元数据
        if expired_keys:
            self._save_metadata()
        
        return cleaned_count
    
    def clean_orphaned_cache(self) -> int:
        """
        清理孤立的缓存文件（元数据中不存在记录的文件）
        
        返回:
        - 清理的文件数量
        """
        # 获取元数据中记录的所有文件路径
        recorded_files = set()
        for cache_info in self.metadata.values():
            recorded_files.add(cache_info['file_path'])
        
        # 查找缓存目录中的所有CSV文件
        cache_files = glob.glob(os.path.join(self.cache_dir, "*.csv"))
        
        cleaned_count = 0
        for file_path in cache_files:
            if file_path not in recorded_files:
                try:
                    os.remove(file_path)
                    cleaned_count += 1
                    print(f"已删除孤立缓存文件: {file_path}")
                except Exception as e:
                    print(f"删除孤立缓存文件失败: {file_path}, 错误: {e}")
        
        return cleaned_count
    
    def list_cache(self, data_type: Optional[str] = None, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        列出缓存信息
        
        参数:
        - data_type: 数据类型过滤（可选）
        - symbol: 品种代码过滤（可选）
        
        返回:
        - 缓存信息列表
        """
        cache_list = []
        
        for cache_key, cache_info in self.metadata.items():
            # 应用过滤条件
            if data_type and cache_info.get('data_type') != data_type:
                continue
            if symbol and cache_info.get('symbol') != symbol:
                continue
            
            # 添加文件存在性和大小信息
            file_path = cache_info['file_path']
            if os.path.exists(file_path):
                file_size = os.path.getsize(file_path)
                file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path)).isoformat()
                cache_info_copy = cache_info.copy()
                cache_info_copy.update({
                    'file_exists': True,
                    'file_size': file_size,
                    'file_mod_time': file_mod_time
                })
                cache_list.append(cache_info_copy)
            else:
                cache_info_copy = cache_info.copy()
                cache_info_copy.update({
                    'file_exists': False,
                    'file_size': 0,
                    'file_mod_time': None
                })
                cache_list.append(cache_info_copy)
        
        return cache_list
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """
        获取缓存统计信息
        
        返回:
        - 缓存统计信息字典
        """
        total_count = len(self.metadata)
        existing_count = 0
        total_size = 0
        
        for cache_info in self.metadata.values():
            file_path = cache_info['file_path']
            if os.path.exists(file_path):
                existing_count += 1
                total_size += os.path.getsize(file_path)
        
        # 按数据类型统计
        type_stats = {}
        for cache_info in self.metadata.values():
            data_type = cache_info.get('data_type', 'unknown')
            if data_type not in type_stats:
                type_stats[data_type] = 0
            type_stats[data_type] += 1
        
        return {
            'total_cache_entries': total_count,
            'existing_files': existing_count,
            'missing_files': total_count - existing_count,
            'total_size_bytes': total_size,
            'total_size_mb': round(total_size / (1024 * 1024), 2),
            'type_distribution': type_stats
        }