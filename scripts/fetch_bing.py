"""
Bing Webmaster Tools 数据拉取器（seo-infra 版）
- 使用环境变量 BING_API_KEY 认证（无需 OAuth，不会过期）
- 无 auth.py 依赖，直接读取环境变量

接口覆盖:
  1. GetQueryStats           — 关键词查询统计（点击/曝光/CTR），按月返回
  2. GetPageStats            — Top 页面流量统计（批量，官方推荐的页面维度接口）
  3. GetRankAndTrafficStats  — 每日整站流量趋势
  4. GetCrawlIssues          — 具体抓取错误 URL 列表

用法:
  python3 fetch_bing.py --domain example.com --days 30 --out-dir /tmp/bing
  BING_API_KEY=xxx python3 fetch_bing.py --domain example.com
"""
import json
import sys
import os
import time
import argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path


# Bing Webmaster API 基础 URL
BING_API_BASE = "https://ssl.bing.com/webmaster/api.svc/json"


def get_bing_api_key() -> str:
    """从环境变量获取 Bing API Key"""
    api_key = os.environ.get("BING_API_KEY", "")
    if not api_key:
        print("❌ 未设置环境变量 BING_API_KEY")
        print("   获取方式: Bing Webmaster Tools → Settings → API Access → Generate Key")
        sys.exit(1)
    return api_key


def bing_get(endpoint: str, params: dict, api_key: str, retries: int = 3) -> dict | None:
    """统一的 Bing API GET 请求，含重试逻辑"""
    params["apikey"] = api_key
    url = f"{BING_API_BASE}/{endpoint}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            else:
                print(f"   ⚠️ HTTP {resp.status_code}: {resp.text[:200]}")
                if attempt < retries:
                    time.sleep(2 * attempt)
        except requests.RequestException as e:
            print(f"   ⚠️ 请求异常 (第{attempt}次): {e}")
            if attempt < retries:
                time.sleep(2 * attempt)
    return None


def fetch_query_stats(site_url: str, api_key: str, days: int) -> list[dict]:
    """
    拉取关键词查询统计 (GetQueryStats)
    Bing API 按月返回，需按月迭代请求后合并同一关键词跨月数据。
    返回: [{ query, clicks, impressions, ctr, avg_position }, ...]
    """
    raw = []
    end_date = datetime.now().date() - timedelta(days=2)
    start_date = end_date - timedelta(days=days)

    current = start_date.replace(day=1)
    while current <= end_date:
        month_str = current.strftime("%Y-%m-%d")
        data = bing_get("GetQueryStats", {
            "siteUrl": site_url,
            "startDate": month_str,
            "additionalMonth": 0,
        }, api_key)

        if data and "d" in data:
            rows = data["d"]
            if isinstance(rows, list):
                for row in rows:
                    query = row.get("Query", "")
                    if not query:
                        continue
                    raw.append({
                        "query": query,
                        "clicks": row.get("Clicks", 0),
                        "impressions": row.get("Impressions", 0),
                        "avg_position": round(row.get("AvgClickPosition", 0), 1),
                        "month": month_str[:7],
                    })

        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

        time.sleep(0.3)

    # 合并同一关键词跨月数据
    aggregated: dict[str, dict] = {}
    for row in raw:
        q = row["query"]
        if q not in aggregated:
            aggregated[q] = {"query": q, "clicks": 0, "impressions": 0, "positions": []}
        aggregated[q]["clicks"] += row["clicks"]
        aggregated[q]["impressions"] += row["impressions"]
        if row["avg_position"] > 0:
            aggregated[q]["positions"].append(row["avg_position"])

    result = []
    for q_data in aggregated.values():
        positions = q_data.pop("positions")
        q_data["avg_position"] = round(sum(positions) / len(positions), 1) if positions else 0
        q_data["ctr"] = round(
            q_data["clicks"] / q_data["impressions"], 4
        ) if q_data["impressions"] > 0 else 0
        result.append(q_data)

    result.sort(key=lambda x: x["clicks"], reverse=True)
    return result


def fetch_page_stats(site_url: str, api_key: str) -> list[dict]:
    """
    拉取 Top 页面流量统计 (GetPageStats)
    官方接口：批量返回整站 Top 页面数据，无需逐页请求。
    返回: [{ page, clicks, impressions, ctr, avg_position }, ...]
    """
    data = bing_get("GetPageStats", {"siteUrl": site_url}, api_key)

    results = []
    if data and "d" in data:
        rows = data["d"]
        if isinstance(rows, list):
            for row in rows:
                page = row.get("Url", "") or row.get("Page", "")
                if not page:
                    continue
                clicks = row.get("Clicks", 0)
                impressions = row.get("Impressions", 0)
                results.append({
                    "page": page,
                    "clicks": clicks,
                    "impressions": impressions,
                    "ctr": round(clicks / impressions, 4) if impressions > 0 else 0,
                    "avg_position": round(row.get("AvgClickPosition", 0), 1),
                })

    results.sort(key=lambda x: x["clicks"], reverse=True)
    return results


def fetch_rank_traffic_stats(site_url: str, api_key: str, days: int) -> list[dict]:
    """
    拉取网站整体每日排名与流量趋势 (GetRankAndTrafficStats)
    返回: [{ date, clicks, impressions }, ...]
    """
    end_date = datetime.now().date() - timedelta(days=2)
    start_date = end_date - timedelta(days=days)

    data = bing_get("GetRankAndTrafficStats", {
        "siteUrl": site_url,
        "startDate": start_date.strftime("%Y-%m-%d"),
        "endDate": end_date.strftime("%Y-%m-%d"),
    }, api_key)

    results = []
    if data and "d" in data:
        rows = data["d"]
        if isinstance(rows, list):
            for row in rows:
                results.append({
                    "date": row.get("Date", "")[:10],
                    "clicks": row.get("Clicks", 0),
                    "impressions": row.get("Impressions", 0),
                })
    return results


def fetch_crawl_issues(site_url: str, api_key: str) -> list[dict]:
    """
    获取具体抓取错误 URL 列表 (GetCrawlIssues)
    返回: [{ url, issue_type, last_crawled }, ...]
    """
    data = bing_get("GetCrawlIssues", {"siteUrl": site_url}, api_key)
    results = []
    if data and "d" in data:
        rows = data["d"]
        if isinstance(rows, list):
            for row in rows:
                results.append({
                    "url": row.get("Url", ""),
                    "issue_type": row.get("CrawlIssue", ""),
                    "last_crawled": str(row.get("LastCrawled", ""))[:10],
                })
    return results


def fetch_bing_data(domain: str, days: int, out_dir: Path):
    """拉取 Bing Webmaster 数据并保存为 JSON 到 out_dir"""
    api_key = get_bing_api_key()
    site_url = os.environ.get("BING_SITE_URL") or f"https://{domain}/"

    end_date = datetime.now().date() - timedelta(days=2)
    date_str = end_date.strftime("%Y-%m-%d")

    print(f"\n🔍 Bing Webmaster 数据拉取: {site_url}")
    print(f"📅 日期范围: 近 {days} 天（截至 {date_str}）")

    out_dir.mkdir(parents=True, exist_ok=True)

    # === 1. 关键词查询统计 ===
    print(f"\n🔑 拉取关键词查询统计（近 {days} 天）...")
    try:
        queries = fetch_query_stats(site_url, api_key, days)
        out_file = out_dir / "queries.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({
                "site_url": site_url,
                "days": days,
                "end_date": str(end_date),
                "total_queries": len(queries),
                "data": queries,
            }, f, ensure_ascii=False, indent=2)
        print(f"   ✅ 已保存 {len(queries)} 条关键词 → {out_file.name}")
    except Exception as e:
        print(f"   ❌ 关键词查询拉取失败: {e}")

    # === 2. Top 页面流量统计 (GetPageStats — 批量接口) ===
    print(f"\n📄 拉取 Top 页面流量统计 (GetPageStats)...")
    try:
        pages = fetch_page_stats(site_url, api_key)
        out_file = out_dir / "pages.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({
                "site_url": site_url,
                "end_date": str(end_date),
                "total_pages": len(pages),
                "data": pages,
            }, f, ensure_ascii=False, indent=2)
        if pages:
            print(f"   ✅ 已保存 {len(pages)} 个页面 → {out_file.name}")
        else:
            print(f"   ⚠️ GetPageStats 返回空数据（Bing 可能数据尚未就绪）→ {out_file.name}")
    except Exception as e:
        print(f"   ❌ 页面流量拉取失败: {e}")

    # === 3. 每日流量趋势 ===
    print(f"\n📈 拉取每日流量趋势...")
    try:
        daily = fetch_rank_traffic_stats(site_url, api_key, days)
        out_file = out_dir / "daily.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({
                "site_url": site_url,
                "days": days,
                "end_date": str(end_date),
                "total_days": len(daily),
                "data": daily,
            }, f, ensure_ascii=False, indent=2)
        print(f"   ✅ 已保存 {len(daily)} 天数据 → {out_file.name}")
    except Exception as e:
        print(f"   ❌ 每日趋势拉取失败: {e}")

    # === 4. 抓取错误列表 ===
    print(f"\n⚠️ 拉取抓取错误列表...")
    try:
        crawl_issues = fetch_crawl_issues(site_url, api_key)
        out_file = out_dir / "crawl_issues.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({
                "site_url": site_url,
                "end_date": str(end_date),
                "total_issues": len(crawl_issues),
                "data": crawl_issues,
            }, f, ensure_ascii=False, indent=2)
        if crawl_issues:
            print(f"   ✅ 发现 {len(crawl_issues)} 个抓取问题 → {out_file.name}")
        else:
            print(f"   ✅ 无抓取错误 → {out_file.name}")
    except Exception as e:
        print(f"   ❌ 抓取错误拉取失败: {e}")

    print("\n🎉 Bing 数据拉取完成！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="拉取 Bing Webmaster Tools 数据")
    parser.add_argument("--domain", required=True, help="站点域名，例如 example.com")
    parser.add_argument("--days", type=int, default=30, help="回溯天数（默认 30）")
    parser.add_argument("--out-dir", default="./bing_data", help="JSON 输出目录")
    args = parser.parse_args()

    fetch_bing_data(
        domain=args.domain,
        days=args.days,
        out_dir=Path(args.out_dir),
    )
