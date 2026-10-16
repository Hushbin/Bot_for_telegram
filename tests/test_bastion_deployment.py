"""Fault-injection checks for deployment access and cleanup; no cloud calls."""
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import oci

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy'
sys.path.insert(0, str(DEPLOY))
from bastion_access import BastionAccess
import deploy_via_bastion as deployment


class FakeClient:
    def __init__(self):
        self.cidr = ['198.51.100.1/32']
        self.ttl = 3600
        self.state = 'ACTIVE'
        self.sessions = {}
        self.calls = []
        self.lost_create = False
        self.fail_delete = False
        self.fail_update = False
        self.fail_create = False

    def get_bastion(self, bid):
        return NS(data=NS(lifecycle_state=self.state, max_session_ttl_in_seconds=self.ttl,
                          client_cidr_block_allow_list=self.cidr), headers={'etag': 'version1'})

    def update_bastion(self, bid, details, **kwargs):
        self.calls.append(('allowlist', details.client_cidr_block_allow_list, kwargs))
        if self.fail_update:
            raise oci.exceptions.ServiceError(403, 'NotAuthorized', {}, 'denied')
        self.cidr = details.client_cidr_block_allow_list

    def list_sessions(self, bid, **kwargs):
        return oci.response.Response(200, {}, list(self.sessions.values()), None)

    def get_session(self, sid):
        return NS(data=self.sessions[sid])

    def delete_session(self, sid):
        self.calls.append(('delete', sid))
        if self.fail_delete:
            raise oci.exceptions.ServiceError(403, 'NotAuthorized', {}, 'denied')
        self.sessions[sid].lifecycle_state = 'DELETED'

    def create_session(self, details):
        self.calls.append(('create', details))
        if self.fail_create:
            raise RuntimeError('create rejected')
        self.sessions['session1'] = NS(id='session1', display_name=details.display_name, lifecycle_state='ACTIVE')
        if self.lost_create:
            raise TimeoutError('response lost')
        return NS(data=self.sessions['session1'])


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.tick = 0
        self.access = self.make_access()

    def make_access(self, **kwargs):
        return BastionAccess(self.client, 'ocid1.bastion.oc1.eu-zurich-1.id', '198.51.100.1/32',
                             'deploy123a1', '10.20.0.133', sleep=self.sleep, clock=lambda: self.tick, **kwargs)

    def sleep(self, seconds):
        self.tick += seconds

    def test_invalid_idle_or_target_configuration(self):
        for cidr in ['0.0.0.0/0', '198.51.100.0/24', '::1/128', 'invalid']:
            with self.subTest(cidr=cidr), self.assertRaises(ValueError):
                BastionAccess(self.client, 'ocid1.bastion.oc1.eu-zurich-1.id', cidr, 'deploy123a1', '10.20.0.133')
        with self.assertRaises(ValueError):
            self.access.prepare('999.1.1.1')
        self.assertEqual([], self.client.calls)

    def test_success_restores_idle_and_deletes_session(self):
        self.access.prepare('203.0.113.8')
        self.assertEqual(['203.0.113.8/32'], self.client.cidr)
        sid = self.access.create('ssh-ed25519 public')
        details = next(c[1] for c in self.client.calls if c[0] == 'create')
        self.assertEqual(3600, details.session_ttl_in_seconds)
        self.assertEqual(22, details.target_resource_details.target_resource_port)
        self.access.cleanup()
        self.assertEqual('DELETED', self.client.sessions[sid].lifecycle_state)
        self.assertEqual(['198.51.100.1/32'], self.client.cidr)
        self.assertTrue(all(c[2] == {'if_match': 'version1'} for c in self.client.calls if c[0] == 'allowlist'))

    def test_stale_sessions_removed_before_opening_access(self):
        self.client.sessions['stale'] = NS(id='stale', display_name='deploy111a2', lifecycle_state='ACTIVE')
        self.access.prepare('203.0.113.8')
        self.assertEqual(('delete', 'stale'), self.client.calls[0])

    def test_unexpected_session_blocks_without_deleting(self):
        self.client.sessions['other'] = NS(id='other', display_name='operator-session', lifecycle_state='ACTIVE')
        with self.assertRaisesRegex(RuntimeError, 'Unexpected session'):
            self.access.prepare('203.0.113.8')
        self.access.cleanup()
        self.assertEqual([], self.client.calls)

    def test_bad_bastion_state_or_ttl_blocks(self):
        for attr, value in [('state', 'UPDATING'), ('ttl', 10800)]:
            with self.subTest(attr=attr):
                setattr(self.client, attr, value)
                with self.assertRaises(RuntimeError):
                    self.access.prepare('203.0.113.8')
                setattr(self.client, attr, 'ACTIVE' if attr == 'state' else 3600)
        self.assertEqual([], self.client.calls)

    def test_failed_allowlist_never_creates_session(self):
        self.client.fail_update = True
        with self.assertRaises(oci.exceptions.ServiceError):
            self.access.prepare('203.0.113.8')
        self.access.cleanup()  # Idle allowlist already matches.
        self.assertFalse(any(c[0] == 'create' for c in self.client.calls))

    def test_lost_creation_response_recovers_session_by_name(self):
        self.access.prepare('203.0.113.8')
        self.client.lost_create = True
        with self.assertRaises(TimeoutError):
            self.access.create('public')
        self.access.cleanup()
        self.assertEqual('DELETED', self.client.sessions['session1'].lifecycle_state)
        self.assertEqual(['198.51.100.1/32'], self.client.cidr)

    def test_failed_deletion_still_restores_allowlist_and_fails(self):
        self.access.prepare('203.0.113.8')
        self.access.create('public')
        self.client.fail_delete = True
        with self.assertRaisesRegex(RuntimeError, 'cleanup could not be verified'):
            self.access.cleanup()
        self.assertEqual(['198.51.100.1/32'], self.client.cidr)

    def test_failed_idle_restore_is_reported(self):
        self.access.prepare('203.0.113.8')
        self.access.create('public')
        self.client.fail_update = True
        with self.assertRaisesRegex(RuntimeError, 'idle Bastion allowlist'):
            self.access.cleanup()
        self.assertEqual('DELETED', self.client.sessions['session1'].lifecycle_state)

    def test_ambiguous_create_without_visible_session_is_bounded(self):
        self.access.prepare('203.0.113.8')
        self.client.fail_create = True
        with self.assertRaises(RuntimeError):
            self.access.create('public')
        with self.assertRaisesRegex(RuntimeError, 'cleanup could not be verified'):
            self.access.cleanup()
        self.assertLessEqual(self.tick, 123)
        self.assertEqual(['198.51.100.1/32'], self.client.cidr)


class OrchestrationTests(unittest.TestCase):
    def env(self):
        values = {name: 'value' for name in deployment.REQUIRED}
        values.update(GITHUB_SHA='a'*40, GITHUB_RUN_ID='123', GITHUB_RUN_ATTEMPT='1', OCI_REGION='eu-zurich-1')
        return values

    def test_missing_configuration_fails_before_client_creation(self):
        with self.assertRaisesRegex(ValueError, 'OCI_USER_OCID'):
            deployment.run({}, client_factory=lambda *a, **kw: self.fail('client created'))

    def test_transfer_failure_or_termination_always_cleans_access(self):
        for failure in [RuntimeError('activation failed'), InterruptedError('terminated')]:
            with self.subTest(failure=failure), patch.object(deployment, 'BastionAccess') as access, \
                 patch.object(deployment.urllib.request, 'urlopen') as url, \
                 patch.object(deployment.subprocess, 'run') as command, \
                 patch.object(Path, 'read_text', return_value='public'):
                url.return_value.__enter__.return_value.read.return_value = b'203.0.113.8'
                def transfer(*args):
                    raise failure
                with self.assertRaises(type(failure)):
                    deployment.run(self.env(), client_factory=lambda *a, **kw: object(), transfer=transfer)
                access.return_value.cleanup.assert_called_once()

    def test_connection_failure_closes_tunnel(self):
        env = self.env()
        env.update(BOT_SSH_HOST_KEY='ssh-ed25519 public', BOT_PRIVATE_IP='10.20.0.133')
        with self.subTest(), patch.object(deployment.subprocess, 'Popen') as popen, \
             patch.object(deployment.subprocess, 'run') as command, \
             patch.object(deployment.time, 'sleep'), patch.object(Path, 'write_text'), patch.object(Path, 'chmod'):
            popen.return_value.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, 'did not become ready'):
                deployment.deploy(env, '/tmp/testing', 'session')
            self.assertEqual(30, popen.call_count)
            popen.return_value.terminate.assert_called()


if __name__ == '__main__':
    unittest.main()
