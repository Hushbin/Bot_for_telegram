"""Subprocess driver for the real controller with a disposable VM and fake clock."""
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy'))
from activation import Controller

root = Path(sys.argv[2] if sys.argv[1] == '--fake' else sys.argv[1])
scenario = json.loads((root / 'scenario.json').read_text())


def state_output(restart, now):
    state = scenario.get('states', {}).get(str(restart), 'active')
    invocation = f'{restart:032x}'
    restarts = 0
    if scenario.get('restart_loop') and restart == 1:
        restarts = 1
    if (scenario.get('invocation_change') and restart == 1 and
            now >= float((root / 'restart_at').read_text()) + 5):
        invocation = 'f' * 32
    return f'ActiveState={state}\nMainPID={123 + restart}\nInvocationID={invocation}\nNRestarts={restarts}\n'


class Harness(Controller):
    def __init__(self):
        self.tick = 1000.0
        self.counts = {}
        self.restarts = 0
        self.identity = None
        self.sampled_states = set()
        super().__init__(root, root / 'run', clock=lambda: self.tick, wall=lambda: self.tick,
                         sleep=self.advance)

    def advance(self, seconds):
        self.tick += seconds

    def command(self, args, timeout):
        # Probe/restart commands and the first state read per restart cross a real
        # subprocess boundary. Later state polls use the same dynamic simulator
        # in-process: advancing fake time must not spawn ~100 Python interpreters
        # and exhaust the outer real-time watchdog on a slower test worker.
        if timeout <= 0 or self.tick >= self.end:
            from activation import GateFailure
            raise GateFailure('attempt_deadline')
        if 'smebot-probe' in args[-2]:
            sha = args[-1]
            self.counts[sha] = self.counts.get(sha, 0) + 1
            command = ['probe', sha, str(self.counts[sha])]
        elif args[-2:] == ['restart', 'smebot.service']:
            self.restarts += 1
            command = ['restart', str(self.restarts)]
        else:
            command = ['state', str(self.restarts)]
        if command[0] == 'state' and self.restarts in self.sampled_states:
            result = subprocess.CompletedProcess(command, 0,
                                                 state_output(self.restarts, self.tick).encode(), b'')
        else:
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--fake',
                                     str(root), str(self.tick), *command], capture_output=True, timeout=3)
            if command[0] == 'state':
                self.sampled_states.add(self.restarts)
        if command[0] == 'probe':
            if (scenario.get('hang_probe') or self.counts[sha] in
                    scenario.get('hang_probe_at', {}).get(sha, [])):
                self.tick += min(timeout, 48)
                raise subprocess.TimeoutExpired('redacted', timeout, output=b'PRIVATE SECRET')
            self.tick += scenario.get('probe_duration', 0)
        if command[0] == 'restart' and not result.returncode:
            (root / 'restart_at').write_text(str(self.tick))
            current = (root / 'current').resolve().name
            ready = scenario.get('signals', {}).get(current, 'good')
            if ready != 'missing':
                record = dict(v=1, release=current, pid=123 + self.restarts,
                              invocation=f'{self.restarts:032x}', observed_at=self.tick)
                if ready == 'stale':
                    record['observed_at'] -= 100
                if ready == 'wrong_release':
                    record['release'] = 'f' * 40
                if ready == 'wrong_pid':
                    record['pid'] = 9999
                if ready == 'wrong_invocation':
                    record['invocation'] = 'f' * 32
                (root / 'run/ready.json').write_text(json.dumps(record))
        # Emulate the real process's continuing successful polls.
        path = root / 'run/ready.json'
        current = (root / 'current').resolve().name
        if path.exists() and scenario.get('signals', {}).get(current, 'good') == 'good':
            record = json.loads(path.read_text())
            record['observed_at'] = self.tick
            path.write_text(json.dumps(record))
        return result


def fake():
    root = Path(sys.argv[2])
    now, action = float(sys.argv[3]), sys.argv[4]
    scenario = json.loads((root / 'scenario.json').read_text())
    if action == 'probe':
        sha, count = sys.argv[5], int(sys.argv[6])
        statuses = scenario.get('probes', {}).get(sha, [{}])
        status = statuses[min(count - 1, len(statuses) - 1)]
        checks = {name: dict(status=status.get(name, 'pass'), reason='ok', started_at=now, observed_at=now)
                  for name in ('configuration', 'telegram', 'database', 'backup', 'seller_api')}
        print(json.dumps(dict(v=1, release=sha, started_at=now, observed_at=now, checks=checks)))
        sys.exit(0 if all(i['status'] == 'pass' for i in checks.values()) else 1)
    if action == 'restart':
        sys.exit(1 if int(sys.argv[5]) in scenario.get('restart_failure', []) else 0)
    print(state_output(int(sys.argv[5]), now), end='')


if __name__ == '__main__':
    if sys.argv[1] == '--fake':
        fake()
    else:
        controller = Harness()
        result = controller.activate(sys.argv[2])
        controller.save_diagnostics(sys.argv[2])
        (root / 'result.json').write_text(json.dumps(dict(result=result, tick=controller.tick,
                                                         restarts=controller.restarts)))
        sys.exit(result)
