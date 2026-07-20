# -*- coding: utf-8 -*-
"""
ETF数据抓取模块

数据源（均来自公开免费接口，无需 API Key）：
  - 实时行情（价格/涨跌）：新浪财经 hq.sinajs.cn（主源），腾讯财经 qt.gtimg.cn（备用源）
  - 净值 / IOPV 近似（溢价率计算）：天天基金盘中估值 fundgz.1234567.com.cn
  - 每日资金净流入（资金测算 / 国家队识别核心维度）：
      东方财富 push2his 资金流日K 接口（fflow/daykline），公开免费、无需鉴权。
      该接口返回每只 ETF 每日「主力净流入(元)」等资金流数据，直接用于
      「4日累计净流入」与「连续净流入天数」判定，比旧版「份额×净值估算」更准确。

已弃用：东方财富 fundMnfh 份额接口（需鉴权 / 404，自 2024 起失效）。

注意：
  - 新浪 ETF 接口实际返回 34 个字段（非 38），解析时按字段索引取数。
  - 溢价率 = (市价 - 盘中估值净值) / 盘中估值净值 * 100。
  - 各源编码不同：新浪 GB18030、腾讯 GBK、东财/天天基金 UTF-8，_request 按源指定编码。
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
        })
        # 各数据源健康状态（用于运行日志与面板展示）
        self.source_status = {
            "sina_quote": "unused",
            "tencent_quote": "unused",
            "ttjj_nav": "unused",
            "em_flow": "unused",
        }

    def _request(self, url: str, params: dict = None, headers: dict = None,
                 encoding: str = None) -> Optional[str]:
        """带重试的 HTTP 请求；encoding 为空时由响应 content-type 决定。"""
        for i in range(REQUEST_RETRY):
            try:
                resp = self.session.get(
                    url, params=params, headers=headers, timeout=REQUEST_TIMEOUT
                )
                if resp.status_code == 200:
                    if encoding:
                        resp.encoding = encoding
                    return resp.text
            except Exception as e:
                print(f"[请求重试 {i+1}/{REQUEST_RETRY}] {url} - {e}")
                time.sleep(2)
        return None

    # ------------------------------------------------------------------ #
    # 实时行情：新浪（主）+ 腾讯（备）
    # ------------------------------------------------------------------ #
    def fetch_realtime_quote(self, code: str, exchange: str = "sh") -> Optional[Dict]:
        """抓取 ETF 实时行情。主源新浪失败自动降级腾讯。"""
        q = self._fetch_sina_quote(code, exchange)
        if q:
            self.source_status["sina_quote"] = "ok"
            return q
        self.source_status["sina_quote"] = "fail"

        q = self._fetch_tencent_quote(code, exchange)
        if q:
            self.source_status["tencent_quote"] = "ok"
            return q
        self.source_status["tencent_quote"] = "fail"
        return None

    def _fetch_sina_quote(self, code: str, exchange: str) -> Optional[Dict]:
        """新浪财经实时行情（34 字段格式，GB18030 编码）"""
        url = f"https://hq.sinajs.cn/list={exchange}{code}"
        text = self._request(
            url, headers={"Referer": "https://finance.sina.com.cn/"}, encoding="GB18030"
        )
        if not text or "=" not in text or '=""' in text:
            return None

        try:
            raw = text.split('"')[1].split(",")
            if len(raw) < 30:  # 新浪 ETF 实测 34 字段，放宽校验避免误杀
                return None

            return {
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
        except (ValueError, IndexError) as e:
            print(f"解析{code}新浪行情失败: {e}")
            return None

    def _fetch_tencent_quote(self, code: str, exchange: str) -> Optional[Dict]:
        """腾讯财经实时行情（GBK 编码），作为新浪的备用源。"""
        url = f"https://qt.gtimg.cn/q={exchange}{code}"
        text = self._request(url, encoding="gbk")
        if not text or "~" not in text:
            return None

        try:
            raw = text.split('"')[1].split("~")
            if len(raw) < 7:
                return None
            now = datetime.now()
            return {
                "code": code,
                "name": raw[1],
                "open": float(raw[5]) if raw[5] else 0,
                "prev_close": float(raw[4]) if raw[4] else 0,
                "price": float(raw[3]) if raw[3] else 0,
                "high": 0.0,
                "low": 0.0,
                "volume": int(float(raw[6])) if raw[6] else 0,
                "amount": 0.0,
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
            }
        except (ValueError, IndexError) as e:
            print(f"解析{code}腾讯行情失败: {e}")
            return None

    # ------------------------------------------------------------------ #
    # 净值 / IOPV 近似
    # ------------------------------------------------------------------ #
    def fetch_nav(self, code: str) -> Optional[Dict]:
        """天天基金盘中估值接口（UTF-8）。返回 gsz(盘中估算净值) 与 dwjz(最新单位净值)。"""
        url = f"https://fundgz.1234567.com.cn/js/{code}.js"
        text = self._request(
            url, headers={"Referer": "https://fundf10.eastmoney.com/"}, encoding="utf-8"
        )
        if not text or "jsonpgz" not in text:
            self.source_status["ttjj_nav"] = "fail"
            return None

        try:
            m = re.search(r"jsonpgz\((.*)\)", text)
            if not m:
                self.source_status["ttjj_nav"] = "fail"
                return None
            d = json.loads(m.group(1))
            gsz = float(d.get("gsz") or 0)
            dwjz = float(d.get("dwjz") or 0)
            self.source_status["ttjj_nav"] = "ok"
            return {"gsz": gsz, "dwjz": dwjz}
        except Exception as e:
            print(f"解析{code}净值失败: {e}")
            self.source_status["ttjj_nav"] = "fail"
            return None

    def fetch_all_realtime(self) -> List[Dict]:
        """抓取全部监控 ETF 的实时数据，并合并净值计算溢价率。"""
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

    # ------------------------------------------------------------------ #
    # 每日资金净流入（国家队识别核心维度）—— 东方财富 push2his 资金流日K
    # ------------------------------------------------------------------ #
    @staticmethod
    def _secid(code: str, exchange: str) -> str:
        """东方财富 secid：沪市 market=1，深市 market=0。"""
        market = "1" if exchange.lower() == "sh" else "0"
        return f"{market}.{code}"

    def fetch_etf_flow_history(self, code: str, exchange: str, days: int = 30) -> pd.DataFrame:
        """
        抓取 ETF 每日资金净流入历史（东方财富 push2his 资金流日K，公开免费、无需鉴权）。

        返回字段：
            date            -> 交易日
            net_inflow_yuan -> 当日主力净流入（元，负为净流出）
            net_inflow_yi   -> 当日主力净流入（亿元）

        字段映射（fields2=f51..f61）：
            f51 日期 | f52 主力净流入(元) | f53 超大单 | f54 大单 | f55 中单
            | f56 小单 | f57 主力净占比% | ...
        """
        secid = self._secid(code, exchange)
        url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        params = {
            "lmt": str(min(days, 120)),   # 最多取 120 个交易日
            "klt": "101",                 # 101 = 日K
            "secid": secid,
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "forcect": "1",
        }
        headers = {"Referer": "https://data.eastmoney.com/"}
        text = self._request(url, params=params, headers=headers)
        if not text:
            self.source_status["em_flow"] = "fail"
            return pd.DataFrame()

        try:
            d = json.loads(text)
            klines = (d.get("data") or {}).get("klines") if d.get("rc") == 0 else None
            if not klines:
                self.source_status["em_flow"] = "fail"
                return pd.DataFrame()

            rows = []
            for kl in klines:
                parts = kl.split(",")
                if len(parts) < 2:
                    continue
                try:
                    main_inflow = float(parts[1]) if parts[1] else 0.0  # f52 主力净流入(元)
                except ValueError:
                    main_inflow = 0.0
                rows.append({
                    "date": parts[0],
                    "net_inflow_yuan": main_inflow,
                    "net_inflow_yi": round(main_inflow / 1e8, 4),  # 亿元
                })

            df = pd.DataFrame(rows)
            if df.empty:
                self.source_status["em_flow"] = "fail"
                return df
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            self.source_status["em_flow"] = "ok"
            return df
        except Exception as e:
            print(f"解析{code}资金流历史失败: {e}")
            self.source_status["em_flow"] = "fail"
            return pd.DataFrame()

    def fetch_all_flow_history(self, days: int = 30) -> Dict[str, pd.DataFrame]:
        """抓取全部 ETF 的资金净流入历史。"""
        result = {}
        for etf in ETF_MONITOR_LIST:
            df = self.fetch_etf_flow_history(etf["code"], etf["exchange"], days)
            if not df.empty:
                result[etf["code"]] = df
            time.sleep(0.4)  # 限速
        return result

    def save_daily_summary(self):
        """
        保存每日汇总数据（每日资金净流入 + 份额变动估算）。

        数据写入 ETF_DAILY_DATA（CSV），仅保留有效行；并自动裁剪单只 ETF 超过
        120 个交易日的旧记录，避免 CSV 在 git 中无限增长。
        """
        all_data = []
        flow_data = self.fetch_all_flow_history(days=30)
        has_any = bool(flow_data)

        for etf in ETF_MONITOR_LIST:
            code = etf["code"]
            if code not in flow_data:
                print(f"  ⚠ {etf['name']} 资金流数据不可用，跳过")
                continue

            df = flow_data[code]
            latest = df.iloc[-1]

            # NAV 近似（用于份额变动估算）：优先天天基金估值，否则用实时价
            nav_info = self.fetch_nav(code)
            nav = (nav_info or {}).get("gsz") or (nav_info or {}).get("dwjz") or 0
            if nav <= 0:
                q = self.fetch_realtime_quote(code, etf["exchange"])
                nav = q["price"] if q else 1.0

            # 份额变动估算 = 资金净流入(元) / 净值(元/份) → 亿份
            share_change_est = round(latest["net_inflow_yuan"] / 1e8 / nav, 4) if nav > 0 else 0

            all_data.append({
                "date": latest["date"].strftime("%Y-%m-%d"),
                "code": code,
                "name": etf["name"],
                "category": etf["category"],
                "net_inflow_yi": latest["net_inflow_yi"],
                "nav": round(nav, 4),
                # 以下两列保持向后兼容分析器：
                # inflow_estimate = 当日主力净流入(亿元)；share_change 符号与净流入一致，
                # 用于「连续净流入天数」判定（>0 视为流入日）。
                "inflow_estimate": latest["net_inflow_yi"],
                "share_change": latest["net_inflow_yi"],
                "share_change_est": share_change_est,
            })
            time.sleep(0.3)

        if all_data:
            df = pd.DataFrame(all_data)
            if os.path.exists(ETF_DAILY_DATA):
                old_df = pd.read_csv(ETF_DAILY_DATA, encoding="utf-8-sig")
                # CSV 中 code 会被推断为整数，统一转 str 以与新数据正确去重
                old_df["code"] = old_df["code"].astype(str)
                df = pd.concat([old_df, df]).drop_duplicates(
                    subset=["date", "code"], keep="last"
                )
                # 裁剪：每只 ETF 仅保留最近 120 个交易日
                df = (df.sort_values("date")
                        .groupby("code", as_index=False)
                        .tail(120))
            df.to_csv(ETF_DAILY_DATA, index=False, encoding="utf-8-sig")
            print(f"[日报保存] {len(all_data)} 条记录已写入 {ETF_DAILY_DATA}")
        elif not has_any:
            print("[日报保存] ⚠ 全部 ETF 资金流源暂不可用，跳过写入。"
                  "实时行情与溢价率监测仍可正常使用。")

    def report_source_status(self):
        """打印各数据源健康状态。"""
        print("[数据源状态]", json.dumps(self.source_status, ensure_ascii=False))


if __name__ == "__main__":
    fetcher = ETFDataFetcher()
    print("=== 实时行情 + 溢价率测试 ===")
    quotes = fetcher.fetch_all_realtime()
    for q in quotes:
        print(f"{q['name']}({q['code']}): 价格={q['price']}, 估值净值={q['nav']}, "
              f"溢价率={q['premium_rate']}%")

    print("\n=== 资金净流入历史测试(510300) ===")
    df = fetcher.fetch_etf_flow_history("510300", "sh", days=10)
    print(df.tail() if not df.empty else "资金流数据暂不可用")
    fetcher.report_source_status()
