"""VM secret loading never falls back to plaintext or prints secret material."""

import base64
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from config import load_settings
from vault_secrets import load_vault_secrets


VAULT_ENV = {
    "APP_BACKEND": "oci",
    "SECRETS_SOURCE": "oci_vault",
    "OCI_AUTH": "instance_principal",
    "DB_DSN": "smebotdev_low",
    "DB_WALLET_DIR": "/etc/smebot/wallet",
    "DB_USER": "SME_BOT",
    "OCI_NAMESPACE": "example",
    "OCI_BUCKET_NAME": "media",
    "BOT_TOKEN_SECRET_OCID": "ocid1.vaultsecret.oc1..bot",
    "DB_PASSWORD_SECRET_OCID": "ocid1.vaultsecret.oc1..db",
    "DB_WALLET_PASSWORD_SECRET_OCID": "ocid1.vaultsecret.oc1..wallet",
}


class VaultSettingsTests(unittest.TestCase):
    def test_vault_settings_resolve_all_three_secrets(self):
        values = {"BOT_TOKEN": "bot-token", "DB_PASSWORD": "db-pass",
                  "DB_WALLET_PASSWORD": "wallet-pass"}
        with patch.dict("os.environ", VAULT_ENV, clear=True), \
             patch("config.load_vault_secrets", return_value=values) as load:
            settings = load_settings()
        self.assertEqual((settings.bot_token, settings.db_password,
                          settings.db_wallet_password),
                         ("bot-token", "db-pass", "wallet-pass"))
        self.assertNotIn("bot-token", repr(settings))
        self.assertNotIn("db-pass", repr(settings))
        self.assertNotIn("wallet-pass", repr(settings))
        load.assert_called_once_with({
            "BOT_TOKEN": VAULT_ENV["BOT_TOKEN_SECRET_OCID"],
            "DB_PASSWORD": VAULT_ENV["DB_PASSWORD_SECRET_OCID"],
            "DB_WALLET_PASSWORD": VAULT_ENV["DB_WALLET_PASSWORD_SECRET_OCID"],
        })

    def test_missing_or_invalid_secret_id_fails_before_network(self):
        for change in ({"BOT_TOKEN_SECRET_OCID": ""},
                       {"DB_PASSWORD_SECRET_OCID": "not-an-ocid"}):
            with self.subTest(change=change), \
                 patch.dict("os.environ", VAULT_ENV | change, clear=True), \
                 patch("config.load_vault_secrets") as load:
                with self.assertRaises(ValueError):
                    load_settings()
                load.assert_not_called()

    def test_vault_failure_does_not_use_plaintext_fallback(self):
        old_values = {"BOT_TOKEN": "old-token", "DB_PASSWORD": "old-db-pass",
                      "DB_WALLET_PASSWORD": "old-wallet-pass"}
        with patch.dict("os.environ", VAULT_ENV | old_values, clear=True), \
             patch("config.load_vault_secrets", side_effect=RuntimeError("vault unavailable")):
            with self.assertRaisesRegex(RuntimeError, "vault unavailable"):
                load_settings()

    def test_local_mode_rejects_vault_source(self):
        with patch.dict("os.environ", {"APP_BACKEND": "local",
                                    "SECRETS_SOURCE": "oci_vault"}, clear=True):
            with self.assertRaisesRegex(ValueError, "Local mode"):
                load_settings()

    def test_vault_requires_instance_principal(self):
        with patch.dict("os.environ", VAULT_ENV | {"OCI_AUTH": "config_file"}, clear=True):
            with self.assertRaisesRegex(ValueError, "instance_principal"):
                load_settings()


class VaultClientTests(unittest.TestCase):
    def test_reads_current_bundle_with_instance_principal(self):
        secret_ids = {"BOT_TOKEN": VAULT_ENV["BOT_TOKEN_SECRET_OCID"],
                      "DB_PASSWORD": VAULT_ENV["DB_PASSWORD_SECRET_OCID"]}
        encoded = {
            secret_ids["BOT_TOKEN"]: base64.b64encode(b"test-token").decode(),
            secret_ids["DB_PASSWORD"]: base64.b64encode(b"test-password").decode(),
        }

        def bundle(secret_id, *, stage):
            self.assertEqual(stage, "CURRENT")
            return SimpleNamespace(data=SimpleNamespace(secret_bundle_content=
                                   SimpleNamespace(content=encoded[secret_id])))

        with patch("vault_secrets.oci.auth.signers.InstancePrincipalsSecurityTokenSigner") as signer, \
             patch("vault_secrets.oci.secrets.SecretsClient") as client:
            client.return_value.get_secret_bundle.side_effect = bundle
            values = load_vault_secrets(secret_ids)
        self.assertEqual(values, {"BOT_TOKEN": "test-token", "DB_PASSWORD": "test-password"})
        signer.assert_called_once_with()
        client.assert_called_once_with({}, signer=signer.return_value)

    def test_invalid_bundle_and_oci_errors_are_redacted(self):
        secret_ids = {"BOT_TOKEN": VAULT_ENV["BOT_TOKEN_SECRET_OCID"]}
        for result in (SimpleNamespace(data=SimpleNamespace(secret_bundle_content=
                       SimpleNamespace(content="not base64!"))),
                       RuntimeError("sensitive-response-body")):
            with self.subTest(result=type(result).__name__), \
                 patch("vault_secrets.oci.auth.signers.InstancePrincipalsSecurityTokenSigner"), \
                 patch("vault_secrets.oci.secrets.SecretsClient") as client:
                if isinstance(result, Exception):
                    client.return_value.get_secret_bundle.side_effect = result
                else:
                    client.return_value.get_secret_bundle.return_value = result
                with self.assertRaises(RuntimeError) as raised:
                    load_vault_secrets(secret_ids)
                self.assertEqual(str(raised.exception),
                                 "Unable to load runtime secrets from OCI Vault")
                self.assertIsNone(raised.exception.__cause__)
