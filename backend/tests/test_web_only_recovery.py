import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


class WebOnlyRecoveryTests(unittest.TestCase):
    def test_web_only_lifespan_does_not_recover_external_or_production_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Settings(_env_file=None, app_mode='integration', integration_dry_run=True,
                database_url='sqlite:///' + str(root / 'isolated.db'), storage_root=root / 'storage',
                production_recovery_enabled=False, external_recovery_enabled=False)
            app = create_app(config)
            services = (app.state.publishing, app.state.descriptions, app.state.local_cutout,
                        app.state.collector, app.state.production, app.state.repairs)
            probes = [patch.object(service, 'recover', new_callable=AsyncMock) for service in services]
            started = []
            try:
                started = [probe.start() for probe in probes]
                before = app.state.repository.state()
                with TestClient(app) as client:
                    health = client.get('/api/health')
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(health.json()['worker'], 'not_started')
                    self.assertFalse(health.json()['production_execution_enabled'])
                after = app.state.repository.state()
                for probe in started:
                    probe.assert_not_awaited()
                self.assertEqual(before, after)
            finally:
                for probe in reversed(probes):
                    probe.stop()

    def test_explicit_external_recovery_retains_existing_default_behavior(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = Settings(_env_file=None, app_mode='integration', integration_dry_run=True,
                database_url='sqlite:///' + str(root / 'isolated.db'), storage_root=root / 'storage',
                production_recovery_enabled=False, external_recovery_enabled=True)
            app = create_app(config)
            with patch.object(app.state.publishing, 'recover', new_callable=AsyncMock) as publishing, patch.object(app.state.descriptions, 'recover', new_callable=AsyncMock) as descriptions:
                with TestClient(app) as client:
                    self.assertEqual(client.get('/api/health').status_code, 200)
                self.assertEqual(publishing.await_count, 2)
                descriptions.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
