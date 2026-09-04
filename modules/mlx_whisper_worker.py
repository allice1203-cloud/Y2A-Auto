#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Isolated MLX Whisper worker.

The parent process sends one JSON request on stdin. The worker writes the
transcription to a private result file and exits, so all MLX model allocations
are returned to macOS after each ASR job.
"""

import json
import os
from pathlib import Path
import sys
from typing import Any


def _json_default(value: Any):
    if hasattr(value, 'item'):
        return value.item()
    if hasattr(value, 'tolist'):
        return value.tolist()
    raise TypeError(f"Unsupported result value: {type(value).__name__}")


def _validated_existing_file(value: Any, label: str) -> Path:
    path = Path(str(value or '')).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"{label} is not an existing file")
    return path


def _validated_existing_dir(value: Any, label: str) -> Path:
    path = Path(str(value or '')).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"{label} is not an existing directory")
    return path


def main() -> int:
    request = json.load(sys.stdin)
    if not isinstance(request, dict):
        raise ValueError("worker request must be a JSON object")

    audio_path = _validated_existing_file(request.get('audio_path'), 'audio_path')
    model_path = _validated_existing_dir(request.get('model_path'), 'model_path')
    output_path = Path(str(request.get('output_path') or '')).expanduser().resolve()
    if not output_path.parent.is_dir():
        raise ValueError("output_path parent is not an existing directory")

    import mlx_whisper

    options = {
        'path_or_hf_repo': str(model_path),
        'verbose': None,
        'word_timestamps': True,
        'task': 'translate' if bool(request.get('translate')) else 'transcribe',
    }
    language = str(request.get('language') or '').strip()
    if language:
        options['language'] = language
    prompt = str(request.get('prompt') or '').strip()
    if prompt:
        options['initial_prompt'] = prompt

    result = mlx_whisper.transcribe(str(audio_path), **options)
    temporary_path = output_path.with_suffix('.json.tmp')
    with open(temporary_path, 'w', encoding='utf-8') as file_obj:
        json.dump(result, file_obj, ensure_ascii=False, default=_json_default)
        file_obj.flush()
        os.fsync(file_obj.fileno())
    os.chmod(temporary_path, 0o600)
    os.replace(temporary_path, output_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
