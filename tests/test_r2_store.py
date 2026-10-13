import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
        self.delete_calls = []
        self.before_conditional_put = None

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
            "ETag": value["etag"],
        }

    def put_object(
        self,
        *,
        Bucket,
        Key,
        Body,
        ContentType,
        Metadata,
        IfNoneMatch=None,
        IfMatch=None,
    ):
        if IfMatch is not None and self.before_conditional_put is not None:
            callback = self.before_conditional_put
            self.before_conditional_put = None
            callback(self, Bucket, Key)
        existing = self.objects.get((Bucket, Key))
        if IfNoneMatch == "*" and existing is not None:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "exists"}},
                "PutObject",
            )
        if IfMatch is not None and (existing is None or existing["etag"] != IfMatch):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "changed"}},
                "PutObject",
            )
        body = bytes(Body)
        self.objects[(Bucket, Key)] = {
            "body": body,
            "metadata": dict(Metadata),
            "etag": hashlib.md5(body).hexdigest(),
        }

    def upload_file(self, source, bucket, key, ExtraArgs):
        self.objects[(bucket, key)] = {
            "body": Path(source).read_bytes(),
            "metadata": dict(ExtraArgs["Metadata"]),
            "etag": MODULE.sha256_file(Path(source)),
        }

    def download_file(self, bucket, key, destination):
        Path(destination).write_bytes(self.objects[(bucket, key)]["body"])

    def copy_object(self, *, Bucket, Key, CopySource, MetadataDirective):
        value = self.objects[(CopySource["Bucket"], CopySource["Key"])]
        self.objects[(Bucket, Key)] = {
            "body": value["body"],
            "metadata": dict(value["metadata"]),
            "etag": value["etag"],
        }

    def delete_object(self, *, Bucket, Key, IfMatch=None):
        self.delete_calls.append({"Bucket": Bucket, "Key": Key, "IfMatch": IfMatch})
        existing = self.objects.get((Bucket, Key))
        if IfMatch is not None and (existing is None or existing["etag"] != IfMatch):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "changed"}},
                "DeleteObject",
            )
        self.objects.pop((Bucket, Key), None)

    def get_paginator(self, name):
        self.assert_list_operation(name)
        return FakePaginator(self)

    @staticmethod
    def assert_list_operation(name):
        if name != "list_objects_v2":
            raise AssertionError(name)


class R2StoreTests(unittest.TestCase):
    @staticmethod
    def object(body=b"x", metadata=None):
        return {
            "body": body,
            "metadata": metadata or {},
            "etag": hashlib.md5(body).hexdigest(),
        }

    def test_make_client_passes_optional_session_token(self):
        if MODULE.boto3 is None:
            self.skipTest("boto3 is not installed")
        environment = {
            "R2_ACCOUNT_ID": "test-account",
            "R2_ACCESS_KEY_ID": "test-access-key",
            "R2_SECRET_ACCESS_KEY": "test-secret-key",
            "R2_SESSION_TOKEN": "test-session-token",
        }
        with (
            mock.patch.dict(MODULE.os.environ, environment, clear=True),
            mock.patch.object(MODULE.boto3, "client") as make_boto_client,
        ):
            MODULE.make_client(2)
        self.assertEqual(make_boto_client.call_args.kwargs["aws_session_token"], "test-session-token")

    def test_list_and_delete_keys_are_explicit(self):
        client = FakeS3()
        client.objects[("bucket", "aggregates/payer/a.json")] = self.object(b"a")
        client.objects[("bucket", "aggregates/cpt-detail/1.json")] = self.object(b"1")
        client.objects[("bucket", "prices/keep.json")] = self.object(b"p")
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
            client.objects[("bucket", key)] = self.object(run_id.encode())
        client.objects[("bucket", "_pipeline/rollback/notes.txt")] = self.object(b"keep")
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
            client.objects[("bucket", "prices/old.json")]["etag"] = hashlib.md5(b"changed").hexdigest()
            client.objects[("bucket", "prices/new.json")] = self.object(b"new")
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

    def test_refresh_lock_blocks_overlap_and_releases_owner(self):
        client = FakeS3()
        now = MODULE.dt.datetime(2026, 8, 3, tzinfo=MODULE.dt.timezone.utc)
        result = MODULE.acquire_lock(
            client,
            bucket="bucket",
            key="_pipeline/locks/cloud-refresh.json",
            owner="run-a",
            ttl_seconds=3600,
            current_time=now,
        )
        self.assertEqual(result["status"], "acquired")
        renewed = MODULE.renew_lock(
            client,
            bucket="bucket",
            key="_pipeline/locks/cloud-refresh.json",
            owner="run-a",
            ttl_seconds=7200,
            current_time=now,
        )
        self.assertEqual(renewed["status"], "renewed")
        with self.assertRaises(SystemExit):
            MODULE.acquire_lock(
                client,
                bucket="bucket",
                key="_pipeline/locks/cloud-refresh.json",
                owner="run-b",
                ttl_seconds=3600,
                current_time=now,
            )
        with self.assertRaises(SystemExit):
            MODULE.release_lock(
                client,
                bucket="bucket",
                key="_pipeline/locks/cloud-refresh.json",
                owner="run-b",
            )
        with self.assertRaises(SystemExit):
            MODULE.renew_lock(
                client,
                bucket="bucket",
                key="_pipeline/locks/cloud-refresh.json",
                owner="run-b",
                ttl_seconds=3600,
                current_time=now,
            )
        self.assertEqual(
            MODULE.release_lock(
                client,
                bucket="bucket",
                key="_pipeline/locks/cloud-refresh.json",
                owner="run-a",
            ),
            "released",
        )

    def test_refresh_lock_replaces_expired_owner_conditionally(self):
        client = FakeS3()
        first = MODULE.dt.datetime(2026, 8, 1, tzinfo=MODULE.dt.timezone.utc)
        later = MODULE.dt.datetime(2026, 8, 3, tzinfo=MODULE.dt.timezone.utc)
        MODULE.acquire_lock(
            client,
            bucket="bucket",
            key="_pipeline/locks/cloud-refresh.json",
            owner="old-run",
            ttl_seconds=3600,
            current_time=first,
        )
        result = MODULE.acquire_lock(
            client,
            bucket="bucket",
            key="_pipeline/locks/cloud-refresh.json",
            owner="new-run",
            ttl_seconds=3600,
            current_time=later,
        )
        self.assertEqual(result["status"], "replaced_stale")

    def test_release_lock_writes_expired_tombstone_without_delete(self):
        client = FakeS3()
        acquired = MODULE.dt.datetime(2026, 8, 3, 4, 17, tzinfo=MODULE.dt.timezone.utc)
        released = acquired + MODULE.dt.timedelta(minutes=30)
        key = "_pipeline/locks/cloud-refresh.json"
        MODULE.acquire_lock(
            client,
            bucket="bucket",
            key=key,
            owner="run-a",
            ttl_seconds=3600,
            current_time=acquired,
        )

        result = MODULE.release_lock(
            client,
            bucket="bucket",
            key=key,
            owner="run-a",
            current_time=released,
        )

        self.assertEqual(result, "released")
        self.assertEqual(client.delete_calls, [])
        tombstone = client.objects[("bucket", key)]
        expected_expiry = "2026-08-03T04:47:00Z"
        self.assertEqual(tombstone["metadata"]["owner"], "run-a")
        self.assertEqual(tombstone["metadata"]["expires-at"], expected_expiry)
        self.assertEqual(json.loads(tombstone["body"])["expires_at"], expected_expiry)
        self.assertLessEqual(
            MODULE.lock_timestamp(tombstone["metadata"]["expires-at"]),
            released,
        )

        takeover = MODULE.acquire_lock(
            client,
            bucket="bucket",
            key=key,
            owner="run-b",
            ttl_seconds=3600,
            current_time=released,
        )
        self.assertEqual(takeover["status"], "replaced_stale")

    def test_release_lock_fails_if_concurrent_takeover_changes_etag(self):
        client = FakeS3()
        acquired = MODULE.dt.datetime(2026, 8, 3, 4, 17, tzinfo=MODULE.dt.timezone.utc)
        released = acquired + MODULE.dt.timedelta(minutes=30)
        key = "_pipeline/locks/cloud-refresh.json"
        MODULE.acquire_lock(
            client,
            bucket="bucket",
            key=key,
            owner="run-a",
            ttl_seconds=3600,
            current_time=acquired,
        )

        def concurrent_takeover(fake, bucket, object_key):
            body = json.dumps(
                {
                    "owner": "run-b",
                    "acquired_at": "2026-08-03T04:46:59Z",
                    "expires_at": "2026-08-03T05:46:59Z",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            fake.objects[(bucket, object_key)] = self.object(
                body,
                {
                    "owner": "run-b",
                    "acquired-at": "2026-08-03T04:46:59Z",
                    "expires-at": "2026-08-03T05:46:59Z",
                },
            )

        client.before_conditional_put = concurrent_takeover
        with self.assertRaisesRegex(SystemExit, "refresh lock changed before release"):
            MODULE.release_lock(
                client,
                bucket="bucket",
                key=key,
                owner="run-a",
                current_time=released,
            )

        self.assertEqual(client.delete_calls, [])
        self.assertEqual(client.objects[("bucket", key)]["metadata"]["owner"], "run-b")


if __name__ == "__main__":
    unittest.main()
