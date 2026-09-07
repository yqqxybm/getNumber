"""Non-secret preferences only. Keys live in memory for the current app session."""
from dataclasses import dataclass, asdict, fields
import json
import os
from pathlib import Path

PRESETS = {
    'dashscope_streaming': ('千问 Audio 3.0 · 实时语音', 'wss://dashscope.aliyuncs.com/api-ws/v1/inference', 'qwen-audio-3.0-asr-flash-streaming'),
    'qwen_realtime': ('百炼 · 实时语音', 'wss://dashscope.aliyuncs.com/api-ws/v1/realtime', 'qwen3-asr-flash-realtime'),
    'qwen_http': ('百炼 · 短音频识别', 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions', 'qwen3-asr-flash'),
    'openai_audio': ('自定义 · 标准音频转写', 'https://你的服务地址/v1/audio/transcriptions', ''),
}


@dataclass
class Settings:
    protocol: str = 'dashscope_streaming'
    endpoint: str = PRESETS['dashscope_streaming'][1]
    model: str = PRESETS['dashscope_streaming'][2]
    min_length: int = 1
    max_length: int = 16
    preview_only: bool = False


def config_path():
    root = Path(os.environ.get('APPDATA', Path.home() / '.config'))
    return root / 'Tingma' / 'settings.json'


def load_settings(path=None):
    try:
        data = json.loads((path or config_path()).read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            return Settings()
        allowed = {field.name for field in fields(Settings)}
        data = {key: value for key, value in data.items() if key in allowed}
        if data.get('protocol', 'dashscope_streaming') not in PRESETS:
            return Settings()
        # Legacy mode fields are ignored by the field allowlist above. Numeric
        # codes are the only supported output, without resetting API settings.
        for key in ('min_length', 'max_length'):
            if key in data and (type(data[key]) is not int or not 1 <= data[key] <= 32):
                return Settings()
        if data.get('min_length', 1) > data.get('max_length', 16):
            return Settings()
        for key in ('endpoint', 'model'):
            if key in data and (not isinstance(data[key], str) or len(data[key]) > 2048):
                return Settings()
        if 'preview_only' in data and type(data['preview_only']) is not bool:
            return Settings()
        return Settings(**data)
    except (OSError, ValueError, TypeError):
        return Settings()


def save_settings(settings, path=None):
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)
