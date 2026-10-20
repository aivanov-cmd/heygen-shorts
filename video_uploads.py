"""Scoped, resumable MP4 uploads for the existing Railway video queue.

The MCP service and upload client are separate machines: bytes must be sent over
HTTPS. A path such as /mnt/data/final.mp4 alone is never treated as an upload.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

MAX_FILE = 256 * 1024 * 1024
MAX_CHUNK = 8 * 1024 * 1024
UPLOAD_TTL = 3600
SCHEMA = """
ALTER TABLE shorts ADD COLUMN IF NOT EXISTS active_video_asset_id TEXT;
CREATE TABLE IF NOT EXISTS video_file_uploads (
 upload_id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, short_id INTEGER NOT NULL,
 file_name TEXT NOT NULL, size_bytes BIGINT NOT NULL, sha256 TEXT NOT NULL,
 token_hash TEXT NOT NULL, received_bytes BIGINT NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'pending', expires_at TIMESTAMPTZ NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), asset_id TEXT
);
CREATE INDEX IF NOT EXISTS video_upload_short ON video_file_uploads(short_id,created_at DESC);
CREATE TABLE IF NOT EXISTS video_file_assets (
 asset_id TEXT PRIMARY KEY, project_id INTEGER NOT NULL, short_id INTEGER NOT NULL,
 size_bytes BIGINT NOT NULL, sha256 TEXT NOT NULL, source TEXT NOT NULL,
 metadata JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


class UploadError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise UploadError('invalid_input', f'{name} must be a positive integer')
    return value


def safe_id(value):
    if not re.fullmatch(r'[a-f0-9]{32}', str(value)):
        raise UploadError('invalid_id', 'Invalid upload/asset ID')
    return str(value)


def digest_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def link_or_copy(source,destination):
    """Keep versioned files on one volume without duplicating bytes when possible."""
    try:os.link(source,destination)
    except OSError:shutil.copyfile(source,destination)


def ensure_video_upload_schema(get_db):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(721406, 21)')
            cur.execute(SCHEMA)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def boxes(f, start, end):
    """Bounded ISO BMFF box iterator; no media payload is loaded into memory."""
    pos = start
    count = 0
    while pos < end:
        if end-pos < 8:
            raise UploadError('invalid_mp4', 'Truncated MP4 box header')
        f.seek(pos)
        size, kind = struct.unpack('>I4s', f.read(8))
        header = 8
        if size == 1:
            data = f.read(8)
            if len(data) != 8:
                raise UploadError('invalid_mp4', 'Truncated large MP4 box')
            size, header = struct.unpack('>Q', data)[0], 16
        elif size == 0:
            size = end-pos
        if size < header or pos+size > end:
            raise UploadError('invalid_mp4', 'MP4 box exceeds file bounds')
        yield kind, pos+header, pos+size
        pos += size
        count += 1
        if count > 100000:
            raise UploadError('invalid_mp4', 'Too many MP4 boxes')


def basic_mp4_probe(path):
    size = path.stat().st_size
    metadata = dict(validation='mp4_container', decoded_frames_checked=False)
    with open(path, 'rb') as f:
        top = list(boxes(f, 0, size))
        kinds = {x[0] for x in top}
        if not {b'ftyp', b'moov', b'mdat'} <= kinds:
            raise UploadError('invalid_mp4', 'Expected an MP4 with ftyp, moov and mdat boxes')
        mdat_size = sum(e-s for k, s, e in top if k == b'mdat')
        if mdat_size < 1:
            raise UploadError('invalid_mp4', 'MP4 contains no media data')
        videos = []
        for kind, start, end in top:
            if kind != b'moov':
                continue
            for k, s, e in boxes(f, start, end):
                if k == b'mvhd':
                    f.seek(s);data = f.read(min(e-s, 40))
                    if len(data) >= 20 and data[0] == 0:
                        scale, duration = struct.unpack('>II', data[12:20])
                    elif len(data) >= 32 and data[0] == 1:
                        scale = struct.unpack('>I', data[20:24])[0]
                        duration = struct.unpack('>Q', data[24:32])[0]
                    else:
                        raise UploadError('invalid_mp4', 'Invalid movie header')
                    if scale:
                        metadata['duration_seconds'] = duration/scale
                if k != b'trak':
                    continue
                track = {}
                for tk, ts, te in boxes(f, s, e):
                    if tk == b'tkhd' and te-ts >= 8:
                        f.seek(te-8)
                        width, height = struct.unpack('>II', f.read(8))
                        track.update(width=width/65536, height=height/65536)
                    if tk == b'mdia':
                        for mk, ms, me in boxes(f, ts, te):
                            if mk == b'hdlr' and me-ms >= 12:
                                f.seek(ms+8);track['handler'] = f.read(4)
                if track.get('handler') == b'vide':
                    videos.append(track)
        if not videos or not any(v.get('width', 0)>0 and v.get('height', 0)>0 for v in videos):
            raise UploadError('invalid_mp4', 'No video track with valid dimensions')
        if not 0 < metadata.get('duration_seconds', 0) < 3600:
            raise UploadError('invalid_mp4', 'Missing or invalid MP4 duration')
        metadata.update(width=videos[0]['width'], height=videos[0]['height'])
    return metadata


def probe_video(path):
    metadata = basic_mp4_probe(path)
    ffprobe = shutil.which('ffprobe')
    if ffprobe:
        try:
            result = subprocess.run([ffprobe, '-v', 'error', '-show_format', '-show_streams',
                                     '-of', 'json', str(path)], capture_output=True, timeout=30,
                                    check=True, text=True, encoding='utf-8')
            data = json.loads(result.stdout)
            video = next(s for s in data.get('streams', []) if s.get('codec_type') == 'video')
            metadata.update(validation='ffprobe_metadata', codec=video.get('codec_name'),
                            width=video.get('width'), height=video.get('height'),
                            audio_present=any(s.get('codec_type') == 'audio' for s in data.get('streams', [])))
            if data.get('format', {}).get('duration'):
                metadata['duration_seconds'] = float(data['format']['duration'])
        except (subprocess.SubprocessError, ValueError, StopIteration, OSError):
            raise UploadError('invalid_mp4', 'ffprobe could not validate this MP4')
    return metadata


class VideoFiles:
    def __init__(self, get_db, base_url, root='/videos', signing_key=None):
        parsed = urlsplit(base_url)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
            raise ValueError('A trusted HTTPS public base URL is required')
        self.get_db, self.base_url, self.root = get_db, base_url.rstrip('/'), Path(root)
        self.signing_key = signing_key if signing_key is not None else os.environ.get('VIDEO_DOWNLOAD_API_KEY', '')

    def canonical(self, short_id):
        return self.root / f'short_{positive_int(short_id, "short_id")}.mp4'

    def staged(self, upload_id):
        return self.root / '.uploads' / (safe_id(upload_id)+'.part')

    def asset_path(self, asset_id):
        return self.root / 'file-assets' / (safe_id(asset_id)+'.mp4')

    @contextmanager
    def lock(self, short_id):
        """Advisory file lock shared by edited attachment, download and publication."""
        positive_int(short_id, 'short_id')
        directory = self.root / '.locks';directory.mkdir(parents=True, exist_ok=True)
        with open(directory/f'short_{short_id}.lock', 'a+b') as f:
            deadline = time.monotonic()+10
            if os.name == 'nt':
                import msvcrt
                f.seek(0);f.write(b'0');f.flush()
                def acquire():
                    f.seek(0);msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                def release():
                    f.seek(0);msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def acquire():fcntl.flock(f.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
                def release():fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            while True:
                try:
                    acquire();break
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        raise UploadError('file_busy', 'Video file is busy; retry later', 409)
                    time.sleep(.05)
            try:
                yield
            finally:
                release()

    def editable(self, cur, project_id, short_id):
        positive_int(project_id, 'project_id');positive_int(short_id, 'short_id')
        cur.execute('''SELECT id,project_id,status,youtube_status,instagram_status,active_video_asset_id
                       FROM shorts WHERE id=%s FOR UPDATE''', (short_id,))
        row = cur.fetchone()
        if not row or row[1] != project_id:
            raise UploadError('wrong_project', 'Short not found in this project', 404)
        if row[2] not in ('approved', 'completed', 'failed'):
            raise UploadError('short_not_editable', 'Finish generation/approval before attaching a file', 409)
        if any(x in ('scheduled', 'requested', 'published') for x in row[3:5]):
            raise UploadError('publication_locked', 'Cannot replace a scheduled, publishing or published video; use a new queue item or cancel its schedule first', 409)
        return row

    def begin(self, project_id, short_id, file_name, size_bytes, sha256):
        positive_int(size_bytes, 'size_bytes')
        if not 32 <= size_bytes <= MAX_FILE:
            raise UploadError('invalid_size', f'MP4 size must be between 32 and {MAX_FILE} bytes')
        if not isinstance(file_name, str) or len(file_name)>200 or not file_name.lower().endswith('.mp4') or re.search(r'[/\\\x00-\x1f]', file_name):
            raise UploadError('invalid_name', 'Provide an MP4 basename, not a local path')
        if not re.fullmatch(r'[a-fA-F0-9]{64}', str(sha256)):
            raise UploadError('invalid_sha256', 'SHA-256 of the actual local file is required')
        upload_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc)+timedelta(seconds=UPLOAD_TTL)
        conn = self.get_db()
        try:
            with conn.cursor() as cur:
                self.editable(cur, project_id, short_id)
                cur.execute('''INSERT INTO video_file_uploads
                  (upload_id,project_id,short_id,file_name,size_bytes,sha256,token_hash,expires_at)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                  (upload_id,project_id,short_id,file_name,size_bytes,sha256.lower(),hashlib.sha256(token.encode()).hexdigest(),expires))
            conn.commit()
        except Exception:
            conn.rollback();raise
        finally:
            conn.close()
        return dict(success=True,upload_id=upload_id,project_id=project_id,short_id=short_id,
                    file_name=file_name,size_bytes=size_bytes,sha256=sha256.lower(),
                    upload_url=self.base_url+'/video-uploads/'+upload_id,upload_token=token,
                    expires_at=expires.isoformat(),max_chunk_bytes=MAX_CHUNK,
                    instructions='Send actual MP4 bytes by HTTPS PUT in Content-Range chunks using this scoped Bearer token. A local path or a Claude attachment link alone is not an upload. Then call complete_video_file_upload. No publication is triggered.')

    def upload_row(self, cur, upload_id, token=None, lock=False):
        safe_id(upload_id)
        cur.execute('''SELECT upload_id,project_id,short_id,size_bytes,sha256,token_hash,
                       received_bytes,status,expires_at,asset_id FROM video_file_uploads
                       WHERE upload_id=%s'''+(' FOR UPDATE NOWAIT' if lock else ''), (upload_id,))
        row = cur.fetchone()
        if not row:
            raise UploadError('upload_not_found', 'Upload not found', 404)
        if token is not None:
            value = hashlib.sha256(token.encode()).hexdigest()
            if not hmac.compare_digest(value, row[5]):
                raise UploadError('unauthorized', 'Invalid upload token', 401)
            if row[8] <= datetime.now(timezone.utc):
                raise UploadError('upload_expired', 'Upload ticket expired; create a new ticket', 401)
        return row

    def active(self, project_id, short_id):
        positive_int(project_id,'project_id');positive_int(short_id,'short_id')
        conn=self.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute('''SELECT s.project_id,s.active_video_asset_id,a.size_bytes,a.sha256,a.metadata
                  FROM shorts s LEFT JOIN video_file_assets a ON a.asset_id=s.active_video_asset_id
                  WHERE s.id=%s''',(short_id,));row=cur.fetchone()
            if not row or row[0]!=project_id:
                raise UploadError('wrong_project','Short not found in this project',404)
            if not row[1]:
                return dict(source='heygen_or_existing',short_id=short_id,project_id=project_id,
                            stored=self.canonical(short_id).is_file())
            path=self.canonical(short_id)
            metadata=row[4] if isinstance(row[4],dict) else json.loads(row[4] or '{}')
            return dict(source='uploaded_file',short_id=short_id,project_id=project_id,asset_id=row[1],
                        stored=path.is_file(),size_bytes=row[2],sha256=row[3],metadata=metadata,
                        download_url=self.download_url(row[1]))
        finally:conn.close()

    def active_for_short(self, short_id):
        conn=self.get_db()
        try:
            with conn.cursor() as cur:
                cur.execute('SELECT project_id FROM shorts WHERE id=%s',(positive_int(short_id,'short_id'),));row=cur.fetchone()
        finally:conn.close()
        return self.active(row[0],short_id) if row else None

    def download_guard(self, short_id):
        active=self.active_for_short(short_id)
        if active and active['source']=='uploaded_file':
            return dict(success=active['stored'],short_id=short_id,source='uploaded_file',
                        size_bytes=active['size_bytes'],sha256=active['sha256'],
                        file_name=self.canonical(short_id).name,file_path=str(self.canonical(short_id)),
                        persistent_storage=True,temporary=False,
                        message='Using the selected uploaded file; HeyGen original was not downloaded over it',
                        **({} if active['stored'] else {'error':'Selected uploaded file is missing; attach it again'}))
        return None

    def verify_active_for_publish(self, short_id):
        active=self.active_for_short(short_id)
        if active and active['source']=='uploaded_file':
            path=self.canonical(short_id)
            if not path.is_file() or path.stat().st_size!=active['size_bytes'] or digest_file(path)!=active['sha256']:
                return dict(success=False,short_id=short_id,error='Selected uploaded file is missing or changed; publication stopped')
        return None

    def enrich_short(self, result):
        if result.get('found') and isinstance(result.get('short'),dict):
            short=result['short'];active=self.active(short['project_id'],short['id'])
            short['active_video_file']=active
            if active['source']=='uploaded_file':
                short['original_heygen_video_url']=short.get('video_url')
                short['video_url']=active['download_url']
        return result

    def download_url(self, asset_id):
        safe_id(asset_id)
        if not self.signing_key:
            raise UploadError('missing_signing_key','VIDEO_DOWNLOAD_API_KEY is required',503)
        expires=int(time.time())+3600
        message=f'asset:{asset_id}:{expires}'.encode()
        signature=hmac.new(self.signing_key.encode(),message,hashlib.sha256).hexdigest()
        return self.base_url+'/video-assets/'+asset_id+'?'+urlencode(dict(expires=expires,signature=signature))

    def authorize_download(self, asset_id, query):
        safe_id(asset_id)
        try:
            expires=int(query.get('expires',['0'])[0]);sig=query.get('signature',[''])[0]
        except (ValueError,TypeError):raise UploadError('unauthorized','Invalid video link',401)
        if expires<int(time.time()) or expires>int(time.time())+3700 or not self.signing_key:
            raise UploadError('unauthorized','Video link expired or invalid',401)
        expected=hmac.new(self.signing_key.encode(),f'asset:{asset_id}:{expires}'.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig.encode(),expected.encode()):
            raise UploadError('unauthorized','Invalid video link',401)
        return self.asset_path(asset_id)

    def complete(self, project_id, short_id, upload_id):
        positive_int(project_id,'project_id');positive_int(short_id,'short_id');safe_id(upload_id)
        with self.lock(short_id):
            return self._complete_locked(project_id,short_id,upload_id)

    def _complete_locked(self, project_id, short_id, upload_id):
        conn=self.get_db();new_asset=None;old_backup=None;canonical=self.canonical(short_id);switched=False;committed=False
        try:
            with conn.cursor() as cur:
                row=self.upload_row(cur,upload_id,lock=True)
                if row[1:3]!=(project_id,short_id):raise UploadError('wrong_project','Upload belongs to another short or project',409)
                if row[7]=='completed':
                    return dict(success=True,already_completed=True,asset_id=row[9],short_id=short_id,project_id=project_id,
                                message='This upload was already committed; it was not reattached over a newer file')
                self.editable(cur,project_id,short_id)
                if row[8]<=datetime.now(timezone.utc):raise UploadError('upload_expired','Upload ticket expired; create a new ticket',409)
                if row[7]!='uploaded' or row[6]!=row[3]:raise UploadError('upload_incomplete','Send all file bytes before completion',409)
                stage=self.staged(upload_id)
                if not stage.is_file() or stage.stat().st_size!=row[3] or digest_file(stage)!=row[4]:
                    raise UploadError('checksum_mismatch','Uploaded file does not match the declared size/SHA-256',409)
                metadata=probe_video(stage)
                asset_id=uuid.uuid4().hex;new_asset=self.asset_path(asset_id);new_asset.parent.mkdir(parents=True,exist_ok=True)
                # Copy first: the staged upload stays available if the DB transaction fails.
                link_or_copy(stage,new_asset)
                if canonical.exists():
                    old_id=uuid.uuid4().hex;old_backup=self.asset_path(old_id)
                    link_or_copy(canonical,old_backup)
                    cur.execute('''INSERT INTO video_file_assets
                      (asset_id,project_id,short_id,size_bytes,sha256,source,metadata)
                      VALUES (%s,%s,%s,%s,%s,'previous_file',%s::jsonb)''',
                      (old_id,project_id,short_id,old_backup.stat().st_size,digest_file(old_backup),json.dumps({'preserved_before_upload':upload_id})))
                cur.execute('''INSERT INTO video_file_assets
                  (asset_id,project_id,short_id,size_bytes,sha256,source,metadata)
                  VALUES (%s,%s,%s,%s,%s,'uploaded_file',%s::jsonb)''',
                  (asset_id,project_id,short_id,row[3],row[4],json.dumps(metadata)))
                temp=self.root/f'.short_{short_id}_{uuid.uuid4().hex}.mp4'
                try:
                    link_or_copy(new_asset,temp);os.replace(temp,canonical);switched=True
                finally:
                    if temp.exists():temp.unlink()
                cur.execute('''UPDATE shorts SET active_video_asset_id=%s,status='completed',
                  last_error=NULL,updated_at=NOW() WHERE id=%s AND project_id=%s''',(asset_id,short_id,project_id))
                cur.execute("UPDATE video_file_uploads SET status='completed',asset_id=%s WHERE upload_id=%s",(asset_id,upload_id))
                download_url=self.download_url(asset_id)
            conn.commit();committed=True
            try:stage.unlink(missing_ok=True)
            except OSError:pass
            return dict(success=True,short_id=short_id,project_id=project_id,asset_id=asset_id,
                        source='uploaded_file',size_bytes=row[3],sha256=row[4],metadata=metadata,
                        stored=True,status='completed',download_url=download_url,
                        publication_triggered=False,message='The uploaded MP4 is now selected for the existing publishing workflow. Original file preserved if it existed.')
        except Exception:
            if not committed:
                conn.rollback()
                if switched:
                    if old_backup and old_backup.exists():os.replace(old_backup,canonical)
                    else:canonical.unlink(missing_ok=True)
                if new_asset:new_asset.unlink(missing_ok=True)
                if old_backup:old_backup.unlink(missing_ok=True)
            raise
        finally:conn.close()


def register_video_upload_tools(mcp, service):
    def safe_call(fn,*args):
        try:return fn(*args)
        except UploadError as exc:return dict(success=False,error=exc.code,message=exc.message)
        except Exception as exc:
            print('Video upload storage error:',type(exc).__name__,flush=True)
            return dict(success=False,error='video_storage_unavailable',message='Video storage operation failed; check Railway deployment and storage')

    @mcp.tool()
    def begin_video_file_upload(project_id:int,short_id:int,file_name:str,size_bytes:int,sha256:str)->dict:
        """Create a one-hour scoped upload ticket for an actual edited local MP4.
        Compute file size and SHA-256 where the file exists. Use the returned URL
        and scoped token to SEND FILE BYTES using HTTPS PUT with Content-Range.
        A local /mnt/data path or attachment reference is not a transferred file.
        After upload call complete_video_file_upload. No automatic publication.
        """
        return safe_call(service.begin,project_id,short_id,file_name,size_bytes,sha256)

    @mcp.tool()
    def complete_video_file_upload(project_id:int,short_id:int,upload_id:str)->dict:
        """Verify uploaded MP4 bytes and checksum, preserve previous file, select
        this file for the existing automation and mark the Short completed.
        Refuses scheduled/in-progress/published items and mismatched projects.
        This tool does NOT publish or schedule anything. Idempotent per upload.
        """
        return safe_call(service.complete,project_id,short_id,upload_id)

    @mcp.tool()
    def get_video_file_status(project_id:int,short_id:int)->dict:
        """Read selected video file, source, stored size, SHA-256, metadata and
        one-hour download link. Check source=uploaded_file and stored=true after
        completion. Existing HeyGen ID is provenance, not selected file source.
        """
        return safe_call(service.active,project_id,short_id)


class VideoUploadMiddleware:
    def __init__(self,app,service):self.app,self.service=app,service

    async def respond(self,send,status,data):
        body=json.dumps(data,ensure_ascii=False).encode()
        await send(dict(type='http.response.start',status=status,headers=[(b'content-type',b'application/json; charset=utf-8'),(b'cache-control',b'no-store')]))
        await send(dict(type='http.response.body',body=body))

    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        if scope.get('type')!='http' or not path.startswith(('/video-uploads/','/video-assets/')):
            return await self.app(scope,receive,send)
        try:
            if path.startswith('/video-assets/'):
                if scope.get('method') not in ('GET','HEAD'):raise UploadError('method_not_allowed','Use GET or HEAD',405)
                asset_id=safe_id(path.removeprefix('/video-assets/'))
                query=parse_qs(scope.get('query_string',b'').decode())
                file=self.service.authorize_download(asset_id,query)
                return await self.stream(scope,send,file)
            upload_id=safe_id(path.removeprefix('/video-uploads/'))
            headers={k.lower():v for k,v in scope.get('headers',[])}
            auth=headers.get(b'authorization',b'').decode('utf-8',errors='replace')
            if not auth.startswith('Bearer '):raise UploadError('unauthorized','Scoped upload Bearer token required',401)
            if scope.get('method')=='GET':
                conn=self.service.get_db()
                try:
                    with conn.cursor() as cur:row=self.service.upload_row(cur,upload_id,auth[7:])
                finally:conn.close()
                return await self.respond(send,200,dict(upload_id=upload_id,received_bytes=row[6],size_bytes=row[3],status=row[7]))
            if scope.get('method')!='PUT':raise UploadError('method_not_allowed','Use GET or PUT',405)
            return await self.receive_chunk(scope,receive,send,upload_id,auth[7:],headers)
        except UploadError as exc:
            return await self.respond(send,exc.status,dict(success=False,error=exc.code,message=exc.message))
        except Exception:
            return await self.respond(send,503,dict(success=False,error='upload_storage_unavailable',message='Upload storage unavailable or busy; retry GET status, then resume'))

    async def receive_chunk(self,scope,receive,send,upload_id,token,headers):
        match=re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)',headers.get(b'content-range',b'').decode())
        if not match:raise UploadError('invalid_range','Content-Range: bytes start-end/total required')
        start,end,total=map(int,match.groups());length=end-start+1
        if start>end or end>=total or not 1<=length<=MAX_CHUNK:
            raise UploadError('invalid_range','Invalid or oversized upload chunk')
        declared=headers.get(b'content-length')
        if declared is not None and declared.decode()!=str(length):raise UploadError('invalid_length','Content-Length must match this chunk')
        conn=self.service.get_db();file=None;offset=None;completed=False
        try:
            with conn.cursor() as cur:
                row=self.service.upload_row(cur,upload_id,token,lock=True)
                if row[7] not in ('pending','uploading','uploaded'):raise UploadError('upload_closed','Upload is already closed',409)
                if total!=row[3]:raise UploadError('invalid_size','Total does not match upload ticket')
                if start!=row[6]:raise UploadError('offset_mismatch',f'Resume at byte {row[6]} using GET upload status',409)
                file=self.service.staged(upload_id);file.parent.mkdir(parents=True,exist_ok=True);offset=start
                if start and (not file.exists() or file.stat().st_size<start):raise UploadError('staging_missing','Staged bytes missing; create a new ticket',409)
                received=0
                with open(file,'r+b' if file.exists() else 'w+b') as output:
                    output.seek(start);output.truncate(start)
                    while True:
                        event=await receive()
                        if event.get('type')=='http.disconnect':raise UploadError('upload_interrupted','Chunk interrupted; resume from GET status',409)
                        block=event.get('body',b'');received+=len(block)
                        if received>length:raise UploadError('invalid_length','Received more bytes than declared')
                        output.write(block)
                        if not event.get('more_body'):break
                    if received!=length:raise UploadError('invalid_length','Received fewer bytes than declared')
                    output.flush();os.fsync(output.fileno())
                new_offset=start+received
                status='uploaded' if new_offset==total else 'uploading'
                cur.execute('UPDATE video_file_uploads SET received_bytes=%s,status=%s WHERE upload_id=%s',(new_offset,status,upload_id))
            conn.commit();completed=True
        except Exception:
            conn.rollback()
            if file and file.exists() and offset is not None:
                with open(file,'r+b') as output:output.truncate(offset)
            raise
        finally:conn.close()
        if completed:
            return await self.respond(send,200,dict(success=True,upload_id=upload_id,received_bytes=new_offset,size_bytes=total,status=status))

    async def stream(self,scope,send,path):
        if not path.is_file():raise UploadError('asset_not_found','Video file not found',404)
        size=path.stat().st_size;start,end=0,size-1;status=200
        headers={k.lower():v for k,v in scope.get('headers',[])}
        extra=[]
        if b'range' in headers:
            m=re.fullmatch(r'bytes=(\d*)-(\d*)',headers[b'range'].decode())
            if not m or (not m[1] and not m[2]):raise UploadError('invalid_range','Single byte range required',416)
            if m[1]:start=int(m[1]);end=min(int(m[2]) if m[2] else end,end)
            else:start=max(0,size-int(m[2]))
            if not 0<=start<=end<size:raise UploadError('invalid_range','Range outside file',416)
            status=206;extra=[(b'content-range',f'bytes {start}-{end}/{size}'.encode())]
        await send(dict(type='http.response.start',status=status,headers=[(b'content-type',b'video/mp4'),(b'accept-ranges',b'bytes'),(b'content-length',str(end-start+1).encode()),(b'cache-control',b'private, no-store')]+extra))
        if scope.get('method')!='HEAD':
            with open(path,'rb') as f:
                f.seek(start);remaining=end-start+1
                while remaining:
                    block=f.read(min(512*1024,remaining))
                    if not block:break
                    remaining-=len(block)
                    await send(dict(type='http.response.body',body=block,more_body=True))
        await send(dict(type='http.response.body',body=b'',more_body=False))
