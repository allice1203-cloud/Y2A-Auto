def serve_app(application, port, serve_impl=None):
    if serve_impl is None:
        from waitress import serve as serve_impl

    return serve_impl(
        application,
        host="0.0.0.0",
        port=int(port),
        threads=4,
        channel_timeout=300,
        cleanup_interval=30,
        expose_tracebacks=False,
    )
