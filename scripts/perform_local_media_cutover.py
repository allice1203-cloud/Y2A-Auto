"""Explicit, local-only cutover phases. Stop both launchd writers first.

This operator tool preserves every source file and keeps consistent SQLite and
configuration backups on the internal disk. It never starts services or posts
videos. Activation requires the successful copy receipt in the same backup.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import sqlite3
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from modules.media_cutover import backup_database, rewrite_database, rewrite_paths
from modules.media_workspace import MediaWorkspaceConfig, validate_media_workspace, build_tree_copy_plan, execute_tree_copy_plan, verify_tree_copy

RUNTIME = Path('/Users/allice/Library/Application Support/超级印钞机/runtime')
SHARED = RUNTIME / 'shared/storage'
EXTERNAL = Path('/Volumes/资料盘4T/视频搬运')
MPT_MEDIA = EXTERNAL / 'moneyprinter-storage'
VOLUME_UUID = 'DE6F13E2-6341-4ECB-93AD-BF41EFDB2E6B'
NEW_RELEASE = RUNTIME / 'releases/20260906-external-media-local-r3'
LABELS = ('com.sg99.video-transfer-channel.local', 'com.sg99.moneyprinter-video-worker.local')
CONFIG = MediaWorkspaceConfig.from_values(EXTERNAL, VOLUME_UUID, min_free_gb=96)


def save(path, payload):
    if path.exists():
        raise ValueError(f'Receipt already exists: {path.name}')
    with path.open('x') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    path.chmod(0o600)


def stopped():
    for label in LABELS:
        result = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{label}'], capture_output=True)
        if result.returncode == 0:
            raise ValueError('Stop both launchd services before cutover')


def inventory(db_path):
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True) as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {table: db.execute('SELECT COUNT(*) FROM "' + table.replace('"','""') + '"').fetchone()[0] for table in tables}


def database_sources(project=PROJECT, shared=SHARED):
    """Include legacy task/outbox and monitor databases, not only transfer jobs."""
    return [*sorted((project/'db').glob('*.db')), *sorted(shared.glob('*.db'))]


def snapshot(backup):
    if backup.exists():
        raise ValueError('Choose a new backup directory')
    backup.mkdir(parents=True, mode=0o700)
    databases = database_sources()
    receipts = []
    for source in databases:
        folder = 'video-db' if source.parent == PROJECT/'db' else 'moneyprinter-db'
        row = backup_database(source, backup/folder/source.name)
        row['table_counts'] = inventory(source)
        receipts.append(row)
    for label in LABELS:
        source = Path('/Users/allice/Library/LaunchAgents')/(label+'.plist')
        shutil.copy2(source, backup/source.name)
        (backup/source.name).chmod(0o600)
    for source, name in ((PROJECT/'config', 'video-config'), (RUNTIME/'shared/config.toml', 'moneyprinter-config.toml')):
        target = backup/name
        if source.is_dir():
            shutil.copytree(source,target,symlinks=True)
            target.chmod(0o700)
            for path in target.rglob('*'):
                if path.is_file() and not path.is_symlink(): path.chmod(0o600)
        else:
            shutil.copy2(source,target)
            target.chmod(0o600)
    for revision, name in (('b9acff6','previous-video-code.tar'), ('HEAD','candidate-video-code.tar')):
        with (backup/name).open('xb') as handle:
            subprocess.run(['git','archive','--format=tar',revision], cwd=PROJECT,stdout=handle,check=True)
        (backup/name).chmod(0o600)
    state = {'old_release':str((RUNTIME/'current').resolve()), 'new_release':str(NEW_RELEASE),
             'databases':receipts, 'source_deleted':False, 'activated':False}
    save(backup/'snapshot.json',state)
    return {'database_backups':len(receipts), 'quick_check':'all ok', 'backup':str(backup)}


def copy_media(backup):
    json.loads((backup/'snapshot.json').read_text())
    validate_media_workspace(CONFIG,create=True,write_probe=True)
    copies = [(PROJECT/'downloads',EXTERNAL/'downloads','downloads'),(PROJECT/'temp',EXTERNAL/'temp','temp')]
    # Exclude root SQLite, WAL and account records. Only media directories move.
    copies += [(path,MPT_MEDIA/path.name,'mpt-'+path.name) for path in sorted(SHARED.iterdir()) if path.is_dir()]
    receipts=[]
    for source,destination,label in copies:
        validate_media_workspace(CONFIG,create=False,write_probe=False)
        plan=build_tree_copy_plan(source,destination,label=label)
        result=execute_tree_copy_plan(plan,reserve_bytes=CONFIG.min_free_bytes)
        validate_media_workspace(CONFIG,create=False,write_probe=False)
        if not verify_tree_copy(plan): raise ValueError('Post-copy verification failed')
        receipts.append({'label':label,'files':result.file_count,'bytes':result.total_bytes,'sha256':result.manifest_sha256,'source_preserved':True})
    if list(MPT_MEDIA.glob('*.db*')): raise ValueError('Database unexpectedly copied to media root')
    save(backup/'copy.json',receipts)
    return receipts


def activate(backup):
    state=json.loads((backup/'snapshot.json').read_text())
    json.loads((backup/'copy.json').read_text())
    validate_media_workspace(CONFIG,create=False,write_probe=True)
    if not (NEW_RELEASE/'app/utils/external_media.py').is_file(): raise ValueError('Guarded release missing')
    mappings=[(str(PROJECT/'downloads'),str(EXTERNAL/'downloads')),(str(PROJECT/'temp'),str(EXTERNAL/'temp'))]
    mappings += [(str(path),str(MPT_MEDIA)) for path in (SHARED,RUNTIME/'current/storage',Path(state['old_release'])/'storage')]
    changes=[]
    for db in database_sources():
        before=inventory(db)
        changes.append(rewrite_database(db,mappings))
        if inventory(db) != before: raise ValueError('Database row count changed')
    manifests=[]
    for root in (EXTERNAL/'downloads',MPT_MEDIA):
        for path in root.rglob('*.json'):
            old=path.read_text()
            new=rewrite_paths(old,mappings)
            if new != old:
                path.write_text(new)
                manifests.append(str(path.relative_to(EXTERNAL)))
    new_env = {
        LABELS[0]: {'VIDEO_TRANSFER_MEDIA_WORKSPACE_ROOT':str(EXTERNAL),'VIDEO_TRANSFER_MEDIA_VOLUME_UUID':VOLUME_UUID,'VIDEO_TRANSFER_MEDIA_MIN_FREE_GB':'60'},
        LABELS[1]: {'MPT_MEDIA_WORKSPACE_ROOT':str(MPT_MEDIA),'MPT_MEDIA_VOLUME_UUID':VOLUME_UUID,'MPT_DATABASE_ROOT':str(SHARED)},
    }
    for label in LABELS:
        path=Path('/Users/allice/Library/LaunchAgents')/(label+'.plist')
        data=plistlib.loads(path.read_bytes())
        data.setdefault('EnvironmentVariables',{}).update(new_env[label])
        temporary=path.with_suffix('.plist.cutover-new')
        if temporary.exists(): raise ValueError('Temporary plist already exists')
        temporary.write_bytes(plistlib.dumps(data))
        temporary.chmod(0o600)
        os.replace(temporary,path)
    link=RUNTIME/'current.cutover-new'
    if link.exists() or link.is_symlink(): raise ValueError('Temporary release link already exists')
    link.symlink_to(NEW_RELEASE,target_is_directory=True)
    os.replace(link,RUNTIME/'current')
    result={'database_changes':changes,'json_changes':manifests,'new_release':str(NEW_RELEASE),
            'database_location':str(SHARED),'media_location':str(MPT_MEDIA),'source_deleted':False,'services_started':False}
    save(backup/'activation.json',result)
    return result


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('phase',choices=('backup','copy','activate'))
    parser.add_argument('--backup',type=Path,required=True)
    args=parser.parse_args()
    permitted=Path('/Users/allice/Library/Application Support/视频搬运通道/backups')
    if args.backup.parent != permitted: raise ValueError('Backup must use the dedicated internal backup directory')
    stopped()
    function={'backup':snapshot,'copy':copy_media,'activate':activate}[args.phase]
    print(json.dumps(function(args.backup),ensure_ascii=False,indent=2))
