#!/usr/bin/env python3
"""Regression tests for the 2026-10-13 hl_wave.py fixes.

1. verify_synced_base: a synced build base must match the live manifest's
   artifact record (size + sha256) or the publish parks -- wave 35 built
   on a stale site-bundled index served by the CDN fallback.
2. retry_pending_publish: the Oct 1 stderr log shows this died with
   NameError: RETRY_ESCALATE_ATTEMPTS. The escalation branch must run on
   the defined RETRY_MAX_ATTEMPTS constant. These tests exercise every
   early branch incl. escalation, with the publish phase stubbed.

Run: ~/hospital-ledger/.venv/bin/python3 test_hl_wave_fixes.py
"""
import argparse
import ast
import hashlib
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hl_wave", HERE / "hl_wave.py")
hw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hw)


def manifest_text(artifacts):
    return json.dumps({"artifacts": artifacts})


class VerifySyncedBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "index.json"
        self.data = b'{"hospitals": []}' * 10
        self.dest.write_bytes(self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        self.good = {"prices/index.json": {"bytes": len(self.data),
                                           "sha256": self.sha}}

    def tearDown(self):
        self.tmp.cleanup()

    def patch_manifest(self, text_or_none, raises=False):
        if raises:
            def boom(url):
                raise RuntimeError("boom")
            hw._fetch_live_manifest_text = boom
        else:
            hw._fetch_live_manifest_text = lambda url: (text_or_none, "test")

    def test_match_ok(self):
        self.patch_manifest(manifest_text(self.good))
        ok, detail = hw.verify_synced_base("prices/index.json", self.dest)
        self.assertTrue(ok, detail)

    def test_sha_mismatch_rejected(self):
        bad = {"prices/index.json": {"bytes": len(self.data),
                                     "sha256": "0" * 64}}
        self.patch_manifest(manifest_text(bad))
        ok, detail = hw.verify_synced_base("prices/index.json", self.dest)
        self.assertFalse(ok)
        self.assertIn("mismatch", detail)

    def test_size_mismatch_rejected(self):
        bad = {"prices/index.json": {"bytes": len(self.data) + 1,
                                     "sha256": self.sha}}
        self.patch_manifest(manifest_text(bad))
        ok, _ = hw.verify_synced_base("prices/index.json", self.dest)
        self.assertFalse(ok)

    def test_stale_bundle_rejected(self):
        # The wave-35 shape: manifest records canonical (3,823-hospital
        # object); the synced file is a different, older object.
        self.dest.write_bytes(b'{"hospitals": "stale"}')
        self.patch_manifest(manifest_text(self.good))
        ok, _ = hw.verify_synced_base("prices/index.json", self.dest)
        self.assertFalse(ok)

    def test_missing_artifact_record_rejected(self):
        self.patch_manifest(manifest_text({}))
        ok, _ = hw.verify_synced_base("prices/index.json", self.dest)
        self.assertFalse(ok)

    def test_manifest_unreachable_rejected(self):
        self.patch_manifest(None)
        ok, _ = hw.verify_synced_base("prices/index.json", self.dest)
        self.assertFalse(ok)

    def test_manifest_fetch_raises_rejected(self):
        self.patch_manifest(None, raises=True)
        ok, _ = hw.verify_synced_base("prices/index.json", self.dest)
        self.assertFalse(ok)


class RetryPendingPublish(unittest.TestCase):
    CCN = "193074"  # price file exists in ~/hospital-ledger/public/data/prices

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pending_path = Path(self.tmp.name) / "hl_pending_publish.json"
        self._orig = {k: getattr(hw, k) for k in
                      ("PENDING_PATH", "run_publish_phase", "record_success",
                       "load_state", "load_index_ccns", "load_route_health",
                       "save_route_health")}
        hw.PENDING_PATH = self.pending_path
        hw.record_success = lambda std: None
        hw.load_state = lambda: {"waves_done": 0, "unpublished": []}
        hw.load_index_ccns = lambda: (set(), 3823)
        hw.load_route_health = lambda: {}
        hw.save_route_health = lambda h: None
        self.args = argparse.Namespace(dry_run=False, no_publish=False)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(hw, k, v)
        self.tmp.cleanup()

    def write_pending(self, **kw):
        p = {"publish_ccns": [self.CCN], "attempts": 0,
             "next_retry_at": 0, "ts": "2026-10-13T00:00:00Z"}
        p.update(kw)
        self.pending_path.write_text(json.dumps(p))

    def test_nothing_pending_returns_0(self):
        rc = hw.retry_pending_publish(self.args, {}, {})
        self.assertEqual(rc, 0)

    def test_not_due_returns_0_without_publishing(self):
        self.write_pending(next_retry_at=time.time() + 3600)
        def forbidden(*a, **k):
            raise AssertionError("run_publish_phase must not be called")
        hw.run_publish_phase = forbidden
        rc = hw.retry_pending_publish(self.args, {}, {})
        self.assertEqual(rc, 0)

    def test_escalation_branch_published(self):
        # attempts at the escalation threshold: the branch that once died
        # with NameError: RETRY_ESCALATE_ATTEMPTS must execute cleanly.
        self.write_pending(attempts=hw.RETRY_MAX_ATTEMPTS)
        hw.run_publish_phase = lambda *a, **k: {
            "status": "published", "publish_ccns": [self.CCN]}
        rc = hw.retry_pending_publish(self.args, {}, {})
        self.assertEqual(rc, 0)

    def test_due_deferred_returns_0(self):
        self.write_pending(attempts=1)
        hw.run_publish_phase = lambda *a, **k: {
            "status": "deferred", "reason": "base-verify-fail"}
        rc = hw.retry_pending_publish(self.args, {}, {})
        self.assertEqual(rc, 0)

    def test_escalate_constant_not_referenced(self):
        src = (HERE / "hl_wave.py").read_text()
        self.assertNotIn("RETRY_ESCALATE_ATTEMPTS", src)
        self.assertTrue(hasattr(hw, "RETRY_MAX_ATTEMPTS"))


class PublishRetryRouteHealth(unittest.TestCase):
    """2026-10-13: lock contention must not be recorded as an r2_put
    transport failure (it earned a bogus 30m cooldown in the live wave)."""

    def setUp(self):
        self._orig = {k: getattr(hw, k) for k in
                      ("publish", "note_route", "route_usable")}
        self.notes = []
        hw.note_route = lambda h, route, ok, detail="": \
            self.notes.append((route, ok))
        hw.route_usable = lambda h, route: True
        self._sleep = hw.time.sleep
        hw.time.sleep = lambda s: None

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(hw, k, v)
        hw.time.sleep = self._sleep

    def test_lock_contention_not_a_route_failure(self):
        hw.publish = lambda *a, **k: \
            (False, "publish lock held by another publisher")
        ok, _ = hw._publish_with_retry(Path("."), ["193074"], {}, {})
        self.assertFalse(ok)
        self.assertEqual(self.notes, [])

    def test_transport_failure_counts(self):
        hw.publish = lambda *a, **k: (False, "VM publish failed rc=1: boom")
        ok, _ = hw._publish_with_retry(Path("."), ["193074"], {}, {},
                                       attempts=1)
        self.assertFalse(ok)
        self.assertEqual(self.notes, [("r2_put", False)])

    def test_success_counts_ok(self):
        hw.publish = lambda *a, **k: (True, "12 keys uploaded + verified")
        ok, _ = hw._publish_with_retry(Path("."), ["193074"], {}, {},
                                       attempts=1)
        self.assertTrue(ok)
        self.assertEqual(self.notes, [("r2_put", True)])


class PublishLockRelease(unittest.TestCase):
    """2026-10-13: a cut lock-release DELETE left a zombie lock for the
    full TTL; release() must retry the idempotent owner-matched op."""

    class R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = ""

    def make_lock(self, seq):
        lock = hw.PublishLock({}, owner="test-owner")
        lock.acquired = True
        lock.release_retry_delay = 0
        calls = []
        lock._r2 = lambda *a: (calls.append(a), seq[len(calls) - 1])[1]
        return lock, calls

    def test_release_retries_until_success(self):
        lock, calls = self.make_lock([self.R(1), self.R(1), self.R(0)])
        lock.release()
        self.assertEqual(len(calls), 3)

    def test_release_gives_up_after_3_without_raising(self):
        lock, calls = self.make_lock([self.R(1)] * 5)
        lock.release()
        self.assertEqual(len(calls), 3)


class PublishLockAcquire(unittest.TestCase):
    """2026-10-13: acquire must survive cut responses -- retry transport
    failures, adopt a lock our own earlier attempt landed, and still
    refuse a genuinely foreign lock immediately."""

    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode = rc
            self.stdout = out
            self.stderr = err

    def setUp(self):
        self._sleep = hw.time.sleep
        hw.time.sleep = lambda s: None

    def tearDown(self):
        hw.time.sleep = self._sleep

    def make_lock(self, seq, owner="hl_wave:test:1"):
        lock = hw.PublishLock({}, owner=owner)
        lock.heartbeat_interval = 3600
        calls = []
        lock._r2 = lambda *a: (calls.append(a), seq[min(len(calls) - 1,
                                                        len(seq) - 1)])[1]
        return lock, calls

    def test_transport_failures_then_success(self):
        lock, calls = self.make_lock(
            [self.R(1, err="RemoteDisconnected"), self.R(0)])
        self.assertTrue(lock.acquire())
        self.assertEqual(len(calls), 2)
        lock._r2 = lambda *a: self.R(0)
        lock.release()

    def test_adopt_own_lock(self):
        lock, calls = self.make_lock([self.R(
            1, out="acquire: lock held by hl_wave:test:1 "
                   "(owner=hl_wave:test:1)")])
        self.assertTrue(lock.acquire())
        self.assertEqual(len(calls), 1)
        lock._r2 = lambda *a: self.R(0)
        lock.release()

    def test_foreign_lock_refused_immediately(self):
        lock, calls = self.make_lock([self.R(
            1, out="acquire: lock held by other:999 (owner=other:999)")])
        self.assertFalse(lock.acquire())
        self.assertEqual(len(calls), 1)

    def test_requester_mention_is_not_adoption(self):
        # r2_put ownership errors name BOTH parties ("... not
        # <requester>"); our owner appearing as the requester must not
        # be mistaken for holding the lock (false-adopted live once).
        lock, calls = self.make_lock([self.R(
            1, out="acquire: lock held by other:999, not hl_wave:test:1")])
        self.assertFalse(lock.acquire())
        self.assertEqual(len(calls), 1)

    def test_all_transport_failures_give_up(self):
        lock, calls = self.make_lock([self.R(1, err="RemoteDisconnected")])
        self.assertFalse(lock.acquire())
        self.assertEqual(len(calls), 3)


class PublishLockedPerKey(unittest.TestCase):
    """2026-10-13: uploads are per-key with retries, so one cut
    connection can no longer discard the whole publish."""

    class R:
        def __init__(self, rc):
            self.returncode = rc
            self.stdout = ""
            self.stderr = "boom"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        (self.out / "prices").mkdir()
        (self.out / "meta").mkdir()
        for k in ("prices/111111.json", "prices/222222.json",
                  "prices/index.json", "meta/summary.json",
                  "meta/hospitals.json", "meta/manifest.json"):
            (self.out / k).write_bytes(b'{"x": 1}')
        self.ccns = ["111111", "222222"]
        self._run = hw.subprocess.run
        self._sha = hw.r2_get_sha256
        self._sleep = hw.time.sleep
        hw.time.sleep = lambda s: None
        hw.r2_get_sha256 = lambda k, penv: hashlib.sha256(
            (self.out / k).read_bytes()).hexdigest()

    def tearDown(self):
        hw.subprocess.run = self._run
        hw.r2_get_sha256 = self._sha
        hw.time.sleep = self._sleep
        self.tmp.cleanup()

    def test_all_keys_ok(self):
        hw.subprocess.run = lambda *a, **k: self.R(0)
        ok, msg = hw._publish_locked(self.out, self.ccns, {})
        self.assertTrue(ok, msg)

    def test_flaky_key_recovers(self):
        state = {"n": 0}
        def run(*a, **k):
            state["n"] += 1
            return self.R(1 if state["n"] <= 2 else 0)
        hw.subprocess.run = run
        ok, msg = hw._publish_locked(self.out, self.ccns, {})
        self.assertTrue(ok, msg)

    def test_dead_key_fails_and_names_it(self):
        def run(cmd, *a, **k):
            return self.R(1 if cmd[-1] == "prices/index.json" else 0)
        hw.subprocess.run = run
        ok, msg = hw._publish_locked(self.out, self.ccns, {})
        self.assertFalse(ok)
        self.assertIn("prices/index.json", msg)


class VerifySyncedBaseManifestStaleHeal(unittest.TestCase):
    """2026-10-23: the manifest-stale discriminator. The 2026-10-18 weekly
    publish died after PUT index+hospitals but before PUT manifest, so R2
    held the canonical NEW index while the manifest still recorded the
    Oct 15 one; every wave parked on base-verify-fail for 5 days. On a
    manifest mismatch, if the synced base is confirmed byte-identical via
    the OTHER independent leg and is sane, the base is canonical and the
    manifest is the stale artifact -- accept and let the publish heal it.
    Any doubt (legs disagree, alt leg unreadable, insane base) fails
    closed exactly as before."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "index.json"
        sane = {"hospitals": [{"ccn": f"{i:06d}"} for i in range(3500)]}
        self.data = json.dumps(sane).encode()
        self.dest.write_bytes(self.data)
        self.sha = hashlib.sha256(self.data).hexdigest()
        # Manifest records a DIFFERENT (stale) object than the synced base.
        stale = {"prices/index.json": {"bytes": 1392465,
                                       "sha256": "95f75ee249e1" + "0" * 50}}
        hw._fetch_live_manifest_text = lambda url: (manifest_text(stale), "t")
        self._orig = {k: getattr(hw, k) for k in ("_s3_get_to", "_cdn_get")}

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(hw, k, v)
        self.tmp.cleanup()

    def _alt_same(self, key, tmp_path, *a):
        Path(tmp_path).write_bytes(self.data)
        return True

    def _alt_different(self, key, tmp_path, *a):
        other = {"hospitals": [{"ccn": f"{i:06d}"} for i in range(3400)]}
        Path(tmp_path).write_bytes(json.dumps(other).encode())
        return True

    def _alt_dead(self, key, tmp_path, *a):
        return False

    def test_heal_when_synced_via_cdn_s3_confirms(self):
        hw._s3_get_to = self._alt_same
        ok, detail = hw.verify_synced_base("prices/index.json", self.dest,
                                           via="cdn", renv={})
        self.assertTrue(ok, detail)
        self.assertIn("auto-heal", detail)

    def test_heal_when_synced_via_s3_cdn_confirms(self):
        hw._cdn_get = self._alt_same
        ok, detail = hw.verify_synced_base("prices/index.json", self.dest,
                                           via="vm-direct", renv={})
        self.assertTrue(ok, detail)
        self.assertIn("auto-heal", detail)

    def test_legs_disagree_fails_closed(self):
        hw._s3_get_to = self._alt_different
        ok, _ = hw.verify_synced_base("prices/index.json", self.dest,
                                      via="cdn", renv={})
        self.assertFalse(ok)

    def test_alt_leg_unreadable_fails_closed(self):
        hw._s3_get_to = self._alt_dead
        ok, _ = hw.verify_synced_base("prices/index.json", self.dest,
                                      via="cdn", renv={})
        self.assertFalse(ok)

    def test_insane_base_fails_closed(self):
        small = json.dumps({"hospitals": [{"ccn": "1"}] * 100}).encode()
        self.dest.write_bytes(small)
        hw._s3_get_to = lambda key, tmp_path, *a: (
            Path(tmp_path).write_bytes(small), True)[1]
        ok, _ = hw.verify_synced_base("prices/index.json", self.dest,
                                      via="cdn", renv={})
        self.assertFalse(ok)

    def test_backoff_never_six_hours(self):
        # Barklee 2026-10-23: "Don't do a six hour back off. Immediately
        # always try to fix it." The retry cadence stays on the 20-minute
        # cron tick scale, never hours.
        self.assertLessEqual(hw.RETRY_BASE_SECS, 300)
        self.assertLessEqual(hw.RETRY_MAX_DELAY_SECS, 1200)


class HashReadBudget(unittest.TestCase):
    def test_caller_exceeds_actual_streaming_hash_read_timeout(self):
        tree = ast.parse((HERE.parents[1] / 'scripts/r2_put.py').read_text())
        function = next(n for n in tree.body
                        if isinstance(n, ast.FunctionDef) and n.name == 'get_sha256')
        call = next(n for n in ast.walk(function) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name) and n.func.id == '_get_with_retry')
        timeout = next(k.value.value for k in call.keywords if k.arg == 'timeout')
        with patch.object(hw.subprocess, 'run') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = 'a' * 64
            self.assertEqual(hw.r2_get_sha256('prices/050025.json', {}), 'a' * 64)
            self.assertGreater(run.call_args.kwargs['timeout'], timeout)

    def test_timeout_uses_independent_byte_verification_and_logs_reason(self):
        body = b'complete CDN bytes'

        def cdn_get(_key, target):
            Path(target).write_bytes(body)
            return True

        with patch.object(hw.subprocess, 'run', side_effect=hw.subprocess.TimeoutExpired('fixture', 660)), \
                patch.object(hw, '_cdn_get', side_effect=cdn_get), patch.object(hw, 'log') as log:
            self.assertEqual(hw.r2_get_sha256('prices/050025.json', {}), hashlib.sha256(body).hexdigest())
            self.assertTrue(any('timed out' in call.args[0] for call in log.call_args_list))

    def test_timeout_and_unavailable_independent_copy_fail_closed(self):
        with patch.object(hw.subprocess, 'run', side_effect=hw.subprocess.TimeoutExpired('fixture', 660)), \
                patch.object(hw, '_cdn_get', return_value=False), patch.object(hw, 'log'):
            self.assertIsNone(hw.r2_get_sha256('prices/050025.json', {}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
