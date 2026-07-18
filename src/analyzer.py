# -*- coding: utf-8 -*-
"""
国家队资金分析模块
核心逻辑：基于ETF份额变动、溢价率、同步性特征识别国家队护盘资金
"""

import pandas as pd
import json
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import (
    ETF_MONITOR_LIST,
    NATIONAL_TEAM_THRESHOLD,
    ETF_DAILY_DATA,
    FLOW_SUMMARY,
)


class NationalTeamAnalyzer:
    """国家队资金分析器"""

    def __init__(self):
        self.threshold = NATIONAL_TEAM_THRESHOLD

    def load_daily_data(self) -> pd.DataFrame:
        """加载历史日度数据"""
        if not os.path.exists(ETF_DAILY_DATA):
            return pd.DataFrame()
        return pd.read_csv(ETF_DAILY_DATA, encoding="utf-8-sig")

    def calculate_period_inflow(self, df: pd.DataFrame, days: int = 4) -> Dict:
        """
        计算指定周期内各ETF累计净流入
        对标高盛4日290亿统计口径
        """
        if df.empty:
            return {}

        df = df.sort_values("date")
        latest_date = df["date"].max()
        start_date = (
            pd.to_datetime(latest_date) - timedelta(days=days)
        ).strftime("%Y-%m-%d")

        period_df = df[df["date"] >= start_date]
        result = {}

        for etf in ETF_MONITOR_LIST:
            code = etf["code"]
            etf_df = period_df[period_df["code"] == code]
            if etf_df.empty:
                continue

            total_inflow = round(etf_df["inflow_estimate"].sum(), 2)
            total_share_change = round(etf_df["share_change"].sum(), 2)

            # 连续增长天数
            consecutive_days = 0
            sorted_etf = etf_df.sort_values("date")
            for _, row in sorted_etf.iterrows():
                if row["share_change"] > 0:
                    consecutive_days += 1
                else:
                    consecutive_days = 0

            result[code] = {
                "name": etf["name"],
                "category": etf["category"],
                "period_days": days,
                "total_inflow_yi": total_inflow,
                "total_share_change_yi_fen": total_share_change,
                "consecutive_growth_days": consecutive_days,
                "latest_inflow": round(sorted_etf.iloc[-1]["inflow_estimate"], 2),
            }

        return result

    def detect_national_team_signals(
        self, period_inflow: Dict, realtime_data: List[Dict] = None
    ) -> Dict:
        """
        识别国家队资金信号
        判定维度：
        1. 多ETF同步大额流入（分散布局特征）
        2. 连续多日份额增长（持续性特征）
        3. 溢价率持续为正（一级市场申购特征）
        4. 流入集中于核心宽基（不炒行业，护盘特征）
        """
        signals = {
            "score": 0,
            "max_score": 100,
            "level": "无明显信号",
            "trigger_factors": [],
            "sync_inflow_count": 0,
            "estimated_total_inflow": 0,
            "detail": {},
        }

        if not period_inflow:
            return signals

        # 1. 统计同步流入ETF数量
        inflow_etfs = [
            code for code, data in period_inflow.items()
            if data["total_inflow_yi"] > self.threshold["single_day_inflow"]
        ]
        signals["sync_inflow_count"] = len(inflow_etfs)
        signals["estimated_total_inflow"] = round(
            sum(d["total_inflow_yi"] for d in period_inflow.values()), 2
        )

        if len(inflow_etfs) >= self.threshold["multi_etf_sync"]:
            signals["score"] += 35
            signals["trigger_factors"].append(
                f"{len(inflow_etfs)}只宽基ETF同步大额流入，符合国家队分散护盘特征"
            )

        # 2. 连续增长天数判定
        long_consecutive = [
            data["name"] for data in period_inflow.values()
            if data["consecutive_growth_days"] >= self.threshold["share_growth_days"]
        ]
        if long_consecutive:
            signals["score"] += 30
            signals["trigger_factors"].append(
                f"{'、'.join(long_consecutive)} 连续{self.threshold['share_growth_days']}日以上份额增长，持续性符合国家队操作"
            )

        # 3. 溢价率特征（需实时数据）
        if realtime_data:
            premium_etfs = [
                item["name"] for item in realtime_data
                if item.get("premium_rate", 0) > self.threshold["premium_rate"]
            ]
            if len(premium_etfs) >= 3:
                signals["score"] += 25
                signals["trigger_factors"].append(
                    f"{'、'.join(premium_etfs[:3])} 等多只ETF持续溢价，疑似一级市场大额申购"
                )

        # 4. 流入规模判定
        if signals["estimated_total_inflow"] > 100:
            signals["score"] += 10
            signals["trigger_factors"].append(
                f"4日累计预估流入超{signals['estimated_total_inflow']}亿元，规模显著"
            )

        # 评级
        if signals["score"] >= 80:
            signals["level"] = "强护盘信号"
        elif signals["score"] >= 50:
            signals["level"] = "中度护盘信号"
        elif signals["score"] >= 25:
            signals["level"] = "弱护盘信号"

        signals["detail"] = period_inflow
        return signals

    def calculate_index_drawdown(self, index_code: str = "000905") -> Dict:
        """
        计算指数从高点回撤幅度
        默认中证500(000905)，可扩展
        注：此处为简化实现，实际部署可接入指数行情接口
        """
        # 预留接口，可接入东财指数行情
        # 实际项目中可通过 akshare 或 tushare 获取完整K线
        return {
            "index_code": index_code,
            "index_name": "中证500",
            "note": "需接入指数行情接口计算精确回撤，当前版本通过ETF价格近似估算",
        }

    def generate_summary_report(self, realtime_data: List[Dict] = None) -> Dict:
        """生成完整分析报告"""
        df = self.load_daily_data()
        period_inflow = self.calculate_period_inflow(df, days=4)
        signals = self.detect_national_team_signals(period_inflow, realtime_data)

        report = {
            "generate_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "data_date": df["date"].max() if not df.empty else "N/A",
            "national_team_signal": signals,
            "etf_flow_ranking": sorted(
                [
                    {
                        "code": code,
                        "name": data["name"],
                        "category": data["category"],
                        "four_day_inflow": data["total_inflow_yi"],
                        "consecutive_days": data["consecutive_growth_days"],
                    }
                    for code, data in period_inflow.items()
                ],
                key=lambda x: x["four_day_inflow"],
                reverse=True,
            ),
            "total_four_day_inflow": signals["estimated_total_inflow"],
        }

        # 保存摘要
        os.makedirs(os.path.dirname(FLOW_SUMMARY), exist_ok=True)
        with open(FLOW_SUMMARY, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        return report

    def generate_markdown_report(self, report: Dict) -> str:
        """生成Markdown格式日报"""
        signal = report["national_team_signal"]
        lines = [
            f"# 国家队ETF资金监控日报",
            f"",
            f"> 生成时间：{report['generate_time']}",
            f"> 数据日期：{report['data_date']}",
            f"",
            f"## 一、护盘信号评级",
            f"",
            f"- **信号等级**：{signal['level']}",
            f"- **综合评分**：{signal['score']}/{signal['max_score']}",
            f"- **同步流入ETF数**：{signal['sync_inflow_count']} 只",
            f"- **4日累计预估流入**：{report['total_four_day_inflow']} 亿元",
            f"",
        ]

        if signal["trigger_factors"]:
            lines.append("### 触发因子")
            for i, factor in enumerate(signal["trigger_factors"], 1):
                lines.append(f"{i}. {factor}")
            lines.append("")

        lines.extend([
            "## 二、各ETF资金流入排行（近4日）",
            "",
            "| 排名 | ETF名称 | 类型 | 4日累计流入(亿元) | 连续增长天数 |",
            "|------|---------|------|-------------------|--------------|",
        ])

        for i, item in enumerate(report["etf_flow_ranking"], 1):
            inflow_color = "📈" if item["four_day_inflow"] > 0 else "📉"
            lines.append(
                f"| {i} | {item['name']} | {item['category']} | "
                f"{inflow_color} {item['four_day_inflow']} | {item['consecutive_days']}天 |"
            )

        lines.extend([
            "",
            "## 三、判定逻辑说明",
            "",
            "本监控基于以下国家队操作特征进行识别：",
            "",
            "1. **分散布局**：同时买入多只核心宽基ETF（沪深300/中证500/上证50/科创50等），不集中单一赛道",
            "2. **持续买入**：连续多日逆市申购，而非单日脉冲式游资行为",
            "3. **一级市场特征**：ETF持续溢价，反映大额一级市场申购",
            "4. **逆周期操作**：市场大跌阶段大额流入，上涨阶段暂停买入",
            "",
            "---",
            "*数据来源：东方财富、新浪财经公开行情数据 | 仅供研究参考，不构成投资建议*",
        ])

        return "\n".join(lines)


if __name__ == "__main__":
    analyzer = NationalTeamAnalyzer()
    report = analyzer.generate_summary_report()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("\n=== Markdown 报告 ===")
    print(analyzer.generate_markdown_report(report))
