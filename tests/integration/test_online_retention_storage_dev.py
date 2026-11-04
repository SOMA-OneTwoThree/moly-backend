"""Existing dev DB: private drafts, live tails and job claims never alter public readers."""
import json
import uuid

import pytest
from sqlalchemy import text

from app.services import checkpoint as cp, checkpoint_repo as repo, context_retention as retention
from app.services import jobs, interaction_contract, retention_release as storage
from app.services.retention_preparation_context import Binding, use_binding
from app.services import retention_release_promotion as promotion
from tests.integration.test_checkpoint_continuity_dev import checkpoint_db as checkpoint_db
from tests.integration.test_guard_context_release_prepare_dev import seed_release


async def preparation(session):
    identity = uuid.uuid4()
    await session.execute(text('INSERT INTO retention_preparations(id,code_sha) VALUES(:p,:s)'),
                          {'p': identity, 's': 'a'*40})
    return identity


async def prepared_checkpoint(session, uid, identity, plan):
    source = [cp.SourceMessage(m['id'],m['sender'],m['kind'],m['content'],m['created_at']) for m in plan.messages]
    state = retention.empty_state()
    state['sequence'] = 1
    proof = cp.completed_progress_proof(previous=None, messages=source, language=plan.user['language'], state=state)
    source_rows = (await session.execute(text("SELECT id,sender,content,activity_date FROM messages WHERE user_id=:u AND kind='normal' AND id<=:t ORDER BY id"), {'u':uid,'t':plan.through})).all()
    with use_binding(Binding(identity, True)):
        await storage.store_contract(session, user_id=uid, generation=0, locale=plan.user['language'],
            items=[], rendered='', digest=interaction_contract.document_hash('[]'), through=plan.through,
            source_rows=source_rows, after=0, draft_items=[{'unapplied': True}])
        value = await repo.insert(session, user_id=uid, through_message_id=plan.through,
            summary=retention.render_state(state), source_hash=cp.source_hash(previous=None,
                messages=source, version=cp.RETENTION_SUMMARIZER_VERSION, preparation_id=identity),
            expected_generation=0, version=cp.RETENTION_SUMMARIZER_VERSION,
            state_json=state, validation_json={'processing_complete':True,
              'source_message_ids':[m.id for m in source],'completed_proof':proof}, coverage_from=source[0].id)
        assert value is not None
    return value


async def test_private_projection_never_changes_legacy_reader_or_public_cursors(checkpoint_db):
    session, (uid, _) = checkpoint_db
    _, _, plan = await seed_release(session, uid)
    identity = await preparation(session)
    old = await repo.insert(session, user_id=uid, through_message_id=plan.through,
        summary='Public legacy summary',source_hash='b'*64,expected_generation=0)
    await session.execute(text("""INSERT INTO user_interaction_contracts
      (user_id,locale,version,document_json,rendered_text,render_hash,status)
      VALUES(:u,:locale,1,'[]'::jsonb,'public sentinel',:hash,'published')"""),
      {'u':uid,'locale':plan.user['language'],'hash':'d'*64})
    before = (await session.execute(text('SELECT anchor_message_id,pending_anchor_message_id,contract_generation,contract_processed_through_message_id FROM chat_contexts WHERE user_id=:u'),{'u':uid})).one()
    made = await prepared_checkpoint(session, uid, identity, plan)
    with use_binding(None):
        assert (await repo.load_latest(session,uid,retention_enabled=False)).id == old
        assert await repo.load_latest(session,uid,retention_enabled=True) is None
    with use_binding(Binding(identity,True)):
        assert (await repo.load_latest(session,uid,retention_enabled=True)).id == made
        assert await storage.contract_cursor(session,user_id=uid,generation=0) == plan.through
    assert (await session.execute(text('SELECT anchor_message_id,pending_anchor_message_id,contract_generation,contract_processed_through_message_id FROM chat_contexts WHERE user_id=:u'),{'u':uid})).one() == before
    assert await session.scalar(text("SELECT rendered_text FROM user_interaction_contracts WHERE user_id=:u AND status='published'"),{'u':uid}) == 'public sentinel'
    assert await session.scalar(text("SELECT count(*) FROM retention_prepared_contracts WHERE preparation_id=:p AND status='draft'"),{'p':identity}) == 1


async def test_new_completed_live_turn_and_derived_replacement_do_not_change_fixed_raw_prefix(checkpoint_db):
    session, (uid, _) = checkpoint_db
    rid, _, plan = await seed_release(session,uid)
    before = promotion.prefix_record(plan)
    await session.execute(text("UPDATE mem0_memory_registry SET semantic_status='superseded' WHERE id=:r"),{'r':rid})
    await session.execute(text("""INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
      VALUES(:u,'user','normal','New live message',CURRENT_DATE,2,1),
            (:u,'moly','normal','New live response',CURRENT_DATE,2,2)"""),{'u':uid})
    await session.execute(text('UPDATE chat_contexts SET last_committed_turn_seq=2 WHERE user_id=:u'),{'u':uid})
    row = (await session.execute(text(str(promotion._USERS).replace('ORDER BY p.id','WHERE p.id=:uid')),{'uid':uid})).mappings().one()
    prefix = await promotion.read_user(session,dict(row),through_id=plan.through)
    assert promotion.prefix_record(prefix) == before
    latest = await promotion.read_user(session,dict(row))
    assert latest.through > plan.through


async def test_exact_preparation_claim_cannot_take_other_stage_or_live_memory_jobs(checkpoint_db):
    session,(uid,_) = checkpoint_db
    first, second = await preparation(session), await preparation(session)
    ids={}
    for label, prep, kind in [('normal',None,'conversation_checkpoint'),('mine',first,'conversation_checkpoint'),
                              ('other',second,'conversation_checkpoint'),('fact',None,'mem0_ingest')]:
        payload={} if prep is None else {'retention_preparation_id':str(prep),'retention_preparation_building':True}
        ids[label]=await session.scalar(text("""INSERT INTO async_jobs(queue,job_type,user_id,dedup_key,payload,max_attempts)
          VALUES('content',:kind,:u,:key,CAST(:body AS jsonb),3) RETURNING id"""),
          {'kind':kind,'u':uid,'key':str(uuid.uuid4()),'body':json.dumps(payload)})
    # The production predicate is exercised against PostgreSQL, scoped only to
    # this fixture's owner so unrelated development jobs are never claimed.
    sql=text(str(jobs._CLAIM_SQL).replace("WHERE queue=:queue", "WHERE queue=:queue AND user_id=:only_user"))
    params={'queue':'content','only_user':uid,'worker_id':'synthetic','batch_size':10,'lease_seconds':30,'preparation_id':None}
    public=(await session.execute(sql,params)).mappings().all()
    assert {r['id'] for r in public} == {ids['normal'],ids['fact']}
    params['preparation_id']=str(first)
    private=(await session.execute(sql,params)).mappings().all()
    assert {r['id'] for r in private} == {ids['mine']}
    assert await session.scalar(text('SELECT state FROM async_jobs WHERE id=:id'),{'id':ids['other']}) == 'ready'


async def test_same_source_in_different_preparations_has_distinct_hash_and_draft_identity(checkpoint_db):
    session,(uid,_) = checkpoint_db
    _,_,plan=await seed_release(session,uid)
    first,second=await preparation(session),await preparation(session)
    a=await prepared_checkpoint(session,uid,first,plan)
    b=await prepared_checkpoint(session,uid,second,plan)
    assert a != b
    await session.execute(text("UPDATE retention_preparations SET state='aborted' WHERE id=:p"),{'p':first})
    with use_binding(Binding(first,True)):
        with pytest.raises(storage.ReleaseError):
            await repo.load_latest(session,uid,retention_enabled=True)
    with use_binding(Binding(second,True)):
        assert (await repo.load_latest(session,uid,retention_enabled=True)).id == b


@pytest.mark.parametrize('fault', [None, 'generation', 'pipeline', 'epoch', 'facts',
    'contract_cursor', 'contract_generation', 'planned', 'pending', 'canonical', 'unsequenced'])
async def test_bulk_completion_preserves_every_single_user_failure_boundary(checkpoint_db, fault):
    from tests.integration.test_provided_memory_dev import CANONICAL
    session, (uid, _) = checkpoint_db
    rid, _, plan = await seed_release(session, uid)
    identity = await preparation(session)
    await session.execute(text('UPDATE mem0_memory_registry SET canonical_text=:body WHERE id=:r'),
                          {'r':rid, 'body':CANONICAL})
    await prepared_checkpoint(session, uid, identity, plan)
    changes = {
        'generation':'UPDATE chat_contexts SET memory_generation=1 WHERE user_id=:u',
        'pipeline':'DELETE FROM memory_pipeline_states WHERE user_id=:u',
        'epoch':'UPDATE memory_pipeline_states SET privacy_epoch=1 WHERE user_id=:u',
        'facts':'UPDATE memory_pipeline_states SET consolidated_through_turn_seq=0 WHERE user_id=:u',
        'contract_cursor':'UPDATE retention_prepared_contracts SET processed_through_message_id=0 WHERE user_id=:u',
        'contract_generation':'UPDATE retention_prepared_contracts SET memory_generation=1 WHERE user_id=:u',
        'planned':"UPDATE mem0_ingest_candidates SET status='planned' WHERE user_id=:u",
        'pending':"UPDATE mem0_memory_registry SET semantic_status='pending' WHERE user_id=:u",
        'canonical':'UPDATE mem0_memory_registry SET canonical_text=NULL WHERE user_id=:u',
        'unsequenced':"UPDATE messages SET turn_seq=NULL WHERE user_id=:u AND sender='user'",
    }
    if fault:
        await session.execute(text(changes[fault]), {'u':uid})
    manifest = {'users': {str(uid): {'through':plan.through,'generation':0,'epoch':0}}}
    assert await promotion.processing_set_complete(session, identity, manifest) is (fault is None)


async def test_bulk_completion_excluded_only_range_needs_no_pipeline_or_contract(checkpoint_db):
    session, (uid, _) = checkpoint_db
    _, _, plan = await seed_release(session, uid)
    identity = await preparation(session)
    await session.execute(text("UPDATE messages SET kind='safety' WHERE user_id=:u"), {'u':uid})
    await session.execute(text('DELETE FROM memory_pipeline_states WHERE user_id=:u'), {'u':uid})
    manifest = {'users': {str(uid): {'through':plan.through,'generation':0,'epoch':0}}}
    assert await promotion.processing_set_complete(session, identity, manifest)


async def test_bulk_completion_never_accepts_missing_context_and_null_generation(checkpoint_db):
    session, (uid, _) = checkpoint_db
    _, _, plan = await seed_release(session, uid)
    identity = await preparation(session)
    await session.execute(text("UPDATE messages SET kind='safety' WHERE user_id=:u"), {'u':uid})
    await session.execute(text('DELETE FROM chat_contexts WHERE user_id=:u'), {'u':uid})
    manifest = {'users': {str(uid): {'through':plan.through,'generation':None,'epoch':0}}}
    assert not await promotion.processing_set_complete(session, identity, manifest)


async def test_sealed_ready_checkpoint_stays_private_and_is_readable_after_pointer_activation(checkpoint_db):
    session, (uid, _) = checkpoint_db
    from tests.integration.test_provided_memory_dev import CANONICAL
    rid, _, plan = await seed_release(session, uid)
    await session.execute(text('UPDATE mem0_memory_registry SET canonical_text=:body WHERE id=:r'),
                          {'r':rid,'body':CANONICAL})
    identity = await preparation(session)
    staged = await prepared_checkpoint(session, uid, identity, plan)
    await session.execute(text("UPDATE retention_preparations SET manifest_json=CAST(:m AS jsonb) WHERE id=:p"),
        {'p':identity,'m':json.dumps(promotion.live_manifest([plan],[],project_ref='synthetic',code_sha='a'*40))})
    await promotion.seal_preparation(session, identity)
    assert await session.scalar(text("SELECT publish_state FROM conversation_checkpoints WHERE id=:c"),{'c':staged}) == 'ready'
    with use_binding(None):
        assert await repo.load_latest(session,uid,retention_enabled=True) is None
    await session.execute(text("UPDATE retention_preparations SET state='active' WHERE id=:p"),{'p':identity})
    with use_binding(Binding(identity,False)):
        latest = await repo.load_latest(session,uid,retention_enabled=True)
        assert latest.id == staged
        coverage = await repo.coverage_for_checkpoint(session,user_id=uid,generation=0,value=latest)
        assert coverage is not None and coverage.through_message_id == plan.through
        assert await storage.contract_cursor(session,user_id=uid,generation=0) == plan.through
        assert await storage.validate_contract_chain(session,user_id=uid,generation=0,through_id=plan.through)


async def test_first_live_contract_atomically_supersedes_private_ready_head(checkpoint_db):
    session, (uid, _) = checkpoint_db
    _, _, plan = await seed_release(session, uid)
    identity = await preparation(session)
    await prepared_checkpoint(session, uid, identity, plan)
    await session.execute(text("UPDATE retention_preparations SET state='active' WHERE id=:p"),{'p':identity})
    await session.execute(text("""INSERT INTO messages(user_id,sender,kind,content,activity_date,turn_seq,turn_position)
      VALUES(:u,'user','normal','New synthetic preference',CURRENT_DATE,2,1),
            (:u,'moly','normal','Synthetic acknowledgement',CURRENT_DATE,2,2)"""), {'u':uid})
    sources = (await session.execute(text("SELECT id,sender,content,activity_date FROM messages WHERE user_id=:u AND id>:after ORDER BY id"),
        {'u':uid,'after':plan.through})).all()
    with use_binding(Binding(identity,False)):
        await storage.store_contract(session,user_id=uid,generation=0,locale=plan.user['language'],
            items=[],rendered='',digest=interaction_contract.document_hash('[]'),through=sources[-1][0],
            source_rows=sources,after=plan.through)
        assert await storage.validate_contract_chain(session,user_id=uid,generation=0,through_id=sources[-1][0])
    states = (await session.execute(text("SELECT status,count(*) FROM retention_prepared_contracts WHERE preparation_id=:p AND status<>'draft' GROUP BY status"),{'p':identity})).all()
    assert dict(states) == {'superseded':1,'published':1}
