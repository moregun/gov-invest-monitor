# -*- coding: utf-8 -*-
"""
ETF数据抓取模块

数据源（均来自公开免费接口，无需 API Key）：
  - 实时行情（价格/涨跌）：新浪财经 hq.sinajs.cn（主源），腾讯财经 qt.gtimg.cn（备用源）
  - 净值 / IOPV 近似（溢价率计算）：天天基金盘中估值 fundgz.1234567.com.cn
  - 每日净申购（国家队识别【核心】维度）：
      东方财富「最新份额」(akshare fund_etf_spot_em 的『最新份额』列，免费)。
      净申购额 = (今日份额 − 昨日份额) × 单位净值，单位为元。
      这正是国家队「护盘」的真实动作——中央汇金等通过一级市场创造新份额申购 ETF，
      体现在「份额增长」而非二级市场的买卖盘。

  - 二级市场资金流（参考/交叉校验，非核心）：
      东方财富 push2his 资金流日K（fflow/daykline）的 f52「主力净流入」，
      反映二级市场交易的大单净买卖，受套利盘/做市商/散户影响，符号与净申购常相反，
      因此【不能】用作国家队净申购判定，仅作辅助参考。

已弃用：东方财富 fundMnfh 份额接口（需鉴权 / 404，自 2024 起失效）。

重要更正（2026-07-20）：早期版本用 f52 主力净流入替代失效的份额接口，实测发现
f52 衡量的是二级买卖盘，与国家队「净申购」不是同一量——真实护盘发生时 f52 反而显示
净流出。故改以「最新份额」口径计算净申购，方为正确监测量。

注意：
  - 新浪 ETF 接口实际返回 34 个字段（非 38），解析时按字段索引取数。
  - 溢价率 = (市价 - 盘中估值净值) / 盘中估值净值 * 100。
  - 各源编码不同：新浪 GB18030、腾讯 GBK、东财/天天基金 UTF-8，_request 按源指定编码。
  - akshare 为本项目新增依赖（仅用于取 ETF 最新份额），缺失时自动降级回 f52 参考口径。
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
            "em_shares": "unused",
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

    def fetch_etf_shares(self) -> Dict[str, float]:
        """
        抓取全部监控 ETF 的【最新份额】(单位：份)。

        主力源：akshare fund_etf_spot_em (免费，返回含『最新份额』列的全市场 ETF 快照)。
        该快照是当日份额规模，配合每日存储的昨日份额即可算出「净申购额」。

        若 akshare 不可用，降级返回空 dict（save_daily_summary 会改用 f52 参考口径）。

        返回：{code: shares(float, 份)}
        """
        try:
            import akshare as ak
            df = ak.fund_etf_spot_em()
            codes = {str(e["code"]) for e in ETF_MONITOR_LIST}
            sub = df[df["代码"].astype(str).isin(codes)]
            result = {}
            for _, r in sub.iterrows():
                code = str(r["代码"])
                sh = r.get("最新份额")
                if sh is not None and not pd.isna(sh):
                    result[code] = float(sh)
            if result:
                self.source_status["em_shares"] = "ok"
                return result
        except Exception as e:
            print(f"[份额] akshare 获取最新份额失败: {e}")
        self.source_status["em_shares"] = "fail"
        return {}

    def save_daily_summary(self):
        """
        保存每日汇总数据（核心：净申购额 = (今日份额 − 昨日份额) × 单位净值）。

        做法：
          1. 取当日『最新份额』快照（akshare fund_etf_spot_em）；
          2. 从已有 CSV 读取该 ETF 上一交易日份额，算 Δ份额；
          3. 净申购额(元) = Δ份额 × 单位净值；净申购额(亿) = /1e8。
        首次运行无历史份额时净申购记为 0，仅建立份额基线，后续交易日自动累积正确历史。

        同时保留 f52 二级市场资金流(net_inflow_yi)作为参考列。

        写入 ETF_DAILY_DATA（CSV），每只 ETF 仅保留最近 120 个交易日，避免无限增长。
        """
        # 一次性取全市场份额快照
        shares_snapshot = self.fetch_etf_shares()
        has_shares = bool(shares_snapshot)

        # 读取已有数据，用于取「上一交易日份额」
        prev_shares = {}
        prev_nav = {}
        if os.path.exists(ETF_DAILY_DATA):
            old_df = pd.read_csv(ETF_DAILY_DATA, encoding="utf-8-sig")
            old_df["code"] = old_df["code"].astype(str)
            for code in {str(e["code"]) for e in ETF_MONITOR_LIST}:
                cdf = old_df[old_df["code"] == code].sort_values("date")
                if cdf.empty:
                    continue
                last = cdf.iloc[-1]
                if "shares" in cdf.columns and pd.notna(last.get("shares")):
                    prev_shares[code] = float(last["shares"])
                if "nav" in cdf.columns and pd.notna(last.get("nav")):
                    prev_nav[code] = float(last["nav"])

        all_data = []
        for etf in ETF_MONITOR_LIST:
            code = etf["code"]
            # NAV：优先天天基金 dwjz/gsz，否则实时价
            nav_info = self.fetch_nav(code)
            nav = (nav_info or {}).get("dwjz") or (nav_info or {}).get("gsz") or 0
            if nav <= 0:
                q = self.fetch_realtime_quote(code, etf["exchange"])
                nav = q["price"] if q else 1.0

            # f52 二级市场资金流（参考列）
            flow = self.fetch_etf_flow_history(code, etf["exchange"], days=5)
            net_inflow_yi = round(flow.iloc[-1]["net_inflow_yi"], 4) if not flow.empty else 0.0

            # 净申购（核心）：需当日份额 + 昨日份额
            shares = shares_snapshot.get(code)
            if shares is not None and code in prev_shares and prev_shares[code] > 0 and nav > 0:
                d_share = shares - prev_shares[code]          # 份额变动(份)
                net_subscription_yuan = d_share * nav          # 净申购额(元)
                net_subscription_yi = round(net_subscription_yuan / 1e8, 4)
            else:
                # 首次运行 / 份额源不可用：建立基线，净申购记为 0
                net_subscription_yi = 0.0

            all_data.append({
                "date": datetime.now().strftime("%Y-%m-%d"),
                "code": code,
                "name": etf["name"],
                "category": etf["category"],
                # 核心量：净申购额（份额口径）
                "shares": round(shares, 0) if shares is not None else "",
                "nav": round(nav, 4),
                "net_subscription_yi": net_subscription_yi,
                # 参考量：二级市场资金流（f52，符号常与净申购相反，仅供参考）
                "net_inflow_yi": net_inflow_yi,
                # 向后兼容分析器：inflow_estimate / share_change 统一取净申购口径
                "inflow_estimate": net_subscription_yi,
                "share_change": net_subscription_yi,
            })
            time.sleep(0.3)

        if all_data:
            df = pd.DataFrame(all_data)
            if os.path.exists(ETF_DAILY_DATA):
                old_df = pd.read_csv(ETF_DAILY_DATA, encoding="utf-8-sig")
                old_df["code"] = old_df["code"].astype(str)
                # 旧数据可能缺 net_subscription_yi 列，补 0 避免 concat 报错
                for col in ["shares", "net_subscription_yi", "net_inflow_yi",
                            "inflow_estimate", "share_change"]:
                    if col not in old_df.columns:
                        old_df[col] = 0 if col != "shares" else ""
                df = pd.concat([old_df, df]).drop_duplicates(
                    subset=["date", "code"], keep="last"
                )
                df = (df.sort_values("date")
                        .groupby("code", as_index=False)
                        .tail(120))
            df.to_csv(ETF_DAILY_DATA, index=False, encoding="utf-8-sig")
            print(f"[日报保存] {len(all_data)} 条记录已写入 {ETF_DAILY_DATA}"
                  f"（份额源={'可用' if has_shares else '不可用(降级f52)'}）")
        else:
            print("[日报保存] ⚠ 未生成任何记录，跳过写入。")

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
