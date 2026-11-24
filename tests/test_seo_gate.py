import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import seo_gate as gate


def _write_page(dist: Path, rel_path: str, html_body: str) -> None:
    target = dist / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html_body, encoding="utf-8")


def _write_sitemap(dist: Path, urls: list[str]) -> None:
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{locs}</urlset>"
    )
    (dist / "sitemap-0.xml").write_text(xml, encoding="utf-8")


def _unique_words(prefix: str, count: int) -> str:
    return " ".join(f"{prefix}w{i}" for i in range(count))


class TestSeoGate(unittest.TestCase):
    def test_clean_tool_and_guide_site_passes_all_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            _write_page(
                dist,
                "index.html",
                f"""<!doctype html><html><head>
                <title>NovaGame: Play Free Daily Word Puzzle</title>
                <link rel="canonical" href="https://novagame.test/">
                </head><body><main>
                <h1>NovaGame</h1>
                <input type="text" placeholder="Type guess">
                <article><p>{_unique_words("home", 450)}</p>
                <a href="/guides/scoring/">Scoring Guide</a></article>
                </main></body></html>""",
            )
            _write_page(
                dist,
                "guides/scoring/index.html",
                f"""<!doctype html><html><head>
                <title>NovaGame Scoring Rules & Depth Chart</title>
                <link rel="canonical" href="https://novagame.test/guides/scoring/">
                </head><body><main>
                <h1>NovaGame Scoring Rules</h1>
                <article><p>{_unique_words("score", 600)}</p>
                <a href="/">Play NovaGame</a></article>
                </main></body></html>""",
            )
            _write_sitemap(dist, ["https://novagame.test/", "https://novagame.test/guides/scoring/"])

            rc = gate.main(["--dist", str(dist), "--format", "json"])
            self.assertEqual(rc, 0)

    def test_catches_all_five_gate_violations_and_ratchet_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp)
            shared_boilerplate = _unique_words("shared", 200)

            # 1. Homepage (links to pages so they aren't orphans)
            _write_page(
                dist,
                "index.html",
                f"""<!doctype html><html><head>
                <title>DemoGame Home</title>
                <link rel="canonical" href="https://demo.test/">
                </head><body><main>
                <h1>DemoGame</h1>
                <input type="text">
                <article><p>{_unique_words("root", 450)}</p>
                <a href="/blitz/">Blitz</a>
                <a href="/modes/blitz-mode/">Blitz Guide</a>
                <a href="/heavy/">Heavy</a>
                <a href="/packs/alpha/">Alpha</a>
                <a href="/packs/beta/">Beta</a>
                </article></main></body></html>""",
            )

            # 2. Overlong + pseudo-academic tool page + twin cannibalization with /modes/blitz-mode/
            _write_page(
                dist,
                "blitz/index.html",
                f"""<!doctype html><html><head>
                <title>Play DemoGame Blitz Online</title>
                <link rel="canonical" href="https://demo.test/blitz/">
                </head><body><main>
                <h1>DemoGame Blitz</h1>
                <canvas id="board"></canvas>
                <article>
                  <h2>Peer-Reviewed Academic References</h2>
                  <p>{_unique_words("blitztool", 2150)}</p>
                </article></main></body></html>""",
            )
            _write_page(
                dist,
                "modes/blitz-mode/index.html",
                f"""<!doctype html><html><head>
                <title>DemoGame Blitz Mode Guide & Rules</title>
                <link rel="canonical" href="https://demo.test/modes/blitz-mode/">
                </head><body><main>
                <h1>DemoGame Blitz Mode Guide</h1>
                <article><p>{_unique_words("blitzguide", 600)}</p></article>
                </main></body></html>""",
            )

            # 3. Oversized HTML payload (>250 KB)
            huge_details = "<details>" + ("<span>item</span>" * 20000) + "</details>"
            _write_page(
                dist,
                "heavy/index.html",
                f"""<!doctype html><html><head>
                <title>Heavy Payload Answers</title>
                <link rel="canonical" href="https://demo.test/heavy/">
                </head><body><main>
                <h1>Heavy Answers</h1>
                {huge_details}
                <article><p>{_unique_words("heavy", 500)}</p></article>
                </main></body></html>""",
            )

            # 4. Template overlap cluster under /packs/*
            for name in ("alpha", "beta"):
                _write_page(
                    dist,
                    f"packs/{name}/index.html",
                    f"""<!doctype html><html><head>
                    <title>DemoGame {name.title()} Pack</title>
                    <link rel="canonical" href="https://demo.test/packs/{name}/">
                    </head><body><main>
                    <h1>{name.title()} Pack</h1>
                    <input type="text">
                    <article><p>{shared_boilerplate} {_unique_words(name, 160)}</p></article>
                    </main></body></html>""",
                )

            # 5. Contradictory sitemap + noindex page
            _write_page(
                dist,
                "oops/index.html",
                """<!doctype html><html><head>
                <title>Oops Noindex Page</title>
                <meta name="robots" content="noindex, nofollow">
                <link rel="canonical" href="https://demo.test/oops/">
                </head><body><main><h1>Oops</h1></main></body></html>""",
            )

            _write_sitemap(
                dist,
                [
                    "https://demo.test/",
                    "https://demo.test/blitz/",
                    "https://demo.test/modes/blitz-mode/",
                    "https://demo.test/heavy/",
                    "https://demo.test/packs/alpha/",
                    "https://demo.test/packs/beta/",
                    "https://demo.test/oops/",
                ],
            )

            records, findings = gate.scan_site(dist, dict(gate.DEFAULT_CONFIG))
            self.assertEqual(len(records), 7)

            red_gates = {f.gate for f in findings if f.severity == "RED"}
            self.assertEqual(
                red_gates,
                {"page_budget", "payload_bloat", "template_overlap", "intent_cannibalization", "index_hygiene"},
            )

            # Strict fails (exit 1), warn-only succeeds (exit 0)
            self.assertEqual(gate.main(["--dist", str(dist)]), 1)
            self.assertEqual(gate.main(["--dist", str(dist), "--warn-only"]), 0)

            # Write baseline -> ratchet mode now exits 0 with zero NEW RED findings
            baseline_file = dist / "seo-gate-baseline.json"
            gate.write_baseline(findings, baseline_file)
            self.assertEqual(gate.main(["--dist", str(dist), "--baseline", str(baseline_file)]), 0)

            # Introduce a brand-new RED violation -> ratchet mode catches it and exits 1
            _write_page(
                dist,
                "broken/index.html",
                """<!doctype html><html><head><title></title></head><body><main></main></body></html>""",
            )
            _write_sitemap(
                dist,
                [
                    "https://demo.test/",
                    "https://demo.test/blitz/",
                    "https://demo.test/modes/blitz-mode/",
                    "https://demo.test/heavy/",
                    "https://demo.test/packs/alpha/",
                    "https://demo.test/packs/beta/",
                    "https://demo.test/oops/",
                    "https://demo.test/broken/",
                ],
            )
            self.assertEqual(gate.main(["--dist", str(dist), "--baseline", str(baseline_file)]), 1)

            # Verify fixed baseline detection when /oops/ is removed from sitemap
            _write_sitemap(
                dist,
                [
                    "https://demo.test/",
                    "https://demo.test/blitz/",
                    "https://demo.test/modes/blitz-mode/",
                    "https://demo.test/heavy/",
                    "https://demo.test/packs/alpha/",
                    "https://demo.test/packs/beta/",
                ],
            )
            _, after_fix_findings = gate.scan_site(dist, dict(gate.DEFAULT_CONFIG))
            _, fixed_keys = gate.apply_baseline(after_fix_findings, baseline_file)
            self.assertIn("index_hygiene:sitemap_noindex:/oops/", fixed_keys)

    def test_base_url_mode_for_nextjs_ssr_server(self) -> None:
        routes: dict[str, tuple[int, str, str]] = {
            "/sitemap.xml": (
                200,
                "application/xml",
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>https://nextgame.example/</loc></url>"
                "<url><loc>https://nextgame.example/guides/rules/</loc></url>"
                "<url><loc>https://nextgame.example/missing-route/</loc></url>"
                "<url><loc>https://nextgame.example/old-mode/</loc></url>"
                "</urlset>",
            ),
            "/": (
                200,
                "text/html; charset=utf-8",
                f"""<!doctype html><html><head>
                <title>NextGame: Play Free Online Puzzle</title>
                <link rel="canonical" href="https://nextgame.example/">
                </head><body><main>
                <h1>NextGame</h1>
                <input type="text">
                <article><p>{_unique_words("nxhome", 450)}</p>
                <a href="https://nextgame.example/guides/rules/">Rules Guide</a>
                <a href="/old-mode/">Old Mode</a></article>
                </main></body></html>""",
            ),
            "/guides/rules/": (
                200,
                "text/html; charset=utf-8",
                f"""<!doctype html><html><head>
                <title>NextGame Rules & Scoring Breakdown</title>
                <link rel="canonical" href="https://nextgame.example/guides/rules/">
                </head><body><main>
                <h1>NextGame Rules</h1>
                <article><p>{_unique_words("nxrules", 500)}</p>
                <a href="/">Play NextGame</a></article>
                </main></body></html>""",
            ),
            "/new-mode/": (
                200,
                "text/html; charset=utf-8",
                f"""<!doctype html><html><head>
                <title>NextGame New Mode</title>
                <link rel="canonical" href="https://nextgame.example/new-mode/">
                </head><body><main>
                <h1>NextGame New Mode</h1>
                <article><p>{_unique_words("nxnew", 450)}</p></article>
                </main></body></html>""",
            ),
        }

        class MockNextHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                return

            def do_GET(self) -> None:
                if self.path == "/old-mode/":
                    self.send_response(301)
                    self.send_header("Location", "/new-mode/")
                    self.end_headers()
                    return
                entry = routes.get(self.path)
                if not entry:
                    self.send_response(404)
                    self.end_headers()
                    return
                code, ctype, body = entry
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), MockNextHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{port}"
            records, findings = gate.scan_live_site(base_url, dict(gate.DEFAULT_CONFIG))
            self.assertEqual(len(records), 4)
            keys = {f.key for f in findings}
            self.assertIn("index_hygiene:http_error:/missing-route/", keys)
            self.assertIn("index_hygiene:redirect:/old-mode/", keys)
            # Clean homepage & rules page (canonical with prod domain matches path) should have zero errors
            self.assertFalse(any(f.target in ("/", "/guides/rules/") for f in findings))
            # CLI strict exits 1 due to /missing-route/ HTTP 404
            self.assertEqual(gate.main(["--base-url", base_url, "--format", "json"]), 1)
            self.assertEqual(gate.main(["--base-url", base_url, "--warn-only"]), 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_detect_project_target_astro_and_nextjs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # Empty project -> none
            self.assertEqual(gate.detect_project_target(root), ("none", None))

            # Next.js SSR (.next/BUILD_ID) -> serve
            (root / ".next").mkdir()
            (root / ".next" / "BUILD_ID").write_text("build123", encoding="utf-8")
            kind, cmd = gate.detect_project_target(root)
            self.assertEqual(kind, "serve")
            self.assertIn("start", cmd)
            self.assertIn("{port}", cmd)

            # Next.js static export (out/) takes precedence over .next/
            (root / "out").mkdir()
            (root / "out" / "index.html").write_text("<html></html>", encoding="utf-8")
            self.assertEqual(gate.detect_project_target(root), ("dist", root / "out"))

            # Astro static (dist/) takes top precedence
            (root / "dist").mkdir()
            (root / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
            self.assertEqual(gate.detect_project_target(root), ("dist", root / "dist"))

    def test_project_auto_detect_astro_loads_config_and_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            dist = project / "dist"
            _write_page(
                dist,
                "index.html",
                f"""<!doctype html><html><head><title>Astro Game</title>
                <link rel="canonical" href="https://example.com/" /></head>
                <body><main><h1>Astro Game</h1><canvas></canvas>
                <p>{_unique_words("home", 120)}</p></main></body></html>""",
            )
            _write_page(
                dist,
                "thin/index.html",
                """<!doctype html><html><head><title>Thin Guide</title>
                <link rel="canonical" href="https://example.com/thin/" /></head>
                <body><main><h1>Thin Guide</h1><p>Too short.</p></main></body></html>""",
            )
            _write_sitemap(dist, ["https://example.com/", "https://example.com/thin/"])

            # Strict fails due to thin content RED
            self.assertEqual(gate.main(["--project", str(project)]), 1)

            # Write baseline into project root
            baseline_file = project / "seo-gate-baseline.json"
            self.assertEqual(
                gate.main(["--project", str(project), "--write-baseline", str(baseline_file), "--warn-only"]),
                0,
            )
            self.assertTrue(baseline_file.is_file())

            # --project auto-loads seo-gate-baseline.json and passes in ratchet mode
            self.assertEqual(gate.main(["--project", str(project)]), 0)

            # --no-baseline forces strict mode even when baseline exists
            self.assertEqual(gate.main(["--project", str(project), "--no-baseline"]), 1)

    def test_serve_cmd_boots_and_stops_local_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            site = Path(tmp)
            _write_page(
                site,
                "index.html",
                f"""<!doctype html><html><head><title>SSR Tool</title>
                <link rel="canonical" href="https://example.com/" /></head>
                <body><main><h1>SSR Tool</h1><canvas></canvas>
                <p>{_unique_words("ssr", 150)}</p></main></body></html>""",
            )
            _write_sitemap(site, ["https://example.com/"])

            serve_cmd = f'"{sys.executable}" -m http.server {{port}} --bind 127.0.0.1 --directory "{site}"'
            rc = gate.main(["--serve-cmd", serve_cmd, "--serve-timeout", "10", "--format", "json"])
            self.assertEqual(rc, 0)

    def test_serve_cmd_early_exit_returns_error_fast(self) -> None:
        rc = gate.main(["--serve-cmd", f'"{sys.executable}" -c "import sys; sys.exit(3)"', "--serve-timeout", "5"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
