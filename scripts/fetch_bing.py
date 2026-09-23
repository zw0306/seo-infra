"""
Bing Webmaster Tools 数据拉取器（seo-infra 周报版）

API 行为说明（实测）：
  - GetQueryStats / GetPageStats：返回最近约 16 个月的【每周】Top 100 数据。
    Date 字段 = 每周起始日（WCF 格式：/Date(ms)/），每次 API 响应包含所有历史周。
    本脚本只取 Date 最大的一周（最新完整周快照）+ 上一周（用于环比）。
  - GetRankAndTrafficStats：返回每日真实流量，忽略日期参数，手动过滤所需天数。
  - AvgClickPosition：永远返回 -1（已废弃），应使用 AvgImpressionPosition。

用法:
  BING_API_KEY=xxx python3 fetch_bing.py --domain example.com --out-dir /tmp/bing
"""
import json
import sys
import os
import re
import time
import argparse
import requests
from datetime import datetime, timedelta
from pathlib import Path


BING_API_BASE = "https://ssl.bing.com/webmaster/api.svc/json"


def get_bing_api_key() -> str:
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


def parse_wcf_date(date_str: str):
    """解析 Bing WCF 日期格式 /Date(ms)/ → date 对象"""
    m = re.search(r'\d+', date_str)
    if m:
        return datetime.fromtimestamp(int(m.group(0)) / 1000.0).date()
    return None


def fetch_query_stats(site_url: str, api_key: str) -> dict:
    """
    取 GetQueryStats 中最新完整一周 + 上一周快照。
    返回:
      {
        week_date: "2026-09-18",
        prev_week_date: "2026-09-11",
        this_week: [{query, clicks, impressions, ctr, avg_position}, ...],
        prev_week: [...],
      }
    """
    data = bing_get("GetQueryStats", {"siteUrl": site_url}, api_key)
    if not data or "d" not in data:
        return {"week_date": "", "prev_week_date": "", "this_week": [], "prev_week": []}

    # 按 Date 分组
    by_date: dict = {}
    for row in data["d"]:
        dt = parse_wcf_date(row.get("Date", ""))
        if not dt:
            continue
        by_date.setdefault(dt, []).append(row)

    if not by_date:
        return {"week_date": "", "prev_week_date": "", "this_week": [], "prev_week": []}

    sorted_dates = sorted(by_date.keys(), reverse=True)
    latest_date = sorted_dates[0]
    prev_date = sorted_dates[1] if len(sorted_dates) > 1 else None

    def process(rows: list) -> list:
        result = []
        for row in rows:
            query = row.get("Query", "")
            if not query:
                continue
            clicks = row.get("Clicks", 0)
            impressions = row.get("Impressions", 0)
            # AvgClickPosition 永远 -1（已废弃），使用 AvgImpressionPosition
            result.append({
                "query": query,
                "clicks": clicks,
                "impressions": impressions,
                "ctr": round(clicks / impressions, 4) if impressions > 0 else 0,
                "avg_position": round(row.get("AvgImpressionPosition", 0), 1),
            })
        result.sort(key=lambda x: x["clicks"], reverse=True)
        return result

    return {
        "week_date": latest_date.strftime("%Y-%m-%d"),
        "prev_week_date": prev_date.strftime("%Y-%m-%d") if prev_date else "",
        "this_week": process(by_date[latest_date]),
        "prev_week": process(by_date[prev_date]) if prev_date else [],
    }


def fetch_page_stats(site_url: str, api_key: str) -> dict:
    """
    取 GetPageStats 中最新完整一周 + 上一周快照。
    注：GetPageStats 的 URL 存在 Query 字段中。
    """
    data = bing_get("GetPageStats", {"siteUrl": site_url}, api_key)
    if not data or "d" not in data:
        return {"week_date": "", "prev_week_date": "", "this_week": [], "prev_week": []}

    by_date: dict = {}
    for row in data["d"]:
        dt = parse_wcf_date(row.get("Date", ""))
        if not dt:
            continue
        by_date.setdefault(dt, []).append(row)

    if not by_date:
        return {"week_date": "", "prev_week_date": "", "this_week": [], "prev_week": []}

    sorted_dates = sorted(by_date.keys(), reverse=True)
    latest_date = sorted_dates[0]
    prev_date = sorted_dates[1] if len(sorted_dates) > 1 else None

    def process(rows: list) -> list:
        result = []
        for row in rows:
            page = row.get("Query", "")  # GetPageStats 把 URL 存在 Query 字段
            if not page:
                continue
            clicks = row.get("Clicks", 0)
            impressions = row.get("Impressions", 0)
            result.append({
                "page": page,
                "clicks": clicks,
                "impressions": impressions,
                "ctr": round(clicks / impressions, 4) if impressions > 0 else 0,
                "avg_position": round(row.get("AvgImpressionPosition", 0), 1),
            })
        result.sort(key=lambda x: x["clicks"], reverse=True)
        return result

    return {
        "week_date": latest_date.strftime("%Y-%m-%d"),
        "prev_week_date": prev_date.strftime("%Y-%m-%d") if prev_date else "",
        "this_week": process(by_date[latest_date]),
        "prev_week": process(by_date[prev_date]) if prev_date else [],
    }


def fetch_rank_traffic_stats(site_url: str, api_key: str, days: int = 7) -> list[dict]:
    """
    取最近 days 天的每日流量趋势（GetRankAndTrafficStats）。
    API 返回全部历史日数据，需手动过滤 WCF 日期。
    """
    end_date = datetime.now().date() - timedelta(days=2)
    start_date = end_date - timedelta(days=days - 1)

    data = bing_get("GetRankAndTrafficStats", {"siteUrl": site_url}, api_key)
    results = []
    if data and "d" in data:
        for row in data["d"]:
            dt = parse_wcf_date(row.get("Date", ""))
            if dt and start_date <= dt <= end_date:
                results.append({
                    "date": dt.strftime("%Y-%m-%d"),
                    "clicks": row.get("Clicks", 0),
                    "impressions": row.get("Impressions", 0),
                })
    results.sort(key=lambda x: x["date"])
    return results


def fetch_crawl_issues(site_url: str, api_key: str) -> list[dict]:
    """获取具体抓取错误 URL 列表 (GetCrawlIssues)"""
    data = bing_get("GetCrawlIssues", {"siteUrl": site_url}, api_key)
    results = []
    if data and "d" in data:
        for row in data["d"]:
            results.append({
                "url": row.get("Url", ""),
                "issue_type": row.get("CrawlIssue", ""),
                "last_crawled": str(row.get("LastCrawled", ""))[:10],
            })
    return results


def fetch_bing_data(domain: str, out_dir: Path):
    """拉取 Bing Webmaster 数据并保存为 JSON 到 out_dir"""
    api_key = get_bing_api_key()
    site_url = os.environ.get("BING_SITE_URL") or f"https://{domain}/"

    print(f"\n🔍 Bing Webmaster 数据拉取: {site_url}")

    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. 关键词查询统计（最新周 + 上一周）
    print("\n🔑 拉取关键词查询统计（最新周快照）...")
    try:
        queries = fetch_query_stats(site_url, api_key)
        out_file = out_dir / "queries.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"site_url": site_url, **queries}, f, ensure_ascii=False, indent=2)
        n = len(queries["this_week"])
        wdate = queries["week_date"]
        print(f"   ✅ 已保存 {n} 条关键词（{wdate} 周）→ {out_file.name}")
    except Exception as e:
        print(f"   ❌ 关键词查询拉取失败: {e}")

    # 2. Top 页面流量统计（最新周 + 上一周）
    print("\n📄 拉取 Top 页面流量统计（最新周快照）...")
    try:
        pages = fetch_page_stats(site_url, api_key)
        out_file = out_dir / "pages.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"site_url": site_url, **pages}, f, ensure_ascii=False, indent=2)
        n = len(pages["this_week"])
        print(f"   ✅ 已保存 {n} 个页面 → {out_file.name}")
    except Exception as e:
        print(f"   ❌ 页面流量拉取失败: {e}")

    # 3. 每日流量趋势（最近 7 天）
    print("\n📈 拉取每日流量趋势（最近 7 天）...")
    try:
        daily = fetch_rank_traffic_stats(site_url, api_key, days=7)
        out_file = out_dir / "daily.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"site_url": site_url, "data": daily}, f, ensure_ascii=False, indent=2)
        print(f"   ✅ 已保存 {len(daily)} 天数据 → {out_file.name}")
    except Exception as e:
        print(f"   ❌ 每日趋势拉取失败: {e}")

    # 4. 抓取错误列表
    print("\n⚠️ 拉取抓取错误列表...")
    try:
        crawl_issues = fetch_crawl_issues(site_url, api_key)
        out_file = out_dir / "crawl_issues.json"
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump({"site_url": site_url, "data": crawl_issues}, f, ensure_ascii=False, indent=2)
        if crawl_issues:
            print(f"   ⚠️ 发现 {len(crawl_issues)} 个抓取问题 → {out_file.name}")
        else:
            print(f"   ✅ 无抓取错误 → {out_file.name}")
    except Exception as e:
        print(f"   ❌ 抓取错误拉取失败: {e}")

    print("\n🎉 Bing 数据拉取完成！")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="拉取 Bing Webmaster Tools 数据")
    parser.add_argument("--domain", required=True, help="站点域名，例如 example.com")
    parser.add_argument("--out-dir", default="./bing_data", help="JSON 输出目录")
    args = parser.parse_args()

    fetch_bing_data(
        domain=args.domain,
        out_dir=Path(args.out_dir),
    )
