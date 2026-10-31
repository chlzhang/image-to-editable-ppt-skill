import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]


class OfflineRunnerTest(unittest.TestCase):
    def test_runner_isolates_credentials_and_guards_python_children(self):
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / 'test_probe.py'
            probe.write_text('''import os,subprocess,sys,unittest
from pathlib import Path
class Probe(unittest.TestCase):
 def test_offline(self):
  for name in ['OPENAI_API_KEY','OPENAI_BASE_URL','PADDLE_OCR_TOKEN','CODEX_IMAGES_BASE_URL']:
   self.assertNotIn(name,os.environ)
  self.assertFalse(Path(os.environ['CODEX_AUTH_FILE']).exists())
  self.assertFalse((Path(os.environ['EDITPPT_CONFIG_HOME'])/'config.yaml').exists())
  child=subprocess.run([sys.executable,'-c',"import socket;socket.create_connection(('example.invalid',443))"],capture_output=True,text=True)
  self.assertNotEqual(0,child.returncode)
  self.assertIn('EDITPPT_OFFLINE_NETWORK_BLOCKED',child.stderr)
''', encoding='utf-8')
            env = dict(os.environ, OPENAI_API_KEY='synthetic-test-key', OPENAI_BASE_URL='https://example.invalid',
                       PADDLE_OCR_TOKEN='synthetic-test-token', CODEX_IMAGES_BASE_URL='https://example.invalid')
            result = subprocess.run([sys.executable, str(ROOT / 'tests/run_offline.py'),
                                     'discover', '-s', directory, '-v'], env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_ci_has_offline_windows_linux_matrix(self):
        workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text(encoding='utf-8'))
        job = workflow['jobs']['test']
        matrix = job.get('strategy', {}).get('matrix', {})
        self.assertEqual({'ubuntu-latest', 'windows-latest'}, set(matrix.get('os', [])))
        self.assertEqual({'3.10', '3.12'}, set(matrix.get('python-version', [])))
        self.assertTrue(any('tests/run_offline.py' in step.get('run', '') for step in job['steps']))
        hygiene = next(step for step in job['steps'] if step.get('name') == 'Check packaged skill hygiene')
        self.assertEqual('bash', hygiene.get('shell'))


if __name__ == '__main__':
    unittest.main()
