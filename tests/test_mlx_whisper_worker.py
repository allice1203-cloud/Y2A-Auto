import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from modules import mlx_whisper_worker


def test_worker_writes_private_result_without_printing_transcript():
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        audio_path = root / 'synthetic.wav'
        audio_path.write_bytes(b'RIFF')
        model_path = root / 'model'
        model_path.mkdir()
        output_path = root / 'result.json'
        calls = []

        def fake_transcribe(audio, **options):
            calls.append((audio, options))
            return {
                'text': '仅用于合成测试',
                'language': 'zh',
                'segments': [{
                    'start': 0.0,
                    'end': 1.0,
                    'text': '仅用于合成测试',
                    'words': [],
                }],
            }

        request = {
            'audio_path': str(audio_path),
            'model_path': str(model_path),
            'output_path': str(output_path),
            'language': 'zh',
            'prompt': '',
            'translate': False,
        }
        fake_module = SimpleNamespace(transcribe=fake_transcribe)
        with patch.dict(sys.modules, {'mlx_whisper': fake_module}), patch(
            'sys.stdin', io.StringIO(json.dumps(request))
        ):
            assert mlx_whisper_worker.main() == 0

        payload = json.loads(output_path.read_text(encoding='utf-8'))
        assert payload['text'] == '仅用于合成测试'
        assert calls[0][0] == str(audio_path.resolve())
        assert calls[0][1]['path_or_hf_repo'] == str(model_path.resolve())
        assert calls[0][1]['word_timestamps'] is True
        assert output_path.stat().st_mode & 0o777 == 0o600
