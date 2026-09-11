"""Offline frozen-bundle check. No audio capture, API request or chat send."""
from pathlib import Path
import ctypes
import json
import subprocess
import sys


def run(output):
    result = {'ok': False, 'platform': sys.platform, 'frozen': bool(getattr(sys, 'frozen', False))}
    try:
        if sys.platform != 'win32' or not result['frozen']:
            raise RuntimeError('This check must run inside the built Windows executable.')
        import pyaudiowpatch  # noqa: F401
        import uiautomation  # noqa: F401
        from PySide6.QtWidgets import QApplication, QLineEdit
        from playwright.sync_api import sync_playwright  # noqa: F401
        from playwright._impl._driver import compute_driver_executable
        from .app import MainWindow  # noqa: F401: validate all UI/runtime imports
        from .windows_audio import _float32_to_pcm16
        from .windows_input import _QtClipboard, _WinApi
        from backend.normalizer import normalize_cued, has_cued_payload_boundary

        qt = QApplication.instance() or QApplication([])
        editor = QLineEdit()
        normalized = normalize_cued('扣两个零，八')
        if not normalized['accepted'] or normalized['value'] != '008':
            raise RuntimeError('Frozen numeric parser check failed.')
        live_prompt = normalize_cued('飘一个数字9')
        if not live_prompt['accepted'] or live_prompt['value'] != '9':
            raise RuntimeError('Frozen live prompt parser check failed.')
        editor.setText(normalized['value']); qt.processEvents()
        if editor.text() != '008':
            raise RuntimeError('Frozen Qt plugin check failed.')
        cued = normalize_cued('这件29，扣一个00')
        if not cued['accepted'] or cued['value'] != '00':
            raise RuntimeError('Frozen cue priority check failed.')
        product = normalize_cued('这件30扣一个2乘3')
        if not product['accepted'] or product['value'] != '6':
            raise RuntimeError('Frozen multiplication check failed.')
        for spoken, expected in (('29一件飘一个9全羊毛', '9'), ('扣2加3乘4全羊毛', '14'), ('飘9除3减1全羊毛', '2')):
            if normalize_cued(spoken)['value'] != expected or not has_cued_payload_boundary(spoken):
                raise RuntimeError('Frozen arithmetic/boundary check failed.')
        if any(normalize_cued(text)['accepted'] for text in ('00', '数字是00', '打00', '发00')):
            raise RuntimeError('Frozen mandatory cue check failed.')
        if ctypes.sizeof(_WinApi._Input) != 40:
            raise RuntimeError('Windows x64 keyboard INPUT layout is incorrect.')
        _WinApi()  # Resolve the bundled Win32 bindings without sending any key.
        editor.clear(); _QtClipboard().set_text(cued['value']); editor.paste(); qt.processEvents()
        if editor.text() != '00':
            raise RuntimeError('Frozen Qt clipboard paste check failed.')
        if len(_float32_to_pcm16(bytes(960 * 2 * 4), channels=2, sample_rate=48000)) != 640:
            raise RuntimeError('Frozen audio conversion check failed.')
        node, cli = compute_driver_executable()
        if not Path(node).is_file() or not Path(cli).is_file():
            raise RuntimeError('Bundled browser driver is missing.')
        completed = subprocess.run([node, cli, '--version'], capture_output=True, text=True,
                                   check=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        if '1.62.0' not in completed.stdout:
            raise RuntimeError('Bundled browser driver version check failed.')
        editor.close()
        result.update(ok=True, driver_version=completed.stdout.strip(), digits='008', live_prompt_digits='9',
                      cued_digits='00', multiplication_digits='6', cue_required=True,
                      narrative_digits='9', arithmetic_digits='14', division_digits='2', payload_boundary=True,
                      qt_clipboard_digits=editor.text(), keyboard_input_size=40)
    except Exception as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
    Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if result['ok'] else 1
