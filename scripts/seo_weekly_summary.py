#!/usr/bin/env python3
"""
SEO 周报摘要生成器 (seo_weekly_summary.py)

继承自 seo-project/report_weekly.py 的分析逻辑，
读取 claude-seo 的 JSON 输出，生成结构化 Telegram 卡片摘要。

输入:
  --gsc   gsc.json         (claude-seo run gsc_query.py --json 的输出)
  --ga4   ga4.json         (claude-seo run ga4_report.py --json 的输出)
  --prev  prev_gsc.json    (上周快照，可选；不存在时跳过环比)
  --domain example.com     (站点域名，用于显示)
  --week  34               (可选，报告期数)

输出:
  --out   telegram.txt     (Telegram 消息正文，UTF-8)
  --snapshot snap.json     (本周快照，供下次对比使用)
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def pct_change(current: float, previous: float) -> str:
    """计算百分比变化，返回带符号字符串。"""
    if previous == 0:
        return "+∞%" if current > 0 else "—"
    change = (current - previous) / previous * 100
    sign = "+" if change >= 0 else ""
    return f"{sign}{change:.1f}%"


def trend_arrow(current: float, previous: float, higher_is_better: bool = True) -> str:
    """返回趋势 emoji（↑↓→），higher_is_better 控制好坏方向。"""
    if previous == 0:
        return "🆕"
    delta = current - previous
    if abs(delta) < 0.01 * max(abs(previous), 1):
        return "→"
    if higher_is_better:
        return "↑" if delta > 0 else "↓"
    else:
        return "↓" if delta > 0 else "↑"   # 排名数字越小越好


def fmt_num(n: float, decimals: int = 0) -> str:
    """格式化数字，加千位分隔符。"""
    if decimals == 0:
        return f"{int(n):,}"
    return f"{n:,.{decimals}f}"


# ──────────────────────────────────────────────
# 数据解析
# ──────────────────────────────────────────────

def parse_gsc(data: dict) -> dict:
    """
    从 claude-seo gsc_query.py --json 输出中提取关键数据。

    claude-seo 实际输出格式:
      totals: {clicks, impressions, ctr, position}
      rows:   [{query, page, clicks, impressions, ctr, position}, ...]
      quick_wins: [{query, position, impressions, ctr, clicks}, ...]  (内置)
    """
    totals = data.get("totals") or {}
    rows = data.get("rows") or []
    quick_wins = data.get("quick_wins") or []  # claude-seo 已内置，直接用

    return {
        "clicks": totals.get("clicks", 0),
        "impressions": totals.get("impressions", 0),
        "ctr": totals.get("ctr", 0),
        "position": totals.get("position", 0),
        "rows": rows,
        "quick_wins": quick_wins,
        "date_range": data.get("date_range", {}),
    }


def parse_ga4(data: dict) -> dict:
    """
    从 claude-seo ga4_report.py --json 输出中提取关键数据。

    Expected keys: totals, daily_data, top_pages
    totals: {sessions, users, pageviews}
    daily_data: [{bounce_rate, avg_session_duration, engagement_rate, ...}, ...]
    top_pages: [{landing_page, sessions, bounce_rate, ...}, ...]
    """
    totals = data.get("totals") or {}
    daily = data.get("daily_data") or []
    top_pages = data.get("top_pages") or []

    # 从 daily_data 聚合平均跳出率和停留时长
    if daily:
        avg_bounce = sum(d.get("bounce_rate", 0) for d in daily) / len(daily)
        avg_duration = sum(d.get("avg_session_duration", 0) for d in daily) / len(daily)
    else:
        avg_bounce = 0
        avg_duration = 0

    return {
        "sessions": totals.get("sessions", 0),
        "users": totals.get("users", 0),
        "pageviews": totals.get("pageviews", 0),
        "bounce_rate": round(avg_bounce, 1),
        "avg_session_duration": round(avg_duration, 1),
        "top_pages": top_pages,
    }


# ──────────────────────────────────────────────
# 分析逻辑（继承自 seo-project/report_weekly.py）
# ──────────────────────────────────────────────

def find_quick_wins(gsc: dict, min_impressions: int = 30) -> list:
    """
    快速优化机会：排名 4-20，高曝光，CTR 偏低。
    优先使用 claude-seo 内置的 quick_wins 字段，否则从 rows 计算。
    """
    # 优先用 claude-seo 内置的 quick_wins（已经过滤好了）
    built_in = gsc.get("quick_wins", [])
    if built_in:
        return built_in

    # 如果内置的不存在，从 rows 中自行聚合计算，避免多维度碎片化数据覆盖
    query_agg = {}
    for r in gsc.get("rows", []):
        q = r.get("query")
        if not q: 
            continue
        if q not in query_agg:
            query_agg[q] = {"query": q, "clicks": 0, "impressions": 0, "sum_pos_x_imp": 0.0}
        item = query_agg[q]
        imp = r.get("impressions", 0)
        item["clicks"] += r.get("clicks", 0)
        item["impressions"] += imp
        item["sum_pos_x_imp"] += r.get("position", 0) * imp

    wins = []
    for item in query_agg.values():
        imp = item["impressions"]
        if imp < min_impressions:
            continue
        pos = item["sum_pos_x_imp"] / imp if imp > 0 else 0
        ctr = item["clicks"] / imp if imp > 0 else 0
        
        if 4 <= pos <= 20 and ctr < 0.05:
            wins.append({
                "query": item["query"],
                "position": round(pos, 1),
                "impressions": imp,
                "clicks": item["clicks"],
                "ctr": ctr,
            })
    wins.sort(key=lambda x: x["impressions"], reverse=True)
    return wins


def find_page_drift(curr_rows: list, prev_rows: list, top_n: int = 10) -> tuple[list, list]:
    """
    页面级涨跌分析：对比本周和上周页面点击量。
    claude-seo rows 格式：{page: str, clicks: int, impressions: int, ...}
    返回 (gainers Top N, losers Top N)
    """
    def to_map(rows):
        m = {}
        for r in rows:
            key = r.get("page") or r.get("query", "")
            if not key:
                continue
            if key not in m:
                m[key] = {"clicks": 0, "impressions": 0}
            m[key]["clicks"] += r.get("clicks", 0)
            m[key]["impressions"] += r.get("impressions", 0)
        return m

    curr_map = to_map(curr_rows)
    prev_map = to_map(prev_rows)

    gainers = []
    losers = []

    all_pages = set(curr_map) | set(prev_map)
    for page in all_pages:
        curr_clicks = curr_map.get(page, {}).get("clicks", 0)
        prev_clicks = prev_map.get(page, {}).get("clicks", 0)
        diff = curr_clicks - prev_clicks
        short = "/" + "/".join(page.split("/")[3:]) if "://" in page else page
        if len(short) > 45:
            short = short[:42] + "…"

        if diff > 0 and prev_clicks > 0:
            gainers.append({"page": short, "curr": curr_clicks, "prev": prev_clicks, "diff": diff})
        elif diff < 0:
            losers.append({"page": short, "curr": curr_clicks, "prev": prev_clicks, "diff": diff})

    gainers.sort(key=lambda x: x["diff"], reverse=True)
    losers.sort(key=lambda x: x["diff"])
    return gainers[:top_n], losers[:top_n]


def classify_keyword(query: str, position: float, impressions: int, clicks: int) -> str:
    """
    关键词四象限分类（继承自 seo-project/report_monthly.py）
    """
    if position <= 3 and clicks >= 10:
        return "🏆 高价值"
    elif 4 <= position <= 10 and impressions >= 100:
        return "🎯 潜力"
    elif position <= 20 and impressions >= 50 and clicks < 5:
        return "📈 待优化"
    elif impressions >= 30 and len(query.split()) >= 3:
        return "🔑 长尾"
    return None


# ──────────────────────────────────────────────
# Telegram 卡片生成
# ──────────────────────────────────────────────

def build_telegram_message(
    domain: str,
    gsc: dict,
    ga4: dict,
    prev_gsc: dict | None,
    psi_score: int | None,
    indexed_count: int | None,
    week_num: int | None,
) -> str:
    """生成结构化 Telegram 卡片摘要。"""

    prev = prev_gsc or {}
    date_str = datetime.now().strftime("%Y-%m-%d")
    week_label = f" #{week_num}" if week_num else ""

    lines = []
    lines.append(f"📊 *{domain} SEO 周报{week_label}*")
    lines.append(f"📅 {date_str}")
    lines.append("─────────────────────")

    # ── 核心流量指标（GSC）
    lines.append("*🔍 搜索表现（GSC）*")

    c, pc = gsc["clicks"], prev.get("clicks", 0)
    lines.append(f"点击数　 {fmt_num(c)} {trend_arrow(c, pc)} {pct_change(c, pc)} vs 上周")

    imp, pimp = gsc["impressions"], prev.get("impressions", 0)
    lines.append(f"曝光量　 {fmt_num(imp)} {trend_arrow(imp, pimp)} {pct_change(imp, pimp)}")

    pos, ppos = gsc["position"], prev.get("position", 0)
    lines.append(f"平均排名 {fmt_num(pos, 1)} {trend_arrow(pos, ppos, higher_is_better=False)} {pct_change(pos, ppos)}")

    ctr = gsc["ctr"] * 100
    pctr = prev.get("ctr", 0) * 100
    lines.append(f"平均 CTR {ctr:.1f}% {trend_arrow(ctr, pctr)} {pct_change(ctr, pctr)}")

    if indexed_count is not None:
        lines.append(f"已收录页 {fmt_num(indexed_count)} 个")

    lines.append("")

    # ── 用户行为（GA4）
    lines.append("*👥 用户行为（GA4）*")
    lines.append(f"有机会话 {fmt_num(ga4['sessions'])} 次")
    lines.append(f"跳出率　 {ga4['bounce_rate']:.1f}%")
    dur = ga4["avg_session_duration"]
    lines.append(f"平均停留 {int(dur // 60)}m {int(dur % 60)}s")

    # ── 网页速度
    if psi_score is not None:
        emoji = "🟢" if psi_score >= 90 else "🟡" if psi_score >= 50 else "🔴"
        lines.append(f"速度评分 {emoji} {psi_score}/100")

    lines.append("─────────────────────")

    # ── 快速优化机会
    quick_wins = find_quick_wins(gsc)
    if quick_wins:
        lines.append("*🎯 本周优化机会（排名4-10，高曝光低CTR）*")
        for i, qw in enumerate(quick_wins[:5], 1):
            # claude-seo quick_wins 用 keys[0] 存 query，ctr 已乘以 100
            q_text = qw.get("query") or (qw.get("keys") or [""])[0]
            ctr_val = qw.get("ctr", 0)
            # 兼容两种格式：比例(0.03) 或 百分比(3.0)
            ctr_display = f"{ctr_val:.1f}%" if ctr_val > 1 else f"{ctr_val:.1%}"
            lines.append(f"{i}. 「{q_text}」排名{qw['position']} 曝光{qw['impressions']} CTR {ctr_display}")
        lines.append("")

    # ── 页面涨跌（仅当有上周快照时）
    if prev_gsc and prev_gsc.get("rows"):
        gainers, losers = find_page_drift(
            gsc.get("rows", []), prev_gsc.get("rows", []), top_n=10
        )

        if gainers:
            lines.append("*📈 本周涨幅页面 Top 10*")
            for g in gainers[:10]:
                lines.append(f"↑ {g['page']}  +{g['diff']}次点击")
            lines.append("")

        if losers:
            lines.append("*📉 本周跌幅页面 Top 10*")
            for l in losers[:10]:
                lines.append(f"↓ {l['page']}  {l['diff']}次点击")
            lines.append("")

    lines.append("─────────────────────")
    lines.append("完整报告见附件 👆")

    return "\n".join(lines)


# ──────────────────────────────────────────────
# Bing 数据段生成（可选）
# ──────────────────────────────────────────────

def build_bing_section(bing_dir: Path) -> str:
    """
    读取 fetch_bing.py 输出的 queries.json，生成 Bing 摘要文字段落。
    包含：总流量、Top 10 关键词、CTR 优化机会词。
    """
    queries_file = bing_dir / "queries.json"
    if not queries_file.exists():
        return ""

    try:
        with open(queries_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return ""

    this_week = data.get("this_week", [])
    prev_week = data.get("prev_week", [])
    week_date = data.get("week_date", "")

    if not this_week:
        return ""

    # 汇总统计
    total_clicks = sum(r["clicks"] for r in this_week)
    total_impr = sum(r["impressions"] for r in this_week)
    avg_ctr = total_clicks / total_impr if total_impr > 0 else 0

    # 上周总点击（环比）
    prev_total_clicks = sum(r["clicks"] for r in prev_week) if prev_week else 0

    lines = []
    lines.append("─────────────────────")
    lines.append(f"*🔵 Bing 搜索（本周快照，{week_date}）*")

    # 总点击 + 环比
    if prev_total_clicks > 0:
        delta = total_clicks - prev_total_clicks
        sign = "+" if delta >= 0 else ""
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        lines.append(f"点击数　 {total_clicks:,} {arrow} {sign}{delta:,} vs 上周")
    else:
        lines.append(f"点击数　 {total_clicks:,}（Top 100 词合计）")

    lines.append(f"曝光量　 {total_impr:,}")

    # 加权平均排名
    positions = [r["avg_position"] for r in this_week if r["avg_position"] > 0]
    if positions:
        weighted_pos = sum(
            r["avg_position"] * r["impressions"]
            for r in this_week if r["avg_position"] > 0
        ) / sum(r["impressions"] for r in this_week if r["avg_position"] > 0)
        lines.append(f"平均排名 {weighted_pos:.1f}")

    lines.append(f"平均 CTR {avg_ctr:.1%}")
    lines.append("")

    # Top 10 关键词
    lines.append("*Top 10 关键词：*")
    for i, row in enumerate(this_week[:10], 1):
        q = row["query"]
        if len(q) > 28:
            q = q[:25] + "…"
        ctr_str = f"{row['ctr']:.0%}"
        pos_str = f"排名{row['avg_position']}" if row["avg_position"] > 0 else ""
        lines.append(f"{i}. {q} — {row['clicks']}次 {pos_str} CTR {ctr_str}")

    # CTR 优化机会（曝光≥100 且 CTR<3%）
    opportunities = [
        r for r in this_week
        if r["impressions"] >= 100 and r["ctr"] < 0.03 and r["clicks"] < r["impressions"] * 0.03
    ]
    opportunities.sort(key=lambda x: x["impressions"], reverse=True)

    if opportunities:
        lines.append("")
        lines.append("*⚡ CTR 优化机会（曝光≥100，CTR<3%）：*")
        for row in opportunities[:5]:
            q = row["query"]
            if len(q) > 24:
                q = q[:21] + "…"
            lines.append(
                f"「{q}」曝光{row['impressions']:,} CTR {row['ctr']:.1%}"
            )

    return "\n".join(lines)


# ──────────────────────────────────────────────
# 快照存储
# ──────────────────────────────────────────────

def build_snapshot(gsc: dict) -> dict:
    """构建本周快照，供下次对比用。"""
    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "clicks": gsc["clicks"],
        "impressions": gsc["impressions"],
        "ctr": gsc["ctr"],
        "position": gsc["position"],
        "rows": gsc.get("rows", []),
    }


# ──────────────────────────────────────────────
# 主程序
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="生成 SEO 周报 Telegram 摘要")
    parser.add_argument("--gsc", required=True, help="GSC JSON 文件路径")
    parser.add_argument("--ga4", required=True, help="GA4 JSON 文件路径")
    parser.add_argument("--prev", default=None, help="上周 GSC 快照 JSON（可选）")
    parser.add_argument("--domain", required=True, help="站点域名，如 mp3transcript.com")
    parser.add_argument("--week", type=int, default=None, help="报告期数（可选）")
    parser.add_argument("--psi", type=int, default=None, help="PageSpeed Insights 分数（可选）")
    parser.add_argument("--indexed", type=int, default=None, help="已收录页面数（可选）")
    parser.add_argument("--out", default="telegram.txt", help="输出 Telegram 消息文件路径")
    parser.add_argument("--snapshot", default="snapshot.json", help="本周快照输出路径")
    parser.add_argument("--bing-dir", default=None, help="Bing 数据目录（可选），由 fetch_bing.py 生成")
    args = parser.parse_args()

    # 读取输入
    with open(args.gsc, encoding="utf-8") as f:
        gsc_raw = json.load(f)
    with open(args.ga4, encoding="utf-8") as f:
        ga4_raw = json.load(f)

    prev_gsc = None
    if args.prev and Path(args.prev).exists():
        with open(args.prev, encoding="utf-8") as f:
            prev_gsc = json.load(f)
        print(f"✅ 已加载上周快照: {args.prev}")
    else:
        print("ℹ️  未找到上周快照，跳过环比分析")

    gsc = parse_gsc(gsc_raw)
    ga4 = parse_ga4(ga4_raw)

    # 生成摘要
    message = build_telegram_message(
        domain=args.domain,
        gsc=gsc,
        ga4=ga4,
        prev_gsc=prev_gsc,
        psi_score=args.psi,
        indexed_count=args.indexed,
        week_num=args.week,
    )

    # 追加 Bing 段落（可选）
    if args.bing_dir:
        bing_dir = Path(args.bing_dir)
        bing_section = build_bing_section(bing_dir)
        if bing_section:
            # 移除原有的结尾行，插入 Bing 段落后再加回来
            if message.endswith("完整报告见附件 👆"):
                message = message[: message.rfind("─────────────────────")]
            message = message.rstrip() + "\n" + bing_section + "\n─────────────────────\n完整报告见附件 👆"
            print("✅ 已追加 Bing 数据段落")
        else:
            print("ℹ️  Bing 数据为空或未找到，跳过 Bing 段落")

    # 写出 Telegram 消息
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(message)
    print(f"✅ Telegram 摘要已写入: {args.out}")
    print("\n──── 预览 ────")
    print(message)
    print("──────────────")

    # 写出本周快照
    snapshot = build_snapshot(gsc)
    with open(args.snapshot, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
    print(f"✅ 本周快照已写入: {args.snapshot}")


if __name__ == "__main__":
    main()
