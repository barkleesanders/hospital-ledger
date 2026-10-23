#!/usr/bin/env python3
"""Regression tests for the 2026-10-23 mrf_parse web-asset guard.

CCN 150042 (wave 41): the hospital's xlsx MRF URL returned an HTML
bot-wall interstitial whose only file-like href was the site's web-app
manifest (</icons/manifest.json> from <link rel="manifest">). The hub
extractor scored it as an MRF candidate, parsed it as pricing JSON,
and produced a 0-item price file that poisoned the whole publish set.
score_candidate_url must never score web assets (app manifests,
favicons, robots/sitemaps, service workers, anything under /icons/).

Run: ~/hospital-ledger/.venv/bin/python3 test_mrf_parse_guard.py
"""
import importlib.util
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mrf_parse", HERE / "mrf_parse.py")
mp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mp)


class WebAssetGuard(unittest.TestCase):
    def test_app_manifest_rejected(self):
        self.assertEqual(mp.score_candidate_url(
            "https://www.medstarhealth.org/-/media/project/mho/medstar/"
            "icons/manifest.json"), 0)

    def test_favicon_rejected(self):
        self.assertEqual(mp.score_candidate_url(
            "https://example.org/favicon.ico"), 0)

    def test_robots_and_sitemap_rejected(self):
        self.assertEqual(mp.score_candidate_url(
            "https://example.org/robots.txt"), 0)
        self.assertEqual(mp.score_candidate_url(
            "https://example.org/sitemap.xml"), 0)

    def test_service_worker_rejected(self):
        self.assertEqual(mp.score_candidate_url(
            "https://example.org/service-worker.js"), 0)

    def test_legit_json_mrf_scores(self):
        self.assertGreater(mp.score_candidate_url(
            "https://example.org/files/520591607_hospital_standardcharges.json"), 0)

    def test_legit_csv_mrf_scores(self):
        self.assertGreater(mp.score_candidate_url(
            "https://example.org/files/charges.csv"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
