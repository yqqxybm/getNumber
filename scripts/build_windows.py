"""Build and smoke-check a single-file Windows x64 executable."""
from pathlib import Path
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def require_windows_builder():
    if sys.platform != 'win32' or platform.machine().lower() not in ('amd64', 'x86_64'):
        raise RuntimeError('Build requires Windows x64; this is not a cross-compiler.')
    if sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
        raise RuntimeError('Build requires 64-bit Python 3.12.')


def run(*args, timeout=None, env=None):
    subprocess.run(args, cwd=ROOT, check=True, timeout=timeout, env=env)


def main():
    require_windows_builder()
    python = sys.executable
    run(python, '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt'), 'pyinstaller==6.22.2')
    run(python, '-m', 'unittest', 'discover', '-s', 'tests', '-v')
    run(python, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onefile',
        '--name', 'Tingma', '--paths', str(ROOT),
        '--collect-all', 'pyaudiowpatch', '--collect-all', 'uiautomation',
        '--collect-all', 'playwright', str(ROOT / 'scripts' / 'windows_entry.py'))
    executable = ROOT / 'dist' / 'Tingma.exe'
    with executable.open('rb') as stream:
        if stream.read(2) != b'MZ':
            raise RuntimeError('Build output is not a Windows executable.')
    with tempfile.TemporaryDirectory(prefix='tingma-build-check-') as temp:
        result_file = Path(temp) / 'result.json'
        # Check the frozen bundle using its own Qt plugin and driver. A parent
        # Python/Qt installation must not make an incomplete bundle appear sound.
        env = {key: value for key, value in os.environ.items()
               if key not in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH',
                              'QT_QPA_PLATFORM_PLUGIN_PATH', 'PLAYWRIGHT_NODEJS_PATH')}
        env['QT_QPA_PLATFORM'] = 'offscreen'
        run(str(executable), '--build-smoke', str(result_file), timeout=120, env=env)
        result = json.loads(result_file.read_text(encoding='utf-8'))
        if not result.get('ok') or result.get('platform') != 'win32' or result.get('frozen') is not True:
            raise RuntimeError('Frozen executable smoke check did not pass.')
    release = ROOT / 'dist' / 'windows-release'
    if release.exists():
        shutil.rmtree(release)
    release.mkdir()
    shutil.copy2(executable, release / 'Tingma.exe')
    shutil.copy2(ROOT / 'README.md', release / '使用说明.md')
    (release / 'build-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with executable.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    (release / 'SHA256.txt').write_text(digest + '  Tingma.exe\n', encoding='utf-8')
    print('Built and smoke-checked: ' + str(executable), flush=True)
    print('SHA256: ' + digest, flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Build failed: ' + str(error), file=sys.stderr)
        raise SystemExit(1)
