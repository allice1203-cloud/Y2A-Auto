import unittest
from pathlib import Path
from types import SimpleNamespace
import tempfile
from unittest.mock import Mock, patch

from modules.speech_recognition import SpeechRecognizer, create_speech_recognizer_from_config


class SpeechRecognitionConfigTests(unittest.TestCase):
    def test_mlx_whisper_failure_does_not_reload_model_for_chunk_fallback(self):
        recognizer = SpeechRecognizer.__new__(SpeechRecognizer)
        recognizer.config = SimpleNamespace(provider='mlx_whisper')
        recognizer._asr = SimpleNamespace(client=True)
        recognizer.last_warning_message = ''
        recognizer.last_error_message = ''
        recognizer._extract_audio_wav = Mock(return_value='synthetic.wav')
        recognizer._probe_media_duration = Mock(return_value=60.0)
        recognizer._fallback_whole_audio = Mock(return_value=[])
        recognizer._fallback_transcription = Mock(return_value=[])
        recognizer._cleanup_temp_files = Mock()

        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / 'synthetic.mp4'
            video_path.write_bytes(b'synthetic')
            result = recognizer.transcribe_video_to_subtitles(
                str(video_path),
                str(Path(temp_dir) / 'synthetic.srt'),
            )

        self.assertIsNone(result)
        recognizer._fallback_whole_audio.assert_called_once_with('synthetic.wav', 60.0)
        recognizer._fallback_transcription.assert_not_called()
        recognizer._cleanup_temp_files.assert_called_once()

    @patch('modules.asr_api_client.AsrApiClient._init_client')
    def test_mlx_whisper_is_local_and_forces_single_worker(self, _init_client):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'mlx_whisper',
            'WHISPER_MODEL_NAME': '/tmp/local-whisper-model',
            'WHISPER_MAX_WORKERS': 8,
        }, task_id='unit-test-mlx-whisper')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.provider, 'mlx_whisper')
        self.assertEqual(recognizer.config.api_provider, 'mlx_whisper')
        self.assertEqual(recognizer.config.api_key, '')
        self.assertEqual(recognizer.config.base_url, '')
        self.assertEqual(recognizer.config.max_workers, 1)

    def test_whisper_config_maps_timestamp_granularities(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'whisper',
            'WHISPER_TIMESTAMP_GRANULARITIES': 'word',
        }, task_id='unit-test-whisper')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.provider, 'whisper')
        self.assertEqual(recognizer.config.api_provider, 'whisper')
        self.assertEqual(recognizer.config.whisper_timestamp_granularities, 'word')

    def test_voxtral_config_keeps_voxtral_timestamp_granularities(self):
        recognizer = create_speech_recognizer_from_config({
            'SPEECH_RECOGNITION_ENABLED': True,
            'SPEECH_RECOGNITION_PROVIDER': 'voxtral',
            'VOXTRAL_TIMESTAMP_GRANULARITIES': 'segment,word',
            'VOXTRAL_BASE_URL': 'https://api.mistral.ai/v1',
        }, task_id='unit-test-voxtral')

        self.assertIsNotNone(recognizer)
        self.assertEqual(recognizer.config.provider, 'voxtral')
        self.assertEqual(recognizer.config.api_provider, 'voxtral')
        self.assertEqual(recognizer.config.voxtral_timestamp_granularities, 'segment,word')


if __name__ == '__main__':
    unittest.main()
