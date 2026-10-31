"""Run unit tests with isolated credentials and a subprocess-inherited network guard."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    base = root / 'output/offline-tests'
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='run-', dir=base) as directory:
        scratch = Path(directory)
        (scratch / 'tmp').mkdir()
        env = os.environ.copy()
        for key in ('OPENAI_API_KEY', 'OPENAI_BASE_URL', 'PADDLE_OCR_TOKEN',
                    'CODEX_IMAGES_BASE_URL', 'IMAGE_TO_EDITABLE_PPT_IMAGE_MODEL'):
            env.pop(key, None)
        env.update(EDITPPT_CONFIG_HOME=str(scratch / 'config'),
                   CODEX_AUTH_FILE=str(scratch / 'absent-auth.json'),
                   CODEX_PPT_HOME=str(scratch / 'absent-codex-ppt'),
                   PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                   TEMP=str(scratch / 'tmp'), TMP=str(scratch / 'tmp'))
        env['PYTHONPATH'] = os.pathsep.join(filter(None, [str(root / 'tests/support'),
            str(root / 'skills/image-to-editable-ppt/cli'), env.get('PYTHONPATH', '')]))
        arguments = sys.argv[1:] or ['discover', '-s', 'tests', '-v']
        return subprocess.run([sys.executable, '-m', 'unittest', *arguments], cwd=root, env=env).returncode


if __name__ == '__main__':
    raise SystemExit(main())
