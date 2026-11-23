#!/usr/bin/env python3
"""
Pre-Release SEO Traffic-Light Gate for Tool & Game Websites (seo_gate.py)

Zero external dependencies (Python 3 standard library only).
Scans a built static output directory (e.g., dist/, out/, .output/public/) + sitemap*.xml
and evaluates 5 deterministic SEO risk gates:

  1. page_budget           — Interactive tool/game page prose cap & pseudo-academic bloat; thin content check
  2. payload_bloat         — Oversized static HTML payload (>250 KB) & low text-to-HTML ratio
  3. template_overlap      — Pairwise 5-gram containment across sitemap pages (catches template farms/duplication)
  4. intent_cannibalization— Cross-type twin pages competing for the same head topic (e.g. /mode/ vs /modes/mode/)
  5. index_hygiene         — Sitemap vs noindex/canonical/missing-file/duplicate-title/orphan checks

Execution modes:
  - Default (--strict):    exit 1 if any RED finding exists
  - --warn-only:           always exit 0 (report only)
  - --baseline <file>:     ratchet mode; only NEW RED findings (not in baseline) cause exit 1
  - --write-baseline <f>:  save current RED/YELLOW finding keys to a JSON baseline file
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatch
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Default thresholds & heuristics (overridable via --config .seo-gate.json)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "tool_words_warn": 1200,
    "tool_words_red": 2000,
    "content_words_warn": 350,
    "content_words_red": 200,
    "html_kb_warn": 180.0,
    "html_kb_red": 250.0,
    "text_ratio_warn": 0.05,
    "text_ratio_red": 0.025,
    "overlap_containment_warn": 0.28,
    "overlap_containment_red": 0.35,
    "overlap_min_shared_5grams": 60,
    "cannibal_slug_stems_ignore": ["index", "home", "about", "faq", "contact", "privacy", "terms"],
    "tool_paths": [],
    "content_paths": [],
    "ignore_paths": [],
}

LEGAL_PATH_PATTERNS = (
    "/privacy*",
    "/terms*",
    "/copyright*",
    "/contact*",
    "/about*",
    "/legal*",
    "/cookie*",
    "/dmca*",
    "/404*",
    "/account*",
)

PSEUDO_ACADEMIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Peer-Reviewed", re.compile(r"\bpeer[- ]reviewed\b", re.I)),
    ("Academic References", re.compile(r"\bacademic references\b", re.I)),
    ("Cognitive Psychology References", re.compile(r"\bcognitive psychology references\b", re.I)),
    ("Research References", re.compile(r"\bresearch references\b", re.I)),
    ("Scientific References", re.compile(r"\bscientific references\b", re.I)),
    ("Bibliography", re.compile(r"\bbibliography\b", re.I)),
)

GENERIC_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "best", "by", "can", "complete",
        "daily", "do", "does", "for", "free", "from", "full", "game", "games", "guide",
        "guides", "how", "in", "info", "into", "is", "it", "list", "live", "mode",
        "modes", "new", "of", "official", "on", "online", "or", "page", "play",
        "playing", "pro", "rules", "strategy", "the", "tips", "to", "today", "todays",
        "tool", "tools", "top", "ultimate", "vs", "web", "what", "when", "with", "work",
        "works", "your", "app", "arena", "station", "hub", "pack", "packs",
    }
)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class PageRecord:
    path: str
    url: str
    file_path: str | None
    exists: bool = False
    html_bytes: int = 0
    title: str = ""
    h1: str = ""
    headings: list[str] = field(default_factory=list)
    canonical: str = ""
    robots: str = ""
    noindex: bool = False
    schema_types: list[str] = field(default_factory=list)
    has_interactive_widget: bool = False
    has_article_tag: bool = False
    page_type: str = "content"  # tool | content | hub | legal
    main_words: int = 0
    article_words: int = 0
    prose_words: int = 0
    main_text_bytes: int = 0
    text_ratio: float = 0.0
    details_bytes: int = 0
    pseudo_academic_hits: list[str] = field(default_factory=list)
    main_links: set[str] = field(default_factory=set)
    all_links: set[str] = field(default_factory=set)
    five_grams: set[tuple[str, ...]] = field(default_factory=set)
    inbound_main_links: int = 0
    inbound_all_links: int = 0


@dataclass
class Finding:
    gate: str
    severity: str  # RED | YELLOW
    key: str
    target: str
    message: str
    metric: dict[str, Any] = field(default_factory=dict)
    baselined: bool = False


# ---------------------------------------------------------------------------
# Lightweight HTML parser (zero dependencies)
# ---------------------------------------------------------------------------

class StaticPageParser(HTMLParser):
    SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})
    VOID_TAGS = frozenset(
        {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_chunks: list[str] = []
        self.h1_chunks: list[str] = []
        self.headings: list[str] = []
        self._cur_heading_chunks: list[str] = []
        self._in_title = False
        self._in_h1 = False
        self._in_heading = False
        self._in_jsonld = False
        self._jsonld_chunks: list[str] = []
        self.jsonld_blobs: list[str] = []

        self.canonical = ""
        self.robots = ""

        self._skip_depth = 0
        self._main_depth = 0
        self._article_depth = 0
        self._nav_depth = 0
        self._details_depth = 0

        self.has_main = False
        self.has_article = False
        self.has_interactive_in_main = False

        self.main_text_chunks: list[str] = []
        self.body_text_chunks: list[str] = []
        self.article_text_chunks: list[str] = []
        self.details_text_chunks: list[str] = []
        self.main_links: set[str] = set()
        self.all_links: set[str] = set()
        self._in_body = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}

        if tag == "script":
            if attr_map.get("type", "").lower().startswith("application/ld+json"):
                self._in_jsonld = True
                self._jsonld_chunks = []
            self._skip_depth += 1
            return

        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return

        if tag == "body":
            self._in_body = True
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = attr_map.get("name", "").lower()
            if name in ("robots", "googlebot"):
                content = attr_map.get("content", "")
                if content:
                    self.robots = f"{self.robots}, {content}".strip(", ")
        elif tag == "link":
            rel = attr_map.get("rel", "").lower().split()
            if "canonical" in rel and attr_map.get("href"):
                self.canonical = attr_map["href"].strip()
        elif tag == "main":
            self.has_main = True
            self._main_depth += 1
        elif tag == "article":
            if self._main_depth > 0 or not self.has_main:
                self.has_article = True
            self._article_depth += 1
        elif tag in ("nav", "header", "footer"):
            self._nav_depth += 1
        elif tag == "details":
            self._details_depth += 1

        if self._skip_depth == 0:
            if tag == "h1" and not self.h1_chunks:
                self._in_h1 = True
            if tag in ("h1", "h2", "h3"):
                self._in_heading = True
                self._cur_heading_chunks = []
            if tag == "a" and attr_map.get("href"):
                href = attr_map["href"].strip()
                self.all_links.add(href)
                if self._main_depth > 0 and self._nav_depth == 0:
                    self.main_links.add(href)
            if (self._main_depth > 0 or (self._in_body and self._nav_depth == 0)):
                if tag == "canvas":
                    self.has_interactive_in_main = True
                elif tag in ("input", "textarea") and attr_map.get("type", "text").lower() != "hidden":
                    # Ignore generic site search inputs if outside main
                    if self._main_depth > 0:
                        self.has_interactive_in_main = True

        if tag in self.VOID_TAGS:
            # Void tags don't have closing tags
            return

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script":
            if self._in_jsonld:
                self.jsonld_blobs.append("".join(self._jsonld_chunks))
                self._in_jsonld = False
                self._jsonld_chunks = []
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        if tag in self.SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return

        if tag == "body":
            self._in_body = False
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False
        elif tag == "main" and self._main_depth > 0:
            self._main_depth -= 1
        elif tag == "article" and self._article_depth > 0:
            self._article_depth -= 1
        elif tag in ("nav", "header", "footer") and self._nav_depth > 0:
            self._nav_depth -= 1
        elif tag == "details" and self._details_depth > 0:
            self._details_depth -= 1

        if tag in ("h1", "h2", "h3") and self._in_heading:
            heading_text = re.sub(r"\s+", " ", "".join(self._cur_heading_chunks)).strip()
            if heading_text:
                self.headings.append(heading_text)
            self._in_heading = False
            self._cur_heading_chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._jsonld_chunks.append(data)
            return
        if self._skip_depth > 0:
            return
        if self._in_title:
            self.title_chunks.append(data)
        if self._in_h1:
            self.h1_chunks.append(data)
        if self._in_heading:
            self._cur_heading_chunks.append(data)
        if self._main_depth > 0:
            self.main_text_chunks.append(data)
        if self._in_body and self._nav_depth == 0:
            self.body_text_chunks.append(data)
        if self._article_depth > 0:
            self.article_text_chunks.append(data)
        if self._details_depth > 0:
            self.details_text_chunks.append(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def normalize_path(raw: str) -> str:
    parsed = urlparse(raw)
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    if path != "/" and not path.endswith("/"):
        # Keep file extensions as-is; trailing-slash directories normalize uniformly
        if "." not in Path(path).name:
            path += "/"
    return path


def resolve_html_file(dist_dir: Path, path: str) -> Path | None:
    clean = path.strip("/")
    candidates: list[Path] = []
    if not clean:
        candidates.append(dist_dir / "index.html")
    else:
        candidates.append(dist_dir / clean / "index.html")
        candidates.append(dist_dir / f"{clean}.html")
        if clean.endswith(".html"):
            candidates.append(dist_dir / clean)
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def discover_sitemap_urls(dist_dir: Path, explicit_sitemap: Path | None = None) -> list[str]:
    sitemap_files: list[Path] = []
    if explicit_sitemap:
        sitemap_files.append(explicit_sitemap)
    else:
        for name in ("sitemap-0.xml", "sitemap.xml", "sitemap-index.xml"):
            p = dist_dir / name
            if p.is_file():
                sitemap_files.append(p)
                break
        if not sitemap_files:
            sitemap_files = sorted(dist_dir.glob("sitemap*.xml"))

    urls: list[str] = []
    visited: set[Path] = set()

    def parse_xml_file(xml_path: Path) -> None:
        if xml_path in visited or not xml_path.is_file():
            return
        visited.add(xml_path)
        try:
            tree = ET.parse(xml_path)
            root = tree.getroot()
        except ET.ParseError:
            return
        tag_local = root.tag.split("}")[-1].lower()
        if tag_local == "sitemapindex":
            for loc in root.iter():
                if loc.tag.split("}")[-1].lower() == "loc" and loc.text:
                    sub_name = Path(urlparse(loc.text.strip()).path).name
                    sub_path = dist_dir / sub_name
                    if sub_path.is_file():
                        parse_xml_file(sub_path)
        else:
            for loc in root.iter():
                if loc.tag.split("}")[-1].lower() == "loc" and loc.text:
                    urls.append(loc.text.strip())

    for sf in sitemap_files:
        parse_xml_file(sf)

    # Fallback: if no sitemap exists, scan dist HTML files
    if not urls:
        for html_file in sorted(dist_dir.rglob("*.html")):
            rel = html_file.relative_to(dist_dir).as_posix()
            if rel == "index.html":
                urls.append("https://local.invalid/")
            elif rel.endswith("/index.html"):
                urls.append(f"https://local.invalid/{rel[:-10]}")
            elif rel == "404.html":
                continue
            else:
                urls.append(f"https://local.invalid/{rel}")

    # Deduplicate preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return unique


def extract_schema_types(blobs: list[str]) -> list[str]:
    types: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("@type")
            if isinstance(t, str):
                types.append(t)
            elif isinstance(t, list):
                types.extend(str(x) for x in t)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for raw in blobs:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        walk(data)
    return list(dict.fromkeys(types))


def tokenize_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)?", text.lower())


def build_five_grams(words: list[str]) -> set[tuple[str, ...]]:
    if len(words) < 5:
        return set()
    return {tuple(words[i : i + 5]) for i in range(len(words) - 4)}


def matches_any_pattern(path: str, patterns: list[str] | tuple[str, ...]) -> bool:
    for pat in patterns:
        if fnmatch(path, pat) or fnmatch(path.rstrip("/") or "/", pat.rstrip("/") or "/"):
            return True
    return False


def classify_page_type(rec: PageRecord, cfg: dict[str, Any]) -> str:
    if matches_any_pattern(rec.path, cfg.get("tool_paths", [])):
        return "tool"
    if matches_any_pattern(rec.path, cfg.get("content_paths", [])):
        return "content"
    if matches_any_pattern(rec.path, LEGAL_PATH_PATTERNS):
        return "legal"
    if rec.has_interactive_widget or any(
        t in ("VideoGame", "WebApplication", "SoftwareApplication") for t in rec.schema_types
    ):
        return "tool"
    if "ItemList" in rec.schema_types and not rec.has_article_tag:
        return "hub"
    return "content"


def infer_brand_tokens(records: list[PageRecord]) -> set[str]:
    brand: set[str] = set()
    for r in records:
        if r.path == "/":
            host = urlparse(r.url).hostname or ""
            stem = host.split(".")[0] if host else ""
            for tok in re.findall(r"[a-z0-9]+", stem.lower()):
                if len(tok) >= 3 and tok not in GENERIC_STOPWORDS:
                    brand.add(tok)
            for tok in tokenize_words(r.h1):
                if len(tok) >= 3 and tok not in GENERIC_STOPWORDS:
                    brand.add(tok)
            break
    return brand


def slug_stem(path: str) -> str:
    parts = [p for p in path.strip("/").split("/") if p]
    if not parts:
        return "home"
    last = parts[-1].lower()
    # Normalize trailing -mode / -game / -tool suffixes to catch /unlimited/ vs /modes/unlimited-mode/
    last = re.sub(r"-(mode|game|tool|guide|page|online|free)$", "", last)
    return last


def extract_topic_tokens(text: str, brand_tokens: set[str]) -> set[str]:
    return {
        t
        for t in tokenize_words(text)
        if len(t) >= 3 and t not in GENERIC_STOPWORDS and t not in brand_tokens
    }


# ---------------------------------------------------------------------------
# Core Scanner & 5 Gates
# ---------------------------------------------------------------------------

def scan_site(
    dist_dir: Path,
    cfg: dict[str, Any],
    explicit_sitemap: Path | None = None,
) -> tuple[list[PageRecord], list[Finding]]:
    sitemap_urls = discover_sitemap_urls(dist_dir, explicit_sitemap)
    records: list[PageRecord] = []
    ignore_patterns = cfg.get("ignore_paths", [])

    # Determine site origin for resolving internal links
    site_hosts: set[str] = set()
    for u in sitemap_urls:
        h = urlparse(u).hostname
        if h:
            site_hosts.add(h.lower())

    for url in sitemap_urls:
        path = normalize_path(url)
        if matches_any_pattern(path, ignore_patterns):
            continue
        html_file = resolve_html_file(dist_dir, path)
        rec = PageRecord(
            path=path,
            url=url,
            file_path=str(html_file) if html_file else None,
            exists=html_file is not None,
        )
        if html_file:
            raw_bytes = html_file.read_bytes()
            rec.html_bytes = len(raw_bytes)
            raw_html = raw_bytes.decode("utf-8", errors="replace")

            parser = StaticPageParser()
            parser.feed(raw_html)
            parser.close()

            rec.title = re.sub(r"\s+", " ", html_lib.unescape("".join(parser.title_chunks))).strip()
            rec.h1 = re.sub(r"\s+", " ", html_lib.unescape("".join(parser.h1_chunks))).strip()
            rec.headings = parser.headings
            rec.canonical = parser.canonical
            rec.robots = parser.robots
            rec.noindex = "noindex" in parser.robots.lower()
            rec.schema_types = extract_schema_types(parser.jsonld_blobs)
            rec.has_interactive_widget = parser.has_interactive_in_main
            rec.has_article_tag = parser.has_article

            main_text = re.sub(
                r"\s+", " ", "".join(parser.main_text_chunks if parser.has_main else parser.body_text_chunks)
            ).strip()
            article_text = re.sub(r"\s+", " ", "".join(parser.article_text_chunks)).strip()

            main_word_list = tokenize_words(main_text)
            article_word_list = tokenize_words(article_text)

            rec.main_words = len(main_word_list)
            rec.article_words = len(article_word_list)
            rec.prose_words = rec.article_words if rec.has_article_tag and rec.article_words > 0 else rec.main_words
            rec.main_text_bytes = len(main_text.encode("utf-8"))
            rec.text_ratio = (rec.main_text_bytes / rec.html_bytes) if rec.html_bytes > 0 else 0.0

            # Measure raw <details> byte footprint (catches hidden accordion DOM bloat like /answers/)
            rec.details_bytes = sum(len(m.group(0).encode("utf-8")) for m in re.finditer(r"<details\b[^>]*>.*?</details>", raw_html, re.S | re.I))

            # Pseudo-academic markers in headings
            headings_joined = " | ".join(rec.headings)
            for label, pat in PSEUDO_ACADEMIC_PATTERNS:
                if pat.search(headings_joined):
                    rec.pseudo_academic_hits.append(label)

            # Normalize internal links
            for raw_href in parser.main_links:
                p_href = _resolve_internal_href(raw_href, site_hosts)
                if p_href and p_href != rec.path:
                    rec.main_links.add(p_href)
            for raw_href in parser.all_links:
                p_href = _resolve_internal_href(raw_href, site_hosts)
                if p_href and p_href != rec.path:
                    rec.all_links.add(p_href)

            rec.page_type = classify_page_type(rec, cfg)
            # Build 5-grams across <main> visible text (captures shared FAQ/CTA/template blocks too).
            # If two tool pages share an identical interactive widget shell, only flag sibling clusters
            # or genuine boilerplate (min_shared_5grams + containment filter).
            rec.five_grams = build_five_grams(main_word_list)

        records.append(rec)

    # Compute inbound internal link counts
    by_path = {r.path: r for r in records}
    for src in records:
        if not src.exists or src.noindex:
            continue
        for target_path in src.main_links:
            if target_path in by_path:
                by_path[target_path].inbound_main_links += 1
        for target_path in src.all_links:
            if target_path in by_path:
                by_path[target_path].inbound_all_links += 1

    findings: list[Finding] = []
    findings.extend(_gate_page_budget(records, cfg))
    findings.extend(_gate_payload_bloat(records, cfg))
    findings.extend(_gate_template_overlap(records, cfg))
    findings.extend(_gate_intent_cannibalization(records, cfg))
    findings.extend(_gate_index_hygiene(records))

    # Sort deterministically: RED first, then gate, then key
    severity_rank = {"RED": 0, "YELLOW": 1}
    findings.sort(key=lambda f: (severity_rank.get(f.severity, 9), f.gate, f.key))
    return records, findings


def _resolve_internal_href(href: str, site_hosts: set[str]) -> str | None:
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return None
    parsed = urlparse(href)
    if parsed.scheme in ("http", "https"):
        if (parsed.hostname or "").lower() not in site_hosts:
            return None
        return normalize_path(parsed.path)
    if href.startswith("/"):
        return normalize_path(parsed.path)
    return None


def _gate_page_budget(records: list[PageRecord], cfg: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    tool_warn = int(cfg["tool_words_warn"])
    tool_red = int(cfg["tool_words_red"])
    content_warn = int(cfg["content_words_warn"])
    content_red = int(cfg["content_words_red"])

    for r in records:
        if not r.exists or r.noindex or r.page_type == "legal":
            continue
        if r.page_type == "tool":
            if r.pseudo_academic_hits:
                findings.append(
                    Finding(
                        gate="page_budget",
                        severity="RED",
                        key=f"page_budget:academic:{r.path}",
                        target=r.path,
                        message=(
                            f"Tool/game page contains pseudo-academic section headings ({', '.join(r.pseudo_academic_hits)}) "
                            f"with {r.prose_words} prose words — high Search-engine-first risk."
                        ),
                        metric={"prose_words": r.prose_words, "hits": r.pseudo_academic_hits},
                    )
                )
            if r.prose_words > tool_red:
                findings.append(
                    Finding(
                        gate="page_budget",
                        severity="RED",
                        key=f"page_budget:overlong:{r.path}",
                        target=r.path,
                        message=(
                            f"Tool/game page prose is {r.prose_words} words (red threshold >{tool_red}). "
                            "Keep playable/tool pages concise (300–800w) to preserve tool intent."
                        ),
                        metric={"prose_words": r.prose_words, "threshold": tool_red},
                    )
                )
            elif r.prose_words > tool_warn:
                findings.append(
                    Finding(
                        gate="page_budget",
                        severity="YELLOW",
                        key=f"page_budget:overlong:{r.path}",
                        target=r.path,
                        message=(
                            f"Tool/game page prose is {r.prose_words} words (warn threshold >{tool_warn}). "
                            "Verify copy stays practical and player-focused."
                        ),
                        metric={"prose_words": r.prose_words, "threshold": tool_warn},
                    )
                )
        else:
            if r.main_words < content_red:
                findings.append(
                    Finding(
                        gate="page_budget",
                        severity="RED",
                        key=f"page_budget:thin:{r.path}",
                        target=r.path,
                        message=f"Indexable {r.page_type} page has only {r.main_words} words (<{content_red}); thin content risk.",
                        metric={"main_words": r.main_words, "threshold": content_red},
                    )
                )
            elif r.main_words < content_warn:
                findings.append(
                    Finding(
                        gate="page_budget",
                        severity="YELLOW",
                        key=f"page_budget:thin:{r.path}",
                        target=r.path,
                        message=f"Indexable {r.page_type} page has {r.main_words} words (<{content_warn}); consider enriching or noindexing.",
                        metric={"main_words": r.main_words, "threshold": content_warn},
                    )
                )
    return findings


def _gate_payload_bloat(records: list[PageRecord], cfg: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    kb_warn = float(cfg["html_kb_warn"])
    kb_red = float(cfg["html_kb_red"])
    ratio_warn = float(cfg["text_ratio_warn"])
    ratio_red = float(cfg["text_ratio_red"])

    for r in records:
        if not r.exists or r.noindex:
            continue
        kb = round(r.html_bytes / 1024.0, 1)
        details_kb = round(r.details_bytes / 1024.0, 1)
        if kb > kb_red:
            extra = f" (including {details_kb} KB inside <details>)" if details_kb > 80 else ""
            findings.append(
                Finding(
                    gate="payload_bloat",
                    severity="RED",
                    key=f"payload_bloat:size:{r.path}",
                    target=r.path,
                    message=f"Static HTML payload is {kb} KB (>{kb_red} KB){extra}; text ratio {r.text_ratio:.1%}.",
                    metric={"html_kb": kb, "details_kb": details_kb, "text_ratio": round(r.text_ratio, 4)},
                )
            )
        elif kb > kb_warn:
            findings.append(
                Finding(
                    gate="payload_bloat",
                    severity="YELLOW",
                    key=f"payload_bloat:size:{r.path}",
                    target=r.path,
                    message=f"Static HTML payload is {kb} KB (>{kb_warn} KB); text ratio {r.text_ratio:.1%}.",
                    metric={"html_kb": kb, "text_ratio": round(r.text_ratio, 4)},
                )
            )

        if r.page_type != "legal":
            if r.text_ratio < ratio_red:
                findings.append(
                    Finding(
                        gate="payload_bloat",
                        severity="RED",
                        key=f"payload_bloat:ratio:{r.path}",
                        target=r.path,
                        message=f"Visible text-to-HTML ratio is {r.text_ratio:.1%} (<{ratio_red:.1%}) at {kb} KB.",
                        metric={"text_ratio": round(r.text_ratio, 4), "html_kb": kb},
                    )
                )
            elif r.text_ratio < ratio_warn and r.page_type != "tool":
                findings.append(
                    Finding(
                        gate="payload_bloat",
                        severity="YELLOW",
                        key=f"payload_bloat:ratio:{r.path}",
                        target=r.path,
                        message=f"Visible text-to-HTML ratio is {r.text_ratio:.1%} (<{ratio_warn:.1%}) at {kb} KB.",
                        metric={"text_ratio": round(r.text_ratio, 4), "html_kb": kb},
                    )
                )
    return findings


def _gate_template_overlap(records: list[PageRecord], cfg: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    c_warn = float(cfg["overlap_containment_warn"])
    c_red = float(cfg["overlap_containment_red"])
    min_shared = int(cfg["overlap_min_shared_5grams"])

    eligible = [r for r in records if r.exists and not r.noindex and r.page_type != "legal" and len(r.five_grams) >= 60]

    # Collect high-overlap pairs
    raw_pairs: list[tuple[PageRecord, PageRecord, float, float, int]] = []
    for i in range(len(eligible)):
        a = eligible[i]
        for j in range(i + 1, len(eligible)):
            b = eligible[j]
            shared = len(a.five_grams & b.five_grams)
            if shared < min_shared:
                continue
            min_len = min(len(a.five_grams), len(b.five_grams))
            containment = shared / min_len if min_len > 0 else 0.0
            if containment < c_warn:
                continue
            union_len = len(a.five_grams | b.five_grams)
            jaccard = shared / union_len if union_len > 0 else 0.0
            raw_pairs.append((a, b, containment, jaccard, shared))

    # Group sibling directory template clusters (e.g., /packs/* <-> /packs/*) to avoid noisy O(n^2) output
    dir_clusters: dict[str, list[tuple[PageRecord, PageRecord, float, float, int]]] = defaultdict(list)
    standalone_pairs: list[tuple[PageRecord, PageRecord, float, float, int]] = []

    def parent_dir(p: str) -> str:
        parts = [x for x in p.strip("/").split("/") if x]
        return f"/{parts[0]}/" if len(parts) >= 2 else "/"

    for pair in raw_pairs:
        a, b, cont, jac, shared = pair
        pa, pb = parent_dir(a.path), parent_dir(b.path)
        if pa == pb and pa != "/":
            dir_clusters[pa].append(pair)
        else:
            standalone_pairs.append(pair)

    for prefix, pairs in sorted(dir_clusters.items()):
        pages = sorted({p.path for a, b, *_ in pairs for p in (a, b)})
        max_cont = max(x[2] for x in pairs)
        top_pair = max(pairs, key=lambda x: x[2])
        sev = "RED" if max_cont >= c_red else "YELLOW"
        findings.append(
            Finding(
                gate="template_overlap",
                severity=sev,
                key=f"template_overlap:cluster:{prefix}",
                target=f"{prefix}* ({len(pages)} pages)",
                message=(
                    f"Sibling pages under {prefix} share heavy template boilerplate: {len(pairs)} overlapping pairs "
                    f"(peak {top_pair[0].path} <-> {top_pair[1].path}: {max_cont:.1%} containment, {top_pair[4]} shared 5-grams)."
                ),
                metric={
                    "cluster_prefix": prefix,
                    "affected_pages": len(pages),
                    "pair_count": len(pairs),
                    "max_containment": round(max_cont, 3),
                },
            )
        )

    # For cross-directory pairs, collapse hub-vs-direct-child snippet repetition into at most 1 note
    # (category hubs naturally quote child titles/descriptions).
    hub_child_seen: set[str] = set()
    for a, b, cont, jac, shared in sorted(standalone_pairs, key=lambda x: -x[2]):
        p1, p2 = sorted([a.path, b.path])
        is_parent_child = (b.path.startswith(a.path) and a.path != "/") or (a.path.startswith(b.path) and b.path != "/")
        if is_parent_child:
            hub_path = a.path if b.path.startswith(a.path) else b.path
            if cont < 0.65 or hub_path in hub_child_seen:
                continue
            hub_child_seen.add(hub_path)
            findings.append(
                Finding(
                    gate="template_overlap",
                    severity="YELLOW",
                    key=f"template_overlap:hub:{hub_path}",
                    target=f"{hub_path} (hub vs children)",
                    message=(
                        f"Category hub {hub_path} relies heavily on child-card snippets "
                        f"({cont:.1%} containment against direct child; add unique hub intro or noindex)."
                    ),
                    metric={"hub": hub_path, "containment": round(cont, 3), "shared_5grams": shared},
                )
            )
            continue
        sev = "RED" if cont >= c_red else "YELLOW"
        findings.append(
            Finding(
                gate="template_overlap",
                severity=sev,
                key=f"template_overlap:pair:{p1}<->{p2}",
                target=f"{p1} <-> {p2}",
                message=f"High text overlap: {cont:.1%} 5-gram containment ({shared} shared 5-grams, Jaccard {jac:.3f}).",
                metric={"containment": round(cont, 3), "jaccard": round(jac, 3), "shared_5grams": shared},
            )
        )

    return findings


def _gate_intent_cannibalization(records: list[PageRecord], cfg: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    brand_tokens = infer_brand_tokens(records)
    ignore_stems = set(cfg.get("cannibal_slug_stems_ignore", []))

    eligible = [r for r in records if r.exists and not r.noindex and r.page_type != "legal" and r.path != "/"]

    seen_keys: set[str] = set()
    for i in range(len(eligible)):
        a = eligible[i]
        stem_a = slug_stem(a.path)
        toks_a = extract_topic_tokens(f"{a.title} {a.h1}", brand_tokens)
        for j in range(i + 1, len(eligible)):
            b = eligible[j]
            stem_b = slug_stem(b.path)
            toks_b = extract_topic_tokens(f"{b.title} {b.h1}", brand_tokens)

            same_stem = stem_a == stem_b and stem_a not in ignore_stems and len(stem_a) >= 4
            cross_type = {a.page_type, b.page_type} == {"tool", "content"}

            overlap = toks_a & toks_b
            union = toks_a | toks_b
            jac = (len(overlap) / len(union)) if union else 0.0
            cont = (len(overlap) / min(len(toks_a), len(toks_b))) if toks_a and toks_b else 0.0

            p1, p2 = sorted([a.path, b.path])
            key = f"intent_cannibalization:{p1}<->{p2}"
            if key in seen_keys:
                continue

            # Exclude well-separated /packs/<topic>/ vs /guides/<topic>/ unless titles collide heavily
            is_pack_vs_guide = (
                (a.path.startswith("/packs/") and b.path.startswith("/guides/"))
                or (b.path.startswith("/packs/") and a.path.startswith("/guides/"))
            )

            if same_stem and cross_type and not is_pack_vs_guide:
                seen_keys.add(key)
                findings.append(
                    Finding(
                        gate="intent_cannibalization",
                        severity="RED",
                        key=key,
                        target=f"{p1} <-> {p2}",
                        message=(
                            f"Twin playable/content pages share identical slug stem '{stem_a}' "
                            f"({a.path} [{a.page_type}] vs {b.path} [{b.page_type}]) — high Google deduplication risk."
                        ),
                        metric={"stem": stem_a, "types": [a.page_type, b.page_type], "title_jaccard": round(jac, 3)},
                    )
                )
            elif len(overlap) >= 3 and (jac >= 0.55 or (cross_type and cont >= 0.75)) and not is_pack_vs_guide:
                seen_keys.add(key)
                findings.append(
                    Finding(
                        gate="intent_cannibalization",
                        severity="YELLOW",
                        key=key,
                        target=f"{p1} <-> {p2}",
                        message=(
                            f"Strong Title/H1 topic overlap ({', '.join(sorted(overlap))}; Jaccard {jac:.2f}) "
                            f"between {a.path} [{a.page_type}] and {b.path} [{b.page_type}]."
                        ),
                        metric={"shared_tokens": sorted(overlap), "title_jaccard": round(jac, 3)},
                    )
                )
    return findings


def _gate_index_hygiene(records: list[PageRecord]) -> list[Finding]:
    findings: list[Finding] = []
    titles_map: dict[str, list[str]] = defaultdict(list)

    for r in records:
        if not r.exists:
            findings.append(
                Finding(
                    gate="index_hygiene",
                    severity="RED",
                    key=f"index_hygiene:missing:{r.path}",
                    target=r.path,
                    message=f"URL listed in sitemap has no built HTML file in dist/ ({r.url}).",
                )
            )
            continue

        if r.noindex:
            findings.append(
                Finding(
                    gate="index_hygiene",
                    severity="RED",
                    key=f"index_hygiene:sitemap_noindex:{r.path}",
                    target=r.path,
                    message="Contradictory signal: URL is in sitemap but renders meta robots noindex.",
                )
            )
            continue

        if not r.canonical:
            findings.append(
                Finding(
                    gate="index_hygiene",
                    severity="RED",
                    key=f"index_hygiene:no_canonical:{r.path}",
                    target=r.path,
                    message="Missing <link rel='canonical'> tag.",
                )
            )
        elif normalize_path(r.canonical) != r.path:
            findings.append(
                Finding(
                    gate="index_hygiene",
                    severity="YELLOW",
                    key=f"index_hygiene:non_self_canonical:{r.path}",
                    target=r.path,
                    message=f"Sitemap URL canonical points elsewhere ({r.canonical}).",
                )
            )

        if not r.title:
            findings.append(
                Finding(
                    gate="index_hygiene",
                    severity="RED",
                    key=f"index_hygiene:no_title:{r.path}",
                    target=r.path,
                    message="Missing <title> tag.",
                )
            )
        else:
            titles_map[r.title.lower()].append(r.path)

        if not r.h1:
            findings.append(
                Finding(
                    gate="index_hygiene",
                    severity="RED",
                    key=f"index_hygiene:no_h1:{r.path}",
                    target=r.path,
                    message="Missing <h1> heading.",
                )
            )

        if r.path != "/" and r.page_type != "legal":
            if r.inbound_all_links == 0:
                findings.append(
                    Finding(
                        gate="index_hygiene",
                        severity="RED",
                        key=f"index_hygiene:orphan:{r.path}",
                        target=r.path,
                        message="True orphan page: 0 internal inbound links from any other sitemap page.",
                    )
                )
            elif r.inbound_main_links == 0:
                findings.append(
                    Finding(
                        gate="index_hygiene",
                        severity="YELLOW",
                        key=f"index_hygiene:no_main_inbound:{r.path}",
                        target=r.path,
                        message=(
                            f"Weak internal link equity: 0 inbound links from <main> content "
                            f"(only {r.inbound_all_links} nav/footer links)."
                        ),
                        metric={"inbound_main": 0, "inbound_all": r.inbound_all_links},
                    )
                )

    for title_lower, paths in sorted(titles_map.items()):
        if len(paths) > 1:
            joined = ", ".join(sorted(paths))
            findings.append(
                Finding(
                    gate="index_hygiene",
                    severity="RED",
                    key=f"index_hygiene:dup_title:{'|'.join(sorted(paths))}",
                    target=joined,
                    message=f"Duplicate <title> across {len(paths)} sitemap pages: '{title_lower[:70]}'.",
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Baseline Ratchet & Reporting
# ---------------------------------------------------------------------------

def apply_baseline(findings: list[Finding], baseline_path: Path) -> tuple[list[Finding], list[str]]:
    data = json.loads(baseline_path.read_text(encoding="utf-8"))
    known_keys = set(data.get("keys", []))
    current_keys = {f.key for f in findings}
    fixed_keys = sorted(known_keys - current_keys)
    for f in findings:
        if f.key in known_keys:
            f.baselined = True
    return findings, fixed_keys


def write_baseline(findings: list[Finding], baseline_path: Path) -> None:
    payload = {
        "schema_version": 1,
        "description": "SEO Gate baseline ratchet — only NEW RED findings outside this list fail CI.",
        "keys": sorted({f.key for f in findings}),
    }
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def gate_status_summary(findings: list[Finding]) -> dict[str, str]:
    gates = ["page_budget", "payload_bloat", "template_overlap", "intent_cannibalization", "index_hygiene"]
    status: dict[str, str] = {}
    for g in gates:
        gf = [f for f in findings if f.gate == g and not f.baselined]
        if any(f.severity == "RED" for f in gf):
            status[g] = "RED"
        elif any(f.severity == "YELLOW" for f in gf):
            status[g] = "YELLOW"
        else:
            status[g] = "GREEN"
    return status


def format_markdown(
    dist_dir: Path,
    records: list[PageRecord],
    findings: list[Finding],
    fixed_keys: list[str],
    mode: str,
) -> str:
    gate_labels = {
        "page_budget": "1. Page Type & Word Budget",
        "payload_bloat": "2. HTML Payload & Text Ratio",
        "template_overlap": "3. Site-Wide 5-Gram Overlap",
        "intent_cannibalization": "4. Twin Intent Cannibalization",
        "index_hygiene": "5. Sitemap / Index Hygiene",
    }
    icons = {"GREEN": "🟢 PASS", "YELLOW": "🟡 WARN", "RED": "🔴 FAIL"}

    status = gate_status_summary(findings)
    type_counts: dict[str, int] = defaultdict(int)
    for r in records:
        type_counts[r.page_type] += 1

    active_red = [f for f in findings if f.severity == "RED" and not f.baselined]
    active_yellow = [f for f in findings if f.severity == "YELLOW" and not f.baselined]
    baselined_list = [f for f in findings if f.baselined]

    lines: list[str] = [
        f"# Tool/Game Site SEO Pre-Release Gate (`{dist_dir}`)",
        "",
        f"- **Mode**: `{mode}` | **Sitemap URLs**: `{len(records)}` "
        f"(tool: {type_counts['tool']}, content: {type_counts['content']}, hub: {type_counts['hub']}, legal: {type_counts['legal']})",
        f"- **Active Findings**: 🔴 **{len(active_red)} RED** · 🟡 **{len(active_yellow)} YELLOW**"
        + (f" · 📦 {len(baselined_list)} baselined" if baselined_list else ""),
        "",
        "## Gate Summary",
        "",
        "| Gate | Status | Active RED | Active YELLOW |",
        "|---|---|---:|---:|",
    ]

    for g, label in gate_labels.items():
        r_cnt = sum(1 for f in active_red if f.gate == g)
        y_cnt = sum(1 for f in active_yellow if f.gate == g)
        lines.append(f"| {label} | {icons[status[g]]} | {r_cnt} | {y_cnt} |")

    if active_red or active_yellow:
        lines.extend(["", "## Active Findings", ""])
        for f in active_red + active_yellow:
            badge = "🔴 RED" if f.severity == "RED" else "🟡 WARN"
            lines.append(f"- **{badge}** `{f.gate}` · **{f.target}** — {f.message}")

    if fixed_keys:
        lines.extend(
            [
                "",
                f"## 🎉 Fixed Baseline Issues ({len(fixed_keys)})",
                "",
                "Run with `--write-baseline <file>` to tighten the ratchet:",
            ]
        )
        for k in fixed_keys:
            lines.append(f"- `{k}`")

    if not active_red and not active_yellow:
        lines.extend(["", "🟢 **All 5 SEO pre-release gates passed with zero active warnings.**"])

    return "\n".join(lines) + "\n"


def load_config(config_path: Path | None, tool_paths: list[str]) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if config_path and config_path.is_file():
        user_cfg = json.loads(config_path.read_text(encoding="utf-8"))
        cfg.update(user_cfg)
    if tool_paths:
        cfg["tool_paths"] = list(cfg.get("tool_paths", [])) + tool_paths
    return cfg


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="5-gate pre-release SEO traffic-light scanner for tool & game sites.")
    ap.add_argument("--dist", type=Path, required=True, help="Path to built static directory (e.g. ./dist)")
    ap.add_argument("--sitemap", type=Path, default=None, help="Optional explicit sitemap XML path")
    ap.add_argument("--config", type=Path, default=None, help="Optional JSON config overriding thresholds/tool_paths")
    ap.add_argument("--tool-path", action="append", default=[], help="Explicit glob/path treated as interactive tool page")
    ap.add_argument("--warn-only", action="store_true", help="Always exit 0 (report only)")
    ap.add_argument("--baseline", type=Path, default=None, help="Ratchet baseline JSON (only NEW RED findings exit 1)")
    ap.add_argument("--write-baseline", type=Path, default=None, help="Write current finding keys to baseline JSON")
    ap.add_argument("--format", choices=("markdown", "json"), default="markdown", help="Output format")
    args = ap.parse_args(argv)

    if not args.dist.is_dir():
        print(f"ERROR: dist directory not found: {args.dist}", file=sys.stderr)
        return 2

    cfg = load_config(args.config, args.tool_path)
    records, findings = scan_site(args.dist, cfg, explicit_sitemap=args.sitemap)

    fixed_keys: list[str] = []
    if args.write_baseline:
        write_baseline(findings, args.write_baseline)

    mode = "warn-only" if args.warn_only else ("baseline-ratchet" if args.baseline else "strict")
    if args.baseline and args.baseline.is_file():
        findings, fixed_keys = apply_baseline(findings, args.baseline)

    active_red = [f for f in findings if f.severity == "RED" and not f.baselined]

    if args.format == "json":
        output = {
            "dist": str(args.dist),
            "mode": mode,
            "page_count": len(records),
            "gate_status": gate_status_summary(findings),
            "active_red_count": len(active_red),
            "active_yellow_count": sum(1 for f in findings if f.severity == "YELLOW" and not f.baselined),
            "baselined_count": sum(1 for f in findings if f.baselined),
            "fixed_baseline_keys": fixed_keys,
            "findings": [asdict(f) for f in findings],
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        print(format_markdown(args.dist, records, findings, fixed_keys, mode))

    if args.warn_only:
        return 0
    return 1 if active_red else 0


if __name__ == "__main__":
    raise SystemExit(main())
