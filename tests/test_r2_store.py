import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "r2_store.py"
SPEC = importlib.util.spec_from_file_location("r2_store", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)
ClientError = MODULE.ClientError


class FakePaginator:
    def __init__(self, client):
        self.client = client

    def paginate(self, *, Bucket, Prefix):
        contents = [
            {"Key": key, "Size": len(value["body"])}
            for (bucket, key), value in self.client.objects.items()
            if bucket == Bucket and key.startswith(Prefix)
        ]
        return [{"Contents": contents}]


class FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, *, Bucket, Key):
        value = self.objects.get((Bucket, Key))
        if value is None:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "missing"}},
                "HeadObject",
            )
        return {
            "ContentLength": len(value["body"]),
            "Metadata": value["metadata"],
        }

    def upload_file(self, source, bucket, key, ExtraArgs):
        self.objects[(bucket, key)] = {
            "body": Path(source).read_bytes(),
            "metadata": dict(ExtraArgs["Metadata"]),
        }

    def download_file(self, bucket, key, destination):
        Path(destination).write_bytes(self.objects[(bucket, key)]["body"])

    def copy_object(self, *, Bucket, Key, CopySource, MetadataDirective):
        value = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        self.objects[(Bucket, Key)] = {
            "body": value["body"],
            "metadata": dict(value["metadata"]),
        }

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, name):
        self.assert_list_operation(name)
        return FakePaginator(self)

    @staticmethod
    def assert_list_operation(name):
        if name != "list_objects_v2":
            raise AssertionError(name)


class R2StoreTests(unittest.TestCase):
    def test_list_and_delete_keys_are_explicit(self):
        client = FakeS3()
        client.objects[("bucket", "aggregates/payer/a.json")] = {"body": b"a", "metadata": {}}
        client.objects[("bucket", "aggregates/cpt-detail/1.json")] = {"body": b"1", "metadata": {}}
        client.objects[("bucket", "prices/keep.json")] = {"body": b"p", "metadata": {}}
        keys = MODULE.list_keys(
            client,
            bucket="bucket",
            prefixes=["aggregates/payer/", "aggregates/cpt-detail/"],
        )
        self.assertEqual(
            keys,
            ["aggregates/cpt-detail/1.json", "aggregates/payer/a.json"],
        )
        self.assertEqual(
            MODULE.delete_keys(client, bucket="bucket", keys=keys[:1], workers=1),
            {"deleted": 1},
        )
        self.assertIn(("bucket", "aggregates/payer/a.json"), client.objects)
        self.assertIn(("bucket", "prices/keep.json"), client.objects)

    def test_snapshot_pruning_retains_newest_runs_only(self):
        client = FakeS3()
        for run_id in (
            "2026-07-20T04-17-00Z",
            "2026-07-27T04-17-00Z",
            "2026-08-03T04-17-00Z",
        ):
            key = f"_pipeline/rollback/{run_id}/prices/index.json"
            client.objects[("bucket", key)] = {"body": run_id.encode(), "metadata": {}}
        client.objects[("bucket", "_pipeline/rollback/notes.txt")] = {"body": b"keep", "metadata": {}}
        result = MODULE.prune_snapshots(
            client,
            bucket="bucket",
            prefix="_pipeline/rollback",
            retain=2,
            workers=1,
        )
        self.assertEqual(result, {"runs": 3, "retained": 2, "deleted": 1})
        self.assertNotIn(
            ("bucket", "_pipeline/rollback/2026-07-20T04-17-00Z/prices/index.json"),
            client.objects,
        )
        self.assertIn(("bucket", "_pipeline/rollback/notes.txt"), client.objects)

    def test_snapshot_restore_reverts_changed_and_new_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            client = FakeS3()
            old = root / "old.json"
            old.write_bytes(b"old")
            client.upload_file(
                str(old),
                "bucket",
                "prices/old.json",
                {"Metadata": {"sha256": MODULE.sha256_file(old)}},
            )
            manifest = root / "rollback.json"
            MODULE.snapshot_keys(
                client,
                bucket="bucket",
                snapshot_prefix="_pipeline/rollback/run",
                keys=["prices/new.json", "prices/old.json"],
                manifest_path=manifest,
                workers=1,
            )
            client.objects[("bucket", "prices/old.json")]["body"] = b"changed"
            client.objects[("bucket", "prices/new.json")] = {"body": b"new", "metadata": {}}
            result = MODULE.restore_snapshot(client, manifest_path=manifest, workers=1)
            self.assertEqual(result, {"restored": 1, "deleted": 1})
            self.assertEqual(client.objects[("bucket", "prices/old.json")]["body"], b"old")
            self.assertNotIn(("bucket", "prices/new.json"), client.objects)

    def test_download_replaces_same_size_file_when_hash_differs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "state.json"
            destination.write_bytes(b"old!")
            client = FakeS3()
            source = root / "remote.json"
            source.write_bytes(b"new!")
            client.upload_file(
                str(source),
                "bucket",
                "state.json",
                {"Metadata": {"sha256": MODULE.sha256_file(source)}},
            )
            result = MODULE.download_one(
                client,
                "bucket",
                "state.json",
                destination,
                4,
                MODULE.sha256_file(source),
            )
            self.assertEqual(result, "downloaded")
            self.assertEqual(destination.read_bytes(), b"new!")

    def test_upload_is_content_addressed_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "123456.json"
            source.write_text('{"value":1}')
            client = FakeS3()
            first = MODULE.upload_paths(
                client,
                bucket="bucket",
                prefix="prices/",
                root=root,
                paths=[source],
                workers=1,
            )
            second = MODULE.upload_paths(
                client,
                bucket="bucket",
                prefix="prices/",
                root=root,
                paths=[source],
                workers=1,
            )
            self.assertEqual(first["uploaded"], 1)
            self.assertEqual(second["unchanged"], 1)

    def test_ccn_list_maps_to_expected_extension(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ccns = root / "ccns.txt"
            ccns.write_text("123456\n654321\n")
            paths = MODULE.paths_from_ccns(root, ccns, ".json.gz")
            self.assertEqual(
                [path.name for path in paths],
                ["123456.json.gz", "654321.json.gz"],
            )


if __name__ == "__main__":
    unittest.main()
