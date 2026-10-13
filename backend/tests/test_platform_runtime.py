import errno
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.integrations import file_lock, process_runner
from app.integrations import process_probe


class PlatformRuntimeTests(unittest.TestCase):
    def test_native_lease_excludes_a_second_process_and_releases_on_close(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'native.lock'
            program = (
                "from app.integrations.file_lock import lock_file;import sys;"
                "f=open(sys.argv[1],'a');lock_file(f,blocking=False)"
            )
            with path.open('a') as handle:
                file_lock.lock_file(handle, blocking=False)
                denied = subprocess.run([sys.executable, '-c', program, str(path)], capture_output=True)
                self.assertNotEqual(denied.returncode, 0)
                self.assertIn(b'BlockingIOError', denied.stderr)
            granted = subprocess.run([sys.executable, '-c', program, str(path)], capture_output=True)
            self.assertEqual(granted.returncode, 0, granted.stderr)

    def test_windows_lease_locks_byte_zero_and_maps_busy(self):
        native = Mock(LK_NBLCK=2)
        native.locking.side_effect = PermissionError(errno.EACCES, 'held')
        platform = types.SimpleNamespace(name='nt', fstat=os.fstat)
        with tempfile.TemporaryDirectory() as directory, (Path(directory) / 'mock.lock').open('a') as handle:
            with patch.object(file_lock, 'os', platform), patch.object(file_lock, 'msvcrt', native, create=True):
                with self.assertRaises(BlockingIOError):
                    file_lock.lock_file(handle, blocking=False)
                native.locking.assert_called_once_with(handle.fileno(), 2, 1)
            self.assertEqual(handle.tell(), 0)

    def test_windows_blocking_lease_retries_only_lock_contention(self):
        native = Mock(LK_NBLCK=2)
        native.locking.side_effect = [PermissionError(errno.EACCES, 'held'), None]
        platform = types.SimpleNamespace(name='nt', fstat=os.fstat)
        with tempfile.TemporaryDirectory() as directory, (Path(directory) / 'mock.lock').open('a') as handle:
            with patch.object(file_lock, 'os', platform), patch.object(file_lock, 'msvcrt', native, create=True), patch.object(file_lock.time, 'sleep') as wait:
                file_lock.lock_file(handle)
                self.assertEqual(native.locking.call_count, 2)
                wait.assert_called_once_with(.05)

    def test_windows_runner_keeps_system_variables_and_terminates_only_its_child(self):
        class Child:
            pid = 1234567
            returncode = None
            stdout = io.StringIO('isolated mock child\n')
            stderr = io.StringIO('')

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 1

            def kill(self):
                self.returncode = 1

            def wait(self):
                return self.returncode

        child = Child()
        child.terminate = Mock(side_effect=child.terminate)
        child.kill = Mock(side_effect=child.kill)
        platform = types.SimpleNamespace(name='nt', environ={
            'SystemRoot': r'C:\Windows', 'TEMP': r'C:\Temp', 'PATH': 'isolated-path', 'UNRELATED_SECRET': 'not-forwarded'})
        with tempfile.TemporaryDirectory() as directory, patch.object(process_runner, 'os', platform), patch.object(process_runner.subprocess, 'Popen', return_value=child) as start:
            root = Path(directory)
            result = process_runner.ProcessRunner().run(['python.exe', '-c', 'pass'], cwd=root, log_dir=root, timeout_s=0)
        child.terminate.assert_called_once()
        child.kill.assert_not_called()
        self.assertTrue(result.timed_out)
        self.assertEqual(result.resource_info['scope'], 'not_available')
        self.assertFalse(start.call_args.kwargs['start_new_session'])
        environment = start.call_args.kwargs['env']
        self.assertEqual(environment['SystemRoot'], r'C:\Windows')
        self.assertNotIn('UNRELATED_SECRET', environment)

    def test_real_native_process_captures_exit_and_resource_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = process_runner.ProcessRunner().run([sys.executable, '-c', "print('native-ok')"], cwd=root, log_dir=root)
            self.assertEqual(result.exit_code, 0)
            self.assertIn('native-ok', result.stdout)
            expected = 'not_available' if os.name == 'nt' else 'cumulative_child_processes'
            self.assertEqual(result.resource_info['scope'], expected)

    def test_native_process_probe_is_readonly_and_preserves_lookup_errors(self):
        process_probe.probe_process(os.getpid())
        platform = types.SimpleNamespace(name='posix', kill=Mock(side_effect=ProcessLookupError(errno.ESRCH, 'gone')))
        with patch.object(process_probe, 'os', platform):
            with self.assertRaises(ProcessLookupError):
                process_probe.probe_process(1234567)
        platform.kill.assert_called_once_with(1234567, 0)

    def windows_probe(self, *, opened=True, exited=False, error=0):
        kernel = Mock()
        kernel.OpenProcess.return_value = 42 if opened else 0
        def read_exit(_handle, pointer):
            pointer._obj.value = 0 if exited else 259
            return True
        kernel.GetExitCodeProcess.side_effect = read_exit
        platform = types.SimpleNamespace(name='nt')
        with patch.object(process_probe, 'os', platform), patch.object(process_probe.ctypes, 'WinDLL', return_value=kernel, create=True), patch.object(process_probe.ctypes, 'get_last_error', return_value=error, create=True):
            process_probe.probe_process(1234567)
        return kernel

    def test_windows_probe_does_not_send_a_signal_and_closes_handle(self):
        kernel = self.windows_probe()
        kernel.OpenProcess.assert_called_once_with(0x1000, False, 1234567)
        kernel.CloseHandle.assert_called_once_with(42)
        kernel.TerminateProcess.assert_not_called()

    def test_windows_probe_preserves_missing_and_permission_errors(self):
        with self.assertRaises(ProcessLookupError):
            self.windows_probe(opened=False, error=87)
        with self.assertRaises(PermissionError):
            self.windows_probe(opened=False, error=5)
        with self.assertRaises(ProcessLookupError):
            self.windows_probe(exited=True)

    def test_windows_delete_writer_probe_fails_closed(self):
        from app.integrations import asset_registry
        with patch.object(asset_registry, 'os', types.SimpleNamespace(name='nt')), patch.object(asset_registry.shutil, 'which') as tool:
            with self.assertRaisesRegex(ValueError, 'WINDOWS_WRITER_PROBE_NOT_AVAILABLE'):
                asset_registry.AssetRegistry._file_writers([Path('disposable-test.mp4')])
        tool.assert_not_called()


if __name__ == '__main__':
    unittest.main()
