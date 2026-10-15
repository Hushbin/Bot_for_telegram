import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import recovery as job


class HealthMetricTests(unittest.TestCase):
    @patch.object(job.oci.monitoring, "MonitoringClient")
    @patch.object(job.oci.auth.signers, "InstancePrincipalsSecurityTokenSigner")
    def test_metrics_use_ingestion_endpoint(self, signer_type, client_type):
        signer_type.return_value = SimpleNamespace(region="eu-zurich-1")
        job.publish_health({"service": True, "database": False}, "compartment", "instance")
        self.assertEqual(
            client_type.call_args.kwargs["service_endpoint"],
            "https://telemetry-ingestion.eu-zurich-1.oraclecloud.com",
        )
        details = client_type.return_value.post_metric_data.call_args.args[0]
        self.assertEqual({metric.dimensions["check"] for metric in details.metric_data},
                         {"service", "database"})
