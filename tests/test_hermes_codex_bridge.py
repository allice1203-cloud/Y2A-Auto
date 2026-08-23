import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "ops" / "hermes_codex_bridge.py"
SPEC = importlib.util.spec_from_file_location("hermes_codex_bridge", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


class BridgeSerializationTests(unittest.TestCase):
    def test_serializes_text_and_usage(self):
        result = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="ok", tool_calls=None),
                    finish_reason="stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=3,
                completion_tokens=2,
                total_tokens=5,
            ),
        )
        payload = MODULE._serialize_completion(result, "gpt-5.6-luna")
        self.assertEqual(payload["choices"][0]["message"]["content"], "ok")
        self.assertEqual(payload["usage"]["total_tokens"], 5)
        self.assertEqual(payload["model"], "gpt-5.6-luna")

    def test_redacts_bearer_tokens(self):
        message = MODULE._safe_error(RuntimeError("Bearer abcdefghijklmnop"))
        self.assertNotIn("abcdefghijklmnop", message)


if __name__ == "__main__":
    unittest.main()
