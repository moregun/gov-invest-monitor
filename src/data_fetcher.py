# -*- coding: utf-8 -*-
"""
ETF数据抓取模块
数据源：新浪财经实时行情 + 东方财富历史份额数据
"""

import requests
import pandas as pd
import json
import time
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    ETF_MONITOR_LIST,
    REQUEST_TIMEOUT,
    REQUEST_RETRY,
    ETF_DAILY_DATA,
    ETF_HOUR_DATA,
)


class ETFDataFetcher:
    """ETF数据抓取器"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://finance.sina.com.cn/"
        })

    def _request(self, url: str, params: dict = None) -> Optional[str]:
        """带重试的HTTP请求"""
        for i in range(REQUEST_RETRY):
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
                resp.encoding = "utf-8"
                if resp.status_code == 200:
                    return resp.text
            except Exception as e:
                print(f"[请求重试 {i+1}/{REQUEST_RETRY}] {url} - {e}")
                time.sleep(2)
        return None

    def fetch_realtime_quote(self, code: str, exchange: str = "sh") -> Optional[Dict]:
        """
        抓取ETF实时行情（新浪财经接口）
        返回：价格、净值、涨跌幅、溢价率、成交量等
        """
        url = f"https://hq.sinajs.cn/list={exchange}{code}"
        text = self._request(url)
        if not text or '=""' in text:
            return None

        try:
            raw = text.split('"')[1].split(",")
            if len(raw) < 38:
                return None

            result = {
                "code": code,
                "name": raw[0],
                "open": float(raw[1]) if raw[1] else 0,
                "prev_close": float(raw[2]) if raw[2] else 0,
                "price": float(raw[3]) if raw[3] else 0,
                "high": float(raw[4]) if raw[4] else 0,
                "low": float(raw[5]) if raw[5] else 0,
                "volume": int(float(raw[8])) if raw[8] else 0,
                "amount": float(raw[9]) if raw[9] else 0,
                "nav": float(raw[4]) if raw[4] else 0,  # IOPV净值近似
                "date": raw[30] if len(raw) > 30 else datetime.now().strftime("%Y-%m-%d"),
                "time": raw[31] if len(raw) > 31 else datetime.now().strftime("%H:%M:%S"),
            }

            # 计算溢价率
            if result["nav"] > 0 and result["price"] > 0:
                result["premium_rate"] = round(
                    (result["price"] - result["nav"]) / result["nav"] * 100, 4
                )
            else:
                result["premium_rate"] = 0

            return result
        except (ValueError, IndexError) as e:
            print(f"解析{code}实时行情失败: {e}")
            return None

    def fetch_etf_share_history(self, code: str, days: int = 60) -> pd.DataFrame:
        """
        抓取ETF历史份额数据（东方财富接口）
        返回：日期、总份额、份额变动、估算净流入
        """
        url = "https://fund.eastmoney.com/f10/F10DataApi.aspx"
        params = {
            "type": "jjgm",
            "code": code,
            "page": 1,
            "sdate": (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),
            "edate": datetime.now().strftime("%Y-%m-%d"),
        }

        text = self._request(url, params)
        if not text:
            return pd.DataFrame()

        try:
            # 解析HTML表格数据
            import re
            table_match = re.search(r'<table[^>]*>(.*?)</table>', text, re.S)
            if not table_match:
                return pd.DataFrame()

            rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_match.group(1), re.S)
            data = []
            for row in rows[1:]:  # 跳过表头
                cols = re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)
                if len(cols) >= 4:
                    date_str = cols[0].strip()
                    share_str = cols[1].strip().replace(",", "")
                    change_str = cols[2].strip().replace(",", "").replace("--", "0")
                    try:
                        data.append({
                            "date": date_str,
                            "total_share": float(share_str),  # 亿份
                            "share_change": float(change_str),  # 亿份
                        })
                    except ValueError:
                        continue

            df = pd.DataFrame(data)
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"解析{code}份额历史失败: {e}")
            return pd.DataFrame()

    def fetch_all_realtime(self) -> List[Dict]:
        """抓取全部监控ETF的实时数据"""
        results = []
        for etf in ETF_MONITOR_LIST:
            data = self.fetch_realtime_quote(etf["code"], etf["exchange"])
            if data:
                data["category"] = etf["category"]
                data["fetch_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                results.append(data)
            time.sleep(0.3)  # 限速
        return results

    def fetch_all_share_history(self, days: int = 60) -> Dict[str, pd.DataFrame]:
        """抓取全部ETF的份额历史数据"""
        result = {}
        for etf in ETF_MONITOR_LIST:
            df = self.fetch_etf_share_history(etf["code"], days)
            if not df.empty:
                result[etf["code"]] = df
            time.sleep(0.5)
        return result

    def save_hourly_snapshot(self):
        """保存小时级快照（用于日内监控）"""
        data = self.fetch_all_realtime()
        if not data:
            return

        df = pd.DataFrame(data)
        if os.path.exists(ETF_HOUR_DATA):
            df.to_csv(ETF_HOUR_DATA, mode="a", header=False, index=False, encoding="utf-8-sig")
        else:
            df.to_csv(ETF_HOUR_DATA, index=False, encoding="utf-8-sig")
        print(f"[快照保存] {len(data)} 只ETF数据已写入 {ETF_HOUR_DATA}")

    def save_daily_summary(self):
        """保存每日汇总数据（份额变动 + 资金估算）"""
        all_data = []
        share_data = self.fetch_all_share_history(days=30)

        for etf in ETF_MONITOR_LIST:
            code = etf["code"]
            if code not in share_data:
                continue
            df = share_data[code]
            latest = df.iloc[-1]

            # 估算净流入 = 份额变动 × 净值
            realtime = self.fetch_realtime_quote(code, etf["exchange"])
            nav = realtime["nav"] if realtime else latest.get("nav", 1.0)
            inflow_estimate = round(latest["share_change"] * nav, 2)

            all_data.append({
                "date": latest["date"].strftime("%Y-%m-%d"),
                "code": code,
                "name": etf["name"],
                "category": etf["category"],
                "total_share": latest["total_share"],
                "share_change": latest["share_change"],
                "nav": nav,
                "inflow_estimate": inflow_estimate,  # 亿元
            })
            time.sleep(0.3)

        if all_data:
            df = pd.DataFrame(all_data)
            if os.path.exists(ETF_DAILY_DATA):
                old_df = pd.read_csv(ETF_DAILY_DATA, encoding="utf-8-sig")
                # 去重
                df = pd.concat([old_df, df]).drop_duplicates(
                    subset=["date", "code"], keep="last"
                )
            df.to_csv(ETF_DAILY_DATA, index=False, encoding="utf-8-sig")
            print(f"[日报保存] {len(all_data)} 条记录已写入 {ETF_DAILY_DATA}")


if __name__ == "__main__":
    fetcher = ETFDataFetcher()
    print("=== 实时行情测试 ===")
    quotes = fetcher.fetch_all_realtime()
    for q in quotes:
        print(f"{q['name']}({q['code']}): 价格={q['price']}, 溢价率={q['premium_rate']}%")

    print("\n=== 份额历史测试(510300) ===")
    df = fetcher.fetch_etf_share_history("510300", days=10)
    print(df.tail())
