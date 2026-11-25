#!/usr/bin/env python3
"""Reusable Similarweb Keyword Generator fetcher & offline analyzer.

Supports BOTH official Similarweb (`https://pro.similarweb.com`) and any
authenticated mirror host (`SIMILARWEB_MIRROR_URL` or `--base-url`) by executing
same-origin API requests inside the user's real Chrome session via `opencli`.

Zero third-party Python dependencies (standard library only).

Security & compliance:
- Never stores credentials or bypasses authentication/CAPTCHA gates.
- If the browser tab is not signed in or presents an anti-bot challenge, the
  script fails closed and asks the user to sign in manually in Chrome.
- Exported JSON/CSV never records private mirror IPs/hostnames (records only
  `sourceType: "official" | "mirror"`).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlparse

OFFICIAL_ORIGIN = "https://pro.similarweb.com"

COUNTRY_NAMES: dict[int, str] = {
    999: "Worldwide",
    840: "United States",
    826: "United Kingdom",
    124: "Canada",
    36: "Australia",
    276: "Germany",
    250: "France",
    392: "Japan",
    410: "South Korea",
    356: "India",
    76: "Brazil",
    724: "Spain",
    380: "Italy",
    528: "Netherlands",
    484: "Mexico",
    608: "Philippines",
    702: "Singapore",
    158: "Taiwan",
    344: "Hong Kong",
}

CSV_COLUMNS: list[str] = [
    "keyword",
    "windowVolume",
    "averageVolume",
    "clicks",
    "primaryIntent",
    "cpc",
    "difficulty",
    "latestZeroClicks",
    "leadingSite",
    "volumeChangePresentation",
]

STOPWORDS: set[str] = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "get",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "play",
    "the",
    "to",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "with",
    "you",
    "your",
}


class SimilarwebError(RuntimeError):
    """Actionable failure during Similarweb session or API execution."""


def slugify_seed(seed: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", seed.strip().lower()).strip("-")
    return slug or "keywords"


def normalize_origin(url: str) -> str:
    raw = url.strip()
    if not raw:
        raise SimilarwebError("Base URL cannot be empty.")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SimilarwebError(f"Invalid base URL: {url!r}")
    return f"{parsed.scheme}://{parsed.netloc}"


def classify_source(origin: str) -> str:
    host = (urlparse(origin).hostname or "").lower()
    if host == "pro.similarweb.com" or host.endswith(".similarweb.com"):
        return "official"
    return "mirror"


def resolve_target_origin(
    target: str,
    base_url: str | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Resolve target origin; returns None when `--bind` should inspect active tab."""
    env_map = env if env is not None else os.environ
    if base_url:
        return normalize_origin(base_url)
    if target == "bind":
        return None
    if target == "official":
        return OFFICIAL_ORIGIN
    if target == "mirror":
        mirror = env_map.get("SIMILARWEB_MIRROR_URL") or env_map.get("SIMILARWEB_BASE_URL")
        if not mirror:
            raise SimilarwebError(
                "Target 'mirror' requires --base-url or SIMILARWEB_MIRROR_URL environment variable "
                "(or use --target bind with your mirror tab already open in Chrome)."
            )
        return normalize_origin(mirror)
    raise SimilarwebError(f"Unsupported target mode: {target!r}")


def format_sw_date(d: dt.date) -> str:
    return f"{d.year:04d}|{d.month:02d}|{d.day:02d}"


def compute_date_window(
    from_date: str | None = None,
    to_date: str | None = None,
    today: dt.date | None = None,
    lag_days: int = 3,
    window_days: int = 28,
) -> tuple[dt.date, dt.date]:
    """Compute inclusive (start_date, end_date) for Similarweb's 28-day window."""
    if from_date and to_date:
        start = dt.date.fromisoformat(from_date)
        end = dt.date.fromisoformat(to_date)
        if start > end:
            raise SimilarwebError(f"--from ({from_date}) must be <= --to ({to_date}).")
        return start, end
    if from_date or to_date:
        raise SimilarwebError("Specify both --from and --to together (YYYY-MM-DD).")
    ref = today or dt.date.today()
    end = ref - dt.timedelta(days=lag_days)
    start = end - dt.timedelta(days=window_days - 1)
    return start, end


def build_suggest_path(
    seed: str,
    country: int,
    start_date: dt.date,
    end_date: dt.date,
    *,
    query_type: str = "Broad",
    label: str | None = None,
    sort: str = "windowVolume",
    rows_per_page: int = 100,
    page: int = 1,
    latest: str = "28d",
) -> str:
    """Build relative `/api/KeywordGenerator/google/suggest?...` path."""
    params: list[tuple[str, str]] = [
        ("keyword", seed),
        ("country", str(country)),
        ("from", format_sw_date(start_date)),
        ("to", format_sw_date(end_date)),
        ("isWindow", "true"),
        ("websource", "Total"),
        ("sort", sort),
        ("asc", "false"),
        ("rowsPerPage", str(rows_per_page)),
        ("type", query_type),
    ]
    if label:
        params.append(("label", label))
    if latest:
        params.append(("latest", latest))
    params.append(("page", str(page)))
    return "/api/KeywordGenerator/google/suggest?" + urlencode(params, quote_via=quote)


def build_generator_ui_url(origin: str, seed: str, country: int, duration: str = "28d") -> str:
    """Build the Keyword Generator UI landing URL (for owned-tab open mode)."""
    kw = quote(seed, safe="")
    return (
        f"{origin}/#/digitalsuite/research/keywords/generator/"
        f"{kw}/{country}/{duration}?webSource=Total"
    )


def extract_json_payload(stdout: str) -> Any:
    """Parse JSON returned by `opencli browser <session> eval`, stripping CLI notices."""
    text = stdout.strip()
    if not text:
        raise SimilarwebError("Empty response from opencli browser eval.")
    # Fast path: entire stdout is valid JSON.
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # Strip any update banners or non-JSON lines around the JSON block.
        start_idx = min(
            (i for i in (text.find("{"), text.find("["), text.find('"')) if i != -1),
            default=-1,
        )
        if start_idx == -1:
            raise SimilarwebError(f"Non-JSON output from opencli: {text[:240]}")
        decoder = json.JSONDecoder()
        try:
            parsed, _ = decoder.raw_decode(text[start_idx:])
        except json.JSONDecodeError as exc:
            raise SimilarwebError(f"Failed to decode opencli JSON: {exc}; raw={text[:240]}") from exc

    # opencli eval wraps return value as {"result": ...} or returns JSON string.
    if isinstance(parsed, dict) and "result" in parsed and len(parsed) <= 3:
        parsed = parsed["result"]
    if isinstance(parsed, str):
        s = parsed.strip()
        if (s.startswith("{") and s.endswith("}")) or (s.startswith("[") and s.endswith("]")):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return parsed
    return parsed


def build_fetch_js(relative_path: str) -> str:
    """Return an async IIFE that runs same-origin POST fetch inside the browser tab."""
    path_json = json.dumps(relative_path)
    return (
        "(async () => {"
        f" const url = window.location.origin + {path_json};"
        " const resp = await fetch(url, {"
        "   method: 'POST',"
        "   credentials: 'include',"
        "   headers: { 'Accept': 'application/json, text/plain, */*', 'X-Requested-With': 'XMLHttpRequest' }"
        " });"
        " const ct = resp.headers.get('content-type') || '';"
        " const bodyText = await resp.text();"
        " let data = null;"
        " if (ct.includes('json') || bodyText.trim().startsWith('{')) {"
        "   try { data = JSON.parse(bodyText); } catch (_) {}"
        " }"
        " return JSON.stringify({"
        "   ok: resp.ok,"
        "   status: resp.status,"
        "   origin: window.location.origin,"
        "   href: window.location.href,"
        "   contentType: ct,"
        "   data: data,"
        "   snippet: data ? '' : bodyText.slice(0, 300)"
        " });"
        "})()"
    )


def build_probe_js() -> str:
    """Return an IIFE checking the current tab's origin, title, and login/challenge signals."""
    return (
        "(() => {"
        " const href = window.location.href || '';"
        " const title = document.title || '';"
        " const body = (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 800);"
        " return JSON.stringify({ origin: window.location.origin, href, title, bodySnippet: body });"
        "})()"
    )


def detect_access_block(status: int, content_type: str, href: str, snippet: str) -> str | None:
    """Detect auth expiry, login redirects, or anti-bot / CAPTCHA pages (fail closed)."""
    low_href = (href or "").lower()
    low_snip = (snippet or "").lower()
    if status in (401, 403):
        return (
            f"HTTP {status} from Similarweb. Your browser session may be signed out or blocked. "
            "Please sign in (or complete any verification) directly in your Chrome tab, then retry."
        )
    challenge_markers = (
        "verify you are human",
        "captcha",
        "cloudflare",
        "turnstile",
        "just a moment",
        "access denied",
        "perimeterx",
        "px-captcha",
    )
    if any(m in low_snip for m in challenge_markers):
        return (
            "Anti-bot / human verification challenge detected in Chrome. "
            "Please complete verification manually in Chrome, then re-run with --target bind."
        )
    login_markers = ("/login", "/signin", "account.similarweb.com", "log in to similarweb")
    if any(m in low_href for m in login_markers) or ("sign in" in low_snip and "password" in low_snip):
        return (
            "Browser tab is on a login page. Please sign in manually in Chrome first, "
            "then re-run with --target bind."
        )
    if status >= 400:
        return f"Similarweb API returned HTTP {status} ({content_type}): {snippet[:180]}"
    return None


def dedupe_and_sort_records(records: list[dict[str, Any]], limit: int | None = None) -> list[dict[str, Any]]:
    """Deduplicate keyword rows case-insensitively and sort by windowVolume descending."""
    seen: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for rec in records:
        kw = str(rec.get("keyword") or "").strip()
        if not kw:
            continue
        key = kw.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(rec)
    cleaned.sort(
        key=lambda r: (
            float(r.get("windowVolume") or 0),
            float(r.get("averageVolume") or 0),
            float(r.get("clicks") or 0),
        ),
        reverse=True,
    )
    return cleaned[:limit] if limit is not None else cleaned


def write_outputs(
    out_dir: Path,
    prefix: str,
    payload: dict[str, Any],
) -> tuple[Path, Path]:
    """Write standardized `.json` and `.csv` exports and return `(json_path, csv_path)`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{prefix}.json"
    csv_path = out_dir / f"{prefix}.csv"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    rows = payload.get("phraseMatch") or []
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: row.get(col, "") for col in CSV_COLUMNS})

    return json_path, csv_path


def cluster_keywords(
    records: list[dict[str, Any]],
    seed: str,
    top_clusters: int = 12,
) -> list[dict[str, Any]]:
    """Group phrase-match keywords by their dominant non-seed modifier token."""
    seed_tokens = set(re.findall(r"[a-z0-9]+", seed.lower()))
    buckets: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"modifier": "", "totalVolume": 0, "count": 0, "minKd": 999, "topKeywords": []}
    )
    for rec in records:
        kw = str(rec.get("keyword") or "").strip()
        vol = int(round(float(rec.get("windowVolume") or 0)))
        kd_raw = rec.get("difficulty")
        kd = int(kd_raw) if isinstance(kd_raw, (int, float)) else None
        tokens = [t for t in re.findall(r"[a-z0-9]+", kw.lower()) if t not in seed_tokens and t not in STOPWORDS]
        mod = tokens[0] if tokens else "(brand-exact)"
        b = buckets[mod]
        b["modifier"] = mod
        b["totalVolume"] += vol
        b["count"] += 1
        if kd is not None and kd < b["minKd"]:
            b["minKd"] = kd
        if len(b["topKeywords"]) < 5:
            b["topKeywords"].append({"keyword": kw, "volume": vol, "kd": kd})

    ranked = sorted(buckets.values(), key=lambda x: x["totalVolume"], reverse=True)
    for item in ranked:
        if item["minKd"] == 999:
            item["minKd"] = None
    return ranked[:top_clusters]


def format_analysis_markdown(payload: dict[str, Any], max_kd: int = 10) -> str:
    """Render a compact, high-signal markdown summary for agent & human review."""
    seed = payload.get("seed") or (
        payload.get("phraseMatch", [{}])[0].get("keyword", "unknown") if payload.get("phraseMatch") else "unknown"
    )
    phrase = payload.get("phraseMatch") or []
    trending = payload.get("trending") or []
    questions = payload.get("questions") or []

    lines: list[str] = [
        f"# Similarweb Keyword Report: `{seed}`",
        "",
        f"- **Date Range**: `{payload.get('dateRange', 'unknown')}` | **Country**: `{payload.get('country', 'unknown')}` | **Source**: `{payload.get('sourceType', 'unknown')}`",
        f"- **Captured**: Phrase Match `{len(phrase)}` (of `{payload.get('totalRecords', len(phrase))}` total) · Trending `{len(trending)}` · Questions `{len(questions)}`",
        "",
        "## Top 15 Phrase Match Keywords",
        "",
        "| # | Keyword | 28d Vol | KD | Intent | Leading Site |",
        "|---:|---|---:|---:|---|---|",
    ]
    for i, r in enumerate(phrase[:15], 1):
        lines.append(
            f"| {i} | `{r.get('keyword', '')}` | {int(round(float(r.get('windowVolume') or 0))):,} "
            f"| {r.get('difficulty', '')} | {r.get('primaryIntent', '')} | {r.get('leadingSite', '')} |"
        )

    low_kd = [
        r
        for r in phrase
        if isinstance(r.get("difficulty"), (int, float))
        and r["difficulty"] <= max_kd
        and float(r.get("windowVolume") or 0) > 0
    ]
    lines += [
        "",
        f"## Low-KD Goldmine Keywords (`KD <= {max_kd}`, Top 15)",
        "",
        "| Keyword | 28d Vol | KD | Intent | Leading Site |",
        "|---|---:|---:|---|---|",
    ]
    for r in low_kd[:15]:
        lines.append(
            f"| `{r.get('keyword', '')}` | {int(round(float(r.get('windowVolume') or 0))):,} "
            f"| {r.get('difficulty', '')} | {r.get('primaryIntent', '')} | {r.get('leadingSite', '')} |"
        )

    clusters = cluster_keywords(phrase, seed=seed, top_clusters=10)
    lines += [
        "",
        "## Top 10 Topic Clusters (by Modifier)",
        "",
        "| Modifier | Sum 28d Vol | Count | Min KD | Sample Keywords |",
        "|---|---:|---:|---:|---|",
    ]
    for c in clusters:
        samples = ", ".join(f"`{k['keyword']}`" for k in c["topKeywords"][:3])
        min_kd = "" if c["minKd"] is None else str(c["minKd"])
        lines.append(f"| `{c['modifier']}` | {c['totalVolume']:,} | {c['count']} | {min_kd} | {samples} |")

    if trending:
        lines += ["", "## Top 10 Trending Keywords", ""]
        for r in trending[:10]:
            lines.append(
                f"- `{r.get('keyword', '')}` — Vol `{int(round(float(r.get('windowVolume') or 0))):,}` "
                f"(KD `{r.get('difficulty', '')}`, `{r.get('volumeChangePresentation', '')}`, leader: `{r.get('leadingSite', '')}`)"
            )

    if questions:
        lines += ["", "## Top 10 Question Queries", ""]
        for r in questions[:10]:
            lines.append(
                f"- `{r.get('keyword', '')}` — Vol `{int(round(float(r.get('windowVolume') or 0))):,}` "
                f"(KD `{r.get('difficulty', '')}`, leader: `{r.get('leadingSite', '')}`)"
            )

    return "\n".join(lines) + "\n"


def build_gate_keywords_draft(payload: dict[str, Any], top_clusters: int = 8) -> dict[str, Any]:
    """Generate a starter `seo-gate-keywords.json` draft from top clusters."""
    seed = payload.get("seed") or (
        payload.get("phraseMatch", [{}])[0].get("keyword", "site") if payload.get("phraseMatch") else "site"
    )
    phrase = payload.get("phraseMatch") or []
    clusters = cluster_keywords(phrase, seed=seed, top_clusters=top_clusters)
    pages: dict[str, Any] = {
        "/": {
            "primary": seed.lower(),
            "synonyms": [r["keyword"].lower() for r in phrase[1:4] if r.get("keyword")],
            "protect_tdh": False,
        }
    }
    for c in clusters:
        mod = c["modifier"]
        if mod == "(brand-exact)":
            continue
        top_kws = [k["keyword"].lower() for k in c["topKeywords"]]
        if not top_kws:
            continue
        pages[f"/{slugify_seed(mod)}/"] = {
            "primary": top_kws[0],
            "synonyms": top_kws[1:5] if len(top_kws) > 1 else [mod],
        }
    return {
        "schema_version": 1,
        "description": f"Draft Gate 6 keyword map generated from Similarweb ({payload.get('dateRange', '')}). Review routes before using.",
        "pages": pages,
    }


class OpenCliBrowser:
    """Thin wrapper around `opencli browser <session> ...`."""

    def __init__(self, session: str, opencli_bin: str | None = None, window: str = "background") -> None:
        self.session = session
        self.window = window
        self.bin = opencli_bin or shutil.which("opencli")
        if not self.bin:
            raise SimilarwebError("`opencli` binary not found in PATH. Install opencli first.")

    def _run(self, *args: str, timeout: int = 45) -> str:
        cmd = [self.bin, "browser", self.session, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).strip()
            raise SimilarwebError(f"`{' '.join(cmd[:5])} ...` failed (exit {proc.returncode}): {err[:300]}")
        return proc.stdout

    def bind(self) -> None:
        self._run("bind", timeout=20)

    def unbind(self) -> None:
        subprocess.run([self.bin, "browser", self.session, "unbind"], capture_output=True, text=True, timeout=15)

    def open(self, url: str) -> None:
        self._run("open", url, "--window", self.window, timeout=45)

    def close(self) -> None:
        subprocess.run([self.bin, "browser", self.session, "close"], capture_output=True, text=True, timeout=15)

    def eval_json(self, js: str, timeout: int = 45) -> Any:
        stdout = self._run("eval", js, timeout=timeout)
        return extract_json_payload(stdout)


def fetch_suggest_endpoint(
    browser: OpenCliBrowser,
    relative_path: str,
) -> dict[str, Any]:
    res = browser.eval_json(build_fetch_js(relative_path))
    if not isinstance(res, dict):
        raise SimilarwebError(f"Unexpected browser eval payload: {res!r}")
    block_reason = detect_access_block(
        int(res.get("status") or 0),
        str(res.get("contentType") or ""),
        str(res.get("href") or ""),
        str(res.get("snippet") or ""),
    )
    if block_reason:
        raise SimilarwebError(block_reason)
    data = res.get("data")
    if not isinstance(data, dict):
        raise SimilarwebError(
            f"Similarweb did not return JSON (status={res.get('status')}, ct={res.get('contentType')}): "
            f"{str(res.get('snippet') or '')[:200]}"
        )
    return data


def run_seed_fetch(args: argparse.Namespace) -> int:
    origin = resolve_target_origin(args.target, args.base_url)
    start_date, end_date = compute_date_window(args.from_date, args.to_date)

    session = args.session or f"sw-kw-{int(time.time()) % 100000}"
    browser = OpenCliBrowser(session=session, window=args.window)
    bound_mode = origin is None

    try:
        if bound_mode:
            browser.bind()
            probe = browser.eval_json(build_probe_js())
            if not isinstance(probe, dict) or not probe.get("origin"):
                raise SimilarwebError("Could not read active Chrome tab origin after bind.")
            origin = normalize_origin(str(probe["origin"]))
            block = detect_access_block(
                200,
                "text/html",
                str(probe.get("href") or ""),
                str(probe.get("bodySnippet") or ""),
            )
            if block:
                raise SimilarwebError(block)
        else:
            assert origin is not None
            ui_url = build_generator_ui_url(origin, args.seed, args.country)
            browser.open(ui_url)
            time.sleep(args.wait_seconds)
            probe = browser.eval_json(build_probe_js())
            if isinstance(probe, dict):
                block = detect_access_block(
                    200,
                    "text/html",
                    str(probe.get("href") or ""),
                    str(probe.get("bodySnippet") or ""),
                )
                if block:
                    raise SimilarwebError(block)

        assert origin is not None
        source_type = classify_source(origin)

        # Page 1 fetch with automatic -1/-2 day lag fallback when dates aren't pinned.
        rows_per_page = 100
        max_pages = max(1, (args.limit + rows_per_page - 1) // rows_per_page)
        first_data: dict[str, Any] | None = None
        last_err: Exception | None = None
        retry_shifts = [0] if (args.from_date and args.to_date) else [0, 1, 2]
        for shift in retry_shifts:
            cand_start = start_date - dt.timedelta(days=shift)
            cand_end = end_date - dt.timedelta(days=shift)
            path1 = build_suggest_path(
                args.seed,
                args.country,
                cand_start,
                cand_end,
                query_type="Broad",
                sort="windowVolume",
                rows_per_page=rows_per_page,
                page=1,
            )
            try:
                candidate = fetch_suggest_endpoint(browser, path1)
                if candidate.get("records") or shift == retry_shifts[-1]:
                    first_data = candidate
                    start_date, end_date = cand_start, cand_end
                    break
            except SimilarwebError as exc:
                last_err = exc
                if shift == retry_shifts[-1]:
                    raise
        if first_data is None:
            raise SimilarwebError(str(last_err or "Failed to fetch page 1 from Similarweb."))

        total_records = int(first_data.get("totalRecords") or len(first_data.get("records") or []))
        phrase_raw: list[dict[str, Any]] = list(first_data.get("records") or [])

        for page in range(2, max_pages + 1):
            if len(phrase_raw) >= args.limit or len(phrase_raw) >= total_records:
                break
            path_p = build_suggest_path(
                args.seed,
                args.country,
                start_date,
                end_date,
                query_type="Broad",
                sort="windowVolume",
                rows_per_page=rows_per_page,
                page=page,
            )
            page_data = fetch_suggest_endpoint(browser, path_p)
            recs = page_data.get("records") or []
            if not recs:
                break
            phrase_raw.extend(recs)
            if args.delay_ms > 0:
                time.sleep(args.delay_ms / 1000.0)

        phrase_records = dedupe_and_sort_records(phrase_raw, limit=args.limit)

        # Trending
        trending_path = build_suggest_path(
            args.seed,
            args.country,
            start_date,
            end_date,
            query_type="Broad",
            label="Trending",
            sort="windowVolume",
            rows_per_page=min(100, args.trending_limit),
            page=1,
        )
        trending_data = fetch_suggest_endpoint(browser, trending_path)
        trending_records = dedupe_and_sort_records(
            list(trending_data.get("records") or []), limit=args.trending_limit
        )

        # Questions
        questions_path = build_suggest_path(
            args.seed,
            args.country,
            start_date,
            end_date,
            query_type="Broad",
            label="Questions",
            sort="windowVolume",
            rows_per_page=min(100, args.questions_limit),
            page=1,
        )
        questions_data = fetch_suggest_endpoint(browser, questions_path)
        questions_records = dedupe_and_sort_records(
            list(questions_data.get("records") or []), limit=args.questions_limit
        )

        related_records: list[dict[str, Any]] | None = None
        if args.include_related:
            related_path = build_suggest_path(
                args.seed,
                args.country,
                start_date,
                end_date,
                query_type="Related",
                sort="score",
                rows_per_page=100,
                page=1,
            )
            related_data = fetch_suggest_endpoint(browser, related_path)
            related_records = dedupe_and_sort_records(list(related_data.get("records") or []), limit=100)

        country_name = COUNTRY_NAMES.get(args.country, f"Country {args.country}")
        today_iso = dt.date.today().isoformat()
        payload: dict[str, Any] = {
            "fetchedAt": today_iso,
            "seed": args.seed,
            "sourceType": source_type,
            "dateRange": f"{start_date.isoformat()}..{end_date.isoformat()}",
            "country": f"{country_name} ({args.country})",
            "countryCode": args.country,
            "totalRecords": total_records,
            "phraseMatch": phrase_records,
            "trending": trending_records,
            "questions": questions_records,
        }
        if related_records is not None:
            payload["related"] = related_records

        out_dir = Path(args.out_dir)
        prefix = args.prefix or f"{slugify_seed(args.seed)}-keywords-{today_iso[:7]}"
        json_path, csv_path = write_outputs(out_dir, prefix, payload)

        if args.emit_gate_draft:
            draft = build_gate_keywords_draft(payload)
            draft_path = Path(args.emit_gate_draft)
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if args.format == "json":
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "jsonPath": str(json_path.resolve()),
                        "csvPath": str(csv_path.resolve()),
                        "sourceType": source_type,
                        "dateRange": payload["dateRange"],
                        "country": payload["country"],
                        "totalRecords": total_records,
                        "phraseMatchCount": len(phrase_records),
                        "trendingCount": len(trending_records),
                        "questionsCount": len(questions_records),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(format_analysis_markdown(payload, max_kd=args.max_kd))
            print(f"Saved JSON: {json_path}")
            print(f"Saved CSV : {csv_path}")
        return 0
    finally:
        if bound_mode:
            browser.unbind()
        elif not args.keep_open:
            browser.close()


def load_export_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SimilarwebError(f"File not found: {path}")
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "phraseMatch" in data:
            return data
        raise SimilarwebError("JSON file does not match Similarweb keyword export schema (missing 'phraseMatch').")
    if path.suffix.lower() == ".csv":
        rows: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                for num_col in ("windowVolume", "averageVolume", "clicks", "cpc", "latestZeroClicks"):
                    if row.get(num_col) not in (None, ""):
                        try:
                            row[num_col] = float(row[num_col])
                        except ValueError:
                            pass
                if row.get("difficulty") not in (None, ""):
                    try:
                        row["difficulty"] = int(float(row["difficulty"]))
                    except ValueError:
                        pass
                rows.append(row)
        return {
            "fetchedAt": "from-csv",
            "seed": rows[0]["keyword"] if rows else path.stem,
            "sourceType": "file",
            "dateRange": "unknown",
            "country": "unknown",
            "totalRecords": len(rows),
            "phraseMatch": dedupe_and_sort_records(rows),
            "trending": [],
            "questions": [],
        }
    raise SimilarwebError(f"Unsupported input format: {path.suffix} (use .json or .csv)")


def run_analyze(args: argparse.Namespace) -> int:
    payload = load_export_file(Path(args.file))
    if args.seed:
        payload["seed"] = args.seed
    if args.emit_gate_draft:
        draft = build_gate_keywords_draft(payload)
        draft_path = Path(args.emit_gate_draft)
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.format == "json":
        seed = payload.get("seed") or "unknown"
        out = {
            "seed": seed,
            "dateRange": payload.get("dateRange"),
            "country": payload.get("country"),
            "phraseMatchCount": len(payload.get("phraseMatch") or []),
            "clusters": cluster_keywords(payload.get("phraseMatch") or [], seed=seed, top_clusters=15),
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(format_analysis_markdown(payload, max_kd=args.max_kd))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="similarweb_keywords.py",
        description="Reusable Similarweb Keyword Generator fetcher (official + mirror) & offline analyzer.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="Fetch Phrase Match, Trending & Questions keywords for a seed term.")
    p_seed.add_argument("seed", help="Seed keyword (e.g. 'krillion', 'wordle', 'blox fruits').")
    p_seed.add_argument(
        "--target",
        choices=("bind", "official", "mirror"),
        default="bind",
        help="Browser source: 'bind' active Chrome tab (default, works for both official & mirror), 'official', or 'mirror' (uses SIMILARWEB_MIRROR_URL).",
    )
    p_seed.add_argument(
        "--base-url",
        default=None,
        help="Explicit origin URL (overrides --target), e.g. https://pro.similarweb.com or http://<mirror>:<port>.",
    )
    p_seed.add_argument("--country", type=int, default=999, help="Similarweb country code (999=Worldwide, 840=US, 826=UK).")
    p_seed.add_argument("--from", dest="from_date", default=None, help="Explicit start date YYYY-MM-DD.")
    p_seed.add_argument("--to", dest="to_date", default=None, help="Explicit end date YYYY-MM-DD.")
    p_seed.add_argument("--limit", type=int, default=1000, help="Max Phrase Match keywords to fetch (default: 1000).")
    p_seed.add_argument("--trending-limit", type=int, default=100, help="Max Trending keywords (default: 100).")
    p_seed.add_argument("--questions-limit", type=int, default=100, help="Max Questions keywords (default: 100).")
    p_seed.add_argument("--include-related", action="store_true", help="Also fetch type=Related keywords.")
    p_seed.add_argument("--out-dir", default="docs/keywords", help="Output directory (default: docs/keywords).")
    p_seed.add_argument("--prefix", default=None, help="Output filename prefix (default: <seed>-keywords-YYYY-MM).")
    p_seed.add_argument("--emit-gate-draft", default=None, help="Optional path to write a starter seo-gate-keywords.json draft.")
    p_seed.add_argument("--max-kd", type=int, default=10, help="Max KD threshold for goldmine table (default: 10).")
    p_seed.add_argument("--session", default=None, help="Custom opencli browser session name.")
    p_seed.add_argument("--window", choices=("background", "foreground"), default="background", help="Window mode when opening a managed tab.")
    p_seed.add_argument("--wait-seconds", type=float, default=4.0, help="Seconds to wait after opening a managed tab (default: 4.0).")
    p_seed.add_argument("--delay-ms", type=int, default=150, help="Polite delay in ms between pagination pages (default: 150).")
    p_seed.add_argument("--keep-open", action="store_true", help="Keep managed browser tab open after completion.")
    p_seed.add_argument("--format", choices=("markdown", "json"), default="markdown", help="Stdout report format.")
    p_seed.set_defaults(func=run_seed_fetch)

    p_analyze = sub.add_parser("analyze", help="Analyze an existing Similarweb JSON/CSV export offline.")
    p_analyze.add_argument("file", help="Path to exported .json or .csv file.")
    p_analyze.add_argument("--seed", default=None, help="Override seed keyword for clustering.")
    p_analyze.add_argument("--max-kd", type=int, default=10, help="Max KD threshold for goldmine table (default: 10).")
    p_analyze.add_argument("--emit-gate-draft", default=None, help="Optional path to write a starter seo-gate-keywords.json draft.")
    p_analyze.add_argument("--format", choices=("markdown", "json"), default="markdown", help="Stdout report format.")
    p_analyze.set_defaults(func=run_analyze)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except SimilarwebError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
