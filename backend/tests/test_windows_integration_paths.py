import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import runtime_paths
from app.config import Settings
from app.integrations.comfy_asset_adapter import ComfyAssetAdapter
from app.integrations.legacy import browser_client
from app.integrations.legacy.collector_adapter import LegacyCollectorAdapter
from app.integrations.legacy.video_adapter import LegacyVideoAdapter, LegacyComposeAdapter


class NativeIntegrationPathTests(unittest.TestCase):
    def test_windows_settings_defaults_use_scripts_python_without_editing_legacy(self):
        with patch.object(runtime_paths.sys, 'platform', 'win32'):
            settings = Settings(_env_file=None, legacy_tiktok_auto_root=Path('/fixture/TikTok_Auto'),
                                legacy_video_auto_root=Path('/fixture/video_auto'))
        self.assertEqual(settings.legacy_tiktok_auto_python, Path('/fixture/TikTok_Auto/.venv/Scripts/python.exe'))
        self.assertEqual(settings.legacy_video_auto_python, Path('/fixture/video_auto/.venv/Scripts/python.exe'))
        self.assertEqual(settings.quality_python.parts[-3:], ('.venv', 'Scripts', 'python.exe'))

    def test_explicit_interpreters_are_never_replaced(self):
        with patch.object(runtime_paths.sys, 'platform', 'win32'):
            settings = Settings(_env_file=None, legacy_tiktok_auto_python=Path('/existing/tiktok/python.exe'),
                legacy_video_auto_python=Path('/existing/video/python.exe'), quality_python=Path('/existing/comfy/python.exe'))
        self.assertEqual(settings.legacy_tiktok_auto_python, Path('/existing/tiktok/python.exe'))
        self.assertEqual(settings.legacy_video_auto_python, Path('/existing/video/python.exe'))
        self.assertEqual(settings.quality_python, Path('/existing/comfy/python.exe'))

    def test_posix_venv_default_retains_native_layout(self):
        with patch.object(runtime_paths.sys, 'platform', 'darwin'):
            self.assertEqual(runtime_paths.venv_python(Path('/existing')), Path('/existing/.venv/bin/python'))

    def test_adapters_use_native_paths_and_same_parent_comfy(self):
        root = Path('/fixture/video_auto')
        with patch.object(runtime_paths.sys, 'platform', 'win32'):
            for adapter in (LegacyCollectorAdapter(root, root), LegacyVideoAdapter(root), ComfyAssetAdapter(root)):
                self.assertEqual(adapter.python_path, root / '.venv/Scripts/python.exe')
            composition = LegacyComposeAdapter(root)
        self.assertEqual(composition.assets.python_path, root.parent / 'ComfyUI/.venv/Scripts/python.exe')

    def test_chrome_windows_candidates_use_existing_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'Google/Chrome/Application/chrome.exe'
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b'unit fixture, not an executable')
            with patch.object(runtime_paths.sys, 'platform', 'win32'), patch.dict(runtime_paths.os.environ, {'ProgramFiles': directory}):
                self.assertEqual(runtime_paths.chrome_executable(), executable)

    def test_mac_launcher_is_not_wrapped(self):
        class Legacy:
            pass
        with patch.object(browser_client.sys, 'platform', 'darwin'):
            self.assertIs(browser_client.native_browser_class(Legacy), Legacy)

    def windows_client(self, profile, endpoint):
        class Legacy:
            def __init__(self):
                self.config = {'profile_path': str(profile), 'cdp_endpoint': endpoint}
                self.logger = logging.getLogger('isolated-windows-launch-test')
        with patch.object(browser_client.sys, 'platform', 'win32'):
            return browser_client.native_browser_class(Legacy)()

    def test_windows_launch_reuses_profile_and_configured_loopback_port(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.windows_client(Path(directory), 'http://127.0.0.1:9433')
            with patch.object(browser_client, 'chrome_executable', return_value=Path(sys.executable)), \
                 patch.object(browser_client.subprocess, 'Popen', return_value=Mock()) as launch, \
                 patch.object(browser_client.socket, 'create_connection') as connection:
                client._start_debug_chrome()
            arguments = launch.call_args.args[0]
            self.assertIn('--remote-debugging-port=9433', arguments)
            self.assertIn('--remote-debugging-address=127.0.0.1', arguments)
            self.assertIn(f'--user-data-dir={Path(directory).resolve()}', arguments)
            self.assertFalse(launch.call_args.kwargs['shell'])
            connection.assert_called_once_with(('127.0.0.1', 9433), timeout=.5)

    def test_windows_launch_cannot_create_a_new_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / 'not-created'
            client = self.windows_client(missing, 'http://127.0.0.1:9433')
            with patch.object(browser_client.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(RuntimeError, 'EXISTING_CHROME_PROFILE_REQUIRED'):
                    client._start_debug_chrome()
                launch.assert_not_called()
            self.assertFalse(missing.exists())

    def test_windows_launch_rejects_remote_or_credential_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            for endpoint in ('http://example.com:9433', 'http://user:secret@127.0.0.1:9433', 'http://127.0.0.1:9433?token=secret'):
                client = self.windows_client(Path(directory), endpoint)
                with patch.object(browser_client.subprocess, 'Popen') as launch:
                    with self.assertRaisesRegex(ValueError, 'EXISTING_LOCAL_CDP_REQUIRED'):
                        client._start_debug_chrome()
                    launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
