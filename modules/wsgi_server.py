import os


def serve_app(application, port, host=None, serve_impl=None):
    if serve_impl is None:
        from waitress import serve as serve_impl

    return serve_impl(
        application,
        host=host or os.environ.get("HOST", "0.0.0.0"),
        port=int(port),
        threads=4,
        channel_timeout=300,
        cleanup_interval=30,
        expose_tracebacks=False,
    )
