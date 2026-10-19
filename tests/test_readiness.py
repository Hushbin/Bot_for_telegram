import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import NetworkError
from telegram.ext import ExtBot

import main
import readiness
from process_readiness import ProcessReadiness, ReadinessBot


class ProcessSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_successful_poll_after_runtime_start_publishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            signal = ProcessReadiness(tmp, 'a' * 40, 'b' * 32)
            bot = ReadinessBot('123456:TEST', signal)
            with patch.object(ExtBot, 'get_updates', new_callable=AsyncMock) as poll:
                poll.return_value = ()
                await bot.get_updates(timeout=10)
                self.assertFalse((Path(tmp) / 'ready.json').exists())
                signal.runtime_started(lambda: True)
                poll.side_effect = NetworkError('PRIVATE SECRET')
                with self.assertRaises(NetworkError):
                    await bot.get_updates(timeout=10)
                self.assertFalse((Path(tmp) / 'ready.json').exists())
                poll.side_effect = None
                await bot.get_updates(timeout=10)
                record = json.loads((Path(tmp) / 'ready.json').read_text())
                self.assertEqual(set(record), {'v', 'release', 'pid', 'invocation', 'observed_at'})
                self.assertEqual(record['pid'], os.getpid())
                signal.stop()
                await bot.get_updates(timeout=10)  # Updater's final acknowledgement cannot republish.
                self.assertFalse((Path(tmp) / 'ready.json').exists())
                self.assertEqual(poll.await_count, 4)

    async def test_worker_or_panel_exit_clears_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            healthy = [True]
            signal = ProcessReadiness(tmp, 'a' * 40, 'b' * 32)
            signal.runtime_started(lambda: healthy[0])
            signal.polling_succeeded()
            healthy[0] = False
            signal.polling_succeeded()
            self.assertFalse((Path(tmp) / 'ready.json').exists())

    async def test_signal_storage_failure_preserves_successful_poll_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            signal = ProcessReadiness(Path(tmp) / 'missing', 'a' * 40, 'b' * 32)
            signal.runtime_started(lambda: True)
            bot = ReadinessBot('123456:TEST', signal)
            updates = (object(),)
            with patch.object(ExtBot, 'get_updates', new_callable=AsyncMock, return_value=updates), \
                 self.assertLogs('process_readiness', level='WARNING') as logs:
                self.assertIs(await bot.get_updates(), updates)
            self.assertEqual(logs.output, ['WARNING:process_readiness:Readiness signal unavailable'])

    async def test_shutdown_does_not_remove_another_invocations_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            signal = ProcessReadiness(tmp, 'a' * 40, 'b' * 32)
            path = Path(tmp) / 'ready.json'
            path.write_text(json.dumps(dict(invocation='c' * 32, pid=os.getpid())))
            signal.stop()
            self.assertTrue(path.exists())

    async def test_main_startup_requires_commands_panel_and_worker(self):
        from local_backend import SQLiteStore, LocalMediaStorage
        for panel_enabled, panel_failure, menu_failure in ((False, False, False), (True, False, False),
                                                           (True, True, False), (True, False, True)):
            with self.subTest(panel=panel_enabled, failure=panel_failure, menu_failure=menu_failure), \
                 tempfile.TemporaryDirectory() as tmp, \
                 patch.object(main.ProcessReadiness, 'for_process') as signal_type, \
                 patch.object(main, 'register_command_menu', new_callable=AsyncMock) as commands, \
                 patch.object(main, 'OrderWorker') as worker_type, \
                 patch('seller_api.SellerRuntime') as panel_type:
                settings = NS(backend='local', bot_token='123456:TEST', orders_enabled=True,
                              seller_panel_url='https://example.test' if panel_enabled else '', seller_api_port=8081)
                panel = panel_type.return_value
                panel.start = AsyncMock(side_effect=RuntimeError('unavailable') if panel_failure else None)
                panel.stop = AsyncMock()
                worker_type.return_value.stop = AsyncMock()
                worker_type.return_value.task.done.return_value = False
                if menu_failure:
                    commands.side_effect = RuntimeError('menu unavailable')
                app = main.build_application(settings, SQLiteStore(Path(tmp) / 'db'), LocalMediaStorage(Path(tmp) / 'media'))
                if panel_failure or menu_failure:
                    with self.assertRaises(RuntimeError):
                        await app.post_init(app)
                    signal_type.return_value.runtime_started.assert_not_called()
                    worker_type.return_value.start.assert_not_called()
                else:
                    await app.post_init(app)
                    signal_type.return_value.runtime_started.assert_called_once()
                    worker_type.return_value.start.assert_called_once()
                    # post_init by itself is not a ready polling application.
                    guards = signal_type.return_value.runtime_started.call_args.args
                    self.assertFalse(all(guard() for guard in guards))
                await app.post_shutdown(app)
                signal_type.return_value.stop.assert_called_once()


class ProbeTests(unittest.TestCase):
    def test_fixed_cli_emits_structured_configuration_failure_without_network(self):
        env = {**os.environ, 'APP_BACKEND': 'local', 'SECRETS_SOURCE': 'env', 'BOT_TOKEN': '123456:TEST'}
        result = subprocess.run([sys.executable, str(Path(readiness.__file__).parent / 'scripts/readiness.py')],
                                env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        report = json.loads(result.stdout)
        self.assertEqual(report['v'], 1)
        self.assertEqual(report['checks']['configuration']['reason'], 'vm_configuration_required')
        self.assertTrue(all(report['checks'][name]['status'] == 'unknown' for name in readiness.CHECKS[1:]))

    def test_report_bounds_children_and_redacts_failures(self):
        def run(args, **kwargs):
            self.assertEqual(kwargs['timeout'], 8)
            if args[-1] == 'configuration':
                return subprocess.CompletedProcess(args, 0, b'{"status":"pass","reason":"ok"}')
            if args[-1] == 'database':
                raise subprocess.TimeoutExpired(args, 8, output=b'PRIVATE SECRET')
            return subprocess.CompletedProcess(args, 1, b'PRIVATE SECRET')
        report = readiness.collect_report('a' * 40, runner=run)
        self.assertEqual(report['checks']['database']['reason'], 'probe_timeout')
        self.assertNotIn('PRIVATE SECRET', json.dumps(report))
        self.assertEqual(set(report['checks']), set(readiness.CHECKS))

    def test_configuration_failure_prevents_other_probes(self):
        runner = MagicMock(return_value=subprocess.CompletedProcess([], 0,
                           b'{"status":"fail","reason":"configuration_unavailable"}'))
        report = readiness.collect_report('a' * 40, runner=runner)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(report['checks']['database']['status'], 'unknown')

    def test_real_hung_subprocess_is_killed(self):
        # Exercise subprocess.run's hard deadline rather than a cooperative SDK timeout.
        def runner(args, **kwargs):
            return subprocess.run([sys.executable, '-c', 'import time; time.sleep(10)'], **kwargs)
        started = time.monotonic()
        report = readiness.collect_report('a' * 40, runner=runner, timeout=0.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(report['checks']['configuration']['reason'], 'probe_timeout')

    def test_database_probe_reads_schema_without_writes(self):
        with patch('database.OracleStore') as store_type:
            connection = store_type.return_value.connect.return_value.__enter__.return_value
            connection.cursor.return_value.__enter__.return_value.fetchone.return_value = [1]
            readiness.database_ok(NS())
            sql = [call.args[0] for call in connection.cursor.return_value.__enter__.return_value.execute.call_args_list]
            self.assertTrue(all(item.startswith('SELECT ') for item in sql))
            self.assertTrue(any('ORDER_NOTIFICATIONS' in item for item in sql))
            connection.commit.assert_not_called()

    def test_backup_requires_fresh_pointer_manifest_and_accessible_data(self):
        with patch('recovery.RecoveryStorage') as factory:
            storage = factory.return_value
            storage.latest_export_age.return_value = 60
            storage.get_json.side_effect = [dict(manifest_key='exports/one.manifest.json', snapshot_at='now'),
                                           dict(snapshot_at='now', format=2, data_key='exports/one.json.gz')]
            settings = NS(oci_namespace='ns', oci_recovery_bucket='bucket', oci_auth='instance_principal',
                          oci_config_file='', oci_profile='DEFAULT')
            self.assertEqual(readiness.backup_ok(settings), ('pass', 'ok'))
            storage.client.head_object.assert_called_once()
            storage.latest_export_age.return_value = 36 * 3600
            storage.get_json.side_effect = [dict(manifest_key='exports/one.manifest.json', snapshot_at='now')]
            self.assertEqual(readiness.backup_ok(settings), ('fail', 'backup_stale'))

    def test_seller_api_is_required_only_when_configured(self):
        with patch('config.load_settings') as load, patch.object(readiness.urllib.request, 'build_opener') as opener:
            load.return_value = NS(backend='oci', oci_auth='instance_principal', seller_panel_url='')
            self.assertEqual(readiness.run_check('seller_api'), ('pass', 'not_configured'))
            opener.assert_not_called()
            load.return_value.seller_panel_url = 'https://example.test'
            load.return_value.seller_api_port = 8081
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status, response.read.return_value = 200, b'{"status":"ok"}'
            self.assertEqual(readiness.run_check('seller_api'), ('pass', 'ok'))
            opener.return_value.open.assert_called_once_with('http://127.0.0.1:8081/api/health', timeout=3)
            response.status = 503
            self.assertEqual(readiness.run_check('seller_api'), ('fail', 'seller_api_unhealthy'))

    def test_telegram_probe_only_gets_identity(self):
        with patch('telegram.Bot') as bot_type, patch('telegram.request.HTTPXRequest'):
            bot = bot_type.return_value.__aenter__.return_value
            asyncio.run(readiness.telegram_ok('123456:TEST'))
            bot.get_me.assert_awaited_once()
            bot.get_updates.assert_not_called()
            bot.send_message.assert_not_called()

    def test_health_preserves_metric_dimensions_and_shares_probes(self):
        from scripts import recovery as job
        report = dict(checks={name: dict(status='pass') for name in readiness.CHECKS})
        with patch.dict(os.environ, OCI_COMPARTMENT_OCID='compartment', OCI_INSTANCE_OCID='instance'), \
             patch.object(job, 'collect_report', return_value=report), \
             patch.object(job.subprocess, 'run', return_value=NS(returncode=0)), \
             patch.object(job, 'publish_health') as publish:
            self.assertEqual(job.health_job(), 0)
            self.assertEqual(publish.call_args.args, ({'service': True, 'telegram': True,
                                                      'database': True, 'backup': True}, 'compartment', 'instance'))
