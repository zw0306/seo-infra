#!/usr/bin/env python3
"""
Trend Scout — SEO niche trend detection (adapted from ericosiu/ai-marketing-skills/seo-ops)

Free data sources:
  - Google Trends RSS (US)
  - Hacker News Algolia API
  - Reddit JSON API
  - Brave Search (X/Twitter proxy, optional)

Usage:
    python trend_scout.py --niche "piano-sheets.org" --out /tmp/trends.json --telegram /tmp/trends.txt

    # 精准关键词（来自 SEO_NICHE_KEYWORDS 环境变量或 --keywords 参数）
    python trend_scout.py --keywords "piano sheet music,sheet music pdf,free piano sheets" \\
                          --out /tmp/trends.json --telegram /tmp/trends.txt

Environment variables:
    SEO_NICHE_KEYWORDS   — 逗号分隔的关键词（优先于 --niche 推断）
    TREND_SUBREDDITS     — 逗号分隔的 subreddit（可选覆盖默认）
    BRAVE_API_KEY        — Brave Search API key（可选，用于 X/Twitter 扫描）
    HIGH_RELEVANCE_KEYWORDS_JSON — JSON 数组，覆盖高相关性关键词列表

Outputs:
    --out       JSON 文件（trends_data + angles，供 Markdown 周报消费）
    --telegram  Telegram 消息文本（仅包含高分信号摘要）
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path


# ─────────────────────────────────────────────
# 关键词推断
# ─────────────────────────────────────────────

def infer_keywords_from_niche(niche: str) -> list[str]:
    """从 domain 或站点名推断关键词。例如 piano-sheets.org → ['piano sheets', 'piano sheet music']"""
    # 去掉 TLD
    base = re.sub(r"\.[a-z]{2,6}$", "", niche.lower().strip())
    # 连字符/下划线转空格
    base = re.sub(r"[-_]", " ", base).strip()
    # 生成派生关键词
    keywords = [base]
    words = base.split()
    if len(words) >= 2:
        keywords.append(f"{base} music")
        keywords.append(f"free {base}")
        keywords.append(f"{base} pdf")
    return list(dict.fromkeys(keywords))  # 去重保序


def build_verticals(keywords: list[str]) -> list[str]:
    """基于关键词列表生成 verticals（用于 Trend Scout 评分说明）"""
    verticals = keywords[:]
    # 通用 SEO 维度
    verticals += ["SEO trends", "Google algorithm", "search optimization"]
    return verticals[:10]


# ─────────────────────────────────────────────
# 相关性评分
# ─────────────────────────────────────────────

def build_relevance_sets(keywords: list[str]) -> tuple[list[str], list[str], list[str]]:
    """从关键词列表构建高/中/低相关性词集合。"""
    # 高相关：精确关键词词元
    high = []
    for kw in keywords:
        high.extend(kw.lower().split())
    high = list(set(high))

    # 中相关：通用 SEO/内容营销词
    medium = [
        "ai", "google", "search", "music", "free", "download",
        "tutorial", "guide", "beginner", "learn", "practice",
        "seo", "content", "digital", "online",
    ]

    # 低相关：泛科技词
    low = ["tech", "software", "platform", "tool", "app", "web"]

    return high, medium, low


def score_trend(title: str, high: list[str], medium: list[str], low: list[str]) -> int:
    """对趋势标题打相关性分数（0-100）。"""
    title_lower = title.lower()
    score = 0
    for kw in high:
        if kw and kw in title_lower:
            score += 25
    for kw in medium:
        if kw and kw in title_lower:
            score += 8
    for kw in low:
        if kw and kw in title_lower:
            score += 3
    return min(score, 100)


# ─────────────────────────────────────────────
# 数据源：Google Trends
# ─────────────────────────────────────────────

def get_google_trends(geo: str = "US") -> list[dict]:
    """拉取 Google Trends RSS 热搜榜。"""
    url = f"https://trends.google.com/trending/rss?geo={geo}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
        root = ET.fromstring(data)
        ns = {"ht": "https://trends.google.com/trending/rss"}
        trends = []
        for item in root.findall(".//item")[:20]:
            title_el = item.find("title")
            traffic_el = item.find("ht:approx_traffic", ns)
            title = title_el.text if title_el is not None else ""
            traffic = traffic_el.text if traffic_el is not None else "N/A"
            if title:
                trends.append({"topic": title, "traffic": traffic, "source": "Google Trends"})
        return trends
    except Exception as e:
        print(f"  ⚠️  Google Trends 拉取失败: {e}")
        return []


# ─────────────────────────────────────────────
# 数据源：Hacker News
# ─────────────────────────────────────────────

def get_hackernews_top(hours_back: int = 48, min_score: int = 50) -> list[dict]:
    """从 HN Algolia API 拉取高分故事（免费，无需 Key）。"""
    cutoff_ts = int((datetime.now().timestamp()) - hours_back * 3600)
    url = (
        "https://hn.algolia.com/api/v1/search?"
        f"tags=story&numericFilters=created_at_i>{cutoff_ts},points>{min_score}"
        "&hitsPerPage=30"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        stories = []
        for hit in data.get("hits", []):
            title = hit.get("title", "")
            if title:
                stories.append({
                    "title": title,
                    "url": hit.get("url", f"https://news.ycombinator.com/item?id={hit.get('objectID','')}"),
                    "score": hit.get("points", 0),
                    "comments": hit.get("num_comments", 0),
                    "source": "Hacker News",
                })
        stories.sort(key=lambda x: x["score"], reverse=True)
        return stories[:20]
    except Exception as e:
        print(f"  ⚠️  Hacker News 拉取失败: {e}")
        return []


# ─────────────────────────────────────────────
# 数据源：Reddit
# ─────────────────────────────────────────────

def get_reddit_trending(subreddits: list[str] | None = None) -> list[dict]:
    """从 Reddit JSON API 拉取热帖（免费，无需 Key）。"""
    default_subs = ["music", "learnmusic", "piano", "musictheory", "SEO", "entrepreneur"]
    subs_env = os.environ.get("TREND_SUBREDDITS", "")
    subs = ([s.strip() for s in subs_env.split(",") if s.strip()]
            or subreddits or default_subs)

    posts = []
    headers = {"User-Agent": "trend-scout/1.0 (+https://github.com/zw0306/seo-infra)"}
    for sub in subs[:6]:
        url = f"https://www.reddit.com/r/{sub}/hot.json?limit=10"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            for child in data.get("data", {}).get("children", [])[:5]:
                post = child.get("data", {})
                title = post.get("title", "")
                if title and post.get("score", 0) > 20:
                    posts.append({
                        "title": title,
                        "subreddit": sub,
                        "score": post.get("score", 0),
                        "comments": post.get("num_comments", 0),
                        "url": f"https://reddit.com{post.get('permalink', '')}",
                        "source": f"Reddit r/{sub}",
                    })
        except Exception as e:
            print(f"  ⚠️  Reddit r/{sub} 拉取失败: {e}")
            continue

    posts.sort(key=lambda x: x["score"], reverse=True)
    return posts[:20]


# ─────────────────────────────────────────────
# 数据源：X/Twitter（Brave Search 代理，可选）
# ─────────────────────────────────────────────

def get_x_twitter_trending(keywords: list[str]) -> list[dict]:
    """通过 Brave Search API 代理查询 X/Twitter 热议（需要 BRAVE_API_KEY）。"""
    api_key = os.environ.get("BRAVE_API_KEY", "")
    if not api_key:
        return []

    posts = []
    for kw in keywords[:3]:
        query = f"site:twitter.com OR site:x.com {kw}"
        url = (
            "https://api.search.brave.com/res/v1/web/search?"
            f"q={urllib.parse.quote(query)}&count=5&freshness=pd"
        )
        try:
            req = urllib.request.Request(url, headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
            for result in data.get("web", {}).get("results", [])[:3]:
                title = result.get("title", "")
                if title:
                    posts.append({
                        "title": title,
                        "url": result.get("url", ""),
                        "description": result.get("description", "")[:200],
                        "source": "X/Twitter",
                        "query": kw,
                    })
        except Exception as e:
            print(f"  ⚠️  X search for '{kw}' 失败: {e}")
            continue

    return posts[:10]


# ─────────────────────────────────────────────
# 内容角度生成
# ─────────────────────────────────────────────

def generate_content_angles(
    trends_data: dict,
    high: list[str],
    medium: list[str],
    low: list[str],
    min_score: int = 10,
) -> list[dict]:
    """基于趋势数据生成内容机会列表，按相关性排序。"""
    angles = []

    for trend in trends_data.get("google_trends", [])[:8]:
        rel = score_trend(trend["topic"], high, medium, low)
        if rel >= min_score:
            angles.append({
                "source": "Google Trends",
                "topic": trend["topic"],
                "traffic": trend.get("traffic", "N/A"),
                "relevance_score": rel,
                "angle": f"结合「{trend['topic']}」热度，发布相关内容",
                "platforms": ["博客文章", "社交媒体"],
            })

    for story in trends_data.get("hackernews", [])[:10]:
        rel = score_trend(story["title"], high, medium, low)
        if rel >= min_score:
            angles.append({
                "source": "Hacker News",
                "topic": story["title"],
                "score": story.get("score", 0),
                "relevance_score": rel,
                "url": story.get("url", ""),
                "angle": f"从专业视角回应此话题",
                "platforms": ["长文博客", "YouTube"],
            })

    for post in trends_data.get("reddit", [])[:10]:
        rel = score_trend(post["title"], high, medium, low)
        if rel >= min_score:
            angles.append({
                "source": f"Reddit r/{post['subreddit']}",
                "topic": post["title"],
                "engagement": f"{post['score']}↑ {post['comments']}评论",
                "relevance_score": rel,
                "url": post.get("url", ""),
                "angle": "回应社区问题，展示专业权威",
                "platforms": ["博客", "社交媒体"],
            })

    for post in trends_data.get("x_twitter", [])[:5]:
        rel = score_trend(post["title"], high, medium, low)
        if rel >= min_score:
            angles.append({
                "source": "X/Twitter",
                "topic": post["title"][:100],
                "relevance_score": rel,
                "url": post.get("url", ""),
                "angle": "加入热议，植入品牌观点",
                "platforms": ["X/Twitter", "LinkedIn"],
            })

    angles.sort(key=lambda x: x.get("relevance_score", 0), reverse=True)
    return angles[:10]


# ─────────────────────────────────────────────
# 输出格式化
# ─────────────────────────────────────────────

def format_telegram(angles: list[dict], trends_data: dict, domain: str) -> str:
    """生成 Telegram 消息摘要（短文本，适合卡片）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        f"🔥 *{domain} 趋势信号*",
        f"📅 {today}",
        "─────────────────────",
    ]

    if angles:
        lines.append("*🎯 本周内容机会 Top 5*")
        for i, a in enumerate(angles[:5], 1):
            score_emoji = "🔴" if a["relevance_score"] >= 50 else "🟡" if a["relevance_score"] >= 25 else "🟢"
            topic_short = a["topic"][:60] + ("…" if len(a["topic"]) > 60 else "")
            lines.append(f"{i}. {score_emoji} 「{topic_short}」")
            lines.append(f"   来源: {a['source']} | 相关度: {a['relevance_score']}/100")
    else:
        lines.append("本周暂无高相关性趋势信号")

    gt = trends_data.get("google_trends", [])
    if gt:
        lines.append("")
        lines.append("*📡 Google 热搜（前5）*")
        for t in gt[:5]:
            lines.append(f"· {t['topic']} ({t['traffic']})")

    return "\n".join(lines)


# ─────────────────────────────────────────────
# 主程序
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SEO niche trend scout")
    parser.add_argument("--niche", default="", help="站点域名，用于自动推断关键词（如 piano-sheets.org）")
    parser.add_argument("--keywords", default="", help="精准关键词，逗号分隔（优先于 --niche 推断）")
    parser.add_argument("--out", default="trends.json", help="JSON 输出路径")
    parser.add_argument("--telegram", default="trends.txt", help="Telegram 文本输出路径")
    parser.add_argument("--geo", default="US", help="Google Trends 地区（默认 US）")
    parser.add_argument("--min-score", type=int, default=10, help="最低相关性分数阈值（默认 10）")
    args = parser.parse_args()

    # ── 确定关键词 ──
    # 优先级：--keywords > 环境变量 SEO_NICHE_KEYWORDS > --niche 推断
    kw_from_env = os.environ.get("SEO_NICHE_KEYWORDS", "")
    kw_cli = args.keywords.strip()

    if kw_cli:
        keywords = [k.strip() for k in kw_cli.split(",") if k.strip()]
        print(f"🔑 使用 --keywords 参数: {keywords}")
    elif kw_from_env:
        keywords = [k.strip() for k in kw_from_env.split(",") if k.strip()]
        print(f"🔑 使用 SEO_NICHE_KEYWORDS 环境变量: {keywords}")
    elif args.niche:
        keywords = infer_keywords_from_niche(args.niche)
        print(f"🔑 从 --niche '{args.niche}' 推断关键词: {keywords}")
    else:
        print("⚠️  未指定关键词，使用通用 SEO 关键词", file=sys.stderr)
        keywords = ["SEO", "content marketing", "search optimization"]

    domain = args.niche or keywords[0]
    verticals = build_verticals(keywords)
    high, medium, low = build_relevance_sets(keywords)

    # 允许通过环境变量覆盖高相关词列表
    high_env = os.environ.get("HIGH_RELEVANCE_KEYWORDS_JSON", "")
    if high_env:
        try:
            high = json.loads(high_env)
        except json.JSONDecodeError:
            pass

    print(f"🔥 Trend Scout 启动 — {domain}")
    print(f"   关键词: {', '.join(keywords[:5])}")
    print(f"   Google Trends 地区: {args.geo}")
    print()

    # ── 拉取数据 ──
    print("  📡 拉取 Google Trends...")
    google_trends = get_google_trends(geo=args.geo)

    print("  📡 拉取 Hacker News...")
    hackernews = get_hackernews_top()

    print("  📡 拉取 Reddit...")
    reddit = get_reddit_trending()

    print("  📡 拉取 X/Twitter（需要 BRAVE_API_KEY）...")
    x_twitter = get_x_twitter_trending(keywords)

    trends_data = {
        "timestamp": datetime.now().isoformat(),
        "domain": domain,
        "keywords": keywords,
        "verticals": verticals,
        "google_trends": google_trends,
        "hackernews": hackernews,
        "reddit": reddit,
        "x_twitter": x_twitter,
    }

    # ── 生成内容角度 ──
    print("  🧠 生成内容机会...")
    angles = generate_content_angles(trends_data, high, medium, low, min_score=args.min_score)

    # ── 写出 JSON ──
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"angles": angles, "trends": trends_data}, f, ensure_ascii=False, indent=2)
    print(f"  💾 JSON 已写入: {out_path}")

    # ── 写出 Telegram 文本 ──
    tg_path = Path(args.telegram)
    tg_path.parent.mkdir(parents=True, exist_ok=True)
    tg_text = format_telegram(angles, trends_data, domain)
    with open(tg_path, "w", encoding="utf-8") as f:
        f.write(tg_text)
    print(f"  📱 Telegram 文本已写入: {tg_path}")

    # ── 摘要输出 ──
    print(f"\n✅ Trend Scout 完成:")
    print(f"  - Google Trends: {len(google_trends)} 条")
    print(f"  - Hacker News: {len(hackernews)} 条")
    print(f"  - Reddit: {len(reddit)} 条")
    print(f"  - X/Twitter: {len(x_twitter)} 条")
    print(f"  - 内容机会: {len(angles)} 个")

    if angles:
        print(f"\n🎯 Top 3 机会:")
        for i, a in enumerate(angles[:3], 1):
            print(f"  {i}. [{a['relevance_score']}/100] {a['topic'][:70]} ({a['source']})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
