# -*- coding: utf-8 -*-
"""
ETF数据抓取模块

数据源（均来自公开免费接口，无需 API Key）：
  - 实时行情（价格/涨跌）：新浪财经 hq.sinajs.cn
  - 净值 / IOPV 近似（溢价率计算）：天天基金盘中估值 fundgz.1234567.com.cn
  - 份额历史（资金测算）：东方财富 fundMnfh（best-effort，接口可能需鉴权，
    若不可用则每日汇总跳过该 ETF，工具仍可展示实时行情与溢价率）

注意：
  - 新浪 ETF 接口实际返回 34 个字段（非 38），解析时按字段索引取数。
  - 溢价率 = (市价 - 盘中估值净值) / 盘中估值净值 * 100，比用最高价更贴近真实 IOPV。
"""

import requests
import pandas as pd
import re
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

    def _request(self, url: str, params: dict = None, headers: dict = None) -> Optional[str]:
        """带重试的 HTTP 请求"""
        for i in range(REQUEST_RETRY):
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
                )
                resp.encoding = "utf-8"
                if resp.status_code == 200:
                    return resp.text
            except Exception as e:
                print(f"[请求重试 {i+1}/{REQUEST_RETRY}] {url} - {e}")
                time.sleep(2)
        return None

    def fetch_realtime_quote(self, code: str, exchange: str = "sh") -> Optional[Dict]:
        """
        抓取 ETF 实时行情（新浪财经接口，34 字段格式）
        返回：价格、昨收、最高、最低、成交量、成交额、日期、时间
        """
        url = f"https://hq.sinajs.cn/list={exchange}{code}"
        text = self._request(url)
        if not text or "=" not in text or '=""' in text:
            return None

        try:
            raw = text.split('"')[1].split(",")
            # 修正：新浪 ETF 接口实际返回 34 个字段，放宽校验避免误杀
            if len(raw) < 30:
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
                "date": raw[30] if len(raw) > 30 else datetime.now().strftime("%Y-%m-%d"),
                "time": raw[31] if len(raw) > 31 else datetime.now().strftime("%H:%M:%S"),
            }
            return result
        except (ValueError, IndexError) as e:
            print(f"解析{code}实时行情失败: {e}")
            return None

    def fetch_nav(self, code: str) -> Optional[Dict]:
        """
        抓取 ETF 净值 / IOPV 近似（天天基金盘中估值接口）
        返回：gsz（盘中估算净值，用作 IOPV 近似）、dwjz（最新单位净值）
        """
        url = f"https://fundgz.1234567.com.cn/js/{code}.js"
        text = self._request(url)
        if not text or "jsonpgz" not in text:
            return None

        try:
            m = re.search(r"jsonpgz\((.*)\)", text)
            if not m:
                return None
            d = json.loads(m.group(1))
            gsz = float(d.get("gsz") or 0)
            dwjz = float(d.get("dwjz") or 0)
            return {"gsz": gsz, "dwjz": dwjz}
        except Exception as e:
            print(f"解析{code}净值失败: {e}")
            return None

    def fetch_all_realtime(self) -> List[Dict]:
        """
        抓取全部监控 ETF 的实时数据，并合并净值计算溢价率
        返回含 price / nav / premium_rate 的完整快照
        """
        results = []
        for etf in ETF_MONITOR_LIST:
            q = self.fetch_realtime_quote(etf["code"], etf["exchange"])
            nav = self.fetch_nav(etf["code"])

            if not q:
                print(f"  ⚠ {etf['name']} 实时行情获取失败，跳过")
                continue

            gsz = (nav or {}).get("gsz") or (nav or {}).get("dwjz") or 0
            if gsz > 0 and q["price"] > 0:
                premium_rate = round((q["price"] - gsz) / gsz * 100, 4)
            else:
                premium_rate = 0

            item = {
                "code": etf["code"],
                "name": q["name"] or etf["name"],
                "category": etf["category"],
                "price": q["price"],
                "nav": gsz,
                "prev_close": q["prev_close"],
                "high": q["high"],
                "low": q["low"],
                "volume": q["volume"],
                "amount": q["amount"],
                "premium_rate": premium_rate,
                "date": q["date"],
                "time": q["time"],
                "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            results.append(item)
            time.sleep(0.3)  # 限速

        return results

    def fetch_etf_share_history(self, code: str, days: int = 60) -> pd.DataFrame:
        """
        抓取 ETF 历史份额数据（东方财富 fundMnfh 接口，best-effort）
        返回：日期、总份额、份额变动
        注：该接口目前可能需鉴权 / 已变更，失败时返回空 DataFrame，
            调用方需优雅降级（跳过该 ETF 的份额相关分析）。
        若未来该接口恢复或替换为本项目可用的份额源，按以下字段映射解析：
            date        -> 日期 (FSRQ)
            total_share -> 期末总份额 (LJFE)
            share_change-> 本期份额变动 (FE)
        """
        url = "https://api.fund.eastmoney.com/f10/fundMnfh"
        params = {"fundCode": code, "pageIndex": "1", "pageSize": str(min(days, 20))}
        headers = {"Referer": "https://fundf10.eastmoney.com/"}

        text = self._request(url, params=params, headers=headers)
        if not text or "Data" not in text:
            return pd.DataFrame()

        try:
            d = json.loads(text)
            data = (d.get("Data") or {}).get("data") if isinstance(d, dict) else None
            if not data:
                return pd.DataFrame()

            rows = []
            for it in data:
                try:
                    rows.append({
                        "date": it.get("FSRQ") or it.get("date"),
                        "total_share": float(it.get("LJFE", 0) or 0),
                        "share_change": float(it.get("FE", 0) or 0),
                    })
                except (ValueError, TypeError):
                    continue

            df = pd.DataFrame(rows)
            if not df.empty:
                df["date"] = pd.to_datetime(df["date"])
                df = df.sort_values("date").reset_index(drop=True)
            return df
        except Exception as e:
            print(f"解析{code}份额历史失败: {e}")
            return pd.DataFrame()

    def fetch_all_share_history(self, days: int = 60) -> Dict[str, pd.DataFrame]:
        """抓取全部 ETF 的份额历史数据"""
        result = {}
        for etf in ETF_MONITOR_LIST:
            df = self.fetch_etf_share_history(etf["code"], days)
            if not df.empty:
                result[etf["code"]] = df
            time.sleep(0.5)
        return result

    def save_daily_summary(self):
        """保存每日汇总数据（份额变动 + 资金估算）

        份额数据不可用时跳过该 ETF，不写入空记录，保证 etf_daily_data.csv
        中只保留有效行。
        """
        all_data = []
        share_data = self.fetch_all_share_history(days=30)
        has_any = bool(share_data)

        for etf in ETF_MONITOR_LIST:
            code = etf["code"]
            if code not in share_data:
                print(f"  ⚠ {etf['name']} 份额数据不可用，跳过")
                continue

            df = share_data[code]
            latest = df.iloc[-1]

            # 估算净流入 = 份额变动 × 净值（用实时价近似）
            realtime = self.fetch_realtime_quote(code, etf["exchange"])
            nav = realtime["price"] if realtime else latest.get("nav", 1.0)
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
        elif not has_any:
            print("[日报保存] ⚠ 全部 ETF 份额数据源暂不可用，跳过写入。"
                  "实时行情与溢价率监测仍可正常使用。")


if __name__ == "__main__":
    fetcher = ETFDataFetcher()
    print("=== 实时行情 + 溢价率测试 ===")
    quotes = fetcher.fetch_all_realtime()
    for q in quotes:
        print(f"{q['name']}({q['code']}): 价格={q['price']}, 估值净值={q['nav']}, "
              f"溢价率={q['premium_rate']}%")

    print("\n=== 份额历史测试(510300) ===")
    df = fetcher.fetch_etf_share_history("510300", days=10)
    print(df.tail() if not df.empty else "份额数据暂不可用（接口需鉴权）")
