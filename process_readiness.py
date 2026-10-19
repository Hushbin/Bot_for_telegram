"""Identity-only startup signal from the bot that actually polls Telegram."""
import json
import logging
import os
from pathlib import Path
import re
import time

from telegram.ext import ExtBot


class ProcessReadiness:
    def __init__(self, directory=None, release=None, invocation=None):
        self.directory = Path(directory) if directory else None
        self.release = release
        self.invocation = invocation
        self.started = False
        self.stopped = False
        self.guards = []

    @classmethod
    def for_process(cls):
        release = Path(__file__).resolve().parent.name
        invocation = os.getenv('INVOCATION_ID', '')
        if re.fullmatch(r'[0-9a-f]{40}', release) and re.fullmatch(r'[0-9a-f]{32}', invocation):
            return cls('/run/smebot', release, invocation)
        return cls()  # Local development and non-systemd runs do not publish.

    def runtime_started(self, *guards):
        self.guards = guards
        self.started = True

    def polling_succeeded(self):
        if not self.directory or not self.started or self.stopped:
            return
        if not all(guard() for guard in self.guards):
            self.clear()
            return
        record = dict(v=1, release=self.release, invocation=self.invocation,
                      pid=os.getpid(), observed_at=time.time())
        temporary = self.directory / f'.ready-{os.getpid()}'
        try:
            temporary.write_text(json.dumps(record, separators=(',', ':')) + '\n')
            temporary.chmod(0o644)
            temporary.replace(self.directory / 'ready.json')
        except OSError:
            # Readiness storage failure must fail the gate without discarding a
            # successful getUpdates response or exposing request/response data.
            self.clear()
            logging.getLogger(__name__).warning('Readiness signal unavailable')
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def clear(self):
        if self.directory:
            path = self.directory / 'ready.json'
            try:
                record = json.loads(path.read_text())
                if (isinstance(record, dict) and record.get('invocation') == self.invocation
                        and record.get('pid') == os.getpid()):
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

    def stop(self):
        self.stopped = True
        self.clear()


class ReadinessBot(ExtBot):
    """Use PTB's public bot integration; never issue an extra polling request."""
    __slots__ = ('readiness',)

    def __init__(self, token, readiness):
        super().__init__(token)
        object.__setattr__(self, 'readiness', readiness)

    async def get_updates(self, *args, **kwargs):
        updates = await super().get_updates(*args, **kwargs)
        self.readiness.polling_succeeded()
        return updates
