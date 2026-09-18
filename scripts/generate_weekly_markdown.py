#!/usr/bin/env python3
"""
SEO 周报生成器（工作流版）
从工作流已产出的 JSON 文件生成 Markdown 周报，无任何认证依赖。

数据输入:
  --gsc-this  : 本周 GSC JSON（query+page 双维度 + totals）
  --gsc-last  : 上周 GSC JSON（格式相同）
  --ga4-14d   : GA4 最近 14 天 daily_data（--days 14 拉取，脚本内按日期分割本周/上周）
  --domain    : 站点域名
  --out       : 输出 Markdown 文件路径

格式说明:
  gsc_query.py 输出的 ctr 已 ×100（是百分比），本脚本读入后统一 /100 转为小数。
  行数据既有 keys[] 也有 query/page 字段，优先读命名字段。
"""
import json
import argparse
import re
import os
import math
from datetime import datetime, timedelta, date
from pathlib import Path
from collections import defaultdict, Counter


# ──────────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────────

def load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠️  读取 {path} 失败: {e}")
        return {}


def pct_change(current, previous) -> str:
    if previous == 0:
        return "+∞" if current > 0 else "0%"
    change = (current - previous) / previous * 100
    sign = "+" if change > 0 else ""
    return f"{sign}{change:.1f}%"


def fmt_ctr(val: float) -> str:
    return f"{val:.1%}"


# ──────────────────────────────────────────────
# GSC 数据解析
# ──────────────────────────────────────────────

def parse_gsc_rows(gsc_data: dict) -> list:
    """
    解析 gsc_query.py 输出的 rows，统一 ctr 为小数。
    gsc_query.py 默认 --dimensions query,page，每行同时有 query 和 page 字段。
    ctr 已 ×100（是百分比），读入时 /100 还原为小数。
    """
    rows = gsc_data.get("rows", [])
    result = []
    for r in rows:
        keys = r.get("keys", [])
        query = r.get("query") or (keys[0] if len(keys) > 0 else "")
        page  = r.get("page")  or (keys[1] if len(keys) > 1 else "")
        ctr_raw = r.get("ctr", 0) or 0
        result.append({
            "keys": keys,
            "query": query,
            "page": page,
            "clicks": r.get("clicks", 0) or 0,
            "impressions": r.get("impressions", 0) or 0,
            "ctr": ctr_raw / 100.0,
            "position": r.get("position", 0) or 0,
        })
    return result


def parse_gsc_totals(gsc_data: dict) -> dict:
    t = gsc_data.get("totals", {})
    if not t:
        return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    return {
        "clicks":      t.get("clicks", 0) or 0,
        "impressions": t.get("impressions", 0) or 0,
        "ctr":         (t.get("ctr", 0) or 0) / 100.0,
        "position":    t.get("position", 0) or 0,
    }


# ──────────────────────────────────────────────
# GA4 数据解析（从 14 天 daily_data 按日期分割）
# ──────────────────────────────────────────────

def split_ga4_by_week(ga4_14d: dict, this_start: date, this_end: date):
    """
    ga4_report.py --days 14 输出 daily_data（日期格式 YYYYMMDD）。
    按日期范围分割为本周和上周并汇总 sessions/users/pageviews。
    """
    daily = ga4_14d.get("daily_data", [])
    this_t = {"sessions": 0, "users": 0, "pageviews": 0}
    last_t = {"sessions": 0, "users": 0, "pageviews": 0}
    last_start = this_start - timedelta(days=7)
    last_end   = this_end   - timedelta(days=7)

    for row in daily:
        try:
            d = datetime.strptime(row.get("date", ""), "%Y%m%d").date()
        except Exception:
            continue
        s = row.get("sessions", 0) or 0
        u = row.get("users", 0) or 0
        pv = row.get("pageviews", 0) or 0
        if this_start <= d <= this_end:
            this_t["sessions"] += s; this_t["users"] += u; this_t["pageviews"] += pv
        elif last_start <= d <= last_end:
            last_t["sessions"] += s; last_t["users"] += u; last_t["pageviews"] += pv

    return this_t, last_t


# ──────────────────────────────────────────────
# 语义聚类（含规则降级）
# ──────────────────────────────────────────────

STOPWORDS = {
    "how","what","why","guide","tutorial","tips","learn","best","top","review",
    "reviews","vs","alternative","alternatives","compare","buy","price","pricing",
    "cheap","coupon","download","purchase","free","online","a","an","the","for",
    "to","of","in","on","and","with",
}

MODIFIER_RULES = {
    "transactional": ["buy","price","pricing","cheap","coupon","download","purchase"],
    "commercial":    ["best","top","review","reviews","vs","alternative","alternatives","compare"],
    "informational": ["how","what","why","guide","tutorial","tips","learn"],
}


def normalize_query(text: str) -> str:
    text = (text or "").lower().strip()
    text = re.sub(r"[\u2018\u2019\u201c\u201d]", "", text)
    text = re.sub(r"[^\w\s\-]", " ", text)
    return re.sub(r"\s+", " ", text.replace("_", " ")).strip()


def get_topic_key(query: str) -> str:
    tokens = [t for t in normalize_query(query).split() if t not in STOPWORDS]
    return " ".join(tokens[:3]) if tokens else normalize_query(query)


def detect_intent(query: str) -> str:
    tokens = set(normalize_query(query).split())
    for intent, terms in MODIFIER_RULES.items():
        if any(t in tokens for t in terms):
            return intent
    return "mixed"


def calc_opportunity_score(impressions, avg_position, ctr, intent) -> float:
    w = {"transactional": 1.2, "commercial": 1.0, "informational": 0.8, "mixed": 0.7}.get(intent, 0.7)
    return round((
        math.log1p(max(impressions, 0)) * 0.45 +
        min(max(avg_position, 0), 20) * 0.35 +
        (1 - max(min(ctr, 1), 0)) * 10 * 0.20
    ) * w, 2)


def analyze_topic_clusters(query_rows: list) -> list:
    """语义聚类，优先 sentence-transformers + KMeans，不可用时规则降级。"""
    if not query_rows:
        return []

    # 归一化并聚合统计
    query_stats = {}
    for r in query_rows:
        raw = (r.get("query") or "").strip()
        if len(raw) <= 1:
            continue
        norm = normalize_query(raw)
        if not norm:
            continue
        if norm not in query_stats:
            query_stats[norm] = {"rep": raw, "impressions": 0, "clicks": 0,
                                  "sum_pos_x_imp": 0, "top_imp": -1}
        s = query_stats[norm]
        imp = r.get("impressions", 0) or 0
        s["impressions"] += imp
        s["clicks"]      += r.get("clicks", 0) or 0
        s["sum_pos_x_imp"] += (r.get("position", 0) or 0) * imp
        if imp > s["top_imp"]:
            s["top_imp"] = imp
            s["rep"] = raw

    unique_texts = list(query_stats.keys())
    if len(unique_texts) < 8:
        print("  ⚠️  查询词过少，跳过聚类")
        return []

    query_to_cluster = {}
    semantic_ok = False
    try:
        from sentence_transformers import SentenceTransformer
        from sklearn.cluster import KMeans
        cache_dir = os.environ.get("SENTENCE_TRANSFORMERS_HOME",
                                   os.path.expanduser("~/.cache/torch/sentence_transformers"))
        print(f"  ⏳ 向量化 {len(unique_texts)} 个查询词...")
        model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2", cache_folder=cache_dir)
        embeddings = model.encode(unique_texts, show_progress_bar=False)
        k = max(4, min(25, len(unique_texts) // 12))
        print(f"  ⏳ KMeans 聚类 K={k}...")
        km = KMeans(n_clusters=k, random_state=42, n_init=10)
        km.fit(embeddings)
        query_to_cluster = {t: int(lb) for t, lb in zip(unique_texts, km.labels_)}
        semantic_ok = True
    except Exception as e:
        print(f"  ⚠️  语义聚类不可用，降级到规则分组: {e}")
        for t in unique_texts:
            query_to_cluster[t] = get_topic_key(t)

    clusters_data = defaultdict(lambda: {"total_impressions": 0, "total_clicks": 0,
                                          "sum_pos_x_imp": 0, "records": []})
    for norm, stats in query_stats.items():
        cid = query_to_cluster[norm]
        c = clusters_data[cid]
        c["total_impressions"] += stats["impressions"]
        c["total_clicks"]      += stats["clicks"]
        c["sum_pos_x_imp"]     += stats["sum_pos_x_imp"]
        c["records"].append(stats)

    result = []
    for c in clusters_data.values():
        imp = c["total_impressions"]
        if imp <= 5:
            continue
        avg_pos = c["sum_pos_x_imp"] / imp if imp > 0 else 0
        ctr = c["total_clicks"] / imp if imp > 0 else 0
        top = sorted(c["records"], key=lambda x: x["impressions"], reverse=True)
        topic = top[0]["rep"]
        intent = detect_intent(topic)
        result.append({
            "topic": topic,
            "query_count": len(c["records"]),
            "impressions": imp,
            "avg_position": round(avg_pos, 1),
            "opportunity_score": calc_opportunity_score(imp, avg_pos, ctr, intent),
            "top_queries": [r["rep"] for r in top[:3]],
        })

    result.sort(key=lambda x: x["opportunity_score"], reverse=True)
    mode = "语义聚类" if semantic_ok else "规则降级分组"
    print(f"  📊 {mode}得到 {len(result)} 个主题")
    # 返回 (clusters, semantic_ok) 元组，让调用方决定如何在报告中标注
    return result, semantic_ok


# ──────────────────────────────────────────────
# 报告生成
# ──────────────────────────────────────────────

def generate_report(args) -> str:
    gsc_this = load_json(args.gsc_this)
    gsc_last = load_json(args.gsc_last)
    ga4_14d  = load_json(args.ga4_14d) if args.ga4_14d else {}
    domain   = args.domain
    today    = datetime.now().date()

    # 日期区间（GSC 有 3 天数据延迟）
    data_end        = today - timedelta(days=3)
    this_week_start = data_end - timedelta(days=6)
    last_week_end   = this_week_start - timedelta(days=1)
    last_week_start = last_week_end - timedelta(days=6)

    print(f"  本周: {this_week_start} → {data_end}")
    print(f"  上周: {last_week_start} → {last_week_end}")

    # GSC 解析
    this_rows = parse_gsc_rows(gsc_this)
    last_rows = parse_gsc_rows(gsc_last)
    this_total = parse_gsc_totals(gsc_this)
    last_total = parse_gsc_totals(gsc_last)

    this_kw_map   = {r["query"]: r for r in this_rows if r["query"]}
    last_kw_map   = {r["query"]: r for r in last_rows if r["query"]}
    this_page_map = {r["page"]: r  for r in this_rows if r["page"]}
    last_page_map = {r["page"]: r  for r in last_rows if r["page"]}

    # GA4 汇总
    ga4_this_t, ga4_last_t = {}, {}
    if ga4_14d:
        ga4_this_t, ga4_last_t = split_ga4_by_week(ga4_14d, this_week_start, data_end)

    # 关键词涨幅
    gainers = []
    for kw, d in this_kw_map.items():
        prev = last_kw_map.get(kw)
        if prev and d["clicks"] > prev["clicks"]:
            gainers.append({"query": kw, "clicks": d["clicks"], "prev_clicks": prev["clicks"],
                             "diff": d["clicks"] - prev["clicks"],
                             "position": round(d["position"], 1), "impressions": d["impressions"]})
    gainers.sort(key=lambda x: x["diff"], reverse=True)

    # 关键词跌幅
    losers = []
    for kw, d in last_kw_map.items():
        curr = this_kw_map.get(kw)
        if curr and d["clicks"] > curr["clicks"]:
            losers.append({"query": kw, "clicks": curr["clicks"], "prev_clicks": d["clicks"],
                            "diff": curr["clicks"] - d["clicks"],
                            "position": round(curr["position"], 1)})
        elif not curr and d["clicks"] >= 3:
            losers.append({"query": kw, "clicks": 0, "prev_clicks": d["clicks"],
                            "diff": -d["clicks"], "position": 0})
    losers.sort(key=lambda x: x["diff"])

    # CTR 快速优化机会
    quick_wins = [
        {"query": r["query"], "position": round(r["position"], 1),
         "impressions": r["impressions"], "clicks": r["clicks"], "ctr": r["ctr"]}
        for r in this_kw_map.values()
        if 4 <= r["position"] <= 20 and r["impressions"] >= 50 and r["ctr"] < 0.05
    ]
    quick_wins.sort(key=lambda x: x["impressions"], reverse=True)

    # 新增流量页面
    new_pages = sorted(
        [d for url, d in this_page_map.items() if url not in last_page_map and d["clicks"] >= 2],
        key=lambda x: x["clicks"], reverse=True
    )

    # 语义聚类（仅对 query 行）
    clusters, semantic_ok = analyze_topic_clusters(list(this_kw_map.values()))

    # ══ 组装 Markdown ══
    date_str = today.strftime("%Y-%m-%d")
    lines = [
        f"# 📊 SEO 周报 — {domain}",
        "",
        f"> **报告日期**: {date_str}",
        f"> **数据周期**: {this_week_start} → {data_end}  |  对比: {last_week_start} → {last_week_end}",
        "",
        "---",
        "",
        "## 1. 📈 流量总览",
        "",
        "| 指标 | 本周 | 上周 | 变化 |",
        "|---|---|---|---|",
        f"| 搜索点击 | {this_total['clicks']:,} | {last_total['clicks']:,} | {pct_change(this_total['clicks'], last_total['clicks'])} |",
        f"| 搜索曝光 | {this_total['impressions']:,} | {last_total['impressions']:,} | {pct_change(this_total['impressions'], last_total['impressions'])} |",
        f"| 平均 CTR | {fmt_ctr(this_total['ctr'])} | {fmt_ctr(last_total['ctr'])} | {pct_change(this_total['ctr']*100, last_total['ctr']*100)} |",
        f"| 平均排名 | {this_total['position']:.1f} | {last_total['position']:.1f} | — |",
        "",
    ]

    if ga4_this_t:
        lines += [
            "### GA4 有机流量（按日汇总）",
            "",
            "| 指标 | 本周 | 上周 | 变化 |",
            "|---|---|---|---|",
            f"| 会话数 | {ga4_this_t['sessions']:,} | {ga4_last_t.get('sessions',0):,} | {pct_change(ga4_this_t['sessions'], ga4_last_t.get('sessions',0))} |",
            f"| 用户数 | {ga4_this_t['users']:,} | {ga4_last_t.get('users',0):,} | {pct_change(ga4_this_t['users'], ga4_last_t.get('users',0))} |",
            f"| 页面浏览 | {ga4_this_t['pageviews']:,} | {ga4_last_t.get('pageviews',0):,} | {pct_change(ga4_this_t['pageviews'], ga4_last_t.get('pageviews',0))} |",
            "",
        ]

    lines += ["## 2. 🚀 关键词涨幅 Top 15", "",
              "| 关键词 | 本周点击 | 上周点击 | 变化 | 排名 | 曝光 |",
              "|---|---|---|---|---|---|"]
    for g in gainers[:15]:
        lines.append(f"| {g['query']} | {g['clicks']} | {g['prev_clicks']} | +{g['diff']} | {g['position']} | {g['impressions']} |")
    lines.append("")

    lines += ["## 3. ⚠️ 关键词跌幅 Top 10", "",
              "| 关键词 | 本周点击 | 上周点击 | 变化 | 排名 |",
              "|---|---|---|---|---|"]
    for lo in losers[:10]:
        lines.append(f"| {lo['query']} | {lo['clicks']} | {lo['prev_clicks']} | {lo['diff']} | {lo['position'] or 'N/A'} |")
    lines.append("")

    lines += ["## 4. 🎯 快速优化机会（排名 4-20，高曝光低 CTR）", "",
              "| 关键词 | 排名 | 曝光 | 点击 | CTR | 建议 |",
              "|---|---|---|---|---|---|"]
    for q in quick_wins[:20]:
        sug = "优化 Title + Meta" if q["position"] <= 10 else "提升内容质量冲首页"
        lines.append(f"| {q['query']} | {q['position']} | {q['impressions']} | {q['clicks']} | {fmt_ctr(q['ctr'])} | {sug} |")
    lines.append("")

    page_list = sorted(this_page_map.values(), key=lambda x: x["clicks"], reverse=True)[:15]
    lines += ["## 5. 📄 Top 15 页面", "",
              "| 页面 | 本周点击 | 上周点击 | 变化 | CTR | 排名 |",
              "|---|---|---|---|---|---|"]
    for p in page_list:
        url = p["page"]
        short = "/" + "/".join(url.split("/")[3:]) if "://" in url else url
        prev = last_page_map.get(url, {})
        prev_clicks = prev.get("clicks", 0)
        lines.append(f"| {short} | {p['clicks']} | {prev_clicks} | {pct_change(p['clicks'], prev_clicks)} | {fmt_ctr(p['ctr'])} | {p['position']:.1f} |")
    lines.append("")

    if new_pages:
        lines += ["## 6. 🆕 新增流量页面", "",
                  "| 页面 | 点击 | 曝光 | CTR |", "|---|---|---|---|"]
        for p in new_pages[:10]:
            url = p["page"]
            short = "/" + "/".join(url.split("/")[3:]) if "://" in url else url
            lines.append(f"| {short} | {p['clicks']} | {p['impressions']} | {fmt_ctr(p['ctr'])} |")
        lines.append("")

    if clusters:
        if semantic_ok:
            cluster_title = "## 7. 🧠 语义主题聚类分析（内容投资候选）"
            cluster_desc  = "高曝光但排名较低的潜在大流量主题簇（基于 sentence-transformers 向量语义聚类）："
        else:
            cluster_title = "## 7. 🧠 主题分组分析（内容投资候选）⚠️ 规则降级版"
            cluster_desc  = (
                "> ⚠️ **本次聚类使用规则降级分组**（sentence-transformers 模型不可用），"
                "分组基于关键词前缀/词根规则，精度低于语义模型，仅供参考。"
            )
        lines += [cluster_title, "",
                  cluster_desc, "",
                  "| 主题聚类 | 词变体数 | 总曝光 | 平均排名 | 示例词 |",
                  "|---|---|---|---|---|"]
        for c in clusters[:10]:
            examples = ", ".join(c["top_queries"][:3])
            lines.append(f"| {c['topic']} | {c['query_count']} | {c['impressions']} | {c['avg_position']} | {examples} |")
        lines.append("")

    lines += ["## 8. ✅ 本周 Action Items", ""]
    actions = []
    if quick_wins:
        qw = quick_wins[0]
        actions.append(f"- [ ] **优化 Title/Meta**: 「{qw['query']}」排名 {qw['position']}，曝光 {qw['impressions']} 但 CTR 仅 {fmt_ctr(qw['ctr'])}")
    if losers:
        lo = losers[0]
        actions.append(f"- [ ] **检查排名下降**: 「{lo['query']}」点击从 {lo['prev_clicks']} 降至 {lo['clicks']}")
    if gainers:
        g = gainers[0]
        actions.append(f"- [ ] **巩固增长词**: 「{g['query']}」涨幅 +{g['diff']} 点击，考虑加强内链和内容深度")
    actions += ["- [ ] 检查 Core Web Vitals 是否有变化",
                "- [ ] 检查 GSC 覆盖率报告是否有新的索引问题"]
    lines.extend(actions)
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="从工作流 JSON 生成 SEO Markdown 周报")
    parser.add_argument("--gsc-this", required=True, help="本周 GSC JSON 路径")
    parser.add_argument("--gsc-last", required=True, help="上周 GSC JSON 路径")
    parser.add_argument("--ga4-14d",  default=None,  help="GA4 最近 14 天 JSON 路径（可选）")
    parser.add_argument("--domain",   required=True, help="站点域名")
    parser.add_argument("--out",      required=True, help="输出 Markdown 文件路径")
    args = parser.parse_args()

    print(f"\n🚀 生成 SEO Markdown 周报: {args.domain}")
    content = generate_report(args)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"\n✅ 周报已生成: {out_path}")


if __name__ == "__main__":
    main()
