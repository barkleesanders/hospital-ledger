import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "refresh_artifacts.py"
SPEC = importlib.util.spec_from_file_location("refresh_artifacts", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class RefreshArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        MODULE.ROOT = root
        MODULE.PARSED = root / "data" / "parsed"
        MODULE.PRICES = root / "public" / "data" / "prices"
        MODULE.PUBLIC_DATA = root / "public" / "data"
        MODULE.PARSED.mkdir(parents=True)
        MODULE.PRICES.mkdir(parents=True)
        self.ccns = root / "ccns.txt"
        self.ccns.write_text("123456\n654321\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_failed_parse_restores_last_good_artifacts(self):
        (MODULE.PARSED / "123456.json.gz").write_bytes(b"old-raw")
        (MODULE.PRICES / "123456.json").write_text('{"n_slim":2}')
        (MODULE.PRICES / "index.json").write_text('{"old":true}')
        backup = MODULE.ROOT / "backup"
        MODULE.backup(self.ccns, backup)

        (MODULE.PARSED / "123456.json").write_text('{"row_count":0}')
        (MODULE.PARSED / "123456.json.gz").unlink()
        (MODULE.PRICES / "123456.json").write_text('{"n_slim":0}')
        keep = MODULE.ROOT / "keep.txt"
        keep.write_text("654321\n")
        MODULE.restore_except(self.ccns, keep, backup)

        self.assertFalse((MODULE.PARSED / "123456.json").exists())
        self.assertEqual((MODULE.PARSED / "123456.json.gz").read_bytes(), b"old-raw")
        self.assertEqual(json.loads((MODULE.PRICES / "123456.json").read_text())["n_slim"], 2)
        self.assertEqual(json.loads((MODULE.PRICES / "index.json").read_text()), {"old": True})

    def test_classifiers_require_rows_and_displayable_output(self):
        (MODULE.PARSED / "123456.json").write_text('{"row_count":12,"items":[]}')
        (MODULE.PARSED / "654321.json").write_text('{"row_count":0,"items":[]}')
        parsed = MODULE.ROOT / "parsed-success.txt"
        self.assertEqual(MODULE.classify_parsed(self.ccns, parsed), ["123456"])
        (MODULE.PRICES / "123456.json").write_text('{"n_slim":3}')
        (MODULE.PRICES / "654321.json").write_text('{"n_slim":0}')
        display = MODULE.ROOT / "display-success.txt"
        self.assertEqual(MODULE.classify_display(self.ccns, display), ["123456"])

    def test_publication_keys_cover_every_live_mapping(self):
        success = MODULE.ROOT / "success.txt"
        success.write_text("123456\n")
        (MODULE.PUBLIC_DATA / "payer").mkdir()
        (MODULE.PUBLIC_DATA / "cpt-detail").mkdir()
        (MODULE.PUBLIC_DATA / "payer" / "aetna.json").write_text("{}")
        (MODULE.PUBLIC_DATA / "cpt-detail" / "99213.json").write_text("{}")
        output = MODULE.ROOT / "keys.txt"
        keys = MODULE.publication_keys(success, output)
        self.assertIn("prices/123456.json", keys)
        self.assertIn("prices/index.json", keys)
        self.assertIn("indexes/cpt-index.json", keys)
        self.assertIn("aggregates/compliance-ranking.json", keys)
        self.assertIn("aggregates/payers-index.json", keys)
        self.assertIn("aggregates/payer/aetna.json", keys)
        self.assertIn("aggregates/cpt-detail/99213.json", keys)


if __name__ == "__main__":
    unittest.main()
