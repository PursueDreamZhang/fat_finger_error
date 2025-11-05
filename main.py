
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta
import os
import glob

def get_future_data(future_code, start_date=None, end_date=None, save_to_csv=True, 
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
    - DataFrame: 包含期货历史数据的数据框--
    """
    
    # 设置默认日期范围（过去3年）
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=3*365)).strftime('%Y%m%d')
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')
    
    # 创建data文件夹（如果不存在）
    if not os.path.exists('data'):
        os.makedirs('data')
    
    # 生成缓存文件名
    cache_file_name = f"data/future_{future_code}_{start_date}_{end_date}.csv"
    
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
                
                # 验证数据有效性
                if not df.empty and len(df) > 0:
                    print(f"从缓存加载 {len(df)} 条数据记录")
                    print("\n数据预览:")
                    print(df.head())
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
        df = ak.futures_main_sina(symbol=future_code)
        
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
        df = add_future_calculated_fields(df)
        
        print(f"成功获取 {len(df)} 条数据记录")
        print("\n数据预览:")
        print(df.head())
        
        # 保存到CSV文件（更新缓存）
        if save_to_csv:
            # 保存数据
            df.to_csv(cache_file_name, index=False, encoding='utf-8-sig')
            print(f"\n数据已保存到: {cache_file_name}")
        
        return df
        
    except Exception as e:
        print(f"获取期货数据时出错: {e}")
        return None

def add_future_calculated_fields(df):
    """
    为期货数据添加计算字段
    
    参数:
    - df: 包含期货历史数据的DataFrame
    
    返回:
    - DataFrame: 添加了计算字段的数据框
    """
    
    # 确保数据不为空
    if df.empty:
        return df
    
    # 获取列名的映射，处理不同接口可能返回的不同列名
    # 常见的列名有：收盘价/收盘/close, 最低价/最低/low, 最高价/最高/high
    close_col = None
    low_col = None
    high_col = None
    
    # 查找收盘价列
    for col in ['收盘', '收盘价', 'close', 'Close']:
        if col in df.columns:
            close_col = col
            break
    
    # 查找最低价列
    for col in ['最低', '最低价', 'low', 'Low']:
        if col in df.columns:
            low_col = col
            break
    
    # 查找最高价列
    for col in ['最高', '最高价', 'high', 'High']:
        if col in df.columns:
            high_col = col
            break
    
    # 如果找不到必要的列，返回原始数据
    if close_col is None or low_col is None or high_col is None:
        print("警告：无法找到必要的价格列（收盘价、最低价、最高价），跳过计算字段的添加")
        print(f"数据列：{df.columns.tolist()}")
        return df
    
    # 添加计算字段：最低价与收盘价的差值百分比
    # 计算公式：((最低价 - 收盘价) / 收盘价) * 100%
    # 处理收盘价为0的异常情况
    df['最低价与收盘价差值百分比'] = df.apply(
        lambda row: round((row[low_col] - row[close_col]) / row[close_col] * 100, 2) 
        if row[close_col] != 0 else 0.0, 
        axis=1
    )
    
    # 添加计算字段：最高价与收盘价的差值百分比
    # 计算公式：((最高价 - 收盘价) / 收盘价) * 100%
    # 处理收盘价为0的异常情况
    df['最高价与收盘价差值百分比'] = df.apply(
        lambda row: round((row[high_col] - row[close_col]) / row[close_col] * 100, 2) 
        if row[close_col] != 0 else 0.0, 
        axis=1
    )
    
    return df

def get_future_main_contract_data(exchange_symbol, start_date=None, end_date=None, 
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
    
    # 设置默认日期范围（过去3年）
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=3*365)).strftime('%Y%m%d')
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')
    
    # 创建data文件夹（如果不存在）
    if not os.path.exists('data'):
        os.makedirs('data')
    
    # 生成缓存文件名
    cache_file_name = f"data/future_main_{exchange_symbol}_{start_date}_{end_date}.csv"
    
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
                
                # 验证数据有效性
                if not df.empty and len(df) > 0:
                    print(f"从缓存加载 {len(df)} 条数据记录")
                    print("\n数据预览:")
                    print(df.head())
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
            df = ak.futures_main_sina(symbol=exchange_symbol)
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
        df = add_future_calculated_fields(df)
        
        print(f"成功获取 {len(df)} 条数据记录")
        print("\n数据预览:")
        print(df.head())
        
        # 保存到CSV文件（更新缓存）
        if save_to_csv:
            # 保存数据
            df.to_csv(cache_file_name, index=False, encoding='utf-8-sig')
            print(f"\n数据已保存到: {cache_file_name}")
        
        return df
        
    except Exception as e:
        print(f"获取期货主力合约数据时出错: {e}")
        return None

def get_stock_data(stock_code, start_date=None, end_date=None, save_to_csv=True, 
                   cache_days=1, use_cache=True):
    """
    获取指定股票过去3年的历史数据并保存到本地，支持本地缓存机制
    
    参数:
    - stock_code: 股票代码，如 '000001'（平安银行）
    - start_date: 开始日期，格式为 'YYYYMMDD'，默认为3年前
    - end_date: 结束日期，格式为 'YYYYMMDD'，默认为今天
    - save_to_csv: 是否保存为CSV文件，默认为True
    - cache_days: 缓存有效期（天数），默认为1天
    - use_cache: 是否使用缓存，默认为True
    
    返回:
    - DataFrame: 包含股票历史数据的数据框
    """
    
    # 设置默认日期范围（过去3年）
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=3*365)).strftime('%Y%m%d')
    if end_date is None:
        end_date = datetime.now().strftime('%Y%m%d')
    
    # 创建data文件夹（如果不存在）
    if not os.path.exists('data'):
        os.makedirs('data')
    
    # 生成缓存文件名
    cache_file_name = f"data/{stock_code}_{start_date}_{end_date}.csv"
    
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
                
                # 验证数据有效性
                if not df.empty and len(df) > 0:
                    print(f"从缓存加载 {len(df)} 条数据记录")
                    print("\n数据预览:")
                    print(df.head())
                    return df
                else:
                    print("缓存数据无效，将重新获取")
            else:
                print(f"缓存数据已过期（超过{cache_days}天），将重新获取")
        except Exception as e:
            print(f"读取缓存文件时出错: {e}，将重新获取数据")
    
    # 缓存不存在、已过期或无效，重新获取数据
    print(f"正在获取股票 {stock_code} 从 {start_date} 到 {end_date} 的历史数据...")
    
    try:
        # 使用AKShare获取股票历史数据
        df = ak.stock_zh_a_hist(symbol=stock_code, period="daily", 
                                start_date=start_date, end_date=end_date, adjust="qfq")
        
        if df.empty:
            print(f"未获取到股票 {stock_code} 的数据，请检查股票代码是否正确")
            return None
        
        print(f"成功获取 {len(df)} 条数据记录")
        print("\n数据预览:")
        print(df.head())
        
        # 保存到CSV文件（更新缓存）
        if save_to_csv:
            # 保存数据
            df.to_csv(cache_file_name, index=False, encoding='utf-8-sig')
            print(f"\n数据已保存到: {cache_file_name}")
        
        return df
        
    except Exception as e:
        print(f"获取数据时出错: {e}")
        return None

def calculate_n_day_average(df, n_days, column='收盘'):
    """
    计算股票的N日均价（移动平均线）
    
    参数:
    - df: 包含股票历史数据的DataFrame
    - n_days: 计算均价的天数，如5、10、20、60等
    - column: 用于计算均价的列名，默认为'收盘'（收盘价）
    
    返回:
    - DataFrame: 原始数据加上N日均价列
    """
    
    # 确保数据按日期排序
    if '日期' in df.columns:
        df = df.sort_values('日期')
    
    # 计算N日移动平均
    avg_column_name = f'{n_days}日均价'
    df[avg_column_name] = df[column].rolling(window=n_days).mean()
    
    # 计算N日均价与收盘价的差值和百分比
    diff_column_name = f'{n_days}日差值'
    pct_column_name = f'{n_days}日差值%'
    
    df[diff_column_name] = df[column] - df[avg_column_name]
    df[pct_column_name] = (df[column] - df[avg_column_name]) / df[avg_column_name] * 100
    
    # 填充NaN值为0（前n-1行没有足够数据计算均价）
    df[avg_column_name] = df[avg_column_name].fillna(0)
    df[diff_column_name] = df[diff_column_name].fillna(0)
    df[pct_column_name] = df[pct_column_name].fillna(0)
    
    return df

def calculate_multiple_averages(df, n_days_list=[5, 10, 20, 60], column='收盘'):
    """
    计算多个N日均价
    
    参数:
    - df: 包含股票历史数据的DataFrame
    - n_days_list: 要计算的天数列表，如[5, 10, 20, 60]
    - column: 用于计算均价的列名，默认为'收盘'（收盘价）
    
    返回:
    - DataFrame: 原始数据加上多个N日均价列
    """
    
    for n_days in n_days_list:
        df = calculate_n_day_average(df, n_days, column)
    
    return df

def analyze_stock_with_averages(stock_code, n_days_list=[5, 10, 20, 60], 
                               start_date=None, end_date=None, save_to_csv=True,
                               cache_days=1, use_cache=True):
    """
    获取股票数据并计算多个N日均价
    
    参数:
    - stock_code: 股票代码
    - n_days_list: 要计算的天数列表，如[5, 10, 20, 60]
    - start_date: 开始日期，格式为 'YYYYMMDD'，默认为3年前
    - end_date: 结束日期，格式为 'YYYYMMDD'，默认为今天
    - save_to_csv: 是否保存为CSV文件，默认为True
    - cache_days: 缓存有效期（天数），默认为1天
    - use_cache: 是否使用缓存，默认为True
    
    返回:
    - DataFrame: 包含原始数据和多个N日均价的数据框
    """
    
    # 获取股票数据
    df = get_stock_data(stock_code, start_date, end_date, save_to_csv=False,
                       cache_days=cache_days, use_cache=use_cache)
    
    if df is None:
        return None
    
    # 计算多个N日均价
    df = calculate_multiple_averages(df, n_days_list)
    
    # 显示最新的均价数据
    print(f"\n股票 {stock_code} 最新均价数据:")
    latest_data = df.iloc[-1][['日期', '开盘', '收盘', '最高', '最低']]
    print(latest_data)
    
    print("\n最新N日均价:")
    for n_days in n_days_list:
        avg_col = f'{n_days}日均价'
        diff_col = f'{n_days}日差值'
        pct_col = f'{n_days}日差值%'
        
        latest_avg = df.iloc[-1][avg_col]
        latest_diff = df.iloc[-1][diff_col]
        latest_pct = df.iloc[-1][pct_col]
        
        print(f"{n_days}日均价: {latest_avg:.2f}, 差值: {latest_diff:.2f}, 差值百分比: {latest_pct:.2f}%")
    
    # 保存到CSV文件
    if save_to_csv:
        # 创建data文件夹（如果不存在）
        if not os.path.exists('data'):
            os.makedirs('data')
        
        # 生成文件名
        file_name = f"data/{stock_code}_averages_{start_date}_{end_date}.csv"
        
        # 保存数据
        df.to_csv(file_name, index=False, encoding='utf-8-sig')
        print(f"\n数据已保存到: {file_name}")
    
    return df

def get_multiple_stocks_data(stock_codes, start_date=None, end_date=None, 
                            cache_days=1, use_cache=True):
    """
    获取多个股票的历史数据并保存到本地
    
    参数:
    - stock_codes: 股票代码列表，如 ['000001', '000002']
    - start_date: 开始日期，格式为 'YYYYMMDD'，默认为3年前
    - end_date: 结束日期，格式为 'YYYYMMDD'，默认为今天
    - cache_days: 缓存有效期（天数），默认为1天
    - use_cache: 是否使用缓存，默认为True
    """
    
    all_data = {}
    
    for code in stock_codes:
        print(f"\n{'='*50}")
        df = get_stock_data(code, start_date, end_date, 
                           cache_days=cache_days, use_cache=use_cache)
        if df is not None:
            all_data[code] = df
    
    return all_data

def clear_cache(stock_code=None, older_than_days=None):
    """
    清理缓存文件
    
    参数:
    - stock_code: 指定股票代码，如果为None则清理所有缓存
    - older_than_days: 清理多少天前的缓存，如果为None则清理所有缓存
    """
    
    if not os.path.exists('data'):
        print("缓存目录不存在")
        return
    
    # 获取所有缓存文件
    pattern = f"data/{stock_code}_*.csv" if stock_code else "data/*.csv"
    cache_files = glob.glob(pattern)
    
    if not cache_files:
        print("没有找到缓存文件")
        return
    
    current_time = datetime.now()
    deleted_count = 0
    
    for file_path in cache_files:
        try:
            # 如果指定了older_than_days，检查文件是否超过指定天数
            if older_than_days is not None:
                file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
                if (current_time - file_mod_time).days < older_than_days:
                    continue
            
            # 删除文件
            os.remove(file_path)
            print(f"已删除缓存文件: {file_path}")
            deleted_count += 1
        except Exception as e:
            print(f"删除缓存文件 {file_path} 时出错: {e}")
    
    print(f"共删除 {deleted_count} 个缓存文件")

def list_cache():
    """
    列出所有缓存文件及其信息
    """
    
    if not os.path.exists('data'):
        print("缓存目录不存在")
        return
    
    cache_files = glob.glob("data/*.csv")
    
    if not cache_files:
        print("没有找到缓存文件")
        return
    
    print("缓存文件列表:")
    print("-" * 80)
    print(f"{'文件名':<30} {'大小(KB)':<10} {'修改时间':<20} {'天数':<10}")
    print("-" * 80)
    
    current_time = datetime.now()
    
    for file_path in sorted(cache_files):
        try:
            file_name = os.path.basename(file_path)
            file_size = os.path.getsize(file_path) / 1024  # KB
            file_mod_time = datetime.fromtimestamp(os.path.getmtime(file_path))
            days_old = (current_time - file_mod_time).days
            
            print(f"{file_name:<30} {file_size:<10.2f} {file_mod_time.strftime('%Y-%m-%d %H:%M:%S'):<20} {days_old:<10}")
        except Exception as e:
            print(f"处理文件 {file_path} 时出错: {e}")

if __name__ == "__main__":
    # 示例1: 获取单个股票数据（带缓存）
    # 平安银行(000001)过去3年的数据
    stock_code = "159915"  # 您可以修改为任何A股代码
    
    # 首先列出当前缓存
    #print("="*60)
    #print("当前缓存状态:")
    #list_cache()
    
    #print("\n" + "="*60)
    #print("获取股票数据（使用缓存）:")
    # 使用缓存获取数据，缓存有效期为7天
    #data = get_stock_data(stock_code, cache_days=7)
    
    # 再次获取相同数据，应该使用缓存
    #print("\n" + "="*60)
    #print("再次获取相同数据（应该使用缓存）:")
    #data_again = get_stock_data(stock_code, cache_days=7)
    
    # 示例2: 获取多个股票数据（取消注释以使用）
    # stock_codes = ["000001", "000002", "600000"]  # 平安银行、万科A、浦发银行
    # all_data = get_multiple_stocks_data(stock_codes, cache_days=7)
    
    # 示例3: 自定义日期范围（取消注释以使用）
    # start_date = "20200101"
    # end_date = "20231231"
    # custom_data = get_stock_data(stock_code, start_date, end_date, cache_days=7)
    
    # 示例4: 计算N日均价（新功能）
    #print("\n" + "="*60)
    #print("计算N日均价示例")
    #print("="*60)
    
    # 获取股票数据并计算5日、10日、20日、60日均价
    #stock_with_averages = analyze_stock_with_averages(stock_code, [5, 10, 20, 60], cache_days=7)
    
    # 如果您只想计算特定天数的均价，可以修改下面的代码
    # custom_averages = analyze_stock_with_averages(stock_code, [30, 120], cache_days=7)  # 计算30日和120日均价
    
    # 示例5: 期货数据获取（新功能）
    print("\n" + "="*60)
    print("期货数据获取示例")
    print("="*60)
    
    # 获取期货主力合约数据
    # 沪铜主力合约
    #future_code = "CU"  # 沪铜
    #future_data = get_future_main_contract_data(future_code, cache_days=7)
    
    # 获取指定期货合约数据
    # 遍历获取2023年1月1日至2023年12月31日的所有期货合约数据
    for month in range(1, 13):
        specific_future = f"CU24{month:02d}"  # 格式为CU24MM，例如CU2401
        specific_future_data = get_future_main_contract_data(specific_future, cache_days=7)
        print(f"获取到 {specific_future} 数据，共 {len(specific_future_data)} 天")
    

