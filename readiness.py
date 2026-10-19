"""Read-only deployment probes. SDK calls live in disposable, deadline-bound children."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


PROTOCOL = 1
CHECKS = ('configuration', 'telegram', 'database', 'backup', 'seller_api')
CHECK_TIMEOUT = 8


async def telegram_ok(token):
    from telegram import Bot
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(connect_timeout=3, read_timeout=3, write_timeout=3, pool_timeout=3)
    async with Bot(token=token, request=request) as bot:
        await bot.get_me()  # Never poll, send messages or change Telegram configuration.


def database_ok(settings):
    from database import OracleStore, REQUIRED_COLUMNS
    store = OracleStore(settings)
    with store.connect() as connection:
        connection.call_timeout = 3000
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1 FROM dual')
            if cursor.fetchone()[0] != 1:
                raise ValueError('Unexpected read result')
            for table, columns in REQUIRED_COLUMNS.items():
                cursor.execute(f"SELECT {', '.join(columns)} FROM {store.schema}.{table} WHERE 1 = 0")


def backup_ok(settings):
    from recovery import RecoveryStorage
    storage = RecoveryStorage(settings.oci_namespace, settings.oci_recovery_bucket,
                              auth=settings.oci_auth, config_file=settings.oci_config_file,
                              profile=settings.oci_profile)
    # Disable retries for these requests. The subprocess also bounds signer/Vault
    # setup and all SDK work, including any retries performed during authentication.
    import oci
    latest = storage.get_json('latest.json', retry_strategy=oci.retry.NoneRetryStrategy())
    age = storage.latest_export_age(latest)
    if not 0 <= age < 36 * 3600:
        return 'fail', 'backup_stale'
    key = latest['manifest_key']
    if not key.startswith('exports/') or not key.endswith('.manifest.json'):
        raise ValueError('Invalid manifest')
    manifest = storage.get_json(key, retry_strategy=oci.retry.NoneRetryStrategy())
    if manifest['snapshot_at'] != latest['snapshot_at'] or manifest['format'] not in (1, 2):
        raise ValueError('Invalid manifest')
    data_key = manifest['data_key']
    if not data_key.startswith('exports/'):
        raise ValueError('Invalid data key')
    storage.client.head_object(storage.namespace, storage.bucket, data_key,
                               retry_strategy=oci.retry.NoneRetryStrategy())
    return 'pass', 'ok'


def run_check(name):
    """Return only fixed codes. Never serialize exception messages or SDK responses."""
    try:
        from config import load_settings
        settings = load_settings()
        if settings.backend != 'oci' or settings.oci_auth != 'instance_principal':
            return 'fail', 'vm_configuration_required'
        if name == 'telegram':
            asyncio.run(telegram_ok(settings.bot_token))
        elif name == 'database':
            database_ok(settings)
        elif name == 'backup':
            return backup_ok(settings)
        elif name == 'seller_api':
            if not settings.seller_panel_url:
                return 'pass', 'not_configured'
            port = settings.seller_api_port
            if not 1 <= port <= 65535:
                return 'fail', 'seller_api_configuration'
            # Disable proxies: a loopback check must stay on this VM.
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(f'http://127.0.0.1:{port}/api/health', timeout=3) as response:
                if response.status != 200 or json.loads(response.read(1024)) != {'status': 'ok'}:
                    return 'fail', 'seller_api_unhealthy'
        return 'pass', 'ok'
    except Exception:
        return 'fail', name + '_unavailable'


def collect_report(release, *, runner=subprocess.run, clock=time.time, timeout=CHECK_TIMEOUT):
    started = clock()
    checks = {}
    for name in CHECKS:
        observed = clock()
        if name != 'configuration' and checks['configuration']['status'] != 'pass':
            status, reason = 'unknown', 'configuration_unavailable'
        else:
            try:
                result = runner([sys.executable, str(Path(__file__).parent / 'scripts/readiness.py'),
                                 '--check', name], capture_output=True, timeout=timeout, check=False)
                item = json.loads(result.stdout)
                status, reason = item['status'], item['reason']
                if result.returncode or status not in ('pass', 'fail') or reason not in {
                    'ok', 'not_configured', 'backup_stale', 'vm_configuration_required',
                    'seller_api_configuration', 'seller_api_unhealthy', name + '_unavailable',
                }:
                    raise ValueError('Invalid check result')
            except subprocess.TimeoutExpired:
                status, reason = 'unknown', 'probe_timeout'
            except Exception:
                status, reason = 'unknown', 'probe_unavailable'
        checks[name] = dict(status=status, reason=reason, started_at=observed, observed_at=clock())
    return dict(v=PROTOCOL, release=release, started_at=started, observed_at=clock(), checks=checks)
