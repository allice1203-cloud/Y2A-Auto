#!/usr/bin/env python3
"""Loopback-only design sandbox. Never loads production data or starts workers.

Uses the actual Flask routes/templates with disposable demo SQLite databases.
External effects and unsupported endpoints are blocked before route dispatch.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import uuid
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def create_preview():
    # The application caches database/config helpers at import time. Reusing an
    # already imported app would defeat isolation; this runner needs its own process.
    if any(name == 'app' or name.startswith('modules.') for name in sys.modules):
        raise RuntimeError('Design preview must start in a fresh process')
    sandbox = Path(tempfile.mkdtemp(prefix='video-ink-preview-'))
    sys.path.insert(0, str(ROOT))
    for name in ('config', 'db', 'logs', 'cookies', 'downloads', 'temp'):
        (sandbox / name).mkdir()
    import modules.utils as utils
    utils.get_app_root_dir = lambda: str(sandbox)
    utils.get_app_subdir = lambda name: str(sandbox / name)
    from modules.config_manager import DEFAULT_CONFIG
    config = dict(DEFAULT_CONFIG)
    config.update(AUTO_MODE_ENABLED=False, LOG_CLEANUP_ENABLED=False,
                  DOWNLOAD_CLEANUP_ENABLED=False, TRANSFER_MAINTENANCE_ENABLED=False,
                  TRANSFER_MPT_WATCHDOG_ENABLED=False, TRANSFER_TELEGRAM_INTAKE_ENABLED=False,
                  TRANSFER_LOCAL_VISION_ENABLED=False, TRANSFER_115_BACKUP_ENABLED=False)
    import modules.config_manager as manager
    manager.load_config = lambda: dict(config)
    # This process may listen on loopback but cannot connect to any service.
    def denied_connection(*args, **kwargs):
        raise OSError('Design preview: outbound network disabled')
    socket.socket.connect = denied_connection
    socket.socket.connect_ex = denied_connection
    socket.socket.sendto = denied_connection
    socket.gethostbyname = denied_connection
    socket.gethostbyname_ex = denied_connection
    socket.gethostbyaddr = denied_connection
    original_getaddrinfo = socket.getaddrinfo
    def numeric_addresses_only(host, port, family=0, type=0, proto=0, flags=0):
        # Permit local listener setup, but never issue DNS queries.
        if host in ('localhost', b'localhost'):
            host = '127.0.0.1'
        return original_getaddrinfo(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)
    socket.getaddrinfo = numeric_addresses_only
    import app as application
    from flask import flash, jsonify, redirect, request, url_for
    application.app.config.update(Y2A_SETTINGS=config, TESTING=False, TEMPLATES_AUTO_RELOAD=True)
    application.app.jinja_env.auto_reload = True
    application._source_login_helper_secret = lambda: ''
    application._load_acfun_partition_mapping = lambda: []
    application._build_bilibili_partition_mapping = lambda: []
    center = application._transfer_center()
    demo_id = center.add_manual_job('https://www.youtube.com/watch?v=demoInk0001', ['bilibili', 'douyin'])
    # Preview media is an eight-second, silent still-frame demo, not real footage.
    media = ROOT / 'static' / 'img' / 'ink-demo.mp4'
    center._update_job(demo_id, title='城市里的慢镜头', source_uploader='设计示例 · 非真实任务',
        status='review', recreation_completed=1, recreation_status='pending',
        local_video_path=str(media), thumbnail_url='/static/img/ink-demo-sunset.png',
        processing_mode='professional', target_platforms=json.dumps(['bilibili','douyin']),
        source_attribution='设计示例，不对应真实平台素材', watermark_status='none',
        commentary_script='日落让城市慢下来。走过桥，沿着河岸，看光线一点点落进水面。',
        recreation_plan_json=json.dumps({'draft_script':'日落让城市慢下来。走过桥，沿着河岸，看光线一点点落进水面。',
          'draft_storyboard':[], 'material_checklist':[]}, ensure_ascii=False),
        platform_variants_json=json.dumps({'bilibili':{'status':'ready','locale':'zh-CN','path':str(media)}}))
    second = center.add_manual_job('https://www.youtube.com/watch?v=demoInk0002', ['x'])
    center._update_job(second, title='河岸散步 · 发布记录示例', status='completed', recreation_status='approved',
        recreation_completed=1, x_publish_status='published', source_uploader='设计示例 · 非真实任务',
        local_video_path=str(media), thumbnail_url='/static/img/ink-demo-sunset.png', backup_status='disabled')
    application.app.context_processor(lambda: {'design_preview':True,'production_navigation_enabled':True})
    def demo_media(job_id):
        from flask import send_file
        if job_id not in {demo_id, second}:
            return '示例任务不存在', 404
        return send_file(media, conditional=True) if media.is_file() else ('示例视频未生成', 404)
    application.app.view_functions['transfer_center_review_media'] = demo_media
    # Match the preview-only endpoint without widening the production media root.
    application._transfer_review_media_available = lambda path: (
        bool(path) and Path(path).resolve() == media.resolve() and media.is_file()
    )
    allowed_get = {'static','index','tasks','transfer_task_state','manual_review','settings',
        'transfer_center_index','transfer_center_review_job','transfer_center_review_media','quick_setup',
        'production_workbench','youtube_monitor_index','youtube_monitor_config','youtube_monitor_edit_config'}
    def preview_boundary():
        if request.host.split(':')[0] not in {'127.0.0.1','localhost'}:
            return 'Local preview only', 403
        origin = request.headers.get('Origin')
        if origin and urlparse(origin).netloc != request.host:
            return 'Same-origin preview only', 403
        if request.method == 'GET' and request.endpoint == 'settings_save_progress':
            # Reuse the existing JSON shape, not its background-save machinery.
            return application.settings_save_progress(request.view_args['operation_id'])
        if request.method == 'GET' and request.endpoint in allowed_get:
            return None
        if request.method == 'POST' and request.endpoint == 'production_save_settings':
            return None  # Real validation and persistence, isolated demo DB only.
        if request.method == 'POST' and request.endpoint == 'settings':
            # Reflect non-secret form choices in memory only. Credentials are not retained.
            values = dict(config)
            for key, default in config.items():
                if any(x in key.lower() for x in ('key','token','secret','password','cookie')):
                    continue
                if key.upper().endswith(('_PATH', '_DIR', '_ROOT')):
                    continue
                if isinstance(default, bool):
                    values[key] = request.form.get(key, '').lower() in {'1', 'true', 'on'}
                elif key in request.form:
                    value = request.form[key]
                    try:
                        values[key] = type(default)(value) if isinstance(default, (int, float)) else value
                    except (ValueError, TypeError):
                        return jsonify(success=False,message='请检查数字设置后重试。'), 422
            config.update(values)
            if application._is_ajax_request():
                operation_id = str(uuid.uuid4())
                message = '预览设置已更新，未修改正式系统或账户凭据。'
                application._update_settings_save_progress(
                    operation_id, stage='complete', message=message, detail='仅当前预览进程有效。',
                    percent=100, done=True, level='success', success=True,
                    messages=[{'category':'success','text':message}])
                return jsonify(success=True,operation_id=operation_id,messages=[])
            flash('预览设置已更新；不会修改正式系统或账户凭据。','info')
            return redirect(url_for('settings'))
        if request.method == 'POST' and request.endpoint == 'transfer_center_save_review':
            if request.view_args['job_id'] not in {demo_id, second}:
                return '示例任务不存在', 404
            if request.form.get('action') == 'draft':
                center._update_job(request.view_args['job_id'], commentary_script=request.form.get('commentary_script','')[:8000])
                flash('草稿已保存在隔离预览中。没有制作或发布真实视频。','info')
            else:
                flash('设计预览不连接平台。正式发布仍须在当前正式系统确认。','info')
            return redirect(url_for('transfer_center_review_job',job_id=request.view_args['job_id']))
        if request.path == '/tasks/events':
            return '', 204
        if request.path.startswith('/api/') or request.is_json or application._is_ajax_request():
            return jsonify(success=False,message='设计预览不调用外部服务，也不修改正式数据。'), 409
        flash('这是独立设计预览，此操作不会执行真实下载、登录、制作或发布。','info')
        return redirect(url_for('index'))
    application.app.before_request_funcs.setdefault(None, []).insert(0, preview_boundary)
    return application.app, demo_id, sandbox


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=15190)
    parser.add_argument('--check',action='store_true')
    args = parser.parse_args()
    app, demo_id, sandbox = create_preview()
    print(f'Design preview: http://127.0.0.1:{args.port}/transfer-center/jobs/{demo_id}/review', flush=True)
    print(f'Isolated demo runtime: {sandbox}', flush=True)
    if args.check:
        client = app.test_client()
        for path in ['/', '/tasks', '/manual_review', '/settings', '/transfer-center', '/quick-setup',
                     '/production', '/production?tab=queue', '/production?tab=receipts',
                     '/production?tab=income', '/youtube_monitor', f'/transfer-center/jobs/{demo_id}/review']:
            response = client.get(path, base_url='http://127.0.0.1')
            print(response.status_code, path, flush=True)
            if response.status_code != 200:
                raise SystemExit(1)
    else:
        from modules.wsgi_server import serve_app
        serve_app(app,args.port,host='127.0.0.1')
