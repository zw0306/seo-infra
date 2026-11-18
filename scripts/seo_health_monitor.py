#!/usr/bin/env python3
"""
SEO Health & Anomaly Monitor (seo_health_monitor.py)

Multi-probe monitor for websites managed under seo-infra:
  1. HTTP Self-Check: status codes, redirect errors, meta/X-Robots-Tag noindex,
     canonical host checks, robots.txt blanket Disallow, sitemap reachability.
  2. GSC Sitemaps: sitemap errors, warnings, and sudden submitted-URL drops.
  3. GSC URL Inspection: crawl/fetch state (403 ACCESS_FORBIDDEN, 5xx, 404),
     robots/indexing blocks, verdict regressions, and canonical drift across
     homepage + configured URLs + auto-discovered Top click pages.
  4. GSC Traffic & Ranking (dataState="all"): T-2 same-weekday drop checks for
     site totals and top pages, plus severe T-1 collapse guard.
  5. GA4 Organic Traffic: T-2 (and post-10:00 UTC T-1) same-weekday drop checks.

Includes deduplication state, cooldown suppression, recovery notifications,
and Telegram HTML alerting.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import statistics
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

GSC_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
GA4_SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; SEOInfraMonitor/1.0; +https://github.com/zw0306/seo-infra)"
)
MAX_TELEGRAM_CHARS = 3800
GSC_PAGE_ROW_LIMIT = 25000
GSC_MAX_PAGE_PAGES = 4
MIN_BYPAGE_COVERAGE_RATIO = 0.2

CRITICAL_FETCH_STATES = {
    "ACCESS_FORBIDDEN",
    "SERVER_ERROR",
    "NOT_FOUND",
    "SOFT_404",
    "REDIRECT_ERROR",
    "BLOCKED_ROBOTS_TXT",
    "INTERNAL_CRAWL_ERROR",
    "INVALID_URL",
}

SEVERITY_RANK = {"P1": 1, "P0": 2}


@dataclass
class Alert:
    """Single deduplicated monitoring finding."""

    key: str
    probe: str
    severity: str  # "P0" or "P1"
    title: str
    detail: str
    url: str = ""
    signature: str = ""


class HtmlSeoParser(HTMLParser):
    """Lightweight HTML parser extracting robots directives and canonical links."""

    def __init__(self) -> None:
        super().__init__()
        self.robots_directives: list[str] = []
        self.canonicals: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if tag_lower == "meta":
            name = attr_map.get("name", "").strip().lower()
            if name in {"robots", "googlebot", "googlebot-news"}:
                content = attr_map.get("content", "")
                for token in content.split(","):
                    cleaned = token.strip().lower()
                    if cleaned:
                        self.robots_directives.append(cleaned)
        elif tag_lower == "link":
            rels = {r.strip().lower() for r in attr_map.get("rel", "").split() if r.strip()}
            href = attr_map.get("href", "").strip()
            if "canonical" in rels and href:
                self.canonicals.append(href)


# ──────────────────────────────────────────────
# URL & Config Helpers
# ──────────────────────────────────────────────

def _parse_iso_date(raw: Any) -> date | None:
    """Safely parse a YYYY-MM-DD string into a date, returning None if invalid."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def clean_domain(raw_domain: str) -> str:
    """Normalize domain string by removing scheme, slashes, and whitespace."""
    text = raw_domain.strip()
    if "://" in text:
        text = urlparse(text).netloc or text
    return text.strip("/").lower()


def normalize_target_url(raw: str, domain: str) -> str | None:
    """Normalize a relative path or URL into an https URL belonging to domain."""
    item = raw.strip()
    if not item:
        return None
    if item.startswith(("http://", "https://")):
        parsed = urlparse(item)
        host = (parsed.hostname or "").lower()
        if host != domain and not host.endswith(f".{domain}"):
            return None
        path = parsed.path or "/"
        query = f"?{parsed.query}" if parsed.query else ""
        return f"https://{host}{path}{query}"
    path = item if item.startswith("/") else f"/{item}"
    return f"https://{domain}{path}"


def short_path(url: str, domain: str) -> str:
    """Shorten a full URL into a readable path for alert keys and messages."""
    if not url:
        return "/"
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    if host and host != domain:
        return f"{host}{path}"
    return path


def extract_top_pages_from_snapshot(snapshot_path: Path, domain: str, limit: int = 10) -> list[str]:
    """Aggregate clicks by page from weekly snapshot JSON and return Top N URLs."""
    if not snapshot_path.exists():
        return []
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"⚠️ 无法读取快照 {snapshot_path}: {exc}", file=sys.stderr)
        return []

    page_clicks: dict[str, float] = {}
    for row in data.get("rows", []):
        page = row.get("page")
        if not page:
            keys = row.get("keys") or []
            if len(keys) >= 2:
                page = keys[1]
            elif len(keys) == 1 and str(keys[0]).startswith("http"):
                page = keys[0]
        if not page:
            continue
        normalized = normalize_target_url(str(page), domain)
        if not normalized:
            continue
        page_clicks[normalized] = page_clicks.get(normalized, 0.0) + float(row.get("clicks", 0) or 0)

    ranked = sorted(page_clicks.items(), key=lambda kv: kv[1], reverse=True)
    return [url for url, _ in ranked[:limit]]


def build_target_urls(
    domain: str,
    explicit_urls: list[str],
    snapshot_top_urls: list[str],
    live_top_urls: list[str],
    active_alerts: dict[str, Any],
    max_urls: int = 15,
) -> list[str]:
    """Build ordered, deduplicated list of inspection/probe URLs."""
    ordered: list[str] = []
    seen: set[str] = set()

    def add(candidate: str | None) -> None:
        if not candidate or candidate in seen:
            return
        seen.add(candidate)
        ordered.append(candidate)

    add(f"https://{domain}/")
    for raw in explicit_urls:
        add(normalize_target_url(raw, domain))

    # Keep any currently alerting URL pinned even if its traffic temporarily drops to zero.
    for alert_info in active_alerts.values():
        alert_url = alert_info.get("url")
        if alert_url:
            add(normalize_target_url(str(alert_url), domain))

    for url in snapshot_top_urls:
        if len(ordered) >= max_urls:
            break
        add(normalize_target_url(url, domain))

    for url in live_top_urls:
        if len(ordered) >= max_urls:
            break
        add(normalize_target_url(url, domain))

    return ordered[:max_urls]


# ──────────────────────────────────────────────
# Parsers for robots.txt & XML sitemaps
# ──────────────────────────────────────────────

def parse_robots_txt(text: str) -> dict[str, Any]:
    """Check whether robots.txt blocks '*' or 'Googlebot' completely and extract sitemaps."""
    blocked_agents: list[str] = []
    sitemaps: list[str] = []

    current_agents: list[str] = []
    rules_by_agent: dict[str, dict[str, list[str]]] = {}
    in_rules_for_group = False

    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            in_rules_for_group = False
            continue
        if ":" not in line:
            continue
        field_name, value = line.split(":", 1)
        key = field_name.strip().lower()
        val = value.strip()

        if key == "sitemap" and val:
            sitemaps.append(val)
            continue

        if key == "user-agent":
            agent = val.lower()
            if in_rules_for_group:
                current_agents = [agent]
                in_rules_for_group = False
            else:
                current_agents.append(agent)
            rules_by_agent.setdefault(agent, {"disallow": [], "allow": []})
        elif key in {"disallow", "allow"} and current_agents:
            in_rules_for_group = True
            for agent in current_agents:
                rules_by_agent[agent][key].append(val)

    for target_agent in ("*", "googlebot"):
        rules = rules_by_agent.get(target_agent)
        if not rules:
            continue
        has_root_disallow = any(path == "/" for path in rules["disallow"])
        has_root_allow = any(path == "/" for path in rules["allow"])
        if has_root_disallow and not has_root_allow:
            blocked_agents.append(target_agent)

    return {"blocked_agents": blocked_agents, "sitemaps": sitemaps}


def parse_sitemap_xml(body: str) -> tuple[bool, int, str]:
    """Validate sitemap XML and return (is_valid, entry_count, error_reason)."""
    try:
        root = ET.fromstring(body.strip())
    except ET.ParseError as exc:
        return False, 0, f"XML 解析失败: {exc}"

    tag_name = root.tag.split("}", 1)[-1].lower()
    if tag_name not in {"urlset", "sitemapindex"}:
        return False, 0, f"根节点异常: <{tag_name}>"

    entries = [
        child for child in root
        if child.tag.split("}", 1)[-1].lower() in {"url", "sitemap"}
    ]
    if not entries:
        return False, 0, "Sitemap 内容为空 (0 条记录)"
    return True, len(entries), ""


# ──────────────────────────────────────────────
# Probe 1: HTTP Self-Check
# ──────────────────────────────────────────────

def check_http_targets(
    domain: str,
    urls: list[str],
    sitemap_urls: list[str] | None = None,
    session: requests.Session | None = None,
    timeout: int = 15,
) -> dict[str, Any]:
    """Run live HTTP checks on robots.txt, target URLs, and discovered sitemaps."""
    sess = session or requests.Session()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    alerts: list[Alert] = []
    url_results: dict[str, dict[str, Any]] = {}
    discovered_sitemaps: list[str] = list(sitemap_urls or [])

    # 1. robots.txt
    robots_url = f"https://{domain}/robots.txt"
    try:
        r_resp = sess.get(robots_url, headers=headers, timeout=timeout, allow_redirects=True)
        if r_resp.status_code == 200:
            parsed_robots = parse_robots_txt(r_resp.text)
            for sm in parsed_robots["sitemaps"]:
                if sm not in discovered_sitemaps:
                    discovered_sitemaps.append(sm)
            if parsed_robots["blocked_agents"]:
                agents_str = ", ".join(parsed_robots["blocked_agents"])
                alerts.append(Alert(
                    key="http:robots_disallow_all",
                    probe="http",
                    severity="P0",
                    title=f"robots.txt 全站拦截爬虫 ({agents_str})",
                    detail=f"{robots_url} 包含 Disallow: /，将阻止搜索引擎抓取整站。",
                    url=robots_url,
                    signature=f"disallow_all:{agents_str}",
                ))
        elif r_resp.status_code >= 500 or r_resp.status_code == 403:
            alerts.append(Alert(
                key="http:robots_error",
                probe="http",
                severity="P0",
                title=f"robots.txt 返回异常状态 HTTP {r_resp.status_code}",
                detail=f"请求 {robots_url} 返回 HTTP {r_resp.status_code}，可能影响全站抓取。",
                url=robots_url,
                signature=f"status:{r_resp.status_code}",
            ))
    except requests.RequestException as exc:
        alerts.append(Alert(
            key="http:robots_error",
            probe="http",
            severity="P0",
            title="robots.txt 请求失败",
            detail=f"请求 {robots_url} 异常: {exc}",
            url=robots_url,
            signature=f"exc:{type(exc).__name__}",
        ))

    # 2. Target URLs
    for url in urls:
        path = short_path(url, domain)
        info: dict[str, Any] = {"status": None, "noindex": False, "canonical": None, "error": None}
        try:
            resp = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True)
            info["status"] = resp.status_code
            final_host = (urlparse(resp.url).hostname or "").lower()

            if resp.status_code >= 400:
                cf_mitigated = resp.headers.get("cf-mitigated", "")
                extra = f" (cf-mitigated: {cf_mitigated})" if cf_mitigated else ""
                alerts.append(Alert(
                    key=f"http:url:{path}",
                    probe="http",
                    severity="P0",
                    title=f"HTTP {resp.status_code} 页面异常: {path}",
                    detail=f"拨测 {url} 返回 HTTP {resp.status_code}{extra}。",
                    url=url,
                    signature=f"status:{resp.status_code}",
                ))
                url_results[url] = info
                continue

            if final_host and final_host != domain and not final_host.endswith(f".{domain}"):
                alerts.append(Alert(
                    key=f"http:url:{path}",
                    probe="http",
                    severity="P0",
                    title=f"页面意外跳转至外部域名: {path}",
                    detail=f"{url} 最终跳转到 {resp.url}。",
                    url=url,
                    signature=f"redirect:{final_host}",
                ))
                url_results[url] = info
                continue

            x_robots = resp.headers.get("X-Robots-Tag", "").lower()
            has_x_noindex = "noindex" in x_robots

            content_type = resp.headers.get("Content-Type", "").lower()
            has_meta_noindex = False
            canonical_url: str | None = None
            if "html" in content_type or not content_type:
                parser = HtmlSeoParser()
                parser.feed(resp.text)
                has_meta_noindex = "noindex" in parser.robots_directives
                canonical_url = parser.canonicals[0] if parser.canonicals else None
                if canonical_url:
                    canonical_url = urljoin(resp.url, canonical_url)

            info["noindex"] = has_x_noindex or has_meta_noindex
            info["canonical"] = canonical_url

            if info["noindex"]:
                src = "X-Robots-Tag" if has_x_noindex else "<meta name=robots>"
                alerts.append(Alert(
                    key=f"http:noindex:{path}",
                    probe="http",
                    severity="P0",
                    title=f"线上页面出现 noindex 标签: {path}",
                    detail=f"{url} 在 {src} 返回 noindex，继续留存会导致被谷歌移出索引。",
                    url=url,
                    signature=f"noindex:{src}",
                ))

            if "html" in content_type:
                if not canonical_url:
                    alerts.append(Alert(
                        key=f"http:canonical:{path}",
                        probe="http",
                        severity="P1",
                        title=f"页面缺少 Canonical 标签: {path}",
                        detail=f"{url} (HTTP 200 HTML) 未检测到 <link rel=\"canonical\">。",
                        url=url,
                        signature="canonical_missing",
                    ))
                else:
                    canon_host = (urlparse(canonical_url).hostname or "").lower()
                    if canon_host and canon_host != domain and not canon_host.endswith(f".{domain}"):
                        alerts.append(Alert(
                            key=f"http:canonical:{path}",
                            probe="http",
                            severity="P0",
                            title=f"Canonical 指向错误域名: {path}",
                            detail=f"{url} 的 canonical 指向 {canonical_url}（非 {domain}）。",
                            url=url,
                            signature=f"canonical_host:{canon_host}",
                        ))
        except requests.RequestException as exc:
            info["error"] = str(exc)
            alerts.append(Alert(
                key=f"http:url:{path}",
                probe="http",
                severity="P0",
                title=f"页面请求超时或连接失败: {path}",
                detail=f"拨测 {url} 失败: {exc}",
                url=url,
                signature=f"exc:{type(exc).__name__}",
            ))

        url_results[url] = info

    # 3. Check primary discovered sitemap(s) (or fallback sitemap-index/sitemap.xml)
    if not discovered_sitemaps:
        discovered_sitemaps = [f"https://{domain}/sitemap-index.xml", f"https://{domain}/sitemap.xml"]
        fallback_mode = True
    else:
        fallback_mode = False

    checked_valid_sitemap = False
    fallback_errors: list[tuple[str, str]] = []
    for sm_url in discovered_sitemaps[:3]:
        sm_path = short_path(sm_url, domain)
        try:
            sm_resp = sess.get(sm_url, headers=headers, timeout=timeout, allow_redirects=True)
            if sm_resp.status_code == 200:
                valid, count, err_msg = parse_sitemap_xml(sm_resp.text)
                if valid:
                    checked_valid_sitemap = True
                    continue
                if not fallback_mode:
                    alerts.append(Alert(
                        key=f"http:sitemap:{sm_path}",
                        probe="http",
                        severity="P0",
                        title=f"Sitemap XML 内容异常: {sm_path}",
                        detail=f"{sm_url} 返回 HTTP 200 但校验失败: {err_msg}",
                        url=sm_url,
                        signature=f"xml:{err_msg[:40]}",
                    ))
                else:
                    fallback_errors.append((sm_url, err_msg))
            elif not fallback_mode or sm_resp.status_code >= 500 or sm_resp.status_code == 403:
                alerts.append(Alert(
                    key=f"http:sitemap:{sm_path}",
                    probe="http",
                    severity="P0",
                    title=f"Sitemap HTTP {sm_resp.status_code}: {sm_path}",
                    detail=f"请求 {sm_url} 返回 HTTP {sm_resp.status_code}。",
                    url=sm_url,
                    signature=f"status:{sm_resp.status_code}",
                ))
            else:
                fallback_errors.append((sm_url, f"HTTP {sm_resp.status_code}"))
        except requests.RequestException as exc:
            if not fallback_mode:
                alerts.append(Alert(
                    key=f"http:sitemap:{sm_path}",
                    probe="http",
                    severity="P0",
                    title=f"Sitemap 请求失败: {sm_path}",
                    detail=f"请求 {sm_url} 异常: {exc}",
                    url=sm_url,
                    signature=f"exc:{type(exc).__name__}",
                ))
            else:
                fallback_errors.append((sm_url, str(exc)))

    if fallback_mode and not checked_valid_sitemap and fallback_errors:
        details = "; ".join(f"{u} ({e})" for u, e in fallback_errors)
        alerts.append(Alert(
            key="http:sitemap:fallback",
            probe="http",
            severity="P1",
            title="未找到可访问的 Sitemap 文件",
            detail=f"robots.txt 未声明 Sitemap 且常见路径均失败: {details}",
            url=f"https://{domain}/sitemap-index.xml",
            signature="no_sitemap",
        ))

    return {
        "alerts": alerts,
        "url_results": url_results,
        "discovered_sitemaps": discovered_sitemaps,
    }


# ──────────────────────────────────────────────
# Google Credentials & GSC / GA4 Clients
# ──────────────────────────────────────────────

def load_service_account_info(sa_file: str | None) -> dict[str, Any] | None:
    """Load Google Service Account JSON from env var or file."""
    raw_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw_json:
        return json.loads(raw_json)

    candidates = []
    if sa_file:
        candidates.append(Path(sa_file))
    env_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / ".config" / "claude-seo" / "service-account.json")

    for path in candidates:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    return None


def build_google_services(
    sa_info: dict[str, Any],
    need_ga4: bool = False,
) -> tuple[Any, Any | None]:
    """Build Search Console API v1 service and optional GA4 BetaAnalyticsDataClient."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    gsc_creds = service_account.Credentials.from_service_account_info(
        sa_info, scopes=GSC_SCOPES
    )
    gsc_service = build("searchconsole", "v1", credentials=gsc_creds, cache_discovery=False)

    ga4_client = None
    if need_ga4:
        from google.analytics.data_v1beta import BetaAnalyticsDataClient

        ga4_creds = service_account.Credentials.from_service_account_info(
            sa_info, scopes=GA4_SCOPES
        )
        ga4_client = BetaAnalyticsDataClient(credentials=ga4_creds)

    return gsc_service, ga4_client


# ──────────────────────────────────────────────
# Probe 2: GSC Sitemaps
# ──────────────────────────────────────────────

def evaluate_gsc_sitemaps(
    response: dict[str, Any],
    domain: str,
    prev_submitted: int | None = None,
) -> tuple[list[Alert], list[str], int]:
    """Analyze GSC sitemaps.list() response for errors, warnings, and URL count drops."""
    alerts: list[Alert] = []
    sitemap_urls: list[str] = []
    sitemaps = response.get("sitemap", []) or []

    if not sitemaps:
        alerts.append(Alert(
            key="gsc_sitemaps:missing",
            probe="gsc_sitemaps",
            severity="P1",
            title="GSC 未提交任何 Sitemap",
            detail=f"sc-domain:{domain} 在 Search Console 中没有已提交的 sitemap。",
            url=f"https://{domain}/",
            signature="empty",
        ))
        return alerts, sitemap_urls, 0

    total_submitted = 0
    for sm in sitemaps:
        sm_url = str(sm.get("path", ""))
        if sm_url:
            sitemap_urls.append(sm_url)
        sm_path = short_path(sm_url, domain)
        errors = int(sm.get("errors") or 0)
        warnings = int(sm.get("warnings") or 0)
        for content in sm.get("contents", []) or []:
            total_submitted += int(content.get("submitted") or 0)

        if errors > 0:
            alerts.append(Alert(
                key=f"gsc_sitemaps:err:{sm_path}",
                probe="gsc_sitemaps",
                severity="P0",
                title=f"GSC Sitemap 报错 ({errors} errors): {sm_path}",
                detail=f"{sm_url} 在 GSC 中出现 {errors} 个错误，{warnings} 个警告，可能影响全站抓取发现。",
                url=sm_url,
                signature=f"errors:{errors}",
            ))
        elif warnings > 0:
            alerts.append(Alert(
                key=f"gsc_sitemaps:warn:{sm_path}",
                probe="gsc_sitemaps",
                severity="P1",
                title=f"GSC Sitemap 告警 ({warnings} warnings): {sm_path}",
                detail=f"{sm_url} 在 GSC 中出现 {warnings} 个警告。",
                url=sm_url,
                signature=f"warnings:{warnings}",
            ))

    if prev_submitted is not None and prev_submitted >= 10 and total_submitted < prev_submitted * 0.5:
        alerts.append(Alert(
            key="gsc_sitemaps:submitted_drop",
            probe="gsc_sitemaps",
            severity="P0",
            title=f"GSC Sitemap 收录提交数骤降: {prev_submitted} → {total_submitted}",
            detail=f"站点 {domain} 的 Sitemap 已提交 URL 总数从 {prev_submitted} 骤降至 {total_submitted}（跌幅超 50%），请排查构建是否漏页。",
            url=sitemap_urls[0] if sitemap_urls else f"https://{domain}/",
            signature=f"drop:{prev_submitted}->{total_submitted}",
        ))

    return alerts, sitemap_urls, total_submitted


def check_gsc_sitemaps(
    gsc_service: Any,
    property_url: str,
    domain: str,
    prev_submitted: int | None = None,
) -> dict[str, Any]:
    """Fetch and evaluate GSC sitemaps."""
    response = gsc_service.sitemaps().list(siteUrl=property_url).execute()
    alerts, sitemap_urls, total_submitted = evaluate_gsc_sitemaps(
        response, domain, prev_submitted=prev_submitted
    )
    return {
        "alerts": alerts,
        "sitemap_urls": sitemap_urls,
        "total_submitted": total_submitted,
    }


# ──────────────────────────────────────────────
# Probe 3: GSC URL Inspection
# ──────────────────────────────────────────────

def fetch_gsc_top_pages(
    gsc_service: Any,
    property_url: str,
    domain: str,
    today_utc: date,
    limit: int = 10,
) -> dict[str, float]:
    """Fetch top pages by clicks over the last 7 days via GSC Search Analytics."""
    start = (today_utc - timedelta(days=9)).strftime("%Y-%m-%d")
    end = (today_utc - timedelta(days=2)).strftime("%Y-%m-%d")
    body = {
        "startDate": start,
        "endDate": end,
        "dimensions": ["page"],
        "rowLimit": max(limit * 3, 25),
        "dataState": "all",
    }
    resp = gsc_service.searchanalytics().query(siteUrl=property_url, body=body).execute()
    result: dict[str, float] = {}
    for row in resp.get("rows", []) or []:
        keys = row.get("keys") or []
        if not keys:
            continue
        norm = normalize_target_url(str(keys[0]), domain)
        if not norm:
            continue
        result[norm] = result.get(norm, 0.0) + float(row.get("clicks", 0) or 0)
    sorted_items = sorted(result.items(), key=lambda kv: kv[1], reverse=True)
    return dict(sorted_items[:limit])


def evaluate_gsc_inspection_result(
    url: str,
    ir: dict[str, Any],
    domain: str,
    prev_verdict_info: dict[str, Any] | None = None,
    known_traffic_clicks: float = 0.0,
    http_status: int | None = None,
) -> tuple[list[Alert], dict[str, Any]]:
    """Evaluate one URL Inspection response and return (alerts, new_cached_status)."""
    idx = ir.get("indexStatusResult", {}) or {}
    verdict = idx.get("verdict") or "VERDICT_UNSPECIFIED"
    coverage_state = idx.get("coverageState") or ""
    robots_state = idx.get("robotsTxtState") or ""
    indexing_state = idx.get("indexingState") or ""
    fetch_state = idx.get("pageFetchState") or ""
    last_crawl = idx.get("lastCrawlTime") or ""
    google_canonical = idx.get("googleCanonical") or ""
    user_canonical = idx.get("userCanonical") or ""

    new_cached = {
        "verdict": verdict,
        "page_fetch_state": fetch_state,
        "coverage_state": coverage_state,
        "last_crawl_time": last_crawl,
    }

    path = short_path(url, domain)
    p0_reasons: list[str] = []
    p1_reasons: list[str] = []
    sig_parts: list[str] = []

    if fetch_state in CRITICAL_FETCH_STATES:
        p0_reasons.append(f"抓取失败: {fetch_state}")
        sig_parts.append(f"fetch:{fetch_state}")
    elif fetch_state and fetch_state not in {"SUCCESSFUL", "PAGE_FETCH_STATE_UNSPECIFIED"}:
        p1_reasons.append(f"抓取状态异常: {fetch_state}")
        sig_parts.append(f"fetch:{fetch_state}")

    if robots_state and robots_state not in {"ALLOWED", "ROBOTS_TXT_STATE_UNSPECIFIED"}:
        p0_reasons.append(f"robots.txt 状态: {robots_state}")
        sig_parts.append(f"robots:{robots_state}")

    if indexing_state and indexing_state not in {"INDEXING_ALLOWED", "INDEXING_STATE_UNSPECIFIED"}:
        p0_reasons.append(f"索引受阻: {indexing_state}")
        sig_parts.append(f"idx:{indexing_state}")

    prev_verdict = (prev_verdict_info or {}).get("verdict")
    was_pass = prev_verdict == "PASS"
    was_neutral = prev_verdict == "NEUTRAL"
    is_core_traffic_page = path == "/" or known_traffic_clicks >= 10
    is_healthy_live_http = (
        http_status == 200
        and fetch_state in {"", "SUCCESSFUL", "PAGE_FETCH_STATE_UNSPECIFIED"}
        and robots_state in {"", "ALLOWED", "ROBOTS_TXT_STATE_UNSPECIFIED"}
        and indexing_state in {"", "INDEXING_ALLOWED", "INDEXING_STATE_UNSPECIFIED"}
    )
    is_transient_neutral = (
        verdict == "NEUTRAL"
        and path != "/"
        and is_healthy_live_http
        and not was_neutral
    )
    if verdict in {"FAIL", "NEUTRAL"} and (was_pass or is_core_traffic_page or was_neutral):
        if is_transient_neutral:
            p1_reasons.append(f"索引状态 {verdict} ({coverage_state or '未知原因'})")
            sig_parts.append(f"verdict:{verdict}")
        elif verdict == "FAIL" or was_pass or was_neutral or known_traffic_clicks >= 20:
            p0_reasons.append(f"索引状态 {verdict} ({coverage_state or '未知原因'})")
            sig_parts.append(f"verdict:{verdict}")
        else:
            p1_reasons.append(f"索引状态 {verdict} ({coverage_state or '未知原因'})")
            sig_parts.append(f"verdict:{verdict}")

    alerts: list[Alert] = []
    if p0_reasons or p1_reasons:
        severity = "P0" if p0_reasons else "P1"
        all_reasons = p0_reasons + p1_reasons
        diag_note = ""
        if fetch_state == "ACCESS_FORBIDDEN" and http_status == 200:
            diag_note = (
                "\n• 诊断提示: GSC 报 403 ACCESS_FORBIDDEN 但 GitHub 拨测返回 200，"
                "极大概率 Cloudflare WAF / Bot Fight Mode 拦截了 Googlebot (AS15169)。"
            )
        elif is_transient_neutral:
            diag_note = (
                "\n• 诊断提示: 本次实时 HTTP 自检返回 200 正常，若上次抓取正值发版或缓存刷新窗口，"
                "可能是临时性波动；若下轮仍未恢复将升级为 P0 告警。"
            )
        detail_lines = [
            f"URL: {url}",
            f"• 问题: {'；'.join(all_reasons)}",
        ]
        if coverage_state:
            detail_lines.append(f"• Coverage: {coverage_state}")
        if last_crawl:
            detail_lines.append(f"• 上次抓取(UTC): {last_crawl}")
        if diag_note:
            detail_lines.append(diag_note.lstrip("\n"))

        headline = f"GSC 抓取 403/受阻: {path}" if fetch_state == "ACCESS_FORBIDDEN" else f"GSC 收录巡检异常: {path}"
        alerts.append(Alert(
            key=f"gsc_inspect:status:{path}",
            probe="gsc_inspect",
            severity=severity,
            title=headline,
            detail="\n".join(detail_lines),
            url=url,
            signature="|".join(sorted(sig_parts)),
        ))

    if google_canonical and user_canonical and google_canonical.rstrip("/") != user_canonical.rstrip("/"):
        alerts.append(Alert(
            key=f"gsc_inspect:canonical:{path}",
            probe="gsc_inspect",
            severity="P1",
            title=f"GSC Canonical 不一致: {path}",
            detail=f"用户声明: {user_canonical}\nGoogle 选择: {google_canonical}",
            url=url,
            signature=f"canon:{google_canonical}",
        ))

    return alerts, new_cached


def check_gsc_inspection(
    gsc_service: Any,
    property_url: str,
    domain: str,
    urls: list[str],
    prev_verdicts: dict[str, Any],
    top_page_clicks: dict[str, float],
    http_url_results: dict[str, Any],
    delay: float = 0.25,
) -> dict[str, Any]:
    """Inspect target URLs via GSC URL Inspection API without leaking failed probes."""
    alerts: list[Alert] = []
    updated_verdicts = dict(prev_verdicts)
    inspected_ok_urls: set[str] = set()
    failed_urls: dict[str, str] = {}

    for idx, url in enumerate(urls):
        if idx > 0 and delay > 0:
            time.sleep(delay)
        try:
            resp = (
                gsc_service.urlInspection()
                .index()
                .inspect(
                    body={
                        "inspectionUrl": url,
                        "siteUrl": property_url,
                        "languageCode": "en",
                    }
                )
                .execute()
            )
            ir = resp.get("inspectionResult", {}) or {}
            http_status = (http_url_results.get(url) or {}).get("status")
            url_alerts, new_cached = evaluate_gsc_inspection_result(
                url=url,
                ir=ir,
                domain=domain,
                prev_verdict_info=prev_verdicts.get(url),
                known_traffic_clicks=top_page_clicks.get(url, 0.0),
                http_status=http_status,
            )
            alerts.extend(url_alerts)
            updated_verdicts[url] = new_cached
            inspected_ok_urls.add(url)
        except Exception as exc:
            failed_urls[url] = str(exc)
            print(f"⚠️ URL Inspection 失败 ({url}): {exc}", file=sys.stderr)

    return {
        "alerts": alerts,
        "updated_verdicts": updated_verdicts,
        "inspected_ok_urls": inspected_ok_urls,
        "failed_urls": failed_urls,
    }


# ──────────────────────────────────────────────
# Shared Weekday Baseline & Traffic Probes (GSC + GA4)
# ──────────────────────────────────────────────

def compute_same_weekday_baseline(
    series: dict[date, float],
    target_date: date,
    weeks: int = 4,
) -> tuple[float | None, str]:
    """
    Compute robust baseline for target_date using median of prior same-weekday values.
    Falls back to prior 7-day rolling median if fewer than 2 same-weekday points exist.
    """
    if not series:
        return None, "no_data"
    earliest = min(series.keys())

    same_weekday_vals: list[float] = []
    for k in range(1, weeks + 1):
        d = target_date - timedelta(days=7 * k)
        if d >= earliest:
            same_weekday_vals.append(float(series.get(d, 0.0)))

    if len(same_weekday_vals) >= 2:
        return float(statistics.median(same_weekday_vals)), f"same_weekday_median({len(same_weekday_vals)}w)"

    rolling_vals: list[float] = []
    for offset in range(1, 8):
        d = target_date - timedelta(days=offset)
        if d >= earliest:
            rolling_vals.append(float(series.get(d, 0.0)))
    if len(rolling_vals) >= 3:
        return float(statistics.median(rolling_vals)), f"rolling_7d_median({len(rolling_vals)}d)"
    return None, "insufficient_history"


def _resolve_complete_t2_date(
    today_utc: date,
    daily_totals: dict[date, dict[str, float]],
    first_incomplete_date: date | None,
) -> date | None:
    """
    Pick the latest complete T-2 (or fallback T-3) date for GSC checks.
    Skips dates >= firstIncompleteDate so incomplete GSC rollups do not false-alarm.
    """
    t2_date = today_utc - timedelta(days=2)
    if first_incomplete_date is None or t2_date < first_incomplete_date:
        return t2_date

    # Step back at most 1 day to T-3 if GSC already finalized T-3; otherwise wait for next run.
    t3_date = today_utc - timedelta(days=3)
    if t3_date < first_incomplete_date and t3_date in daily_totals:
        return t3_date
    return None


def evaluate_gsc_traffic_series(
    daily_totals: dict[date, dict[str, float]],
    daily_by_page: dict[str, dict[date, dict[str, float]]],
    domain: str,
    now_utc: datetime,
    warn_pct: float = 40.0,
    crit_pct: float = 60.0,
    min_site_clicks: float = 20.0,
    min_site_impressions: float = 200.0,
    min_page_clicks: float = 40.0,
    first_incomplete_date: date | None = None,
) -> list[Alert]:
    """Analyze GSC daily totals and top pages for sudden complete-day drops or T-1 collapse."""
    alerts: list[Alert] = []
    if not daily_totals:
        return alerts

    today_utc = now_utc.date()
    t1_date = today_utc - timedelta(days=1)
    eval_t2 = _resolve_complete_t2_date(today_utc, daily_totals, first_incomplete_date)

    click_series = {d: v.get("clicks", 0.0) for d, v in daily_totals.items()}
    imp_series = {d: v.get("impressions", 0.0) for d, v in daily_totals.items()}

    # 1. Site-wide T-2 (or latest complete) check against prior same-weekday median
    if eval_t2 is not None and eval_t2 in daily_totals:
        t2_clicks = click_series.get(eval_t2, 0.0)
        t2_imps = imp_series.get(eval_t2, 0.0)
        base_clicks, base_label = compute_same_weekday_baseline(click_series, eval_t2)
        base_imps, _ = compute_same_weekday_baseline(imp_series, eval_t2)

        click_drop = (
            ((base_clicks - t2_clicks) / base_clicks * 100.0)
            if (base_clicks is not None and base_clicks >= min_site_clicks)
            else 0.0
        )
        imp_drop = (
            ((base_imps - t2_imps) / base_imps * 100.0)
            if (base_imps is not None and base_imps >= min_site_impressions)
            else 0.0
        )
        worst_drop = max(click_drop, imp_drop)
        if worst_drop >= warn_pct:
            severity = "P0" if worst_drop >= crit_pct else "P1"
            alerts.append(Alert(
                key="gsc_traffic:site_t2_drop",
                probe="gsc_traffic",
                severity=severity,
                title=f"GSC 全站搜索流量暴跌 ({eval_t2.isoformat()})",
                detail=(
                    f"基线口径: {base_label}\n"
                    f"• 点击: {int(t2_clicks):,} vs 基线 {int(base_clicks or 0):,} (-{click_drop:.1f}%)\n"
                    f"• 曝光: {int(t2_imps):,} vs 基线 {int(base_imps or 0):,} (-{imp_drop:.1f}%)"
                ),
                url=f"https://{domain}/",
                signature=f"{eval_t2.isoformat()}:{severity}",
            ))

    # 2. Severe T-1 collapse guard (requires >= 10:00 UTC so early-morning partial release doesn't false-alarm)
    t1_base_clicks, t1_label = compute_same_weekday_baseline(click_series, t1_date)
    if now_utc.hour >= 10 and t1_base_clicks is not None and t1_base_clicks >= 100.0:
        t1_clicks = click_series.get(t1_date, 0.0)
        if (t1_date in daily_totals and t1_clicks <= t1_base_clicks * 0.05) or (
            t1_date not in daily_totals and now_utc.hour >= 14
        ):
            alerts.append(Alert(
                key="gsc_traffic:site_t1_collapse",
                probe="gsc_traffic",
                severity="P1",
                title=f"GSC 昨日数据疑似断崖归零 ({t1_date.isoformat()})",
                detail=(
                    f"截止目前 T-1 ({t1_date.isoformat()}) 点击仅 {int(t1_clicks):,}，"
                    f"对比 {t1_label} 基线 {int(t1_base_clicks):,} 跌幅超过 95%（含 GSC 新鲜统计滞后）。"
                ),
                url=f"https://{domain}/",
                signature=f"{t1_date.isoformat()}:t1_zero",
            ))

    # 3. Per-page check on eval_t2, guarded against byPage dimension rollup lag
    if eval_t2 is not None and daily_by_page:
        site_day_clicks = click_series.get(eval_t2, 0.0)
        page_day_clicks = sum(
            page_series.get(eval_t2, {}).get("clicks", 0.0)
            for page_series in daily_by_page.values()
        )
        by_page_ready = (
            site_day_clicks < min_site_clicks
            or page_day_clicks >= site_day_clicks * MIN_BYPAGE_COVERAGE_RATIO
        )
    else:
        by_page_ready = False

    if eval_t2 is not None and by_page_ready:
        for page_url, page_series in daily_by_page.items():
            p_clicks = {d: v.get("clicks", 0.0) for d, v in page_series.items()}
            p_imps = {d: v.get("impressions", 0.0) for d, v in page_series.items()}
            p_pos = {d: v.get("position", 0.0) for d, v in page_series.items()}

            base_p_clicks, _ = compute_same_weekday_baseline(p_clicks, eval_t2)
            if base_p_clicks is None or base_p_clicks < min_page_clicks:
                continue

            curr_clicks = p_clicks.get(eval_t2, 0.0)
            drop_pct = (base_p_clicks - curr_clicks) / base_p_clicks * 100.0

            # Check position drop only when impressions had real volume
            pos_jump = 0.0
            base_p_imps, _ = compute_same_weekday_baseline(p_imps, eval_t2)
            base_pos_vals = [
                p_pos[eval_t2 - timedelta(days=7 * k)]
                for k in range(1, 5)
                if (eval_t2 - timedelta(days=7 * k)) in p_pos and p_pos[eval_t2 - timedelta(days=7 * k)] > 0
            ]
            curr_pos = p_pos.get(eval_t2, 0.0)
            base_pos = float(statistics.median(base_pos_vals)) if base_pos_vals else 0.0
            if base_pos > 0 and curr_pos > 0 and (base_p_imps or 0) >= 100:
                pos_jump = curr_pos - base_pos

            path = short_path(page_url, domain)
            if drop_pct >= warn_pct or (pos_jump >= 5.0 and drop_pct >= 25.0):
                severity = "P0" if (drop_pct >= crit_pct or curr_clicks == 0) else "P1"
                pos_note = (
                    f"\n• 均位变化: {base_pos:.1f} → {curr_pos:.1f} (下降 {pos_jump:.1f} 位)"
                    if pos_jump >= 3.0 else ""
                )
                alerts.append(Alert(
                    key=f"gsc_traffic:page:{path}",
                    probe="gsc_traffic",
                    severity=severity,
                    title=f"核心页流量下跌 {drop_pct:.0f}%: {path}",
                    detail=(
                        f"日期 {eval_t2.isoformat()} 点击 {int(curr_clicks):,} vs 同星期基线 {int(base_p_clicks):,}{pos_note}"
                    ),
                    url=page_url,
                    signature=f"{eval_t2.isoformat()}:{severity}",
                ))

    return alerts


def check_gsc_traffic(
    gsc_service: Any,
    property_url: str,
    domain: str,
    now_utc: datetime,
    warn_pct: float = 40.0,
    crit_pct: float = 60.0,
) -> list[Alert]:
    """Query GSC daily site & page series (dataState=all) and detect drops."""
    today_utc = now_utc.date()
    start_str = (today_utc - timedelta(days=34)).strftime("%Y-%m-%d")
    end_str = (today_utc - timedelta(days=1)).strftime("%Y-%m-%d")

    site_resp = gsc_service.searchanalytics().query(
        siteUrl=property_url,
        body={
            "startDate": start_str,
            "endDate": end_str,
            "dimensions": ["date"],
            "rowLimit": 100,
            "dataState": "all",
        },
    ).execute()

    site_incomplete = _parse_iso_date((site_resp.get("metadata") or {}).get("firstIncompleteDate"))

    daily_totals: dict[date, dict[str, float]] = {}
    for row in site_resp.get("rows", []) or []:
        keys = row.get("keys") or []
        if not keys:
            continue
        d = datetime.strptime(keys[0], "%Y-%m-%d").date()
        daily_totals[d] = {
            "clicks": float(row.get("clicks", 0) or 0),
            "impressions": float(row.get("impressions", 0) or 0),
            "position": float(row.get("position", 0) or 0),
        }

    daily_by_page: dict[str, dict[date, dict[str, float]]] = {}
    page_incomplete: date | None = None
    for page_idx in range(GSC_MAX_PAGE_PAGES):
        page_resp = gsc_service.searchanalytics().query(
            siteUrl=property_url,
            body={
                "startDate": start_str,
                "endDate": end_str,
                "dimensions": ["date", "page"],
                "rowLimit": GSC_PAGE_ROW_LIMIT,
                "startRow": page_idx * GSC_PAGE_ROW_LIMIT,
                "dataState": "all",
            },
        ).execute()
        if page_idx == 0:
            page_incomplete = _parse_iso_date(
                (page_resp.get("metadata") or {}).get("firstIncompleteDate")
            )
        rows = page_resp.get("rows", []) or []
        for row in rows:
            keys = row.get("keys") or []
            if len(keys) < 2:
                continue
            d = datetime.strptime(keys[0], "%Y-%m-%d").date()
            norm_url = normalize_target_url(str(keys[1]), domain)
            if not norm_url:
                continue
            clicks_val = float(row.get("clicks", 0) or 0)
            imps_val = float(row.get("impressions", 0) or 0)
            pos_val = float(row.get("position", 0) or 0)

            bucket = daily_by_page.setdefault(norm_url, {})
            existing = bucket.get(d)
            if existing is None:
                bucket[d] = {
                    "clicks": clicks_val,
                    "impressions": imps_val,
                    "position": pos_val,
                }
            else:
                tot_imps = existing["impressions"] + imps_val
                existing["position"] = (
                    (existing["position"] * existing["impressions"] + pos_val * imps_val) / tot_imps
                    if tot_imps > 0
                    else existing["position"]
                )
                existing["clicks"] += clicks_val
                existing["impressions"] = tot_imps

        if len(rows) < GSC_PAGE_ROW_LIMIT:
            break

    incomplete_dates = [d for d in (site_incomplete, page_incomplete) if d is not None]
    first_incomplete_date = min(incomplete_dates) if incomplete_dates else None

    return evaluate_gsc_traffic_series(
        daily_totals=daily_totals,
        daily_by_page=daily_by_page,
        domain=domain,
        now_utc=now_utc,
        warn_pct=warn_pct,
        crit_pct=crit_pct,
        first_incomplete_date=first_incomplete_date,
    )


def evaluate_ga4_daily_sessions(
    daily_sessions: dict[date, float],
    domain: str,
    now_utc: datetime,
    warn_pct: float = 40.0,
    crit_pct: float = 60.0,
    min_sessions: float = 20.0,
) -> list[Alert]:
    """
    Evaluate GA4 daily organic sessions against same-weekday baseline.
    Always checks T-2; checks T-1 once now_utc.hour >= 10 (avoiding US timezone partial-day bias).
    """
    alerts: list[Alert] = []
    if not daily_sessions:
        return alerts

    today_utc = now_utc.date()
    check_dates = [today_utc - timedelta(days=2)]
    if now_utc.hour >= 10:
        check_dates.insert(0, today_utc - timedelta(days=1))

    for target_d in check_dates:
        base_sessions, base_label = compute_same_weekday_baseline(daily_sessions, target_d)
        if base_sessions is None or base_sessions < min_sessions:
            continue
        curr = float(daily_sessions.get(target_d, 0.0))
        drop_pct = (base_sessions - curr) / base_sessions * 100.0
        if drop_pct >= warn_pct:
            severity = "P0" if drop_pct >= crit_pct else "P1"
            label = "昨日(T-1)" if target_d == (today_utc - timedelta(days=1)) else "前日(T-2)"
            alerts.append(Alert(
                key="ga4:organic_drop",
                probe="ga4",
                severity=severity,
                title=f"GA4 自然流量异常下跌 {drop_pct:.0f}% ({label} {target_d.isoformat()})",
                detail=(
                    f"Organic Search 会话数: {int(curr):,} vs {base_label} 基线 {int(base_sessions):,} (-{drop_pct:.1f}%)"
                ),
                url=f"https://{domain}/",
                signature=f"{target_d.isoformat()}:{severity}",
            ))
            break  # Report most recent affected day cleanly under one dedup key

    return alerts


def check_ga4_traffic(
    ga4_client: Any,
    ga4_property_id: str,
    domain: str,
    now_utc: datetime,
    warn_pct: float = 40.0,
    crit_pct: float = 60.0,
) -> list[Alert]:
    """Query GA4 Data API v1beta daily organic sessions and detect anomalies."""
    from google.analytics.data_v1beta.types import (
        DateRange,
        Dimension,
        Filter,
        FilterExpression,
        Metric,
        OrderBy,
        RunReportRequest,
    )

    prop = ga4_property_id if ga4_property_id.startswith("properties/") else f"properties/{ga4_property_id}"
    today_utc = now_utc.date()
    start_str = (today_utc - timedelta(days=34)).strftime("%Y-%m-%d")
    end_str = (today_utc - timedelta(days=1)).strftime("%Y-%m-%d")

    request = RunReportRequest(
        property=prop,
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="sessions")],
        date_ranges=[DateRange(start_date=start_str, end_date=end_str)],
        dimension_filter=FilterExpression(
            filter=Filter(
                field_name="sessionDefaultChannelGroup",
                string_filter=Filter.StringFilter(
                    match_type=Filter.StringFilter.MatchType.EXACT,
                    value="Organic Search",
                ),
            )
        ),
        order_bys=[OrderBy(dimension=OrderBy.DimensionOrderBy(dimension_name="date"))],
        limit=60,
    )
    response = ga4_client.run_report(request)
    daily_sessions: dict[date, float] = {}
    for row in response.rows:
        raw_d = row.dimension_values[0].value
        d = datetime.strptime(raw_d, "%Y%m%d").date()
        daily_sessions[d] = float(row.metric_values[0].value)

    return evaluate_ga4_daily_sessions(
        daily_sessions=daily_sessions,
        domain=domain,
        now_utc=now_utc,
        warn_pct=warn_pct,
        crit_pct=crit_pct,
    )


# ──────────────────────────────────────────────
# Alert State Reconciliation (Deduplication & Recovery)
# ──────────────────────────────────────────────

def _parse_iso(ts: str) -> datetime:
    cleaned = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(cleaned)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def reconcile_alerts(
    current_alerts: list[Alert],
    prev_state: dict[str, Any],
    now_utc: datetime,
    completed_probes: set[str],
    failed_inspect_urls: set[str],
    cooldown_hours: float = 24.0,
) -> tuple[list[Alert], list[dict[str, Any]], dict[str, Any]]:
    """
    Compare current findings against active_alerts state.

    Returns:
      - to_notify: list[Alert] (new, escalated, signature-changed, or cooldown elapsed)
      - resolved: list[dict] (previously active alerts confirmed cleared by a completed probe)
      - new_active_map: dict[str, dict] updated active_alerts snapshot
    """
    prev_active: dict[str, dict[str, Any]] = dict(prev_state.get("active_alerts", {}) or {})
    curr_by_key = {a.key: a for a in current_alerts}

    to_notify: list[Alert] = []
    resolved: list[dict[str, Any]] = []
    new_active_map: dict[str, dict[str, Any]] = {}

    # 1. Process currently active findings
    for key, alert in curr_by_key.items():
        old = prev_active.get(key)
        now_iso = now_utc.isoformat().replace("+00:00", "Z")
        should_notify = False

        if not old:
            should_notify = True
            first_seen = now_iso
        else:
            first_seen = old.get("first_seen", now_iso)
            old_rank = SEVERITY_RANK.get(old.get("severity", "P1"), 1)
            new_rank = SEVERITY_RANK.get(alert.severity, 1)
            last_notified = _parse_iso(old.get("last_notified", now_iso))
            elapsed_hours = (now_utc - last_notified).total_seconds() / 3600.0
            sig_changed = bool(
                alert.signature and old.get("signature") and alert.signature != old.get("signature")
            )
            if new_rank > old_rank or sig_changed or elapsed_hours >= cooldown_hours:
                should_notify = True

        if should_notify:
            to_notify.append(alert)
            last_notified_str = now_iso
        else:
            last_notified_str = old.get("last_notified", now_iso)

        entry = asdict(alert)
        entry["first_seen"] = first_seen
        entry["last_notified"] = last_notified_str
        new_active_map[key] = entry

    # 2. Detect resolved alerts ONLY for probes/URLs that succeeded in this run
    for key, old in prev_active.items():
        if key in curr_by_key:
            continue
        probe = old.get("probe", "")
        url = old.get("url", "")
        probe_ok = probe in completed_probes
        url_inspect_failed = probe == "gsc_inspect" and url in failed_inspect_urls

        if probe_ok and not url_inspect_failed:
            resolved.append(old)
        else:
            # Preserve unverified alert state until probe succeeds cleanly
            new_active_map[key] = old

    return to_notify, resolved, new_active_map


# ──────────────────────────────────────────────
# Telegram HTML Formatting & Delivery
# ──────────────────────────────────────────────

def format_telegram_messages(
    domain: str,
    to_notify: list[Alert],
    resolved: list[dict[str, Any]],
    probe_errors: list[str],
    summary_stats: dict[str, Any],
    force_summary: bool = False,
) -> list[str]:
    """Format alerts, resolutions, or optional healthy receipt into HTML Telegram chunks."""
    has_events = bool(to_notify or resolved or probe_errors)
    if not has_events and not force_summary:
        return []

    esc = html.escape
    header = (
        f"🚨 <b>SEO 健康告警 · {esc(domain)}</b>"
        if to_notify
        else (
            f"🟢 <b>SEO 告警已恢复 · {esc(domain)}</b>"
            if resolved
            else (
                f"⚠️ <b>SEO 监控探针报错 · {esc(domain)}</b>"
                if probe_errors
                else f"✅ <b>SEO 健康巡检正常 · {esc(domain)}</b>"
            )
        )
    )

    blocks: list[str] = [
        f"{header}\n<i>{esc(datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'))}</i>"
    ]

    if to_notify:
        sorted_alerts = sorted(to_notify, key=lambda a: SEVERITY_RANK.get(a.severity, 0), reverse=True)
        for alert in sorted_alerts:
            badge = "🔴 [P0 紧急]" if alert.severity == "P0" else "🟡 [P1 警告]"
            block = f"{badge} <b>{esc(alert.title)}</b>\n{esc(alert.detail)}"
            blocks.append(block)

    if resolved:
        lines = ["<b>🟢 以下异常已确认恢复：</b>"]
        for item in resolved:
            lines.append(f"• {esc(str(item.get('title', item.get('key', ''))))}")
        blocks.append("\n".join(lines))

    if probe_errors:
        err_lines = ["<b>⚠️ 部分监控探针执行失败（未自动清除相关旧告警）：</b>"]
        for err in probe_errors:
            err_lines.append(f"• {esc(err)}")
        blocks.append("\n".join(err_lines))

    if not has_events and force_summary:
        probes_label = summary_stats.get("executed_probes_label") or "HTTP 自检"
        blocks.append(
            f"• 检查维度: {esc(str(probes_label))}\n"
            f"• 巡检核心页数: {esc(str(summary_stats.get('inspected_urls_count', 0)))}\n"
            f"• HTTP 200 通过: {esc(str(summary_stats.get('http_ok_count', 0)))}\n"
            f"• Sitemap URL 数: {esc(str(summary_stats.get('sitemap_submitted', 0)))}"
        )

    # Chunk cleanly under Telegram 4096-char limit
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= MAX_TELEGRAM_CHARS:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = block[:MAX_TELEGRAM_CHARS]
    if current:
        chunks.append(current)
    return chunks


def send_telegram_messages(
    bot_token: str,
    chat_id: str,
    messages: list[str],
    timeout: int = 15,
) -> None:
    """Send HTML-formatted Telegram messages and verify Telegram API returns ok=true."""
    if not messages:
        return
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for msg in messages:
        resp = requests.post(
            api_url,
            json={
                "chat_id": chat_id,
                "text": msg,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        try:
            payload = resp.json()
        except ValueError:
            payload = {"ok": False, "description": resp.text[:200]}
        if resp.status_code != 200 or not payload.get("ok"):
            raise RuntimeError(
                f"Telegram 推送失败 (HTTP {resp.status_code}): {payload.get('description', resp.text[:200])}"
            )


# ──────────────────────────────────────────────
# Main Runner
# ──────────────────────────────────────────────

def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"version": 1, "active_alerts": {}, "url_verdicts": {}}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"⚠️ 状态文件解析失败，重置为空状态: {exc}", file=sys.stderr)
        return {"version": 1, "active_alerts": {}, "url_verdicts": {}}


def run_monitor(args: argparse.Namespace) -> int:
    domain = clean_domain(args.domain)
    if not domain:
        print("❌ 无效域名", file=sys.stderr)
        return 1

    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()
    property_url = args.property or f"sc-domain:{domain}"
    state_path = Path(args.state)
    state = load_state(state_path)
    prev_active = state.get("active_alerts", {}) or {}
    prev_verdicts = state.get("url_verdicts", {}) or {}

    sa_info = load_service_account_info(args.service_account)
    gsc_service = None
    ga4_client = None
    need_ga4 = bool(args.ga4_property)

    if sa_info:
        try:
            gsc_service, ga4_client = build_google_services(sa_info, need_ga4=need_ga4)
        except Exception as exc:
            print(f"❌ 初始化 Google 客户端失败: {exc}", file=sys.stderr)
            return 1
    elif not args.skip_google:
        print("❌ 未找到 Google Service Account 凭证", file=sys.stderr)
        return 1

    # 1. Discover URLs to monitor
    explicit_urls = [u.strip() for u in (args.urls or "").split(",") if u.strip()]
    snapshot_top_urls = (
        extract_top_pages_from_snapshot(Path(args.snapshot), domain, limit=10)
        if args.snapshot
        else []
    )
    live_top_clicks: dict[str, float] = {}
    if gsc_service is not None:
        try:
            live_top_clicks = fetch_gsc_top_pages(gsc_service, property_url, domain, today_utc, limit=10)
        except Exception as exc:
            print(f"⚠️ 拉取 GSC Top 页面失败（降级仅使用配置列表+快照）: {exc}", file=sys.stderr)

    target_urls = build_target_urls(
        domain=domain,
        explicit_urls=explicit_urls,
        snapshot_top_urls=snapshot_top_urls,
        live_top_urls=list(live_top_clicks.keys()),
        active_alerts=prev_active,
        max_urls=args.max_inspect_urls,
    )
    print(f"🔎 监控站点: {domain} | 目标 URL ({len(target_urls)}): {', '.join(short_path(u, domain) for u in target_urls)}")

    all_alerts: list[Alert] = []
    completed_probes: set[str] = set()
    probe_errors: list[str] = []
    failed_inspect_urls: set[str] = set()

    # 2. GSC Sitemaps (run before HTTP check to collect submitted sitemap URLs too)
    sitemap_urls: list[str] = []
    sitemap_submitted: int = state.get("sitemap_submitted_baseline") or 0
    if gsc_service is not None:
        try:
            sm_res = check_gsc_sitemaps(
                gsc_service,
                property_url,
                domain,
                prev_submitted=state.get("sitemap_submitted_baseline"),
            )
            all_alerts.extend(sm_res["alerts"])
            sitemap_urls = sm_res["sitemap_urls"]
            if sm_res["total_submitted"] > 0:
                sitemap_submitted = sm_res["total_submitted"]
            completed_probes.add("gsc_sitemaps")
        except Exception as exc:
            probe_errors.append(f"GSC Sitemaps 失败: {exc}")

    # 3. HTTP Self-Check
    http_res = check_http_targets(domain, target_urls, sitemap_urls=sitemap_urls)
    all_alerts.extend(http_res["alerts"])
    completed_probes.add("http")

    # 4. GSC URL Inspection
    updated_verdicts = dict(prev_verdicts)
    if gsc_service is not None:
        inspect_res = check_gsc_inspection(
            gsc_service=gsc_service,
            property_url=property_url,
            domain=domain,
            urls=target_urls,
            prev_verdicts=prev_verdicts,
            top_page_clicks=live_top_clicks,
            http_url_results=http_res["url_results"],
        )
        all_alerts.extend(inspect_res["alerts"])
        updated_verdicts = inspect_res["updated_verdicts"]
        failed_inspect_urls = set(inspect_res["failed_urls"].keys())
        if inspect_res["inspected_ok_urls"]:
            completed_probes.add("gsc_inspect")
        if inspect_res["failed_urls"]:
            probe_errors.append(
                f"GSC URL Inspection 有 {len(inspect_res['failed_urls'])} 个页面请求失败"
            )

    # 5. GSC Traffic & Ranking Anomaly
    if gsc_service is not None:
        try:
            gsc_alerts = check_gsc_traffic(
                gsc_service=gsc_service,
                property_url=property_url,
                domain=domain,
                now_utc=now_utc,
                warn_pct=args.warn_pct,
                crit_pct=args.crit_pct,
            )
            all_alerts.extend(gsc_alerts)
            completed_probes.add("gsc_traffic")
        except Exception as exc:
            probe_errors.append(f"GSC 流量分析失败: {exc}")

    # 6. GA4 Organic Traffic Anomaly
    if ga4_client is not None and args.ga4_property:
        try:
            ga4_alerts = check_ga4_traffic(
                ga4_client=ga4_client,
                ga4_property_id=args.ga4_property,
                domain=domain,
                now_utc=now_utc,
                warn_pct=args.warn_pct,
                crit_pct=args.crit_pct,
            )
            all_alerts.extend(ga4_alerts)
            completed_probes.add("ga4")
        except Exception as exc:
            probe_errors.append(f"GA4 流量分析失败: {exc}")

    # 7. Deduplicate, compute recoveries & update state
    to_notify, resolved, new_active_map = reconcile_alerts(
        current_alerts=all_alerts,
        prev_state=state,
        now_utc=now_utc,
        completed_probes=completed_probes,
        failed_inspect_urls=failed_inspect_urls,
        cooldown_hours=args.cooldown_hours,
    )

    probe_display_names = [
        label
        for probe_key, label in (
            ("http", "HTTP 自检"),
            ("gsc_sitemaps", "GSC Sitemaps"),
            ("gsc_inspect", "URL Inspection"),
            ("gsc_traffic", "GSC 流量"),
            ("ga4", "GA4"),
        )
        if probe_key in completed_probes
    ]
    http_ok_count = sum(1 for r in http_res["url_results"].values() if r.get("status") == 200)
    summary_stats = {
        "executed_probes_label": " / ".join(probe_display_names) if probe_display_names else "无",
        "inspected_urls_count": len(target_urls),
        "http_ok_count": http_ok_count,
        "sitemap_submitted": sitemap_submitted,
        "active_alert_count": len(new_active_map),
        "new_notify_count": len(to_notify),
        "resolved_count": len(resolved),
    }

    messages = format_telegram_messages(
        domain=domain,
        to_notify=to_notify,
        resolved=resolved,
        probe_errors=probe_errors,
        summary_stats=summary_stats,
        force_summary=args.send_summary,
    )

    if messages:
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_TARGET_CHAT", "").strip()
        if args.dry_run:
            print("\n──── Telegram 消息预览 (Dry Run) ────")
            for msg in messages:
                print(msg)
                print("────────────────────────────────────────")
        elif not bot_token or not chat_id:
            print("❌ 需要发送告警但未配置 TELEGRAM_BOT_TOKEN / TELEGRAM_TARGET_CHAT", file=sys.stderr)
            return 1
        else:
            send_telegram_messages(bot_token, chat_id, messages)
            print(f"✅ 已推送 {len(messages)} 条 Telegram 通知")
    else:
        print(f"✅ 站点 {domain} 全部巡检正常且无状态变化，保持静默。")

    new_state = {
        "version": 1,
        "domain": domain,
        "last_run_at": now_utc.isoformat().replace("+00:00", "Z"),
        "sitemap_submitted_baseline": max(
            sitemap_submitted, state.get("sitemap_submitted_baseline") or 0
        ) if "gsc_sitemaps:submitted_drop" not in new_active_map else (state.get("sitemap_submitted_baseline") or sitemap_submitted),
        "url_verdicts": updated_verdicts,
        "active_alerts": new_active_map,
    }

    if not args.dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.out_json:
        report_payload = {
            "domain": domain,
            "run_at": new_state["last_run_at"],
            "target_urls": target_urls,
            "summary": summary_stats,
            "all_alerts": [asdict(a) for a in all_alerts],
            "notified_alerts": [asdict(a) for a in to_notify],
            "resolved_alerts": resolved,
            "probe_errors": probe_errors,
        }
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SEO 异常健康监控与 Telegram 告警")
    parser.add_argument("--domain", required=True, help="站点域名，如 krillion-game.app")
    parser.add_argument("--property", default="", help="GSC Property，默认 sc-domain:<domain>")
    parser.add_argument("--ga4-property", default="", help="GA4 Property ID（可选）")
    parser.add_argument("--urls", default="", help="附加核心监控路径或 URL，逗号分隔")
    parser.add_argument("--snapshot", default="", help="周报 GSC 快照路径（自动提取 Top 10 页）")
    parser.add_argument("--max-inspect-urls", type=int, default=15, help="单次最多巡检 URL 数")
    parser.add_argument("--warn-pct", type=float, default=40.0, help="流量跌幅 P1 警告阈值百分比")
    parser.add_argument("--crit-pct", type=float, default=60.0, help="流量跌幅 P0 紧急阈值百分比")
    parser.add_argument("--cooldown-hours", type=float, default=24.0, help="同告警重推冷却小时数")
    parser.add_argument("--service-account", default="", help="Service Account JSON 路径（可选）")
    parser.add_argument("--state", default=".seo-monitor-cache/monitor-state.json", help="状态文件路径")
    parser.add_argument("--out-json", default="", help="可选：输出详细 JSON 巡检报告")
    parser.add_argument("--send-summary", action="store_true", help="即使全绿也发送一条回执到 Telegram")
    parser.add_argument("--dry-run", action="store_true", help="仅打印结果，不发消息也不写 state")
    parser.add_argument("--skip-google", action="store_true", help="仅做 HTTP 自检（本地调试用）")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    return run_monitor(args)


if __name__ == "__main__":
    sys.exit(main())
