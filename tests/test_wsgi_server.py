from pathlib import Path
import os
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.wsgi_server import serve_app


class WsgiServerTests(unittest.TestCase):
    def test_waitress_receives_bounded_production_settings(self):
        calls = []
        application = object()

        def fake_serve(app, **kwargs):
            calls.append((app, kwargs))
            return "served"

        result = serve_app(application, "5000", serve_impl=fake_serve)

        self.assertEqual(result, "served")
        self.assertEqual(calls[0][0], application)
        self.assertEqual(
            calls[0][1],
            {
                "host": "0.0.0.0",
                "port": 5000,
                "threads": 4,
                "channel_timeout": 300,
                "cleanup_interval": 30,
                "expose_tracebacks": False,
            },
        )

    def test_host_can_be_limited_to_loopback_by_environment(self):
        calls = []

        def fake_serve(app, **kwargs):
            calls.append(kwargs)

        with patch.dict(os.environ, {"HOST": "127.0.0.1"}):
            serve_app(object(), "15188", serve_impl=fake_serve)

        self.assertEqual(calls[0]["host"], "127.0.0.1")
        self.assertEqual(calls[0]["port"], 15188)

    def test_explicit_host_takes_priority_over_environment(self):
        calls = []

        def fake_serve(app, **kwargs):
            calls.append(kwargs)

        with patch.dict(os.environ, {"HOST": "0.0.0.0"}):
            serve_app(
                object(),
                "15188",
                host="127.0.0.1",
                serve_impl=fake_serve,
            )

        self.assertEqual(calls[0]["host"], "127.0.0.1")

    def test_task_event_stream_leaves_hop_by_hop_headers_to_waitress(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        route_start = source.index("@app.route('/tasks/stream')")
        route_end = source.index("@app.route('/manual_review')", route_start)
        route_source = source[route_start:route_end]

        self.assertIn("mimetype='text/event-stream'", route_source)
        self.assertIn("response.headers['Cache-Control'] = 'no-cache'", route_source)
        self.assertIn("response.headers['X-Accel-Buffering'] = 'no'", route_source)
        self.assertNotIn("response.headers['Connection']", route_source)
        self.assertNotIn("response.headers['Transfer-Encoding']", route_source)


if __name__ == "__main__":
    unittest.main()
