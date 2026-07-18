# -*- coding: utf-8 -*-
"""
gov-invest-monitor 全局配置
"""

import os

# 项目根目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 数据存储目录
DATA_DIR = os.path.join(BASE_DIR, "data")
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# 国家队核心监控ETF列表（汇金/证金主要操作标的）
ETF_MONITOR_LIST = [
    {
        "code": "510050",
        "name": "上证50ETF",
        "exchange": "sh",
        "category": "大盘蓝筹"
    },
    {
        "code": "510300",
        "name": "沪深300ETF",
        "exchange": "sh",
        "category": "宽基核心"
    },
    {
        "code": "510500",
        "name": "中证500ETF",
        "exchange": "sh",
        "category": "中盘宽基"
    },
    {
        "code": "588000",
        "name": "科创50ETF",
        "exchange": "sh",
        "category": "成长宽基"
    },
    {
        "code": "512100",
        "name": "中证1000ETF",
        "exchange": "sh",
        "category": "小盘宽基"
    },
    {
        "code": "159915",
        "name": "创业板ETF",
        "exchange": "sz",
        "category": "成长宽基"
    },
]

# 国家队资金判定阈值
NATIONAL_TEAM_THRESHOLD = {
    # 溢价率阈值(%)，持续高于此值疑似一级市场大额申购
    "premium_rate": 0.05,
    # 连续份额增长天数阈值
    "share_growth_days": 3,
    # 单只 ETF 在统计周期内（默认4日）的累计净流入阈值(亿元)，
    # 用于判定"同步大额流入"。注意：是比较周期内累计值，而非单日值。
    "etf_period_inflow": 5,
    # 同步流入 ETF 数量阈值
    "multi_etf_sync": 4,
}

# 数据文件路径
ETF_DAILY_DATA = os.path.join(DATA_DIR, "etf_daily_data.csv")
FLOW_SUMMARY = os.path.join(DATA_DIR, "flow_summary.json")

# 抓取超时设置
REQUEST_TIMEOUT = 15
REQUEST_RETRY = 3

# 时区
TIMEZONE = "Asia/Shanghai"
