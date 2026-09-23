#!/usr/bin/env python3
"""
SEO 周报生成器（工作流版）
从工作流已产出的 JSON 文件生成 Markdown 周报，无任何认证依赖。

数据输入:
  --gsc-this  : 本周 GSC JSON（query+page 双维度 + totals）
  --gsc-last  : 上周 GSC JSON（格式相同）
  --ga4-14d   : GA4 最近 14 天 daily_data（--days 14 拉取，脚本内按日期分割本周/上周）
  --trends    : trend_scout.py 输出的 JSON（可选，生成趋势信号 Section）
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


def aggregate_by_key(rows: list, key_field: str, value_field: str) -> dict:
    """按 key_field 聚合数据，避免多维度时数据相互覆盖。同时记录曝光最高的 value_field。"""
    agg = {}
    for r in rows:
        k = r.get(key_field)
        if not k:
            continue
        if k not in agg:
            agg[k] = {
                key_field: k,
                value_field: r.get(value_field, ""),
                "clicks": 0,
                "impressions": 0,
                "sum_pos_x_imp": 0.0,
                "top_imp": -1
            }
        item = agg[k]
        imp = r["impressions"]
        item["clicks"] += r["clicks"]
        item["impressions"] += imp
        item["sum_pos_x_imp"] += r["position"] * imp
        if imp > item["top_imp"]:
            item["top_imp"] = imp
            item[value_field] = r.get(value_field, "")
            
    # 计算加权平均 position 和总 ctr
    for item in agg.values():
        imp = item["impressions"]
        item["ctr"] = item["clicks"] / imp if imp > 0 else 0
        item["position"] = item["sum_pos_x_imp"] / imp if imp > 0 else 0
        
    return agg


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


def analyze_topic_clusters(query_rows: list) -> tuple:
    """语义聚类，优先 sentence-transformers + KMeans，不可用时规则降级。"""
    if not query_rows:
        return [], False

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

    this_kw_map   = aggregate_by_key(this_rows, "query", "page")
    last_kw_map   = aggregate_by_key(last_rows, "query", "page")
    this_page_map = aggregate_by_key(this_rows, "page", "query")
    last_page_map = aggregate_by_key(last_rows, "page", "query")

    # GA4 汇总
    ga4_this_t, ga4_last_t = {}, {}
    if ga4_14d:
        ga4_this_t, ga4_last_t = split_ga4_by_week(ga4_14d, this_week_start, data_end)

    # 关键词涨幅
    gainers = []
    for kw, d in this_kw_map.items():
        prev = last_kw_map.get(kw)
        prev_clicks = prev["clicks"] if prev else 0
        diff = d["clicks"] - prev_clicks
        if diff > 0:
            gainers.append({"query": kw, "clicks": d["clicks"], "prev_clicks": prev_clicks,
                             "diff": diff,
                             "position": round(d["position"], 1), "impressions": d["impressions"]})
    gainers.sort(key=lambda x: x["diff"], reverse=True)

    # 关键词跌幅
    losers = []
    for kw, d in last_kw_map.items():
        curr = this_kw_map.get(kw)
        curr_clicks = curr["clicks"] if curr else 0
        diff = curr_clicks - d["clicks"]
        if diff < 0:
            pos = round(curr["position"], 1) if curr else 0
            losers.append({"query": kw, "clicks": curr_clicks, "prev_clicks": d["clicks"],
                            "diff": diff, "position": pos})
    losers.sort(key=lambda x: x["diff"])

    # CTR 快速优化机会（排名 4-20，高曝光低 CTR）
    quick_wins = []
    for r in this_kw_map.values():
        if 4 <= r["position"] <= 20 and r["impressions"] >= 50 and r["ctr"] < 0.05:
            prev = last_kw_map.get(r["query"])
            prev_pos = prev["position"] if prev else None
            pos = r["position"]
            if prev_pos:
                diff = prev_pos - pos
                if diff > 0:
                    pos_str = f"{pos:.1f} (↑ {diff:.1f})"
                elif diff < 0:
                    pos_str = f"{pos:.1f} (↓ {-diff:.1f})"
                else:
                    pos_str = f"{pos:.1f} (-)"
            else:
                pos_str = f"{pos:.1f} (新)"
            
            quick_wins.append({
                "query": r["query"], 
                "position": round(pos, 1),
                "pos_str": pos_str,
                "impressions": r["impressions"], 
                "clicks": r["clicks"], 
                "ctr": r["ctr"],
                "page": r.get("page", "")
            })
    quick_wins.sort(key=lambda x: x["impressions"], reverse=True)

    # CTR 异常词：排名 1-3 但 CTR 低于该位置期望均值
    # 期望 CTR 参考：P1≈0.28, P2≈0.15, P3≈0.11
    EXPECTED_CTR = {1: 0.28, 2: 0.15, 3: 0.11}
    ctr_anomalies = []
    for r in this_kw_map.values():
        pos = r["position"]
        imp = r["impressions"]
        ctr = r["ctr"]
        if pos < 1 or imp < 30:
            continue
        pos_floor = max(1, min(3, int(pos)))
        expected = EXPECTED_CTR.get(pos_floor)
        if expected and ctr < expected * 0.6 and imp >= 30:
            # impact_score = 潜在可增量点击数
            impact = round(imp * (expected - ctr), 1)
            
            prev = last_kw_map.get(r["query"])
            prev_pos = prev["position"] if prev else None
            if prev_pos:
                diff = prev_pos - pos
                if diff > 0:
                    pos_str = f"{pos:.1f} (↑ {diff:.1f})"
                elif diff < 0:
                    pos_str = f"{pos:.1f} (↓ {-diff:.1f})"
                else:
                    pos_str = f"{pos:.1f} (-)"
            else:
                pos_str = f"{pos:.1f} (新)"

            ctr_anomalies.append({
                "query": r["query"],
                "position": round(pos, 1),
                "pos_str": pos_str,
                "impressions": imp,
                "ctr": ctr,
                "expected_ctr": expected,
                "impact_score": impact,
                "page": r.get("page", "")
            })
    ctr_anomalies.sort(key=lambda x: x["impact_score"], reverse=True)

    # 新增流量页面
    new_pages = sorted(
        [d for url, d in this_page_map.items() if url not in last_page_map and d["clicks"] >= 2],
        key=lambda x: x["clicks"], reverse=True
    )

    # 趋势数据（可选）
    trends_angles: list = []
    trends_raw: dict = {}
    if getattr(args, "trends", None) and args.trends:
        try:
            trends_json = load_json(args.trends)
            trends_angles = trends_json.get("angles", [])
            trends_raw = trends_json.get("trends", {})
            print(f"  🔥 已加载趋势数据: {len(trends_angles)} 个内容机会")
        except Exception as e:
            print(f"  ⚠️  趋势数据加载失败: {e}")

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
              "| 关键词 (带链接) | 排名 (变化) | 曝光 | 点击 | CTR | 建议 |",
              "|---|---|---|---|---|---|"]
    for q in quick_wins[:20]:
        sug = "优化 Title + Meta" if q["position"] <= 10 else "提升内容质量冲首页"
        kw_display = f"[{q['query']}]({q['page']})" if q.get("page") else q['query']
        lines.append(f"| {kw_display} | {q['pos_str']} | {q['impressions']} | {q['clicks']} | {fmt_ctr(q['ctr'])} | {sug} |")
    lines.append("")

    # Section 4b：CTR 异常词（排名好但没人点）
    if ctr_anomalies:
        lines += ["## 4b. 🚨 CTR 异常词（排名靠前但点击率偏低）", "",
                  "> 这些词的排名已进入前 3，但 CTR 远低于同位置均值，优化 Title/描述可大幅提升点击量。",
                  "",
                  "| 关键词 (带链接) | 排名 (变化) | 曝光 | 实际 CTR | 期望 CTR | 潜在增量点击 | 建议 |",
                  "|---|---|---|---|---|---|---|"]
        for a in ctr_anomalies[:15]:
            kw_display = f"[{a['query']}]({a['page']})" if a.get("page") else a['query']
            lines.append(
                f"| {kw_display} | {a['pos_str']} | {a['impressions']} "
                f"| {fmt_ctr(a['ctr'])} | {fmt_ctr(a['expected_ctr'])} "
                f"| +{a['impact_score']:.0f} 次/周 | 重写 Title/Meta Description |"
            )
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
    if ctr_anomalies:
        ca = ctr_anomalies[0]
        ca_display = f"[{ca['query']}]({ca['page']})" if ca.get("page") else f"「{ca['query']}」"
        actions.append(
            f"- [ ] **🚨 优先修复 CTR 异常**: {ca_display} 排名 {ca['position']}，"
            f"实际 CTR {fmt_ctr(ca['ctr'])} vs 期望 {fmt_ctr(ca['expected_ctr'])}，"
            f"潜在增量 +{ca['impact_score']:.0f} 点击/周 → 重写 Title + Meta"
        )
    if quick_wins:
        qw = quick_wins[0]
        qw_display = f"[{qw['query']}]({qw['page']})" if qw.get("page") else f"「{qw['query']}」"
        actions.append(f"- [ ] **优化 Title/Meta**: {qw_display} 排名 {qw['position']}，曝光 {qw['impressions']} 但 CTR 仅 {fmt_ctr(qw['ctr'])}")
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

    # Section 9：趋势信号（来自 trend_scout.py）
    if trends_angles:
        lines += ["## 9. 🔥 本周趋势信号（内容方向建议）", "",
                  "> 数据来源：Google Trends / Hacker News / Reddit",
                  "",
                  "| 主题 | 来源 | 相关度 | 建议动作 |",
                  "|---|---|---|---|"]
        for a in trends_angles[:8]:
            topic_short = a["topic"][:60] + ("…" if len(a["topic"]) > 60 else "")
            score_label = "🔴 高" if a["relevance_score"] >= 50 else "🟡 中" if a["relevance_score"] >= 25 else "🟢 低"
            angle = a.get("angle", "-")[:40]
            lines.append(f"| {topic_short} | {a['source']} | {score_label} | {angle} |")

        gt = trends_raw.get("google_trends", [])
        if gt:
            lines.append("")
            lines.append("**📡 Google 热搜榜 Top 8（US）：**")
            for t in gt[:8]:
                lines.append(f"- {t['topic']} ({t.get('traffic', 'N/A')})")
        lines.append("")

    # Section 10：Bing 搜索数据（可选）
    bing_dir = getattr(args, "bing_dir", None)
    if bing_dir:
        from pathlib import Path as _Path
        import json as _json
        queries_file = _Path(bing_dir) / "queries.json"
        if queries_file.exists():
            try:
                bing_data = _json.loads(queries_file.read_text(encoding="utf-8"))
                this_week = bing_data.get("this_week", [])
                prev_week = bing_data.get("prev_week", [])
                week_date = bing_data.get("week_date", "")
                prev_week_date = bing_data.get("prev_week_date", "")

                if this_week:
                    total_clicks = sum(r["clicks"] for r in this_week)
                    total_impr   = sum(r["impressions"] for r in this_week)
                    avg_ctr      = total_clicks / total_impr if total_impr > 0 else 0
                    w_pos_sum    = sum(r["avg_position"] * r["impressions"] for r in this_week if r["avg_position"] > 0)
                    w_pos_impr   = sum(r["impressions"] for r in this_week if r["avg_position"] > 0)
                    avg_pos      = round(w_pos_sum / w_pos_impr, 1) if w_pos_impr > 0 else 0

                    prev_clicks  = sum(r["clicks"] for r in prev_week) if prev_week else 0
                    prev_impr    = sum(r["impressions"] for r in prev_week) if prev_week else 0

                    # 周标题
                    section_num = "10" if trends_angles else "9"
                    lines += [f"## {section_num}. 🔵 Bing 搜索数据（{week_date} 周快照）", ""]

                    # 总览表
                    lines += ["### 流量总览", "",
                              "| 指标 | 本周 | 上周 | 变化 |",
                              "|---|---|---|---|"]
                    click_chg = pct_change(total_clicks, prev_clicks) if prev_clicks else "—"
                    impr_chg  = pct_change(total_impr, prev_impr) if prev_impr else "—"
                    lines.append(f"| 点击数（Top 100 词合计）| {total_clicks:,} | {prev_clicks:,} | {click_chg} |")
                    lines.append(f"| 曝光量 | {total_impr:,} | {prev_impr:,} | {impr_chg} |")
                    lines.append(f"| 平均 CTR | {avg_ctr:.1%} | — | — |")
                    lines.append(f"| 加权平均排名 | {avg_pos} | — | — |")
                    lines.append("")

                    # Top 10 关键词
                    lines += ["### Top 10 关键词（按点击）", "",
                              "| 关键词 | 点击 | 曝光 | CTR | 排名 |",
                              "|---|---|---|---|---|"]
                    for row in this_week[:10]:
                        ctr_str = f"{row['ctr']:.1%}"
                        pos_str = str(row["avg_position"]) if row["avg_position"] > 0 else "—"
                        lines.append(f"| {row['query']} | {row['clicks']:,} | {row['impressions']:,} | {ctr_str} | {pos_str} |")
                    lines.append("")

                    # CTR 优化机会
                    opps = [r for r in this_week if r["impressions"] >= 100 and r["ctr"] < 0.03]
                    opps.sort(key=lambda x: x["impressions"], reverse=True)
                    if opps:
                        lines += ["### ⚡ CTR 优化机会（曝光≥100，CTR<3%）", "",
                                  "> 这些关键词已有大量曝光但点击率极低，优化标题/Meta 描述可直接提升 Bing 流量。", "",
                                  "| 关键词 | 曝光 | CTR | 排名 | 建议 |",
                                  "|---|---|---|---|---|"]
                        for row in opps[:8]:
                            pos_str = str(row["avg_position"]) if row["avg_position"] > 0 else "—"
                            lines.append(f"| {row['query']} | {row['impressions']:,} | {row['ctr']:.1%} | {pos_str} | 重写 Title/Meta |")
                        lines.append("")
            except Exception as e:
                lines.append(f"> ⚠️ Bing 数据加载失败: {e}")
                lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="从工作流 JSON 生成 SEO Markdown 周报")
    parser.add_argument("--gsc-this", required=True, help="本周 GSC JSON 路径")
    parser.add_argument("--gsc-last", required=True, help="上周 GSC JSON 路径")
    parser.add_argument("--ga4-14d",  default=None,  help="GA4 最近 14 天 JSON 路径（可选）")
    parser.add_argument("--trends",   default=None,  help="trend_scout.py 输出的 JSON 路径（可选）")
    parser.add_argument("--domain",   required=True, help="站点域名")
    parser.add_argument("--out",      required=True, help="输出 Markdown 文件路径")
    parser.add_argument("--bing-dir", default=None,  help="Bing 数据目录（可选），由 fetch_bing.py 生成")
    args = parser.parse_args()

    print(f"\n🚀 生成 SEO Markdown 周报: {args.domain}")
    content = generate_report(args)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"\n✅ 周报已生成: {out_path}")


if __name__ == "__main__":
    main()
