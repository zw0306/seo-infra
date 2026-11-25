import csv
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import similarweb_keywords as sw  # noqa: E402


class TestSimilarwebKeywords(unittest.TestCase):
    def test_origin_resolution_and_classification(self):
        self.assertEqual(sw.normalize_origin("pro.similarweb.com/foo"), "https://pro.similarweb.com")
        self.assertEqual(sw.normalize_origin("http://198.51.100.10:18020/#/abc"), "http://198.51.100.10:18020")
        self.assertEqual(sw.classify_source("https://pro.similarweb.com"), "official")
        self.assertEqual(sw.classify_source("http://198.51.100.10:18020"), "mirror")

        self.assertIsNone(sw.resolve_target_origin("bind"))
        self.assertEqual(sw.resolve_target_origin("official"), "https://pro.similarweb.com")
        self.assertEqual(
            sw.resolve_target_origin("mirror", env={"SIMILARWEB_MIRROR_URL": "http://198.51.100.10:18020"}),
            "http://198.51.100.10:18020",
        )
        self.assertEqual(
            sw.resolve_target_origin("bind", base_url="http://198.51.100.10:18020"),
            "http://198.51.100.10:18020",
        )
        with self.assertRaises(sw.SimilarwebError):
            sw.resolve_target_origin("mirror", env={})

    def test_compute_date_window(self):
        start, end = sw.compute_date_window(today=dt.date(2026, 11, 25))
        self.assertEqual((start.isoformat(), end.isoformat()), ("2026-10-26", "2026-11-22"))

        start2, end2 = sw.compute_date_window(from_date="2026-10-01", to_date="2026-10-28")
        self.assertEqual((start2.isoformat(), end2.isoformat()), ("2026-10-01", "2026-10-28"))

        with self.assertRaises(sw.SimilarwebError):
            sw.compute_date_window(from_date="2026-11-01", to_date="2026-10-01")
        with self.assertRaises(sw.SimilarwebError):
            sw.compute_date_window(from_date="2026-10-01")

    def test_build_suggest_path(self):
        path = sw.build_suggest_path(
            "krillion",
            999,
            dt.date(2026, 10, 26),
            dt.date(2026, 11, 22),
            query_type="Broad",
            label="Questions",
            page=2,
        )
        self.assertTrue(path.startswith("/api/KeywordGenerator/google/suggest?"))
        self.assertIn("keyword=krillion", path)
        self.assertIn("country=999", path)
        self.assertIn("from=2026%7C10%7C26", path)
        self.assertIn("to=2026%7C11%7C22", path)
        self.assertIn("label=Questions", path)
        self.assertIn("page=2", path)

    def test_extract_json_payload_handles_banners_and_envelopes(self):
        raw_banner = (
            "Update available: v1.8.7 -> v2.0.3\n"
            '{"result": "{\\"ok\\": true, \\"status\\": 200, \\"data\\": {\\"totalRecords\\": 42}}"}'
        )
        parsed = sw.extract_json_payload(raw_banner)
        self.assertEqual(parsed["status"], 200)
        self.assertEqual(parsed["data"]["totalRecords"], 42)

        direct = sw.extract_json_payload('{"ok": true, "records": [1, 2]}')
        self.assertEqual(direct["records"], [1, 2])

    def test_detect_access_block(self):
        self.assertIsNotNone(sw.detect_access_block(401, "text/html", "https://pro.similarweb.com/", ""))
        self.assertIsNotNone(sw.detect_access_block(403, "text/html", "https://pro.similarweb.com/", ""))
        self.assertIsNotNone(
            sw.detect_access_block(200, "text/html", "https://pro.similarweb.com/", "Please verify you are human")
        )
        self.assertIsNotNone(
            sw.detect_access_block(200, "text/html", "https://account.similarweb.com/login", "Sign in")
        )
        self.assertIsNone(sw.detect_access_block(200, "application/json", "https://pro.similarweb.com/#/a", ""))

    def test_dedupe_write_cluster_and_draft(self):
        raw = [
            {"keyword": "krillion", "windowVolume": 500000, "averageVolume": 100000, "clicks": 400000, "difficulty": 29, "primaryIntent": "Navigational", "leadingSite": "krillion.io"},
            {"keyword": "Krillion", "windowVolume": 10, "difficulty": 29},
            {"keyword": "krillion sports", "windowVolume": 60000, "difficulty": 5, "primaryIntent": "Informational", "leadingSite": "krillion.io"},
            {"keyword": "krillion sports pack", "windowVolume": 12000, "difficulty": 2, "primaryIntent": "Informational", "leadingSite": "krillion.to"},
            {"keyword": "krillion geography", "windowVolume": 48000, "difficulty": 1, "primaryIntent": "Informational", "leadingSite": "krillion.to"},
        ]
        deduped = sw.dedupe_and_sort_records(raw)
        self.assertEqual(len(deduped), 4)
        self.assertEqual(deduped[0]["keyword"], "krillion")
        self.assertEqual(deduped[1]["keyword"], "krillion sports")

        payload = {
            "fetchedAt": "2026-11-25",
            "seed": "krillion",
            "sourceType": "mirror",
            "dateRange": "2026-10-26..2026-11-22",
            "country": "Worldwide (999)",
            "countryCode": 999,
            "totalRecords": 4,
            "phraseMatch": deduped,
            "trending": deduped[:2],
            "questions": [],
        }

        with tempfile.TemporaryDirectory() as tmp:
            j_path, c_path = sw.write_outputs(Path(tmp), "krillion-keywords-2026-11", payload)
            self.assertTrue(j_path.exists())
            self.assertTrue(c_path.exists())
            # Reload both JSON and CSV cleanly
            reloaded_json = sw.load_export_file(j_path)
            self.assertEqual(len(reloaded_json["phraseMatch"]), 4)
            reloaded_csv = sw.load_export_file(c_path)
            self.assertEqual(len(reloaded_csv["phraseMatch"]), 4)
            self.assertEqual(reloaded_csv["phraseMatch"][1]["difficulty"], 5)

            with c_path.open("r", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
                self.assertEqual(rows[0]["keyword"], "krillion")

        clusters = sw.cluster_keywords(deduped, seed="krillion")
        mods = [c["modifier"] for c in clusters]
        self.assertIn("sports", mods)
        self.assertIn("geography", mods)

        md = sw.format_analysis_markdown(payload, max_kd=10)
        self.assertIn("Low-KD Goldmine Keywords", md)
        self.assertIn("krillion geography", md)

        draft = sw.build_gate_keywords_draft(payload)
        self.assertIn("/", draft["pages"])
        self.assertIn("/sports/", draft["pages"])
        self.assertEqual(draft["pages"]["/sports/"]["primary"], "krillion sports")


if __name__ == "__main__":
    unittest.main()
