from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'skills/image-to-editable-ppt/cli/editppt/runtime'
sys.path.insert(0, str(RUNTIME))
import deck_run_state as state
import _input_normalization as normalization

WORKER = '''
import sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import record_page_dispatch as module
root=Path(sys.argv[2]); page=sys.argv[3]; label=sys.argv[4]
original=module.load_jobs
def load(run):
    data=original(run)
    (root/(label+'.read')).touch()
    if label=='first':
        while not (root/'release').exists(): time.sleep(0.01)
    return data
module.load_jobs=load
sys.argv=['dispatch',str(root),'--page',page,'--agent-id',label,'--prompt-file',
          str(root/'pages'/('page_'+page.zfill(3))/'prompt.md')]
module.main()
'''


class RunStateRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wait_file(self, path, timeout=5):
        end = time.monotonic() + timeout
        while not path.exists() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(path.exists(), path)

    def jobs(self):
        pages = []
        for i in (1, 2):
            directory = self.root / f'pages/page_{i:03d}'
            directory.mkdir(parents=True)
            (directory / 'prompt.md').write_text('fixture', encoding='utf-8')
            state.write_json(directory / 'page_request.json', {})
            pages.append({'page_id': f'page_{i:03d}', 'status': 'pending',
                          'page_dir': f'pages/page_{i:03d}',
                          'page_request': f'pages/page_{i:03d}/page_request.json'})
        state.write_json(self.root / 'page_jobs.json', {'pages': pages})
        return pages

    def race(self, second_page):
        self.jobs()
        processes = []
        try:
            first = subprocess.Popen([sys.executable, '-c', WORKER, str(RUNTIME), str(self.root), '1', 'first'],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(first)
            self.wait_file(self.root / 'first.read')
            second = subprocess.Popen([sys.executable, '-c', WORKER, str(RUNTIME), str(self.root), second_page, 'second'],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(second)
            # Old code can read the same snapshot; locked code waits before loading.
            end = time.monotonic() + 1
            while not (self.root / 'second.read').exists() and time.monotonic() < end:
                time.sleep(0.01)
            (self.root / 'release').touch()
            outputs = [process.communicate(timeout=15) for process in processes]
            return [process.returncode for process in processes], outputs
        finally:
            (self.root / 'release').touch()
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_concurrent_updates_to_different_pages_are_preserved(self):
        codes, output = self.race('2')
        self.assertEqual([0, 0], codes, output)
        self.assertEqual(['dispatched', 'dispatched'],
                         [p['status'] for p in state.load_jobs(self.root)['pages']])

    def test_same_page_can_only_be_claimed_once(self):
        codes, output = self.race('1')
        self.assertEqual(1, codes.count(0), output)

    def test_replace_failure_preserves_old_json_and_cleans_temp(self):
        path = self.root / 'state.json'
        state.write_json(path, {'value': 'old'})
        with mock.patch('os.replace', side_effect=OSError('injected before replace')):
            with self.assertRaises(OSError):
                state.write_json(path, {'value': 'new'})
        self.assertEqual({'value': 'old'}, state.read_json(path))
        self.assertEqual(['state.json'], sorted(p.name for p in self.root.iterdir()))

    def test_serialization_failure_preserves_old_json(self):
        path = self.root / 'state.json'
        state.write_json(path, {'value': 'old'})
        with self.assertRaises(TypeError):
            state.write_json(path, {'value': object()})
        self.assertEqual({'value': 'old'}, state.read_json(path))

    def test_flush_failure_preserves_old_json(self):
        path = self.root / 'state.json'
        state.write_json(path, {'value': 'old'})
        with mock.patch('os.fsync', side_effect=OSError('injected flush failure')):
            with self.assertRaises(OSError):
                state.write_json(path, {'value': 'new'})
        self.assertEqual({'value': 'old'}, state.read_json(path))
        self.assertEqual(['state.json'], sorted(p.name for p in self.root.iterdir()))

    def test_all_run_mutations_wait_before_loading_state(self):
        self.assertTrue(callable(getattr(state, 'run_lock', None)))
        for name in ('record_page_dispatch', 'record_page_result', 'reset_page_job',
                     'configure_image_backend', 'finalize_deck_run'):
            with self.subTest(command=name):
                ready = self.root / (name + '.ready')
                args = [str(self.root)]
                if name in ('record_page_dispatch', 'record_page_result', 'reset_page_job'):
                    args += ['--page', '1', '--agent-id', 'worker']
                if name == 'record_page_dispatch':
                    args += ['--prompt-file', str(self.root / 'prompt.md')]
                script = ('import runpy,sys;from pathlib import Path;'
                          'sys.path.insert(0,sys.argv[1]);target=sys.argv[2];'
                          'ready=sys.argv[3];sys.argv=[target,*sys.argv[4:]];'
                          'Path(ready).touch();runpy.run_module(target,run_name="__main__")')
                with state.run_lock(self.root):
                    child = subprocess.Popen([sys.executable, '-c', script, str(RUNTIME), name, str(ready), *args],
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    try:
                        self.wait_file(ready)
                        with self.assertRaises(subprocess.TimeoutExpired):
                            child.communicate(timeout=0.4)
                    finally:
                        if child.poll() is None:
                            child.kill()
                        child.communicate()

    def test_lock_timeout_and_process_exit_release(self):
        self.assertTrue(callable(getattr(state, 'run_lock', None)))
        script = '''import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from deck_run_state import run_lock
with run_lock(Path(sys.argv[2])):
 Path(sys.argv[2],"locked").touch()
 time.sleep(30)
'''
        child = subprocess.Popen([sys.executable, '-c', script, str(RUNTIME), str(self.root)])
        try:
            self.wait_file(self.root / 'locked')
            with self.assertRaisesRegex(TimeoutError, 'lock'):
                with state.run_lock(self.root, timeout=0.1):
                    self.fail('contended lock was acquired')
        finally:
            child.kill()
            child.wait(timeout=5)
        with state.run_lock(self.root, timeout=0.5):
            pass

    def test_run_status_rolls_back_after_reset(self):
        jobs = {'run_status': 'complete', 'pages': [{'status': 'pending'}, {'status': 'recorded'}]}
        state.update_jobs_run_status(jobs)
        self.assertEqual('inputs_prepared', jobs['run_status'])

    def test_next_mutation_repairs_stale_run_state_from_committed_jobs(self):
        pages = self.jobs()
        for page in pages:
            page['status'] = 'dispatched'
        state.write_json(self.root / 'page_jobs.json', {'pages': pages, 'run_status': 'pages_dispatched'})
        state.write_json(self.root / 'run_state.json', {'status': 'complete', 'history': []})
        state.write_json(self.root / 'deck_manifest.json', {'pages': pages})
        result = subprocess.run([sys.executable, str(RUNTIME / 'configure_image_backend.py'), str(self.root)],
                                capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('pages_dispatched', state.load_run_state(self.root)['status'])

    def test_finalize_retries_after_summary_write_failure(self):
        from test_finalize_recorded_inputs import FinalizeRecordedInputsTest
        import finalize_deck_run as finalize
        fixture = FinalizeRecordedInputsTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original = finalize.write_json
        def fail_summary(path, data):
            if Path(path).name == 'run_summary.json':
                raise OSError('injected summary failure')
            return original(path, data)
        with mock.patch.object(sys, 'argv', ['finalize', str(fixture.run)]), \
             mock.patch.object(finalize, 'write_json', fail_summary):
            with self.assertRaisesRegex(OSError, 'summary failure'):
                finalize.main()
        self.assertTrue(all(p['status'] == 'accepted' for p in state.load_jobs(fixture.run)['pages']))
        self.assertNotEqual('complete', state.load_run_state(fixture.run)['status'])
        fixture.ok(fixture.finalize())
        self.assertEqual('complete', state.read_json(fixture.run / 'final/run_summary.json')['status'])

    def test_failed_repeat_finalize_is_not_recovered_as_complete(self):
        from test_finalize_recorded_inputs import FinalizeRecordedInputsTest
        import finalize_deck_run as finalize
        fixture = FinalizeRecordedInputsTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.ok(fixture.finalize())
        original = finalize.run
        def fail_validation(command):
            if any(Path(str(part)).name == 'validate_pptx.py' for part in command):
                state.write_json(fixture.run / 'final/validation.json', {'passed': False})
                raise subprocess.CalledProcessError(1, command)
            return original(command)
        with mock.patch.object(sys, 'argv', ['finalize', str(fixture.run)]), \
             mock.patch.object(finalize, 'run', fail_validation):
            with self.assertRaises(subprocess.CalledProcessError):
                finalize.main()
        self.assertEqual('deck_built', state.load_run_state(fixture.run)['status'])
        fixture.ok(fixture.cli('run', 'backend', fixture.run))
        self.assertEqual('deck_built', state.load_run_state(fixture.run)['status'])
        self.assertFalse(state.read_json(fixture.run / 'final/validation.json')['passed'])

    def test_prepare_cannot_overwrite_any_existing_run_state(self):
        source = self.root / 'input.png'
        Image.new('RGB', (80, 45), 'white').save(source)
        for status in ('pending', 'dispatched', 'recorded', 'accepted'):
            with self.subTest(status=status):
                run = self.root / status
                normalization.normalize_inputs([source], job_dir=run)
                state.write_json(run / 'page_jobs.json', {'pages': [{'status': status}]})
                before = {p.relative_to(run): p.read_bytes() for p in run.rglob('*') if p.is_file()}
                Image.new('RGB', (80, 45), 'red').save(source)
                with self.assertRaises((ValueError, SystemExit, FileExistsError)):
                    normalization.normalize_inputs([source], job_dir=run)
                self.assertEqual(before, {p.relative_to(run): p.read_bytes() for p in run.rglob('*') if p.is_file()})

    def test_default_run_names_do_not_collide(self):
        source = self.root / 'input.png'
        Image.new('RGB', (80, 45)).save(source)
        candidate = self.root / 'same-second'
        with mock.patch.object(normalization, 'default_job_dir', return_value=candidate):
            first = normalization.normalize_inputs([source], out_root=self.root)
            second = normalization.normalize_inputs([source], out_root=self.root)
        self.assertNotEqual(first.parent, second.parent)


if __name__ == '__main__':
    unittest.main()
