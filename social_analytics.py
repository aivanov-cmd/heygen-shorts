"""Additive analytics storage and ASGI routes for the existing video MCP service.

No publication code or platform credentials are changed by this module.
"""
import asyncio
import hmac
import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs

SCHEMA = """
CREATE TABLE IF NOT EXISTS social_analytics_samples (
 id BIGSERIAL PRIMARY KEY,
 project_id INTEGER NOT NULL,
 short_id INTEGER NOT NULL,
 platform TEXT NOT NULL CHECK(platform IN ('youtube','instagram')),
 media_id TEXT NOT NULL,
 sample_key TEXT NOT NULL,
 observed_at TIMESTAMPTZ NOT NULL,
 published_at TIMESTAMPTZ,
 status TEXT NOT NULL CHECK(status IN ('ok','partial','error')),
 payload JSONB NOT NULL,
 UNIQUE(project_id,platform,media_id,sample_key)
);
CREATE INDEX IF NOT EXISTS social_analytics_lookup
 ON social_analytics_samples(project_id,platform,media_id,observed_at DESC);
CREATE INDEX IF NOT EXISTS social_analytics_time
 ON social_analytics_samples(observed_at DESC);
"""


def ensure_analytics_schema(get_db):
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(721406, 20)")
            cur.execute(SCHEMA)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def int_range(value, low, high, name):
    if isinstance(value, bool):
        raise ValueError(name)
    result = int(value)
    if result < low or result > high:
        raise ValueError(name)
    return result


def iso_datetime(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timestamp requires timezone')
    return dt.astimezone(timezone.utc)


def clean_json(value):
    """Restrict persistence to normalized data; never store API headers or tokens."""
    if isinstance(value, dict):
        return {str(k): clean_json(v) for k, v in value.items()
                if not any(s in str(k).lower() for s in
                           ('token', 'authorization', 'secret', 'headers', 'api_key'))}
    if isinstance(value, list):
        return [clean_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def targets(get_db, lookback_days=90, limit=2000):
    days = int_range(lookback_days, 1, 365, 'lookback_days')
    limit = int_range(limit, 1, 5000, 'limit')
    conn = get_db()
    try:
        with conn.cursor() as cur:
            # Publication time is obtained from the platform, not the planned slot.
            cur.execute("""
              SELECT s.id,s.project_id,s.topic,s.youtube_title,s.script,
                     s.created_at,s.youtube_video_id,s.youtube_status,
                     s.instagram_media_id,s.instagram_status
              FROM shorts s
              WHERE s.project_id BETWEEN 1 AND 6
                AND s.created_at >= NOW()-(%s * INTERVAL '1 day')
                AND ((s.youtube_status='published' AND COALESCE(s.youtube_video_id,'')<>'')
                  OR (s.instagram_status='published' AND COALESCE(s.instagram_media_id,'')<>''))
              ORDER BY s.id DESC LIMIT %s
            """, (days, limit + 1))
            rows = cur.fetchall()
        truncated = len(rows) > limit
        items = []
        seen = set()
        for r in rows[:limit]:
            for platform, media_id, status in [('youtube', r[6], r[7]), ('instagram', r[8], r[9])]:
                if status != 'published' or not media_id:
                    continue
                key = (r[1], platform, str(media_id))
                if key in seen:
                    continue
                seen.add(key)
                items.append(dict(short_id=r[0], project_id=r[1], platform=platform,
                                  media_id=str(media_id), topic=r[2], title=r[3],
                                  script=r[4], created_at=r[5].isoformat() if r[5] else None))
        return dict(items=items, count=len(items), truncated=truncated,
                    selection='shorts.created_at', lookback_days=days)
    finally:
        conn.close()


def validate_sample(item):
    if not isinstance(item, dict):
        raise ValueError('Invalid sample')
    project = int_range(item.get('project_id'), 1, 6, 'project_id')
    short = int_range(item.get('short_id'), 1, 2147483647, 'short_id')
    platform = item.get('platform')
    if platform not in ('youtube', 'instagram'):
        raise ValueError('platform')
    media = str(item.get('media_id', ''))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', media):
        raise ValueError('media_id')
    key = str(item.get('sample_key', ''))
    if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,150}', key):
        raise ValueError('sample_key')
    observed = iso_datetime(item.get('observed_at'))
    if observed > datetime.now(timezone.utc) + timedelta(minutes=10):
        raise ValueError('Future observation')
    published = iso_datetime(item['published_at']) if item.get('published_at') else None
    status = item.get('status')
    if status not in ('ok', 'partial', 'error'):
        raise ValueError('status')
    payload = {k: item.get(k) for k in ('metrics', 'daily', 'errors', 'metadata',
                                     'window_start', 'window_end', 'daily_timezone')}
    encoded = json.dumps(clean_json(payload), ensure_ascii=False, allow_nan=False)
    if len(encoded.encode()) > 200000:
        raise ValueError('Sample too large')
    return project, short, platform, media, key, observed, published, status, encoded


def save_samples(get_db, body):
    items = body.get('samples') if isinstance(body, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 1000:
        raise ValueError('Expected samples list, 1..1000')
    validated = [validate_sample(x) for x in items]
    conn = get_db()
    try:
        with conn.cursor() as cur:
            for values in validated:
                project, short, platform, media = values[:4]
                cur.execute("""SELECT youtube_video_id,instagram_media_id FROM shorts
                               WHERE id=%s AND project_id=%s FOR SHARE""", (short, project))
                target = cur.fetchone()
                if not target or str(target[0 if platform == 'youtube' else 1]) != media:
                    raise ValueError('Publication does not belong to this project/short')
                cur.execute("""INSERT INTO social_analytics_samples
                  (project_id,short_id,platform,media_id,sample_key,observed_at,published_at,status,payload)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                  ON CONFLICT(project_id,platform,media_id,sample_key) DO UPDATE SET
                   observed_at=EXCLUDED.observed_at,published_at=EXCLUDED.published_at,
                   status=EXCLUDED.status,payload=EXCLUDED.payload
                """, values)
        conn.commit()
        return dict(saved=len(validated))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def read_samples(get_db, project_id, platform='', days=30, limit=100, latest=True):
    project = int_range(project_id, 1, 6, 'project_id')
    days = int_range(days, 1, 365, 'days')
    limit = int_range(limit, 1, 1000, 'limit')
    if platform not in ('', 'youtube', 'instagram'):
        raise ValueError('platform')
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT a.short_id,a.platform,a.media_id,a.observed_at,
                         a.published_at,a.status,a.payload,s.topic,s.youtube_title,s.script
              FROM social_analytics_samples a JOIN shorts s
                ON s.id=a.short_id AND s.project_id=a.project_id
              WHERE a.project_id=%s AND (%s='' OR a.platform=%s)
                AND a.observed_at >= NOW()-(%s * INTERVAL '1 day')
              ORDER BY a.observed_at DESC,a.id DESC
            """, (project, platform, platform, days))
            rows = cur.fetchall()
        result, seen = [], set()
        for r in rows:
            key = r[1], r[2]
            if latest and key in seen:
                continue
            seen.add(key)
            payload = r[6] if isinstance(r[6], dict) else json.loads(r[6])
            result.append(dict(short_id=r[0], platform=r[1], media_id=r[2],
                               observed_at=r[3].isoformat(),
                               published_at=r[4].isoformat() if r[4] else None,
                               age_hours=round((r[3]-r[4]).total_seconds()/3600,2) if r[4] else None,
                               status=r[5], topic=r[7], title=r[8], script=r[9], **payload))
            if len(result) >= limit:
                break
        return dict(project_id=project, days=days, selection='observation dates',
                    latest_only=latest, items=result,
                    warning='Lifetime snapshots are not period totals. Daily YouTube dates use America/Los_Angeles; absent rows are not confirmed zero. Instagram reach must not be summed as unique account reach.')
    finally:
        conn.close()


def register_analytics_tools(mcp, get_db):
    @mcp.tool()
    def get_social_video_stats(project_id: int, platform: str = '', days: int = 30,
                               limit: int = 50, history: bool = False) -> dict:
        """Read stored video statistics and scripts. Read-only; does not call social APIs.
        days filters observation dates, not lifetime metrics. history returns multiple
        snapshots; never sum cumulative snapshots. No exact 24h checkpoint guarantee.
        """
        return read_samples(get_db, project_id, platform, days, limit, not history)

    @mcp.tool()
    def get_social_project_summary(project_id: int, days: int = 30) -> dict:
        """Read collection health and latest per-video snapshots, not channel period totals."""
        data = read_samples(get_db, project_id, days=days, limit=1000)
        statuses = {s: sum(x['status'] == s for x in data['items']) for s in ('ok','partial','error')}
        data['collection_status_counts'] = statuses
        data['not_available'] = ['Shorts viewed/swiped away', 'Instagram recommendation eligibility',
                                 'unique account reach', 'channel period totals']
        return data


class AnalyticsMiddleware:
    """Wrap the current service; all non-analytics paths and lifespan pass through."""
    def __init__(self, app, get_db):
        self.app, self.get_db = app, get_db

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        if scope.get('type') != 'http' or not path.startswith('/social-analytics/'):
            return await self.app(scope, receive, send)
        async def respond(status, body):
            encoded = json.dumps(body, ensure_ascii=False).encode()
            await send(dict(type='http.response.start', status=status,
                            headers=[(b'content-type', b'application/json; charset=utf-8'),
                                     (b'cache-control', b'no-store')]))
            await send(dict(type='http.response.body', body=encoded))
        headers = {k.lower(): v for k, v in scope.get('headers', [])}
        expected = os.environ.get('SOCIAL_ANALYTICS_API_KEY', '')
        actual = headers.get(b'x-api-key', b'')
        if not expected or not hmac.compare_digest(actual, expected.encode('utf-8')):
            return await respond(401, dict(error='Unauthorized'))
        method = scope.get('method', 'GET')
        try:
            if path == '/social-analytics/targets' and method == 'GET':
                query = parse_qs(scope.get('query_string', b'').decode())
                result = await asyncio.to_thread(targets, self.get_db,
                    query.get('lookback_days', [90])[0], query.get('limit', [2000])[0])
            elif path == '/social-analytics/samples' and method == 'POST':
                body = bytearray()
                while True:
                    chunk = await receive()
                    if chunk.get('type') == 'http.disconnect':
                        return
                    body.extend(chunk.get('body', b''))
                    if len(body) > 4000000:
                        return await respond(413, dict(error='Request too large'))
                    if not chunk.get('more_body'):
                        break
                result = await asyncio.to_thread(save_samples, self.get_db, json.loads(body))
            else:
                return await respond(404, dict(error='Unknown analytics route or method'))
            return await respond(200, result)
        except (ValueError, TypeError, KeyError, OverflowError):
            return await respond(400, dict(error='Invalid analytics input or publication identity'))
        except Exception:
            # Do not return DB credentials or connection strings from exceptions.
            return await respond(503, dict(error='Analytics storage unavailable; check deployment and database'))
