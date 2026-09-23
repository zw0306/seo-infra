"""
Bing Webmaster Tools 月度报告生成器（seo-infra 版）
读取 fetch_bing.py 输出的 JSON 文件，生成 Markdown 月报，
并交叉对比 GSC 数据，找出双引擎机会词。

用法:
  python3 report_bing.py --domain example.com \
      --bing-dir /tmp/bing \
      --gsc-json /tmp/gsc.json \
      --out /reports/bing_monthly.md
"""
import json
import sys
import os
import argparse
from datetime import datetime
from pathlib import Path


# ─── 工具函数 ────────────────────────────────────────────────

def load_json(path: str) -> dict:
    """加载 JSON 文件，失败时返回空 dict"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"   ⚠️ 读取 {path} 失败: {e}")
        return {}


def pct_str(val: float) -> str:
    return f"{val:.2%}"


def short_url(url: str, max_len: int = 55) -> str:
    """将完整 URL 缩短为路径部分"""
    from urllib.parse import urlparse
    try:
        path = urlparse(url).path or "/"
    except Exception:
        path = url
    return path[:max_len] + "…" if len(path) > max_len else path


# ─── 核心分析函数 ────────────────────────────────────────────

def analyze_quick_wins(bing_queries: list[dict]) -> list[dict]:
    """
    高曝光低 CTR 关键词 — Bing 快速优化机会
    阈值：曝光 ≥ 100，CTR < 3%
    """
    results = [q for q in bing_queries if q["impressions"] >= 100 and q["ctr"] < 0.03]
    return sorted(results, key=lambda x: x["impressions"], reverse=True)


def analyze_daily_trend(bing_daily: list[dict]) -> dict:
    """日均流量趋势：前一半 vs 后一半"""
    if not bing_daily:
        return {}
    mid = len(bing_daily) // 2
    first_half = bing_daily[:mid]
    second_half = bing_daily[mid:]
    avg_first  = sum(d["clicks"] for d in first_half)  / max(len(first_half),  1)
    avg_second = sum(d["clicks"] for d in second_half) / max(len(second_half), 1)
    total_clicks      = sum(d["clicks"]      for d in bing_daily)
    total_impressions = sum(d["impressions"] for d in bing_daily)
    pct = (avg_second - avg_first) / avg_first * 100 if avg_first > 0 else 0
    sign = "+" if pct >= 0 else ""
    return {
        "total_clicks":       total_clicks,
        "total_impressions":  total_impressions,
        "avg_clicks_per_day": round(total_clicks / len(bing_daily), 1),
        "avg_impr_per_day":   round(total_impressions / len(bing_daily), 1),
        "trend_direction":    "📈 上升" if avg_second > avg_first else "📉 下降",
        "trend_pct":          f"{sign}{pct:.1f}%",
    }


def compare_with_gsc(bing_queries: list[dict], gsc_queries: list[dict]) -> dict:
    """
    GSC vs Bing 关键词交叉比对
    返回:
      - only_bing: 仅 Bing 有点击（≥4）的词
      - only_gsc:  仅 Google 有点击（≥5）的词（Bing 机会词）
      - both:      双引擎都有的词（对比点击量）
    """
    bing_map = {q["query"].lower(): q for q in bing_queries}
    gsc_map  = {q["query"].lower(): q for q in gsc_queries}

    only_bing, only_gsc, both = [], [], []

    for kw, bdata in bing_map.items():
        if kw in gsc_map:
            gdata = gsc_map[kw]
            both.append({
                "query":            bdata["query"],
                "bing_clicks":      bdata["clicks"],
                "bing_impressions": bdata["impressions"],
                "bing_ctr":         bdata["ctr"],
                "gsc_clicks":       gdata["clicks"],
                "gsc_impressions":  gdata["impressions"],
                "gsc_position":     gdata.get("position", 0),
            })
        elif bdata["clicks"] >= 4:
            only_bing.append(bdata)

    for kw, gdata in gsc_map.items():
        if kw not in bing_map and gdata["clicks"] >= 5:
            only_gsc.append({
                "query":           gdata["query"],
                "gsc_clicks":      gdata["clicks"],
                "gsc_impressions": gdata["impressions"],
                "gsc_position":    gdata.get("position", 0),
            })

    only_bing.sort(key=lambda x: x["clicks"],      reverse=True)
    only_gsc.sort( key=lambda x: x["gsc_clicks"],  reverse=True)
    both.sort(     key=lambda x: x["bing_clicks"],  reverse=True)
    return {"only_bing": only_bing, "only_gsc": only_gsc, "both": both}


# ─── 报告生成 ────────────────────────────────────────────────

def generate_bing_report(domain: str, bing_dir: Path, gsc_json_path: str, out_path: str):
    """生成 Bing Webmaster Tools Markdown 月报"""
    today    = datetime.now().date()
    date_str = today.strftime("%Y-%m-%d")

    print(f"\n📊 生成 Bing 月报: {domain}")

    # 加载 Bing 数据
    queries_data = load_json(str(bing_dir / "queries.json"))
    pages_data   = load_json(str(bing_dir / "pages.json"))
    daily_data   = load_json(str(bing_dir / "daily.json"))
    issues_data  = load_json(str(bing_dir / "crawl_issues.json"))

    if not queries_data:
        print(f"❌ 未找到 Bing 关键词数据：{bing_dir / 'queries.json'}")
        return

    bing_queries = queries_data.get("data", [])
    bing_pages   = pages_data.get("data",   []) if pages_data   else []
    bing_daily   = daily_data.get("data",   []) if daily_data   else []
    bing_issues  = issues_data.get("data",  []) if issues_data  else []

    data_end = queries_data.get("end_date", "N/A")
    days     = queries_data.get("days",     30)
    site_url = queries_data.get("site_url", f"https://{domain}/")

    print(f"   Bing 关键词: {len(bing_queries)} 条  |  页面: {len(bing_pages)} 个  |  日趋势: {len(bing_daily)} 天")

    # 加载 GSC 数据（兼容 seo-infra 现有格式）
    gsc_queries = []
    if gsc_json_path and os.path.exists(gsc_json_path):
        gsc_data = load_json(gsc_json_path)
        for row in gsc_data.get("rows", []):
            keys  = row.get("keys", [])
            query = row.get("query") or (keys[0] if keys else "")
            if not query:
                continue
            ctr_raw = row.get("ctr", 0) or 0
            gsc_queries.append({
                "query":       query,
                "clicks":      row.get("clicks",      0) or 0,
                "impressions": row.get("impressions", 0) or 0,
                "ctr":         ctr_raw / 100.0,   # gsc.json 中 ctr 已 ×100
                "position":    row.get("position",  0) or 0,
            })
    has_gsc = len(gsc_queries) > 0
    print(f"   GSC 关键词: {len(gsc_queries)} 条 {'(用于对比)' if has_gsc else '(未找到，跳过对比)'}")

    # 执行分析
    quick_wins = analyze_quick_wins(bing_queries)
    trend      = analyze_daily_trend(bing_daily)
    cross      = compare_with_gsc(bing_queries, gsc_queries) if has_gsc else {}

    total_clicks = sum(q["clicks"]      for q in bing_queries)
    total_impr   = sum(q["impressions"] for q in bing_queries)
    avg_ctr      = total_clicks / max(total_impr, 1)

    # ─── 组装 Markdown ────
    lines = [
        f"# 📊 Bing SEO 月报 — {domain}",
        "",
        f"> **报告日期**: {date_str}",
        f"> **Bing 数据**: 近 {days} 天（截至 {data_end}）",
        f"> **站点**: {site_url}",
        "",
        "---",
        "",
        "## 1. 📈 Bing 流量总览",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 总点击数 | {total_clicks:,} |",
        f"| 总曝光数 | {total_impr:,} |",
        f"| 平均 CTR | {avg_ctr:.2%} |",
        f"| 活跃关键词数 | {len(bing_queries):,} |",
        f"| Top 页面数 | {len(bing_pages):,} |",
        "",
    ]

    # 每日趋势
    if trend:
        lines += [
            "### 每日流量趋势",
            "",
            "| 指标 | 数值 |",
            "|---|---|",
            f"| 日均点击 | {trend['avg_clicks_per_day']} |",
            f"| 日均曝光 | {trend['avg_impr_per_day']} |",
            f"| 近期趋势 | {trend['trend_direction']} ({trend['trend_pct']}) |",
            "",
        ]

    # Top 关键词
    lines += [
        "## 2. 🏆 Top 20 关键词（按点击）",
        "",
        "| 关键词 | 点击 | 曝光 | CTR | 均位 |",
        "|---|---|---|---|---|",
    ]
    for q in bing_queries[:20]:
        lines.append(
            f"| {q['query']} | {q['clicks']:,} | {q['impressions']:,} "
            f"| {pct_str(q['ctr'])} | {q['avg_position']} |"
        )
    lines.append("")

    # Top 页面
    if bing_pages:
        lines += [
            "## 3. 📄 Top 20 页面（按点击）",
            "",
            "| 页面 | 点击 | 曝光 | CTR | 均位 |",
            "|---|---|---|---|---|",
        ]
        for p in bing_pages[:20]:
            lines.append(
                f"| {short_url(p['page'])} | {p['clicks']:,} | {p['impressions']:,} "
                f"| {pct_str(p['ctr'])} | {p.get('avg_position', 'N/A')} |"
            )
        lines.append("")
    else:
        lines += [
            "## 3. 📄 Top 页面（数据暂缺）",
            "",
            "> ⚠️ `GetPageStats` 本次返回空数据，可能是 Bing 数据延迟或站点流量过低。",
            "> 以下展示 Top 关键词作为参考。",
            "",
            "| 关键词 | 点击 | 曝光 | CTR |",
            "|---|---|---|---|",
        ]
        for q in bing_queries[:20]:
            lines.append(f"| {q['query']} | {q['clicks']:,} | {q['impressions']:,} | {pct_str(q['ctr'])} |")
        lines.append("")

    # 快速优化机会
    if quick_wins:
        lines += [
            "## 4. 🎯 Bing 快速优化机会（高曝光低 CTR）",
            "",
            "> 这些词在 Bing 曝光量大但点击率偏低，优化 Title/Meta 可快速提升 Bing 流量。",
            "",
            "| 关键词 | 曝光 | 点击 | CTR | 建议 |",
            "|---|---|---|---|---|",
        ]
        for q in quick_wins[:20]:
            sug = "优化 Title + Meta Description" if q["ctr"] < 0.01 else "优化标题点击吸引力"
            lines.append(
                f"| {q['query']} | {q['impressions']:,} | {q['clicks']:,} | {pct_str(q['ctr'])} | {sug} |"
            )
        lines.append("")

    # 抓取错误
    if bing_issues:
        lines += [
            f"## 5. 🕷️ 抓取错误（共 {len(bing_issues)} 个）",
            "",
            "| URL | 错误类型 | 最后抓取 |",
            "|---|---|---|",
        ]
        for issue in bing_issues[:20]:
            url_short = issue["url"][:70] + "…" if len(issue["url"]) > 70 else issue["url"]
            lines.append(f"| {url_short} | {issue['issue_type']} | {issue['last_crawled']} |")
        lines.append("")
    else:
        lines += ["## 5. 🕷️ 抓取错误", "", "> ✅ 暂无抓取错误。", ""]

    # GSC vs Bing 对比
    if cross:
        only_bing = cross["only_bing"]
        only_gsc  = cross["only_gsc"]
        both      = cross["both"]

        lines += ["## 6. 🔄 Google vs Bing 双引擎对比", ""]

        if both:
            lines += [
                "### ✅ 双引擎共有关键词 Top 15",
                "",
                "| 关键词 | Bing 点击 | Google 点击 | G 排名 |",
                "|---|---|---|---|",
            ]
            for q in both[:15]:
                pos_str = f"{q['gsc_position']:.1f}" if q["gsc_position"] > 0 else "N/A"
                lines.append(f"| {q['query']} | {q['bing_clicks']:,} | {q['gsc_clicks']:,} | {pos_str} |")
            lines.append("")

        if only_bing:
            lines += [
                f"### 🔵 仅 Bing 有流量（共 {len(only_bing)} 个）— Google 内容缺口",
                "",
                "> 这些词说明 Bing 用户有需求，但 Google 可能排名较低，可针对性创作/优化内容。",
                "",
                "| 关键词 | Bing 点击 | Bing 曝光 | CTR |",
                "|---|---|---|---|",
            ]
            for q in only_bing[:20]:
                lines.append(f"| {q['query']} | {q['clicks']:,} | {q['impressions']:,} | {pct_str(q['ctr'])} |")
            lines.append("")

        if only_gsc:
            lines += [
                f"### 🟠 仅 Google 有流量 Top 20 — Bing 提升机会",
                "",
                "> 这些词在 Google 有点击，但 Bing 几乎没流量，可在 Bing 站长工具提交相关页面。",
                "",
                "| 关键词 | Google 点击 | Google 曝光 | G 排名 |",
                "|---|---|---|---|",
            ]
            for q in only_gsc[:20]:
                pos_str = f"{q['gsc_position']:.1f}" if q.get("gsc_position", 0) > 0 else "N/A"
                lines.append(f"| {q['query']} | {q['gsc_clicks']:,} | {q['gsc_impressions']:,} | {pos_str} |")
            lines.append("")

    # Action Items
    lines += ["## 7. ✅ Bing SEO Action Items", ""]
    actions = []
    if quick_wins:
        top_qw = quick_wins[0]
        actions.append(
            f"- [ ] **Bing CTR 优化**: 「{top_qw['query']}」曝光 {top_qw['impressions']:,}"
            f" 但 CTR 仅 {pct_str(top_qw['ctr'])}，重写 Title/Meta 提升点击率"
        )
    if cross and cross.get("only_bing"):
        top_bing = cross["only_bing"][0]
        actions.append(
            f"- [ ] **Google 内容补充**: 「{top_bing['query']}」在 Bing 有 {top_bing['clicks']} 次点击，"
            "但 Google 未覆盖，检查 Google 排名并优化"
        )
    if cross and cross.get("only_gsc") and len(cross["only_gsc"]) > 5:
        actions.append(
            f"- [ ] **Bing 排名提升**: {len(cross['only_gsc'])} 个词在 Google 有流量但 Bing 没有，"
            "在 Bing 站长工具提交这些关键词的页面"
        )
    if bing_issues:
        actions.append(f"- [ ] **修复抓取错误**: Bing 发现 {len(bing_issues)} 个抓取问题，优先修复高频错误类型")
    actions += [
        "- [ ] 提交/更新 sitemap 到 Bing 站长工具",
        "- [ ] 对比 Bing vs Google 总流量比例，评估 Bing 战略价值",
    ]
    lines.extend(actions)
    lines.append("")

    # 保存
    out_file = Path(out_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n✅ Bing 月报已生成: {out_file}")
    return str(out_file)


# ─── 入口 ────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="生成 Bing Webmaster Tools SEO 月报")
    parser.add_argument("--domain",    required=True, help="站点域名，例如 example.com")
    parser.add_argument("--bing-dir",  required=True, help="fetch_bing.py 输出的 JSON 目录")
    parser.add_argument("--gsc-json",  default="",    help="GSC JSON 路径（可选，用于双引擎对比）")
    parser.add_argument("--out",       required=True, help="输出 Markdown 文件路径")
    args = parser.parse_args()

    generate_bing_report(
        domain=args.domain,
        bing_dir=Path(args.bing_dir),
        gsc_json_path=args.gsc_json,
        out_path=args.out,
    )
