import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_health_monitor as mon


class TestUrlAndConfigHelpers(unittest.TestCase):
    def test_normalize_target_url_and_domain_filter(self) -> None:
        self.assertEqual(
            mon.normalize_target_url("/noillirk/", "krillion-game.app"),
            "https://krillion-game.app/noillirk/",
        )
        self.assertEqual(
            mon.normalize_target_url("puffer-party/", "krillion-game.app"),
            "https://krillion-game.app/puffer-party/",
        )
        self.assertIsNone(
            mon.normalize_target_url("https://evil-other-site.com/noillirk/", "krillion-game.app")
        )

    def test_extract_top_pages_from_snapshot_and_pin_active_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snap_path = Path(tmp) / "gsc-snapshot-prev.json"
            snap_path.write_text(
                json.dumps(
                    {
                        "rows": [
                            {"query": "reverse krillion", "page": "https://krillion-game.app/noillirk/", "clicks": 500},
                            {"query": "krillion backwards", "page": "https://krillion-game.app/noillirk/", "clicks": 300},
                            {"query": "history", "page": "https://krillion-game.app/guides/history/", "clicks": 200},
                            {"query": "spam", "page": "https://other.example/x", "clicks": 9999},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            top = mon.extract_top_pages_from_snapshot(snap_path, "krillion-game.app", limit=5)
            self.assertEqual(
                top,
                [
                    "https://krillion-game.app/noillirk/",
                    "https://krillion-game.app/guides/history/",
                ],
            )

            urls = mon.build_target_urls(
                domain="krillion-game.app",
                explicit_urls=["/unlimited/"],
                snapshot_top_urls=top,
                live_top_urls=["https://krillion-game.app/faq/"],
                active_alerts={"gsc_inspect:status:/dropped-page/": {"url": "https://krillion-game.app/dropped-page/"}},
                max_urls=4,
            )
            self.assertEqual(
                urls,
                [
                    "https://krillion-game.app/",
                    "https://krillion-game.app/unlimited/",
                    "https://krillion-game.app/dropped-page/",
                    "https://krillion-game.app/noillirk/",
                ],
            )


class TestParsersAndHttp(unittest.TestCase):
    def test_parse_robots_txt(self) -> None:
        ok_robots = """
        # Main rules
        User-agent: *
        Allow: /
        Disallow: /api/

        Sitemap: https://krillion-game.app/sitemap-index.xml
        """
        res_ok = mon.parse_robots_txt(ok_robots)
        self.assertEqual(res_ok["blocked_agents"], [])
        self.assertEqual(res_ok["sitemaps"], ["https://krillion-game.app/sitemap-index.xml"])

        bad_robots = """
        User-agent: *
        Disallow: / # blocked accidentally
        """
        res_bad = mon.parse_robots_txt(bad_robots)
        self.assertEqual(res_bad["blocked_agents"], ["*"])

        gbot_robots = """
        User-agent: Googlebot
        Disallow: /
        """
        self.assertEqual(mon.parse_robots_txt(gbot_robots)["blocked_agents"], ["googlebot"])

    def test_parse_sitemap_xml(self) -> None:
        valid, count, err = mon.parse_sitemap_xml(
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://krillion-game.app/</loc></url></urlset>"
        )
        self.assertTrue(valid)
        self.assertEqual(count, 1)
        self.assertEqual(err, "")

        empty_valid, _, err_empty = mon.parse_sitemap_xml(
            '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"></urlset>'
        )
        self.assertFalse(empty_valid)
        self.assertIn("空", err_empty)

    def test_check_http_targets_catches_403_noindex_and_canonical_drift(self) -> None:
        mock_sess = MagicMock()

        def make_resp(status=200, text="", url="https://krillion-game.app/", headers=None):
            r = MagicMock()
            r.status_code = status
            r.text = text
            r.url = url
            r.headers = headers or {"Content-Type": "text/html; charset=utf-8"}
            return r

        responses = {
            "https://krillion-game.app/robots.txt": make_resp(
                200, "User-agent: *\nAllow: /\nSitemap: https://krillion-game.app/sitemap-0.xml\n",
                headers={"Content-Type": "text/plain"},
            ),
            "https://krillion-game.app/": make_resp(
                200,
                '<html><head><link rel="canonical" href="https://krillion-game.app/"></head></html>',
            ),
            "https://krillion-game.app/noillirk/": make_resp(
                403, "Forbidden", url="https://krillion-game.app/noillirk/",
                headers={"Content-Type": "text/html", "cf-mitigated": "challenge"},
            ),
            "https://krillion-game.app/bad-meta/": make_resp(
                200,
                '<html><head><meta name="robots" content="noindex, follow">'
                '<link rel="canonical" href="https://evil.pages.dev/bad-meta/"></head></html>',
                url="https://krillion-game.app/bad-meta/",
            ),
            "https://krillion-game.app/sitemap-0.xml": make_resp(
                200,
                '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://krillion-game.app/</loc></url></urlset>",
                headers={"Content-Type": "application/xml"},
            ),
        }
        mock_sess.get.side_effect = lambda u, **kwargs: responses[u]

        res = mon.check_http_targets(
            "krillion-game.app",
            [
                "https://krillion-game.app/",
                "https://krillion-game.app/noillirk/",
                "https://krillion-game.app/bad-meta/",
            ],
            session=mock_sess,
        )
        keys = {a.key: a for a in res["alerts"]}
        self.assertIn("http:url:/noillirk/", keys)
        self.assertIn("cf-mitigated: challenge", keys["http:url:/noillirk/"].detail)
        self.assertIn("http:noindex:/bad-meta/", keys)
        self.assertIn("http:canonical:/bad-meta/", keys)
        self.assertEqual(keys["http:canonical:/bad-meta/"].severity, "P0")


class TestGscAndGa4Evaluation(unittest.TestCase):
    def test_gsc_sitemaps_errors_and_drop(self) -> None:
        resp = {
            "sitemap": [
                {
                    "path": "https://krillion-game.app/sitemap-0.xml",
                    "errors": "1",
                    "warnings": 0,
                    "contents": [{"type": "web", "submitted": "12"}],
                }
            ]
        }
        alerts, sm_urls, total = mon.evaluate_gsc_sitemaps(resp, "krillion-game.app", prev_submitted=47)
        self.assertEqual(sm_urls, ["https://krillion-game.app/sitemap-0.xml"])
        self.assertEqual(total, 12)
        keys = {a.key for a in alerts}
        self.assertIn("gsc_sitemaps:err:/sitemap-0.xml", keys)
        self.assertIn("gsc_sitemaps:submitted_drop", keys)

    def test_gsc_inspection_403_with_http_200_diagnostic(self) -> None:
        ir = {
            "indexStatusResult": {
                "verdict": "FAIL",
                "coverageState": "Blocked due to access forbidden (403)",
                "robotsTxtState": "ALLOWED",
                "indexingState": "INDEXING_ALLOWED",
                "pageFetchState": "ACCESS_FORBIDDEN",
                "lastCrawlTime": "2026-11-16T05:03:00Z",
                "googleCanonical": "https://krillion-game.app/noillirk/",
                "userCanonical": "https://krillion-game.app/noillirk/",
            }
        }
        alerts, cached = mon.evaluate_gsc_inspection_result(
            url="https://krillion-game.app/noillirk/",
            ir=ir,
            domain="krillion-game.app",
            prev_verdict_info={"verdict": "PASS"},
            known_traffic_clicks=1500.0,
            http_status=200,
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, "P0")
        self.assertIn("Cloudflare WAF", alerts[0].detail)
        self.assertEqual(cached["page_fetch_state"], "ACCESS_FORBIDDEN")

    def test_same_weekday_baseline_and_gsc_traffic_drop(self) -> None:
        now_utc = datetime(2026, 11, 16, 10, 18, tzinfo=timezone.utc)
        t2 = date(2026, 11, 14)  # Saturday
        t1 = date(2026, 11, 15)  # Sunday

        # Build 4 Saturdays (~900 clicks) and weekdays (~1800 clicks), then crash T-2 & T-1
        daily_totals = {}
        for d_off in range(30, 0, -1):
            d = now_utc.date() - timedelta(days=d_off)
            is_weekend = d.weekday() in (5, 6)
            daily_totals[d] = {
                "clicks": 900.0 if is_weekend else 1800.0,
                "impressions": 3000.0 if is_weekend else 6000.0,
                "position": 2.0,
            }
        daily_totals[t2] = {"clicks": 150.0, "impressions": 500.0, "position": 2.0}
        daily_totals[t1] = {"clicks": 10.0, "impressions": 50.0, "position": 2.0}

        daily_by_page = {
            "https://krillion-game.app/noillirk/": dict(daily_totals),
        }
        alerts = mon.evaluate_gsc_traffic_series(
            daily_totals=daily_totals,
            daily_by_page=daily_by_page,
            domain="krillion-game.app",
            now_utc=now_utc,
        )
        keys = {a.key: a for a in alerts}
        self.assertIn("gsc_traffic:site_t2_drop", keys)
        self.assertEqual(keys["gsc_traffic:site_t2_drop"].severity, "P0")
        self.assertIn("gsc_traffic:site_t1_collapse", keys)
        self.assertIn("gsc_traffic:page:/noillirk/", keys)

        # Verify a normal weekend (900 vs weekday 1800) does NOT false alarm!
        daily_totals[t2] = {"clicks": 880.0, "impressions": 2950.0, "position": 2.0}
        daily_totals[t1] = {"clicks": 860.0, "impressions": 2900.0, "position": 2.0}
        daily_by_page["https://krillion-game.app/noillirk/"] = dict(daily_totals)
        no_alerts = mon.evaluate_gsc_traffic_series(
            daily_totals=daily_totals,
            daily_by_page=daily_by_page,
            domain="krillion-game.app",
            now_utc=now_utc,
        )
        self.assertEqual(no_alerts, [])

    def test_ga4_t1_respects_utc_hour_guard(self) -> None:
        today = date(2026, 11, 16)
        series = {}
        for d_off in range(30, 1, -1):
            series[today - timedelta(days=d_off)] = 1200.0
        # T-1 (yesterday) has 0 sessions so far
        series[today - timedelta(days=1)] = 0.0

        # At 02:18 UTC (still evening in US timezones), T-1 must NOT alert yet
        early_alerts = mon.evaluate_ga4_daily_sessions(
            series, "krillion-game.app", datetime(2026, 11, 16, 2, 18, tzinfo=timezone.utc)
        )
        self.assertEqual(early_alerts, [])

        # At 10:18 UTC, T-1 is evaluated and triggers P0 alert
        late_alerts = mon.evaluate_ga4_daily_sessions(
            series, "krillion-game.app", datetime(2026, 11, 16, 10, 18, tzinfo=timezone.utc)
        )
        self.assertEqual(len(late_alerts), 1)
        self.assertEqual(late_alerts[0].key, "ga4:organic_drop")
        self.assertEqual(late_alerts[0].severity, "P0")


class TestAlertReconcileAndTelegram(unittest.TestCase):
    def test_dedup_escalation_probe_guard_and_recovery(self) -> None:
        t0 = datetime(2026, 11, 16, 2, 18, tzinfo=timezone.utc)
        alert_p1 = mon.Alert(
            key="gsc_inspect:status:/noillirk/",
            probe="gsc_inspect",
            severity="P1",
            title="异常",
            detail="detail",
            url="https://krillion-game.app/noillirk/",
            signature="verdict:NEUTRAL",
        )

        # Run 1: new alert notifies
        notify1, resolved1, state1 = mon.reconcile_alerts(
            [alert_p1], {"active_alerts": {}}, t0, {"gsc_inspect"}, set(), cooldown_hours=24
        )
        self.assertEqual(len(notify1), 1)
        self.assertEqual(resolved1, [])

        # Run 2 (+8h): same signature/severity within cooldown -> silent
        t1 = t0 + timedelta(hours=8)
        notify2, _, state2 = mon.reconcile_alerts(
            [alert_p1], {"active_alerts": state1}, t1, {"gsc_inspect"}, set(), cooldown_hours=24
        )
        self.assertEqual(notify2, [])

        # Run 3 (+10h): escalates to P0 -> immediate notify
        alert_p0 = mon.Alert(
            key="gsc_inspect:status:/noillirk/",
            probe="gsc_inspect",
            severity="P0",
            title="GSC 403",
            detail="403",
            url="https://krillion-game.app/noillirk/",
            signature="fetch:ACCESS_FORBIDDEN|verdict:FAIL",
        )
        notify3, _, state3 = mon.reconcile_alerts(
            [alert_p0], {"active_alerts": state2}, t0 + timedelta(hours=10), {"gsc_inspect"}, set(), cooldown_hours=24
        )
        self.assertEqual(len(notify3), 1)

        # Run 4: URL inspection failed for this URL -> MUST NOT resolve falsely!
        notify4, resolved4, state4 = mon.reconcile_alerts(
            [], {"active_alerts": state3}, t0 + timedelta(hours=12), {"gsc_inspect"}, {"https://krillion-game.app/noillirk/"}, cooldown_hours=24
        )
        self.assertEqual(notify4, [])
        self.assertEqual(resolved4, [])
        self.assertIn("gsc_inspect:status:/noillirk/", state4)

        # Run 5: URL inspection succeeded & returned healthy -> resolves cleanly!
        notify5, resolved5, state5 = mon.reconcile_alerts(
            [], {"active_alerts": state4}, t0 + timedelta(hours=14), {"gsc_inspect"}, set(), cooldown_hours=24
        )
        self.assertEqual(notify5, [])
        self.assertEqual(len(resolved5), 1)
        self.assertEqual(state5, {})

    def test_telegram_html_escaping_and_silence(self) -> None:
        self.assertEqual(
            mon.format_telegram_messages("krillion-game.app", [], [], [], {}, force_summary=False),
            [],
        )
        summary = mon.format_telegram_messages(
            "krillion-game.app",
            [],
            [],
            [],
            {"inspected_urls_count": 10, "http_ok_count": 10, "sitemap_submitted": 47},
            force_summary=True,
        )
        self.assertEqual(len(summary), 1)
        self.assertIn("SEO 健康巡检正常", summary[0])

        alert = mon.Alert(
            key="http:url:/a_b?x=1&y=<tag>",
            probe="http",
            severity="P0",
            title="Err <tag> & _underscore_",
            detail="https://krillion-game.app/a_b?x=1&y=<tag>",
        )
        chunks = mon.format_telegram_messages("krillion-game.app", [alert], [], [], {})
        self.assertEqual(len(chunks), 1)
        self.assertIn("&lt;tag&gt; &amp; _underscore_", chunks[0])


if __name__ == "__main__":
    unittest.main()
