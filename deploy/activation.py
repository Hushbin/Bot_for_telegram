"""Root-installed, unprivileged release controller. No application imports or secrets."""
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time

CHECKS = ('configuration', 'telegram', 'database', 'backup', 'seller_api')
SHA = re.compile(r'[0-9a-f]{40}')
REASONS = {'ok', 'not_configured', 'backup_stale', 'vm_configuration_required',
           'seller_api_configuration', 'seller_api_unhealthy', 'probe_timeout',
           'probe_unavailable', 'configuration_unavailable',
           *(name + '_unavailable' for name in CHECKS)}


class GateFailure(Exception):
    pass


class Controller:
    def __init__(self, root=Path('/opt/smebot'), runtime=Path('/run/smebot'),
                 clock=time.monotonic, wall=time.time, sleep=time.sleep):
        self.root, self.runtime = Path(root).resolve(), Path(runtime)
        self.clock, self.wall, self.sleep = clock, wall, sleep
        self.end = float('inf')
        self.events = []

    def event(self, phase, reason, **fields):
        item = dict(phase=phase, reason=reason, **fields)
        self.events.append(item)
        print('READINESS ' + json.dumps(item, separators=(',', ':')), flush=True)

    def command(self, args, timeout):
        remaining = min(timeout, self.end - self.clock())
        if remaining <= 0:
            raise GateFailure('attempt_deadline')
        return subprocess.run(args, capture_output=True, timeout=remaining, check=False)

    def supported(self, sha):
        if not SHA.fullmatch(sha):
            return False
        release = self.root / 'releases' / sha
        try:
            return (release.resolve() == release and
                    (release / 'deploy/readiness-protocol').read_text().strip() == '1' and
                    (release / 'scripts/readiness.py').is_file() and
                    (release / 'deploy/runtime.env').is_file())
        except OSError:
            return False

    def probe(self, sha, deadline=None):
        start = self.wall()
        try:
            timeout = 48 if deadline is None else min(48, deadline - self.clock())
            result = self.command(['sudo', '-n', '/usr/local/sbin/smebot-probe', sha], timeout)
            if len(result.stdout) > 16384:
                raise ValueError()
            report = json.loads(result.stdout)
            def timestamp(value):
                return (type(value) in (int, float) and math.isfinite(value) and
                        start - 1 <= value <= self.wall() + 1)
            if (report['v'] != 1 or report['release'] != sha or
                    not timestamp(report['started_at']) or not timestamp(report['observed_at']) or
                    report['observed_at'] < report['started_at'] or
                    set(report['checks']) != set(CHECKS)):
                raise ValueError()
            safe = {}
            for name in CHECKS:
                item = report['checks'][name]
                if (item['status'] not in ('pass', 'fail', 'unknown') or
                        item['reason'] not in REASONS or
                        not timestamp(item['started_at']) or not timestamp(item['observed_at']) or
                        not report['started_at'] <= item['started_at'] <= item['observed_at'] <= report['observed_at']):
                    raise ValueError()
                safe[name] = {key: item[key] for key in ('status', 'reason')}
            if result.returncode != (0 if all(i['status'] == 'pass' for i in safe.values()) else 1):
                raise ValueError()
            self.event('probe', 'result', release=sha, checks=safe)
            return safe
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired, GateFailure):
            self.event('probe', 'probe_unavailable', release=sha)
            return {name: dict(status='unknown', reason='probe_unavailable') for name in CHECKS}

    @staticmethod
    def healthy(checks, names=CHECKS):
        return all(checks[name]['status'] == 'pass' for name in names)

    def state(self):
        try:
            result = self.command(['systemctl', 'show', 'smebot.service',
                                   '-p', 'ActiveState', '-p', 'MainPID', '-p', 'InvocationID',
                                   '-p', 'NRestarts'], 3)
            fields = dict(line.split('=', 1) for line in result.stdout.decode().splitlines())
            if result.returncode:
                return None
            return fields['ActiveState'], int(fields['MainPID']), fields['InvocationID'], int(fields['NRestarts'])
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired, GateFailure):
            return None

    def signal_matches(self, sha, identity, since):
        try:
            path = self.runtime / 'ready.json'
            if path.stat().st_size > 1024:
                return False
            record = json.loads(path.read_text())
            return (record['v'] == 1 and record['release'] == sha and
                    record['pid'] == identity[1] and record['invocation'] == identity[2] and
                    type(record['observed_at']) in (int, float) and
                    since <= record['observed_at'] <= self.wall() + 1 and
                    self.wall() - record['observed_at'] <= 30)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def switch(self, sha):
        next_link = self.root / '.next'
        next_link.unlink(missing_ok=True)
        next_link.symlink_to(self.root / 'releases' / sha)
        next_link.replace(self.root / 'current')

    def restart(self):
        try:
            return self.command(['sudo', '-n', '/usr/bin/systemctl', 'restart', 'smebot.service'], 40).returncode == 0
        except (OSError, subprocess.TimeoutExpired, GateFailure):
            return False

    def gate(self, sha):
        # Include the restart in the 90 second candidate/rollback allowance.
        since, deadline = self.wall(), min(self.clock() + 90, self.end)
        if not self.restart():
            return 'restart_failed', None
        identity, stable, checks = None, None, None
        while self.clock() < deadline:
            state = self.state()
            if state is None:
                return 'process_unknown', checks
            if state[0] != 'active' or state[1] <= 0:
                return 'process_dead', checks
            if identity is None:
                identity = state
                if state[3] or not re.fullmatch(r'[0-9a-f]{32}', state[2]):
                    return 'process_restarted', checks
            elif state != identity:
                return 'process_restarted', checks
            ready = self.signal_matches(sha, identity, since)
            if ready:
                stable = self.clock() if stable is None else stable
            else:
                stable = None
            # Reserve time for dependency classification even if startup never
            # signals readiness. No second poller is started by these probes.
            if checks is None and (ready or self.clock() >= deadline - 48):
                checks = self.probe(sha, deadline)
                continue  # Recheck identity after the blocking probe.
            if checks is not None and not self.healthy(checks):
                return 'checks_failed', checks
            if ready and stable is not None and self.clock() - stable >= 10:
                return 'ready', checks
            self.sleep(min(1, max(0, deadline - self.clock())))
        return 'readiness_deadline', checks

    def rollback(self, previous, cause):
        if previous is None:
            self.event('failure', 'no_previous_release', cause=cause)
            return 1
        self.switch(previous)
        reason, _ = self.gate(previous)
        self.event('rollback', 'restored' if reason == 'ready' else 'rollback_failed',
                   release=previous, cause=cause, readiness=reason)
        return 1  # A successful restoration never turns a failed deployment green.

    def activate(self, sha):
        if not self.supported(sha):
            self.event('baseline', 'candidate_protocol_unsupported')
            return 1
        current = self.root / 'current'
        previous = None
        if current.is_symlink() or current.exists():
            target = current.resolve()
            previous = target.name
            if target != self.root / 'releases' / previous or not self.supported(previous):
                self.event('baseline', 'previous_protocol_unsupported')
                return 1
            if not self.healthy(self.probe(previous)):
                self.event('baseline', 'baseline_unhealthy')
                return 1
        else:
            self.event('baseline', 'first_deployment')
        self.end = self.clock() + 295  # Leave headroom under the five-minute outer bound.
        self.switch(sha)
        reason, checks = self.gate(sha)
        self.event('candidate', reason, release=sha)
        if reason == 'ready':
            (self.root / 'incoming' / f'{sha}.tar.gz').unlink()
            print(f'Activated {sha}', flush=True)
            return 0
        state = self.state()
        # A dead/restarting process must not strand a broken release, including
        # when dependency probes are unavailable. Restore at most once.
        if reason in ('restart_failed', 'process_dead', 'process_restarted') or (state and state[0] != 'active'):
            return self.rollback(previous, reason)
        if previous is None:
            self.event('failure', 'no_previous_release', cause=reason, selected=sha)
            return 1
        if checks is None:
            checks = self.probe(sha)
        dependencies = tuple(name for name in CHECKS if name != 'backup')
        if not self.healthy(checks, dependencies):
            # A timeout/missing result alone is not evidence of a regression,
            # even if the fallback's probes succeed. Require a definite failure.
            failed = any(checks[name]['status'] == 'fail' for name in dependencies)
            previous_checks = self.probe(previous)
            if failed and self.healthy(previous_checks):
                return self.rollback(previous, 'candidate_regression')
            self.event('failure', 'external_or_unknown', selected=sha)
            return 1
        if not self.healthy(checks, ('backup',)):
            self.event('failure', 'backup_unavailable', selected=sha)
            return 1
        return self.rollback(previous, reason)

    def save_diagnostics(self, sha):
        # Only normalized events are retained, never raw subprocess output.
        directory = self.root / 'diagnostics'
        directory.mkdir(mode=0o750, exist_ok=True)
        path = directory / f'{sha}-{time.time_ns()}.json'
        path.write_text(json.dumps(dict(v=1, events=self.events), separators=(',', ':')) + '\n')
        path.chmod(0o640)


def main():
    if len(sys.argv) != 2 or not SHA.fullmatch(sys.argv[1]):
        return 2
    controller = Controller()
    try:
        result = controller.activate(sys.argv[1])
    except Exception:
        controller.event('failure', 'controller_error')
        result = 1
    if result:
        try:
            controller.save_diagnostics(sys.argv[1])
        except OSError:
            controller.event('diagnostics', 'write_failed')
    return result


if __name__ == '__main__':
    sys.exit(main())
