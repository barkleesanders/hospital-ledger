"""Exercise the actual runner's phase 0 without network or installation."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SelfUpdateTransport(unittest.TestCase):
    def run_phase(self, response, exit_code=0):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hidden = root / 'hidden'
            hidden.mkdir()
            watermark = hidden / 'pipeline-sha256.txt'
            watermark.write_text('a' * 64 + '\n')
            creds = root / 'credentials'
            creds.write_text('BEARER_SHIM_KEY=synthetic-test-only\n')
            shim = root / 'curl'
            shim.write_text('''#!/bin/bash
set -eu
[[ " $* " == *" --max-time 90 "* ]] || exit 81
[[ " $* " == *" --retry-max-time 240 "* ]] || exit 82
[[ " $* " == *" --retry 2 "* ]] || exit 83
printf '%s' "$FIXTURE_BODY"
if [ "$FIXTURE_EXIT" != 0 ]; then echo 'fixture transport timeout' >&2; fi
exit "$FIXTURE_EXIT"
''')
            shim.chmod(0o755)
            source = (ROOT / 'pipeline/hl_counts_refresh.sh').read_text()
            phase = source.split('phase0_self_update() {', 1)[1].split('\n}\n\nsnapshot_before()', 1)[0]
            phase = 'phase0_self_update() {' + phase + '\n}\nphase0_self_update\n'
            phase = phase.replace('/home/hatch/.config/muse-creds/env', str(creds))
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'],
                       HIDDEN=str(hidden), FIXTURE_BODY=response, FIXTURE_EXIT=str(exit_code))
            result = subprocess.run(['bash', '-c', phase], env=env, capture_output=True, text=True)
            self.assertEqual(watermark.read_text(), 'a' * 64 + '\n')
            self.assertNotIn('synthetic-test-only', result.stdout + result.stderr)
            return result

    def test_valid_unchanged_checksum_succeeds(self):
        result = self.run_phase('a' * 64 + '\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('bundle up to date', result.stdout)

    def test_transport_timeout_fails_closed_and_is_diagnosable(self):
        result = self.run_phase('', 28)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture transport timeout', result.stderr)
        self.assertIn('checksum download failed', result.stdout)

    def test_non_checksum_response_never_installs(self):
        result = self.run_phase('<html>proxy error</html>')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a SHA-256', result.stdout)


if __name__ == '__main__':
    unittest.main()
