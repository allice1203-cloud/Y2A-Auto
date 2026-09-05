import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from modules.asr_api_client import (
    AsrApiClient,
    AsrConfig,
    AsrFormatIncompatibleError,
    AsrHttpError,
)
from modules.subtitle_pipeline_types import DetectedSpeechWindow


class AsrApiClientTests(unittest.TestCase):
    def test_mlx_whisper_uses_isolated_worker_and_preserves_word_timestamps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            model_dir = root / 'model'
            model_dir.mkdir()
            audio_path = root / 'clip.wav'
            audio_path.write_bytes(b'RIFF')
            client = AsrApiClient(AsrConfig(
                provider='mlx_whisper',
                model_name=str(model_dir),
                max_retries=1,
            ))
            client.client = True
            window = DetectedSpeechWindow(
                start_s=0.0,
                end_s=2.0,
                ownership_start_s=0.0,
                ownership_end_s=2.0,
            )

            def fake_run(_argv, **kwargs):
                request = json.loads(kwargs['input'])
                Path(request['output_path']).write_text(json.dumps({
                    'text': '测试语音',
                    'language': 'zh',
                    'segments': [{
                        'start': 0.0,
                        'end': 2.0,
                        'text': '测试语音',
                        'words': [
                            {'word': '测试', 'start': 0.1, 'end': 0.8},
                            {'word': '语音', 'start': 0.9, 'end': 1.8},
                        ],
                    }],
                }), encoding='utf-8')
                return SimpleNamespace(returncode=0, stderr='')

            with patch('modules.asr_api_client.subprocess.run', side_effect=fake_run) as run:
                result = client.transcribe_window(str(audio_path), window=window)

            self.assertTrue(result.ok)
            self.assertEqual(result.provider, 'mlx_whisper')
            self.assertEqual(result.timestamp_mode, 'word')
            self.assertEqual([word.text for word in result.segments[0].words], ['测试', '语音'])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0][1:3], ['-m', 'modules.mlx_whisper_worker'])
            self.assertEqual(run.call_args.kwargs['stdout'], -3)

    def test_mlx_whisper_does_not_probe_remote_capabilities(self):
        client = AsrApiClient(AsrConfig(provider='mlx_whisper', model_name='/missing'))
        self.assertFalse(client._needs_serial_format_probe())

    def test_mlx_whisper_keeps_native_timestamps_before_silent_tail(self):
        client = AsrApiClient(AsrConfig(provider='mlx_whisper', model_name='/missing'))
        window = DetectedSpeechWindow(
            start_s=0.0,
            end_s=100.0,
            ownership_start_s=0.0,
            ownership_end_s=100.0,
        )

        result = client._payload_to_transcription_result(
            {
                'text': 'hello',
                'segments': [{
                    'start': 1.0,
                    'end': 10.0,
                    'text': 'hello',
                    'words': [{'word': 'hello', 'start': 1.0, 'end': 2.0}],
                }],
            },
            provider='mlx_whisper',
            response_format='verbose_json',
            timestamp_mode='word',
            window=window,
            granularities=('segment', 'word'),
        )

        self.assertEqual(result.segments[0].start_s, 1.0)
        self.assertEqual(result.segments[0].end_s, 10.0)
        self.assertEqual(result.segments[0].words[0].start_s, 1.0)

    def test_format_error_requires_parameter_rejection(self):
        plain_error = RuntimeError('request includes response_format')
        format_error = RuntimeError('invalid response_format: verbose_json')

        self.assertFalse(AsrApiClient._is_format_error(plain_error))
        self.assertTrue(AsrApiClient._is_format_error(format_error))

    def test_non_validation_http_error_is_not_format_error(self):
        error = RuntimeError('invalid response_format')
        error.status_code = 401

        self.assertFalse(AsrApiClient._is_format_error(error))

    def test_language_names_are_normalized_to_iso_codes(self):
        self.assertEqual(AsrApiClient._normalize_language_code('english'), 'en')
        self.assertEqual(AsrApiClient._normalize_language_code('Chinese'), 'zh')
        self.assertEqual(AsrApiClient._normalize_language_code('en-US'), 'en')
        self.assertEqual(AsrApiClient._normalize_language_code('unknown'), '')
        self.assertEqual(AsrApiClient._normalize_language_code('not-a-language'), '')

    def test_set_language_hint_normalizes_detected_language_name(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        client.set_language_hint('english')
        self.assertEqual(client._language_hint, 'en')

    def test_raw_http_errors_preserve_status_for_format_classification(self):
        for status_code in (401, 429, 500):
            with self.subTest(status_code=status_code):
                error = AsrHttpError(status_code, 'invalid response_format: verbose_json')
                self.assertEqual(error.status_code, status_code)
                self.assertFalse(AsrApiClient._is_format_error(error))

        validation_error = AsrHttpError(400, 'invalid response_format: verbose_json')
        self.assertTrue(AsrApiClient._is_format_error(validation_error))

    def test_probe_accepts_empty_verbose_json_as_supported_format(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        client._request_whisper_raw_json = Mock(return_value={
            'text': '',
            'segments': [],
            'language': 'en',
            'duration': 1.0,
        })
        client._request_whisper_response = Mock()

        probe = client._probe_capabilities('clip.wav', 'whisper-1', window=None)

        self.assertEqual(probe.transcription_format, 'verbose_json')
        self.assertEqual(probe.transcription_granularities, ('segment', 'word'))
        self.assertIsNotNone(probe.transcription_result)
        self.assertFalse(probe.transcription_result.ok)
        client._request_whisper_response.assert_not_called()

    def test_probe_falls_back_after_explicit_format_rejection(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        format_error = RuntimeError('invalid timestamp_granularities')
        client._request_whisper_raw_json = Mock(side_effect=[
            format_error,
            {'text': '', 'segments': [], 'duration': 1.0},
        ])
        client._request_whisper_response = Mock(side_effect=[format_error])

        probe = client._probe_capabilities('clip.wav', 'whisper-1', window=None)

        self.assertEqual(probe.transcription_format, 'verbose_json')
        self.assertEqual(probe.transcription_granularities, ('segment',))

    def test_probe_does_not_treat_error_payload_as_empty_transcription(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        client._request_whisper_raw_json = Mock(return_value={
            'error': {'message': 'model unavailable'},
        })

        with self.assertRaises(RuntimeError) as context:
            client._probe_capabilities('clip.wav', 'whisper-1', window=None)

        self.assertNotIsInstance(context.exception, AsrFormatIncompatibleError)
        self.assertIn('错误对象', str(context.exception))

    def test_whisper_granularity_candidates_prefer_word_then_fallback(self):
        client = AsrApiClient(AsrConfig(api_key='', timestamp_granularities='segment,word'))

        self.assertEqual(
            client._whisper_granularity_candidates(),
            [('segment', 'word'), ('segment',), tuple()],
        )

    def test_whisper_granularity_candidates_accept_word_only_config(self):
        client = AsrApiClient(AsrConfig(api_key='', timestamp_granularities='word'))

        self.assertEqual(
            client._whisper_granularity_candidates(),
            [('segment', 'word'), ('segment',), tuple()],
        )

    def test_payload_to_transcription_result_uses_top_level_word_timings(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        window = DetectedSpeechWindow(start_s=10.0, end_s=12.0, ownership_start_s=10.0, ownership_end_s=12.0)
        payload = {
            'text': 'hello world',
            'language': 'en',
            'segments': [{'start': 0.0, 'end': 2.0, 'text': 'hello world'}],
            'words': [
                {'word': 'hello', 'start': 0.1, 'end': 0.7},
                {'word': 'world', 'start': 0.8, 'end': 1.5},
            ],
        }

        result = client._payload_to_transcription_result(
            payload,
            provider='whisper',
            response_format='verbose_json',
            timestamp_mode='segment',
            window=window,
            granularities=('segment', 'word'),
        )

        self.assertEqual(result.timestamp_mode, 'word')
        self.assertEqual(result.language, 'en')
        self.assertEqual(len(result.segments), 1)
        self.assertEqual([word.text for word in result.segments[0].words], ['hello', 'world'])

    def test_detect_language_from_segments_uses_three_point_majority(self):
        client = AsrApiClient(AsrConfig(api_key=''))
        detected_by_clip = {
            '0.0-1.0': 'en',
            '2.0-3.0': 'zh',
            '4.0-5.0': 'zh',
        }

        def extract_clip(_audio_wav, start_s, end_s):
            return f'{start_s:.1f}-{end_s:.1f}'

        client.detect_language = lambda clip: detected_by_clip.get(clip, '')

        language = client.detect_language_from_segments(
            'audio.wav',
            [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0), (3.0, 4.0), (4.0, 5.0)],
            extract_clip,
        )

        self.assertEqual(language, 'zh')

    def test_extract_words_skips_token_only_format(self):
        """Backends like parakeet-crispasr return tokens with only text (no timing).
        _extract_words correctly skips them; the segment loop handles interpolation."""
        client = AsrApiClient(AsrConfig(api_key=''))
        raw_tokens = [
            {'token': 'hello'},
            {'token': 'world'},
            {'token': 'here'},
        ]
        words = client._extract_words(raw_tokens)
        self.assertEqual(words, [])

    def test_token_with_start_time_end_time_is_accepted(self):
        """Tokens using start_time/end_time naming are now recognized."""
        client = AsrApiClient(AsrConfig(api_key=''))
        raw_words = [
            {'token': 'hello', 'start_time': 0.1, 'end_time': 0.7},
            {'token': 'world', 'start_time': 0.8, 'end_time': 1.5},
        ]
        words = client._extract_words(raw_words)
        self.assertEqual(len(words), 2)
        self.assertEqual([w.text for w in words], ['hello', 'world'])
        self.assertAlmostEqual(words[0].start_s, 0.1)
        self.assertAlmostEqual(words[0].end_s, 0.7)

    def test_payload_with_token_only_segments_produces_result(self):
        """parakeet-style payload with token dicts but no timing produces
        segment-level result (no word timestamps)."""
        client = AsrApiClient(AsrConfig(api_key=''))
        window = DetectedSpeechWindow(start_s=0.0, end_s=2.0, ownership_start_s=0.0, ownership_end_s=2.0)
        payload = {
            'text': 'hello world',
            'segments': [{
                'start': 0.0, 'end': 2.0, 'text': 'hello world',
                'tokens': [{'token': 'hello'}, {'token': 'world'}],
            }],
        }
        result = client._payload_to_transcription_result(
            payload,
            provider='parakeet',
            response_format='json',
            timestamp_mode='segment',
            window=window,
            granularities=('segment',),
        )
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.segments[0].text, 'hello world')
        # Tokens without start/end timing are skipped by _extract_words
        self.assertEqual(len(result.segments[0].words), 0)

    def test_payload_with_text_only_segment_keeps_segment_mode(self):
        """When a segment has no words/tokens field at all, the result stays
        in segment mode (no synthetic word timestamps are generated)."""
        client = AsrApiClient(AsrConfig(api_key=''))
        window = DetectedSpeechWindow(start_s=0.0, end_s=3.0, ownership_start_s=0.0, ownership_end_s=3.0)
        payload = {
            'text': 'hello world foo',
            'segments': [{
                'start': 0.0, 'end': 3.0, 'text': 'hello world foo',
            }],
        }
        result = client._payload_to_transcription_result(
            payload,
            provider='parakeet',
            response_format='verbose_json',
            timestamp_mode='segment',
            window=window,
            granularities=('segment', 'word'),
        )
        self.assertEqual(len(result.segments), 1)
        self.assertEqual(result.timestamp_mode, 'segment')
        self.assertEqual(result.segments[0].words, [])

    def test_localai_top_level_words_preferred_over_synthesis(self):
        """LocalAI (parakeet-crispasr) puts word-level data at the top-level
        `words` array with {text, start, end} format, NOT inside segments.
        Segments have `tokens: null` ([]int, not token objects).  Real
        top-level word timings must be used — NOT synthesized from text split,
        which would produce less accurate evenly-spaced timings."""
        client = AsrApiClient(AsrConfig(api_key=''))
        window = DetectedSpeechWindow(start_s=0.0, end_s=4.0, ownership_start_s=0.0, ownership_end_s=4.0)
        # Simulate LocalAI crispasr verbose_json: segments have no words,
        # tokens is null ([]int).  Top-level words have real per-word timings.
        payload = {
            'text': 'hello world',
            'language': 'en',
            'duration': 4.0,
            'segments': [{
                'id': 0, 'start': 0.0, 'end': 4.0, 'text': 'hello world',
                'tokens': None,  # LocalAI []int serialized as null
            }],
            'words': [
                # LocalAI uses 'text' key (not OpenAI's 'word' key)
                {'text': 'hello', 'start': 0.5, 'end': 1.5},
                {'text': 'world', 'start': 2.0, 'end': 3.5},
            ],
        }
        result = client._payload_to_transcription_result(
            payload,
            provider='whisper',
            response_format='verbose_json',
            timestamp_mode='segment',
            window=window,
            granularities=('segment', 'word'),
        )
        self.assertEqual(result.timestamp_mode, 'word')
        words = result.segments[0].words
        self.assertEqual(len(words), 2)
        self.assertEqual([w.text for w in words], ['hello', 'world'])
        # Real timings from top-level words, NOT evenly-spaced synthesis
        # (synthesis would give start=0.0/end=2.0 and start=2.0/end=4.0)
        self.assertAlmostEqual(words[0].start_s, 0.5)
        self.assertAlmostEqual(words[0].end_s, 1.5)
        self.assertAlmostEqual(words[1].start_s, 2.0)
        self.assertAlmostEqual(words[1].end_s, 3.5)


if __name__ == '__main__':
    unittest.main()
