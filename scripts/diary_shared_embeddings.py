"""Reviewed diary-vector backfill; plan is read-only, writes require its SHA-256.

Examples (use an absolute env path outside the checkout when appropriate):
  python scripts/diary_shared_embeddings.py plan --for-action link --env dev --output /private/plan.json
  python scripts/diary_shared_embeddings.py link --env dev --plan /private/plan.json --expected-plan-sha256 HASH
  # The command above only reports candidates. Add --commit after review.

Re-plan separately for prune and restore. This tool never changes storage mode,
removes shared rows, changes an existing ready canonical, or prints member data.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

import asyncpg

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from db.envfile import announce, assert_dev_target, is_prod, load_conn, project_ref  # noqa: E402

MODE_KEY = 'diary_embedding_storage_mode'
MODES = {'link': {'private', 'dual'}, 'prune': {'shared'}, 'restore': {'private', 'dual'}}
KEY_FIELDS = ('source_hash', 'embedding_model', 'index_version', 'search_digest', 'vector_digest')

# The original user lock domain is hashtextextended(str(uid), 0), shared with
# begin_subject_deletion and app.core.advisory_lock.advisory_xact_lock.
_USER_LOCK = 'SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))'
_APPROVED = """
((d.kind='capi_day' AND d.source='preset' AND d.title IS NULL AND d.content=m.content)
 OR (d.kind='welcome' AND d.source='welcome' AND (d.title,d.content) IN
     (SELECT * FROM unnest($1::text[],$2::text[]))))
"""
_BASE_FROM = """
FROM public.diary_recall_documents rd
JOIN public.diaries d ON d.user_id=rd.user_id AND d.id=rd.diary_id
LEFT JOIN public.chat_contexts c ON c.user_id=rd.user_id
LEFT JOIN public.privacy_subject_barriers b ON b.user_id=rd.user_id
LEFT JOIN public.moly_life_ments m ON m.id=d.preset_ment_id
LEFT JOIN public.diary_template_embeddings s
  ON s.id=rd.shared_embedding_id AND s.source_hash=rd.source_hash
 AND s.embedding_model=rd.embedding_model AND s.index_version=rd.index_version
 AND s.dimensions=1536 AND s.search_text=rd.search_text AND s.embedding IS NOT NULL
WHERE d.record_status='published' AND d.deleted_at IS NULL
 AND d.published_at IS NOT NULL AND d.published_at<=clock_timestamp()
 AND d.kind IN ('welcome','capi_day','shared_day')
 AND (b.user_id IS NULL OR b.state='active')
 AND rd.suppression_generation=COALESCE(c.memory_generation,0)
 AND rd.source_hash=encode(digest(concat_ws(E'\\n',d.title,d.content),'sha256'),'hex')
 AND rd.search_text=trim(concat_ws(E'\\n',d.title,d.content))
 AND cardinality($1::text[])>=0 AND cardinality($2::text[])>=0
"""
_BASE_SELECT = """
SELECT rd.user_id,rd.diary_id,rd.source_hash,rd.embedding_model,rd.index_version,
 rd.search_text,rd.embedding AS private_embedding,s.embedding AS shared_embedding,
 rd.shared_embedding_id,rd.suppression_generation,COALESCE(b.epoch,0) privacy_epoch,
 rd.updated_at
"""


def base_sql(action: str) -> str:
    # Restore must also cover already-linked historic/withdrawn public revisions.
    # It copies only the existing validated reference; it cannot expand sharing.
    base = _BASE_FROM
    if action == 'restore':
        # Recall intentionally has no current-generation filter. Restore every
        # visible shared-only revision, preserving its original generation;
        # mutate_one still rechecks the captured generation and privacy epoch.
        base = base.replace(' AND rd.suppression_generation=COALESCE(c.memory_generation,0)\n', '')
        # Restore future scheduled publications before an old reader returns;
        # the reader still enforces its independent publication-time filter.
        base = base.replace(' AND d.published_at<=clock_timestamp()', '')
    return _BASE_SELECT + base + (f' AND {_APPROVED}' if action != 'restore' else '')


def action_predicate(action: str) -> str:
    if action == 'link':
        return 'e.private_embedding IS NOT NULL AND e.shared_embedding_id IS NULL'
    if action == 'prune':
        return ('e.private_embedding IS NOT NULL AND e.shared_embedding IS NOT NULL '
                'AND e.private_embedding=e.shared_embedding')
    return 'e.private_embedding IS NULL AND e.shared_embedding IS NOT NULL'


def plan_sql(action: str) -> str:
    base = base_sql(action)
    # Group by the vector's exact round-trippable representation, not its hash.
    # Digests belong only in the public-input plan; mutations compare actual vectors.
    predicate = ('e.private_embedding IS NOT NULL OR e.shared_embedding IS NOT NULL'
                 if action == 'link' else action_predicate(action))
    return f"""
WITH eligible AS MATERIALIZED ({base}), clusters AS (
 SELECT e.source_hash,e.embedding_model,e.index_version,e.search_text,
        COALESCE(e.private_embedding,e.shared_embedding)::text vector_text,count(*) n
 FROM eligible e WHERE {predicate}
 GROUP BY 1,2,3,4,5
), choices AS (
 SELECT g.*,row_number() OVER (
   PARTITION BY g.source_hash,g.embedding_model,g.index_version
   ORDER BY EXISTS (
     SELECT 1 FROM public.diary_template_embeddings t
     WHERE t.source_hash=g.source_hash AND t.embedding_model=g.embedding_model
       AND t.index_version=g.index_version AND t.dimensions=1536
       AND t.search_text=g.search_text AND t.embedding=g.vector_text::public.vector(1536)
   ) DESC,g.n DESC,g.vector_text
 ) rn FROM clusters g
)
SELECT source_hash,embedding_model,index_version,
       encode(digest(search_text,'sha256'),'hex') search_digest,
       encode(digest(vector_text,'sha256'),'hex') vector_digest,n candidate_count
FROM choices WHERE rn=1 ORDER BY source_hash,embedding_model,index_version LIMIT 501
"""


def encode_plan(plan: dict) -> bytes:
    return (json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def load_plan(path: Path, expected: str | None, action: str, ref: str) -> dict:
    raw = path.read_bytes()
    if not expected or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('reviewed plan checksum is required and must match')
    plan = json.loads(raw)
    if plan.get('version') != 1 or plan.get('action') != action or plan.get('project') != ref:
        raise ValueError('plan version/action/project differs')
    if datetime.fromisoformat(plan['cutoff']).tzinfo is None:
        raise ValueError('plan cutoff requires timezone')
    entries = plan.get('entries')
    if not isinstance(entries, list) or len(entries) > 500:
        raise ValueError('plan has too many source keys')
    for row in entries:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in KEY_FIELDS):
            raise ValueError('invalid source key')
        if any(len(row[k]) != 64 or any(c not in '0123456789abcdef' for c in row[k])
               for k in ('source_hash', 'search_digest', 'vector_digest')):
            raise ValueError('invalid source fingerprint')
    return plan


def guard_target(env: str, dsn: str, *, commit: bool, allow_prod: bool) -> None:
    if allow_prod and not is_prod(env):
        raise ValueError('--allow-prod requires an explicit production env')
    if commit and not allow_prod:
        assert_dev_target(env, dsn)
    if project_ref(dsn) == 'unknown':
        raise ValueError('unknown database target')


async def read_mode(conn, *, lock: bool = False) -> str:
    raw = await conn.fetchval('SELECT value FROM public.app_config WHERE key=$1' +
                              (' FOR SHARE' if lock else ''), MODE_KEY)
    mode = json.loads(raw) if isinstance(raw, str) else raw
    if mode is None:
        return 'private'
    if mode not in {'private', 'dual', 'shared'}:
        raise ValueError('invalid diary storage mode')
    return mode


async def prepare_tx(conn) -> None:
    await conn.execute("SET LOCAL lock_timeout='1s'")
    await conn.execute("SET LOCAL statement_timeout='12s'")
    await conn.execute("SET LOCAL idle_in_transaction_session_timeout='15s'")


def key_values(row: dict) -> tuple:
    return tuple(row[k] for k in KEY_FIELDS)


async def seed_canonical(conn, row: dict, welcome: tuple[list[str], list[str]], cutoff: datetime) -> bool:
    # Look up a reviewed representation only. Load candidate ids privately, then
    # enter the shared user-lock domain and recheck all values after any wait.
    query = f"""WITH e AS ({base_sql('link')})
 SELECT user_id,diary_id,privacy_epoch,suppression_generation,updated_at FROM e
 WHERE source_hash=$3 AND embedding_model=$4 AND index_version=$5
 AND encode(digest(search_text,'sha256'),'hex')=$6
 AND encode(digest(COALESCE(private_embedding,shared_embedding)::text,'sha256'),'hex')=$7
 AND updated_at<=$8 ORDER BY user_id,diary_id LIMIT 1"""
    args = (*welcome, *key_values(row), cutoff)
    candidate = await conn.fetchrow(query, *args)
    if candidate is None:
        return False
    await conn.execute(_USER_LOCK, str(candidate['user_id']))
    locked = await conn.fetchrow(f"""{base_sql('link')}
 AND rd.user_id=$3 AND rd.diary_id=$4
 AND COALESCE(b.epoch,0)=$5 AND rd.suppression_generation=$6 AND rd.updated_at=$7
 FOR UPDATE OF rd,d""", *welcome, candidate['user_id'], candidate['diary_id'],
                                candidate['privacy_epoch'], candidate['suppression_generation'],
                                candidate['updated_at'])
    if locked is None:
        return False
    # The vector is an internal value and is never written to a plan or stdout.
    literal = await conn.fetchval("SELECT COALESCE(rd.embedding,s.embedding)::text FROM "
                                  "public.diary_recall_documents rd LEFT JOIN "
                                  "public.diary_template_embeddings s ON s.id=rd.shared_embedding_id "
                                  "WHERE rd.user_id=$1 AND rd.diary_id=$2",
                                  candidate['user_id'], candidate['diary_id'])
    if not literal or hashlib.sha256(literal.encode()).hexdigest() != row['vector_digest']:
        return False
    if (locked['source_hash'], locked['embedding_model'], locked['index_version']) != tuple(
            row[k] for k in KEY_FIELDS[:3]):
        return False
    if hashlib.sha256(locked['search_text'].encode()).hexdigest() != row['search_digest']:
        return False
    await conn.execute("""INSERT INTO public.diary_template_embeddings
 (source_hash,embedding_model,index_version,search_text,embedding)
 VALUES ($1,$2,$3,$4,$5::public.vector(1536))
 ON CONFLICT (source_hash,embedding_model,index_version,dimensions) DO NOTHING""",
                       row['source_hash'], row['embedding_model'], row['index_version'],
                       locked['search_text'], literal)
    # Complete a pending public row, never replace any ready canonical.
    await conn.execute("""UPDATE public.diary_template_embeddings SET
 embedding=$5::public.vector(1536),embedding_repair_attempts=0,updated_at=now()
 WHERE source_hash=$1 AND embedding_model=$2 AND index_version=$3 AND dimensions=1536
 AND search_text=$4 AND embedding IS NULL""", row['source_hash'], row['embedding_model'],
                       row['index_version'], locked['search_text'], literal)
    return bool(await conn.fetchval("""SELECT true FROM public.diary_template_embeddings
 WHERE source_hash=$1 AND embedding_model=$2 AND index_version=$3 AND dimensions=1536
 AND search_text=$4 AND embedding=$5::public.vector(1536) FOR SHARE""",
                                    row['source_hash'], row['embedding_model'], row['index_version'],
                                    locked['search_text'], literal))


def candidate_sql(action: str) -> str:
    # Every commit is limited to the reviewed cutoff; concurrent new documents
    # do not silently expand the data-changing operation.
    return f"""WITH e AS ({base_sql(action)})
 SELECT e.user_id,e.diary_id,e.privacy_epoch,e.suppression_generation,e.updated_at
 FROM e WHERE {action_predicate(action)}
 AND e.source_hash=$3 AND e.embedding_model=$4 AND e.index_version=$5
 AND encode(digest(e.search_text,'sha256'),'hex')=$6
 AND encode(digest(COALESCE(e.private_embedding,e.shared_embedding)::text,'sha256'),'hex')=$7
 AND e.updated_at<=$8
 AND (e.user_id,e.diary_id)>($9::uuid,$10::uuid)
 ORDER BY e.user_id,e.diary_id LIMIT $11"""


async def mutate_one(conn, action: str, row: dict, candidate, welcome) -> bool:
    await conn.execute(_USER_LOCK, str(candidate['user_id']))
    locked = await conn.fetchrow(f"""{base_sql(action)}
 AND rd.user_id=$3 AND rd.diary_id=$4
 AND COALESCE(b.epoch,0)=$5 AND rd.suppression_generation=$6 AND rd.updated_at=$7
 FOR UPDATE OF rd,d""", *welcome, candidate['user_id'], candidate['diary_id'],
                                candidate['privacy_epoch'], candidate['suppression_generation'],
                                candidate['updated_at'])
    if locked is None:
        return False
    if (locked['source_hash'], locked['embedding_model'], locked['index_version']) != tuple(
            row[k] for k in KEY_FIELDS[:3]):
        return False
    if hashlib.sha256(locked['search_text'].encode()).hexdigest() != row['search_digest']:
        return False
    shared = await conn.fetchrow("""SELECT id,embedding::text literal FROM public.diary_template_embeddings
 WHERE source_hash=$1 AND embedding_model=$2 AND index_version=$3 AND dimensions=1536
 AND search_text=$4 AND embedding IS NOT NULL FOR SHARE""",
                                 row['source_hash'], row['embedding_model'], row['index_version'],
                                 locked['search_text'])
    if shared is None or hashlib.sha256(shared['literal'].encode()).hexdigest() != row['vector_digest']:
        return False
    ids = (candidate['user_id'], candidate['diary_id'], shared['id'], shared['literal'])
    # Actual vector equality guards every existing-vector link/prune. A digest
    # collision can at worst cause a skipped row, never a changed representation.
    if action == 'link':
        sql = """UPDATE public.diary_recall_documents SET shared_embedding_id=$3,updated_at=now()
 WHERE user_id=$1 AND diary_id=$2 AND shared_embedding_id IS NULL
 AND embedding=$4::public.vector(1536) RETURNING diary_id"""
    elif action == 'prune':
        sql = """UPDATE public.diary_recall_documents SET embedding=NULL,updated_at=now()
 WHERE user_id=$1 AND diary_id=$2 AND shared_embedding_id=$3
 AND embedding=$4::public.vector(1536) RETURNING diary_id"""
    else:
        sql = """UPDATE public.diary_recall_documents SET embedding=$4::public.vector(1536),
 embedding_repair_attempts=0,updated_at=now()
 WHERE user_id=$1 AND diary_id=$2 AND shared_embedding_id=$3 AND embedding IS NULL RETURNING diary_id"""
    return await conn.fetchval(sql, *ids) is not None


async def apply_plan(conn, plan: dict, *, welcome, batch: int, max_batches: int) -> dict:
    action = plan['action']
    cutoff = datetime.fromisoformat(plan['cutoff'])
    counts = {'changed': 0, 'skipped_after_lock': 0, 'batches': 0, 'more': False}
    for key in plan['entries']:
        if action == 'link':
            async with conn.transaction():
                await prepare_tx(conn)
                if await read_mode(conn, lock=True) not in MODES[action]:
                    raise ValueError('storage mode does not allow this operation')
                if not await seed_canonical(conn, key, welcome, cutoff):
                    continue
        cursor = ('00000000-0000-0000-0000-000000000000',) * 2
        while counts['batches'] < max_batches:
            async with conn.transaction():
                await prepare_tx(conn)
                if await read_mode(conn, lock=True) not in MODES[action]:
                    raise ValueError('storage mode does not allow this operation')
                rows = await conn.fetch(candidate_sql(action), *welcome, *key_values(key), cutoff,
                                        *cursor, batch)
                for candidate in rows:
                    if await mutate_one(conn, action, key, candidate, welcome):
                        counts['changed'] += 1
                    else:
                        counts['skipped_after_lock'] += 1
                if rows:
                    cursor = (rows[-1]['user_id'], rows[-1]['diary_id'])
            if rows:
                counts['batches'] += 1
            if len(rows) < batch:
                break
        else:
            counts['more'] = True
            break
    # A successful batch is not proof that an older reader can be restored.
    # Include references outside this plan and stale-generation/ineligible rows.
    missing = await conn.fetchval("SELECT count(*) FROM public.diary_recall_documents "
                                  "WHERE shared_embedding_id IS NOT NULL AND embedding IS NULL")
    counts['remaining_shared_only'] = int(missing)
    counts['legacy_rollback_ready'] = missing == 0
    return counts


async def run(args) -> dict:
    dsn = load_conn(args.env)
    guard_target(args.env, dsn, commit=args.commit, allow_prod=args.allow_prod)
    announce(args.env, dsn, commit=args.commit)
    # Lazy import keeps CLI argument/target guards before application settings.
    from app.services.diary_shared_embeddings import approved_welcome_pairs
    pairs = approved_welcome_pairs()
    welcome = ([x[0] for x in pairs], [x[1] for x in pairs])
    conn = await asyncpg.connect(dsn, statement_cache_size=0, timeout=10,
                                server_settings={'application_name': 'moly-diary-sharing'})
    try:
        if args.action == 'plan':
            if args.commit or not args.output:
                raise ValueError('plan requires --output and cannot --commit')
            async with conn.transaction(readonly=True, isolation='repeatable_read'):
                await prepare_tx(conn)
                cutoff = await conn.fetchval('SELECT now()')
                rows = await conn.fetch(plan_sql(args.for_action), *welcome)
                if len(rows) > 500:
                    raise ValueError('too many source keys; narrow the reviewed corpus')
                plan = {'version': 1, 'action': args.for_action, 'project': project_ref(dsn),
                        'cutoff': cutoff.isoformat(), 'entries': [dict(x) for x in rows]}
            raw = encode_plan(plan)
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
            os.chmod(args.output, 0o600)
            return {'action': 'plan', 'for_action': args.for_action, 'source_keys': len(rows),
                    'candidate_count': sum(x['candidate_count'] for x in rows),
                    'plan_sha256': hashlib.sha256(raw).hexdigest(), 'mutations': 0}
        if not args.plan:
            raise ValueError('operation requires a reviewed --plan')
        plan = load_plan(args.plan, args.expected_plan_sha256, args.action, project_ref(dsn))
        if not args.commit:
            return {'action': args.action, 'source_keys': len(plan['entries']),
                    'commit': False, 'mutations': 0}
        return {'action': args.action, 'commit': True,
                **await apply_plan(conn, plan, welcome=welcome, batch=args.batch,
                                   max_batches=args.max_batches)}
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'link', 'prune', 'restore'), nargs='?', default='plan')
    parser.add_argument('--for-action', choices=('link', 'prune', 'restore'), default='link')
    parser.add_argument('--env', default='dev')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--allow-prod', action='store_true')
    parser.add_argument('--batch', type=int, default=50)
    parser.add_argument('--max-batches', type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.batch <= 200 or not 1 <= args.max_batches <= 100:
        parser.error('batch must be 1..200, max-batches 1..100')
    try:
        result = asyncio.run(run(args))
    except Exception as exc:
        # Driver exceptions can contain bind values or connection credentials.
        print(f'Operation stopped ({type(exc).__name__}); current transaction rolled back')
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
