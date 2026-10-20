"""Exercise real symlink/archive decisions in subprocesses, without live services."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
OLD, NEW = 'a' * 40, 'b' * 40
spec = importlib.util.spec_from_file_location('activation', ROOT / 'deploy/activation.py')
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)


class ActivationTests(unittest.TestCase):
    def run_case(self, scenario=None, previous=True, unsupported=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for sha in (OLD, NEW):
                release = root / 'releases' / sha
                (release / 'deploy').mkdir(parents=True)
                (release / 'scripts').mkdir()
                (release / 'deploy/readiness-protocol').write_text('0' if unsupported == sha else '1')
                (release / 'deploy/runtime.env').write_text(f'RELEASE={sha}\n')
                (release / 'scripts/readiness.py').touch()
            (root / 'incoming').mkdir()
            archive = root / 'incoming' / f'{NEW}.tar.gz'
            archive.write_bytes(b'archive retained on failure')
            (root / 'run').mkdir()
            if previous:
                (root / 'current').symlink_to(root / 'releases' / OLD)
            (root / 'scenario.json').write_text(json.dumps(scenario or {}))
            result = subprocess.run([sys.executable, str(ROOT / 'tests/activation_harness.py'), tmp, NEW],
                                    capture_output=True, text=True, timeout=15)
            self.assertIn(result.returncode, (0, 1), result.stderr)
            outcome = json.loads((root / 'result.json').read_text())
            selected = (root / 'current').resolve().name if (root / 'current').exists() else None
            self.assertLess(outcome['tick'] - 1000, 300)
            self.assertNotIn('PRIVATE SECRET', result.stdout + result.stderr)
            self.assertEqual((root / 'releases' / OLD / 'deploy/runtime.env').read_text(), f'RELEASE={OLD}\n')
            self.assertEqual((root / 'releases' / NEW / 'deploy/runtime.env').read_text(), f'RELEASE={NEW}\n')
            diagnostics = list((root / 'diagnostics').glob('*.json'))
            self.assertEqual(len(diagnostics), 1)
            return result.returncode, selected, archive.exists(), result.stdout, outcome

    def test_success_requires_stable_process_and_removes_archive(self):
        code, selected, retained, output, outcome = self.run_case()
        self.assertEqual((code, selected, retained), (0, NEW, False))
        self.assertGreaterEqual(outcome['tick'] - 1000, 10)
        self.assertIn(f'Activated {NEW}', output)

    def test_first_deployment_success_and_failure(self):
        self.assertEqual(self.run_case(previous=False)[:3], (0, NEW, False))
        code, selected, retained, output, _ = self.run_case({'restart_failure': [1]}, previous=False)
        self.assertEqual((code, selected, retained), (1, NEW, True))
        self.assertIn('no_previous_release', output)
        self.assertNotIn('restored', output)
        failed_probe = self.run_case({'probes': {NEW: [{'database': 'fail'}]}}, previous=False)
        self.assertEqual(failed_probe[:3], (1, NEW, True))
        self.assertIn('no_previous_release', failed_probe[3])

    def test_bad_baseline_and_hung_probe_do_not_switch(self):
        for scenario in ({'probes': {OLD: [{'database': 'fail'}]}}, {'hang_probe': True},
                         {'probes': {OLD: [{'backup': 'fail'}]}}):
            with self.subTest(scenario=scenario):
                self.assertEqual(self.run_case(scenario)[:3], (1, OLD, True))

    def test_protocol_mismatch_never_switches(self):
        for sha in (OLD, NEW):
            with self.subTest(sha=sha):
                self.assertEqual(self.run_case(unsupported=sha)[:3], (1, OLD, True))

    def test_regression_restores_previous_once(self):
        code, selected, retained, output, outcome = self.run_case({'probes': {NEW: [{'database': 'fail'}]}})
        self.assertEqual((code, selected, retained), (1, OLD, True))
        self.assertEqual(outcome['restarts'], 2)
        self.assertIn('restored', output)
        self.assertNotIn('Activated ', output)

    def test_external_or_indeterminate_outage_keeps_running_candidate(self):
        for status in ('fail', 'unknown'):
            code, selected, retained, output, _ = self.run_case({'probes': {
                OLD: [{}, {'database': status}], NEW: [{'database': 'fail'}]}})
            self.assertEqual((code, selected, retained), (1, NEW, True))
            self.assertIn('external_or_unknown', output)

    def test_backup_failure_is_not_a_regression_signal(self):
        result = self.run_case({'probes': {NEW: [{'backup': 'fail'}]}})
        self.assertEqual(result[:3], (1, NEW, True))
        self.assertIn('backup_unavailable', result[3])

    def test_unknown_candidate_is_not_declared_regression_when_fallback_passes(self):
        result = self.run_case({'probes': {NEW: [{'database': 'unknown'}]}})
        self.assertEqual(result[:3], (1, NEW, True))
        self.assertIn('external_or_unknown', result[3])
        self.assertEqual(result[4]['restarts'], 1)

    def test_readiness_identity_and_deadline_failures_restore(self):
        for signal in ('stale', 'wrong_release', 'wrong_pid', 'wrong_invocation', 'missing'):
            with self.subTest(signal=signal):
                outcome = self.run_case({'signals': {NEW: signal}})
                self.assertEqual(outcome[:3], (1, OLD, True))
                self.assertIn('readiness_deadline', outcome[3])
                # The candidate must exhaust its 90-second gate and the restored
                # process must satisfy the 10-second stable observation period.
                self.assertEqual(outcome[4]['tick'] - 1000, 100)
                self.assertEqual(outcome[4]['restarts'], 2)

    def test_process_exit_restart_and_invocation_change_restore(self):
        for scenario in ({'states': {'1': 'failed'}}, {'restart_loop': True}, {'invocation_change': True},
                         {'restart_failure': [1]}):
            with self.subTest(scenario=scenario):
                result = self.run_case(scenario)
                self.assertEqual(result[:3], (1, OLD, True))
                self.assertEqual(result[4]['restarts'], 2)

    def test_rollback_failure_is_explicit(self):
        outcome = self.run_case({'restart_failure': [1, 2]})
        self.assertEqual(outcome[:3], (1, OLD, True))
        self.assertIn('rollback_failed', outcome[3])
        self.assertNotIn('"reason":"restored"', outcome[3])

    def test_hung_candidate_and_comparison_are_bounded_and_keep_archive(self):
        result = self.run_case({'hang_probe_at': {NEW: [1], OLD: [2]}})
        self.assertEqual(result[:3], (1, NEW, True))
        self.assertIn('external_or_unknown', result[3])

    def test_dead_candidate_is_restored_during_external_outage(self):
        result = self.run_case({'states': {'1': 'failed'}, 'probes': {OLD: [{}, {'database': 'fail'}]}})
        self.assertEqual(result[:3], (1, OLD, True))
        self.assertEqual(result[4]['restarts'], 2)
        self.assertIn('rollback_failed', result[3])

    def test_probe_validates_protocol_and_redacts_subprocess_output(self):
        for raw in (b'PRIVATE SECRET', b'{}', b'null', b'[]'):
            controller = activation.Controller()
            controller.command = lambda *a: subprocess.CompletedProcess([], 1, raw, b'PRIVATE SECRET')
            with self.subTest(raw=raw):
                self.assertFalse(controller.healthy(controller.probe(OLD)))

    def test_helper_rejects_arguments_before_systemd(self):
        for args in ([], [OLD, 'arbitrary'], ['../etc'], [OLD + '\n']):
            result = subprocess.run(['bash', str(ROOT / 'deploy/probe_release.sh'), *args],
                                    capture_output=True, timeout=3)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b'invalid_release', result.stderr)

    def test_helper_uses_fixed_user_paths_and_systemd_environment_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            release = root / 'releases' / OLD
            (release / '.venv/bin').mkdir(parents=True)
            (release / 'scripts').mkdir()
            (release / 'deploy').mkdir()
            (release / 'deploy/readiness-protocol').write_text('1')
            (release / 'scripts/readiness.py').touch()
            (release / '.venv/bin/python').touch(mode=0o755)
            fake = root / 'systemd-run'
            fake.write_text('#!/bin/bash\nprintf "%s\\n" "$@"\n')
            fake.chmod(0o755)
            script = (ROOT / 'deploy/probe_release.sh').read_text()
            # Only the fixture bypasses root ownership; production has no test flags.
            script = script.replace('EUID != 0 || ', '').replace('/opt/smebot', str(root))
            script = script.replace('/usr/bin/systemd-run', str(fake))
            helper = root / 'probe'
            helper.write_text(script)
            result = subprocess.run(['bash', str(helper), OLD], capture_output=True, text=True, timeout=3)
            self.assertEqual(result.returncode, 0, result.stderr)
            args = result.stdout.splitlines()
            envs = [arg for arg in args if arg.startswith('EnvironmentFile=')]
            self.assertEqual(envs, ['EnvironmentFile=/etc/smebot/bot.env',
                                    f'EnvironmentFile={release}/deploy/runtime.env'])
            self.assertIn('User=smebot', args)
            self.assertIn('RuntimeMaxSec=43', args)
            self.assertEqual(args[-2:], [str(release / '.venv/bin/python'), str(release / 'scripts/readiness.py')])
            link = root / 'releases' / NEW
            link.symlink_to(release)
            rejected = subprocess.run(['bash', str(helper), NEW], capture_output=True, timeout=3)
            self.assertEqual(rejected.returncode, 2)
            self.assertIn(b'unsupported_release', rejected.stderr)
