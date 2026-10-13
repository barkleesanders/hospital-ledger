import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

import httpx


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_r2_credentials.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_r2_credentials", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

ACCOUNT_ID = "a" * 32
PERMISSION_ID = "b" * 32
TOKEN_ID = "c" * 32
TOKEN_VALUE = "short-lived-token-value"


def response(payload, status=200):
    return httpx.Response(status, json=payload)


class BootstrapR2CredentialsTests(unittest.TestCase):
    def make_client(self, handler):
        return MODULE.CloudflareTokenClient(
            "owner@example.com",
            "global-key-that-must-not-leak",
            client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    def test_create_payload_has_exact_bucket_scope_and_expiry(self):
        requests = []

        def handler(request):
            requests.append(request)
            if request.method == "GET":
                self.assertFalse(request.url.params)
                return response(
                    {
                        "success": True,
                        "result": [
                            {
                                "id": PERMISSION_ID,
                                "name": MODULE.PERMISSION_GROUP_NAME,
                                "scopes": ["com.cloudflare.edge.r2.bucket"],
                            }
                        ],
                    }
                )
            payload = json.loads(request.content)
            self.assertEqual(
                payload["policies"],
                [
                    {
                        "effect": "allow",
                        "permission_groups": [{"id": PERMISSION_ID, "meta": {}}],
                        "resources": {
                            f"com.cloudflare.edge.r2.bucket.{ACCOUNT_ID}_default_hl-mrf-parsed": "*"
                        },
                    }
                ],
            )
            self.assertNotIn("not_before", payload)
            self.assertEqual(payload["expires_on"], "2026-08-02T18:00:00Z")
            return response(
                {
                    "success": True,
                    "result": {"id": TOKEN_ID, "value": TOKEN_VALUE},
                }
            )

        with tempfile.TemporaryDirectory() as temporary:
            client = self.make_client(handler)
            MODULE.provision_credentials(
                client,
                account_id=ACCOUNT_ID,
                bucket="hl-mrf-parsed",
                output=Path(temporary) / "r2.env",
                ttl_hours=6,
                now=dt.datetime(2026, 8, 2, 12, tzinfo=dt.timezone.utc),
            )
        self.assertEqual([request.method for request in requests], ["GET", "POST"])
        self.assertEqual(
            requests[1].url.path,
            f"/client/v4/accounts/{ACCOUNT_ID}/tokens",
        )

    def test_derivation_and_private_file_mode(self):
        def handler(request):
            if request.method == "GET":
                return response(
                    {
                        "success": True,
                        "result": [
                            {
                                "id": PERMISSION_ID,
                                "name": MODULE.PERMISSION_GROUP_NAME,
                                "scopes": ["com.cloudflare.edge.r2.bucket"],
                            }
                        ],
                    }
                )
            return response(
                {
                    "success": True,
                    "result": {"id": TOKEN_ID, "value": TOKEN_VALUE},
                }
            )

        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "credentials.env"
            MODULE.provision_credentials(
                self.make_client(handler),
                account_id=ACCOUNT_ID,
                bucket="hl-mrf-parsed",
                output=output,
                ttl_hours=1,
                now=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
            )
            contents = output.read_text()
            expected_secret = hashlib.sha256(TOKEN_VALUE.encode()).hexdigest()
            self.assertIn(f"export R2_ACCESS_KEY_ID={TOKEN_ID}\n", contents)
            self.assertIn(f"export R2_SECRET_ACCESS_KEY={expected_secret}\n", contents)
            self.assertNotIn(TOKEN_VALUE, contents)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_revoke_uses_global_key_and_token_id(self):
        captured = []

        def handler(request):
            captured.append(request)
            return response({"success": True, "result": {"id": TOKEN_ID}})

        self.make_client(handler).revoke_token(TOKEN_ID, account_id=ACCOUNT_ID)
        self.assertEqual(captured[0].method, "DELETE")
        self.assertEqual(
            captured[0].url.path,
            f"/client/v4/accounts/{ACCOUNT_ID}/tokens/{TOKEN_ID}",
        )
        self.assertEqual(captured[0].headers["x-auth-email"], "owner@example.com")
        self.assertEqual(
            captured[0].headers["x-auth-key"], "global-key-that-must-not-leak"
        )

    def test_api_error_redacts_response_values(self):
        response_secret = "secret-returned-in-cloudflare-error"

        def handler(_request):
            return response(
                {
                    "success": False,
                    "errors": [
                        {
                            "code": 9109,
                            "message": f"invalid token {response_secret}",
                        }
                    ],
                    "result": {"value": response_secret},
                },
                status=403,
            )

        with self.assertRaises(MODULE.BootstrapError) as caught:
            self.make_client(handler).permission_group_id()
        message = str(caught.exception)
        self.assertNotIn(response_secret, message)
        self.assertNotIn("invalid token", message)
        self.assertIn("9109", message)

    def test_main_redacts_unexpected_exception_details(self):
        stderr = io.StringIO()
        secret = "do-not-print-this-secret"
        with mock.patch.dict(
            os.environ,
            {
                "CLOUDFLARE_EMAIL": "owner@example.com",
                "CLOUDFLARE_API_KEY": "global-key",
                "R2_ACCOUNT_ID": ACCOUNT_ID,
            },
            clear=True,
        ), mock.patch.object(
            MODULE.CloudflareTokenClient,
            "__enter__",
            side_effect=RuntimeError(secret),
        ), redirect_stderr(stderr):
            result = MODULE.main(["revoke", TOKEN_ID])
        self.assertEqual(result, 1)
        self.assertNotIn(secret, stderr.getvalue())

    def test_cloud_runner_bootstraps_and_revokes_after_lock_release(self):
        shell = (SCRIPT.parent / "cloud_refresh.sh").read_text()
        configure_call = shell.index("\nconfigure_r2_credentials\n")
        bucket_check = shell.index('check --bucket "$BUCKET"')
        release_lock = shell.index('release-lock \\\n      "$BUCKET" "$LOCK_KEY" "$RUN_ID"')
        revoke = shell.index('scripts/bootstrap_r2_credentials.py revoke')

        self.assertLess(configure_call, bucket_check)
        self.assertLess(release_lock, revoke)
        self.assertIn('BOOTSTRAPPED_R2_TOKEN_ID="$R2_ACCESS_KEY_ID"', shell)
        self.assertIn('Path(sys.argv[1]).unlink(missing_ok=True)', shell)
        self.assertIn('wrangler deployments list --json > "$DEPLOYMENTS_JSON"', shell)
        self.assertNotIn("\n  wrangler whoami", shell)


if __name__ == "__main__":
    unittest.main()
