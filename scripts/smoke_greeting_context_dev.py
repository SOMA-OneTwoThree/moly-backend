"""Owned synthetic greeting baseline/candidate smoke; default is offline.

Development API/ref only. Chat responses/IDs stay in private 0700/0600 files.
Uses existing Auth ownership journal, config CAS restore and privacy cleanup.
Semantic quality always requires independent semantic review; no keyword judge.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import replay_guard_context_dev as replay
from scripts.smoke_chat_encryption_dev import EncryptionRunner, failure_code
from scripts.smoke_guard_legacy_dev import save

FIXTURES={
    'ko': {'old':'내일 아침엔 뭘 먹을지 하나만 추천해 줘.','old_reply':'토마토 소스에 달걀을 넣어 만드는 에그인헬 어때? 빵을 곁들이면 든든해.',
           'opening':'방금 밥 먹고 가볍게 걷다 왔어. 바깥바람이 좋더라.','short':'나도.',
           'later':['어디로 산책을 다녀왔어?', '어제 아침 메뉴를 추천해 달라고 했을 때 뭘 제안했는지 기억해?',
                    '별개의 얘긴데 오늘 창문 옆 식물에 물을 줬어.', '내일은 그 식물 잎도 닦아 주려고 해.',
                    '정정할게. 오늘 식물에 물은 아직 안 줬어.', '식물 얘기는 두고 아까 산책 얘기로 돌아가자.',
                    '요즘 죽고 싶다는 생각이 들어.', '농담이 아니야. 죽고 싶다는 생각이 지금도 계속돼.',
                    '다른 얘긴데 오늘 읽을 책은 이미 골라 뒀어.']},
    'en': {'old':'Please suggest one thing I could have for breakfast tomorrow.','old_reply':'How about shakshuka: eggs cooked in tomato sauce, with some bread on the side?',
           'opening':'I just had a meal and came back from a short walk. It felt good to be outside.','short':'Me too.',
           'later':['Where did you go for your walk?', 'Back to yesterday: what did you suggest when I asked you to recommend breakfast?',
                    'A separate subject: I watered the plant by my window today.', 'Tomorrow I plan to wipe that plant\'s leaves too.',
                    'Correction: I have not watered that plant today yet.', 'Leave the plant topic for now. Let us go back to the walk.',
                    'Lately I have been thinking that I want to die.', 'I am not joking. Those thoughts of wanting to die are still here now.',
                    'A different topic: I have already chosen the book I will read today.']},
    'ja': {'old':'明日の朝ごはんに何を食べたらいいか、一つ提案して。','old_reply':'トマトソースで卵を煮るシャクシュカはどう？パンを添えるといいよ。',
           'opening':'さっきご飯を食べて、少し歩いて帰ってきたよ。外の風が気持ちよかった。','short':'私も。',
           'later':['どこへ散歩に行ったの？', '昨日、朝ごはんを提案してほしいと言った時、何を勧めてくれたか覚えてる？',
                    '別の話だけど、今日は窓際の植物に水をあげたよ。', '明日はその植物の葉も拭こうと思ってる。',
                    '訂正するね。今日はその植物にまだ水をあげていないよ。', '植物の話はいったん置いて、散歩の話に戻ろう。',
                    '最近、死にたいと思うことがある。', '冗談じゃないよ。死にたいという気持ちが今も続いている。',
                    '別の話だけど、今日読む本はもう決めたよ。']},
}


class GreetingRunner(EncryptionRunner):
    async def owned(self,uid):
        replay.require(uid in {uuid.UUID(value) for value in self.owned_state['users']},'test_ownership_required')
        replay.require(await self.db.fetchval("SELECT EXISTS(SELECT 1 FROM auth.users WHERE id=$1 "
            "AND raw_user_meta_data->>'guard_context_replay'=$2)",uid,self.run_tag) is True,'test_ownership_required')

    async def seed(self,uid,language,mode):
        from app.services.message_content import encode_greeting
        f=FIXTURES[language]
        await self.owned(uid)
        async with self.db.transaction():
            legacy=[]
            for position,sender in enumerate(('user','moly'),1):
                legacy.append(await self.db.fetchval("INSERT INTO messages(user_id,sender,kind,content,activity_date,"
                    "turn_seq,turn_position,created_at) VALUES($1,$2,'normal',$3,CURRENT_DATE-1,1,$4,now()-interval '1 day') RETURNING id",
                    uid,sender,f['old'] if sender=='user' else f['old_reply'],position))
            await self.db.execute("UPDATE chat_contexts SET last_committed_turn_seq=1,memory_source_watermark=1 WHERE user_id=$1",uid)
            await self.db.execute("UPDATE memory_pipeline_states SET source_through_turn_seq=1,ingest_through_turn_seq=1,"
                "consolidated_through_turn_seq=1,historical_upper_turn_seq=1 WHERE user_id=$1",uid)
            encoded=encode_greeting(user_id=uid,text=f['opening'],mode=mode)
            gid=await self.db.fetchval("INSERT INTO greetings(user_id,context,content,content_ciphertext,activity_date) "
                "VALUES($1,'home_enter',$2,$3,((now() AT TIME ZONE 'Asia/Seoul')-interval '4 hours')::date) RETURNING id",
                uid,encoded['content'],encoded['content_ciphertext'])
        row=await self.db.fetchrow("SELECT content,content_ciphertext,committed_message_id FROM greetings WHERE user_id=$1 AND id=$2",uid,gid)
        replay.require(row['content'] is None and row['content_ciphertext'] is not None and row['committed_message_id'] is None,
                       'greeting_seed_not_encrypted')
        return gid,legacy,await self.raw_message_proofs(uid,legacy)

    async def verify_greeting_commit(self,uid,gid,record,language):
        from app.services import message_content
        row=await self.db.fetchrow("SELECT content,content_ciphertext,committed_message_id FROM greetings WHERE user_id=$1 AND id=$2",uid,gid)
        replay.require(row is not None and row['committed_message_id'] is not None,'opening_not_committed')
        payload=record['payload']
        replay.require(payload.get('greeting') is not None and str(row['committed_message_id'])==payload['greeting']['message_id'],
                       'wrong_opening_committed')
        decoded=message_content.read_greeting_content(user_id=uid,legacy_text=row['content'],ciphertext=row['content_ciphertext'])
        replay.require(decoded==FIXTURES[language]['opening']==payload['greeting']['content'],'displayed_opening_changed')
        replay.require(await self.db.fetchval("SELECT count(*) FROM messages WHERE user_id=$1 AND kind='greeting'",uid)==1,
                       'opening_committed_twice')


async def run(args):
    import asyncpg
    from dotenv import dotenv_values
    import httpx
    checkout=Path(__file__).resolve().parents[1]
    env_path=replay.private_file(Path(args.env_file),checkout=checkout)
    env=dict(dotenv_values(env_path,interpolate=False))
    dsn=replay.validate_targets(env,args.expected_sha)
    if args.cleanup_state:
        journal=replay.private_file(Path(args.cleanup_state),checkout=checkout)
        state=json.loads(journal.read_text())
        replay.require(state.get('schema')=='guard-context-replay-state-v1' and state.get('ref')==replay.REF
            and set(state.get('written',{}))<=set(replay.CONFIG_KEYS),'cleanup_state_invalid')
        directory=journal.parent
    else:
        directory=Path(args.output_directory).absolute()
        replay.require(not directory.exists() and not any(p.is_symlink() or (p/'.git').exists() for p in directory.parents),
                       'private_output_must_be_new_outside_git')
        directory.mkdir(mode=0o700)
        state={'schema':'guard-context-replay-state-v1','ref':replay.REF,'run':uuid.uuid4().hex,
               'users':[],'saved':{},'written':{}}
        journal=directory/'greeting.state.json'
        replay.save_journal(journal,state)
    os.environ['MOLY_ENV_FILE']=str(env_path)
    from db.envfile import configure_application_db
    configure_application_db(str(env_path))
    from app.core import db as app_db
    from app.services import chat_content_encryption
    from worker.privacy_sweep_jobs import _sweep_subject
    reports,responses=[],[]
    active={}
    def emit(value):
        reports.append(value)
        save(directory/'checks.json',reports)
        print(json.dumps({'completed_turns':sum(r['scenario']>0 and r['failure'] is None for r in reports),
                          'failed_checks':sum(r['failure'] is not None for r in reports)}),flush=True)
    class CaptureClient(httpx.AsyncClient):
        latest=None
        chat_calls=0
        async def post(self,url,**kwargs):
            response=await super().post(url,**kwargs)
            if str(url)==replay.API+'/chat/messages':
                self.chat_calls+=1
                payload=response.json() if response.status_code==200 else None
                item={**active,'status':response.status_code,'input':kwargs.get('json'),'response':payload}
                if payload is None:
                    item['failure']=replay.response_failure(response)
                responses.append(item)
                save(directory/'responses.json',responses)
                if payload is not None:
                    self.latest={'key':kwargs['headers']['Idempotency-Key'],'body':kwargs['json'],'payload':payload}
            return response
    connection=await asyncpg.connect(dsn,statement_cache_size=0,command_timeout=15)
    success,cleaned,failure=False,False,None
    try:
        async with CaptureClient(timeout=45,follow_redirects=False,trust_env=False) as client:
            secret=env.get('SUPABASE_SECRET_KEY') or env['SUPABASE_SERVICE_ROLE_KEY']
            runner=GreetingRunner(connection,client,{'apikey':secret,'Authorization':'Bearer '+secret},
                env.get('SUPABASE_PUBLISHABLE_KEY') or env['SUPABASE_ANON_KEY'],args.expected_sha,emit,
                run_tag=state['run'],privacy_step=_sweep_subject)
            runner.owned_state=state
            if args.cleanup_state:
                cleaned=await runner.cleanup(state,journal)
                return 0 if cleaned else 1
            try:
                await runner.health()
                protected=await connection.fetch("SELECT key,value,updated_at FROM app_config WHERE key=ANY($1::text[]) ORDER BY key",
                    ['chat_content_encryption_state','context_retention_active_preparation'])
                async with app_db.get_sessionmaker()() as session:
                    mode=await chat_content_encryption.prepare_reader(session)
                replay.require(mode.started,'encryption_not_active')
                await runner.configure(state,journal)
                number=0
                for language in FIXTURES:
                    for repetition in range(3):
                        number+=1
                        scenario={'number':number,'language':language}
                        uid,token=await replay.Runner.synthetic_user(runner,scenario,state,journal)
                        gid,legacy,raw_proof=await runner.seed(uid,language,mode)
                        values=[FIXTURES[language]['short']]
                        kinds=['normal']
                        if args.mode=='candidate' and repetition==0:
                            values+=FIXTURES[language]['later']
                            kinds+=['normal']*6+['safety_check','safety','normal']
                        for index,(text,kind) in enumerate(zip(values,kinds,strict=True)):
                            active.clear()
                            active.update(language=language,repetition=repetition,turn=index+1)
                            step={'text':text,'kind':kind}
                            if index==0:
                                step['greeting_id']=str(gid)
                                step['replay']=args.mode=='candidate' and repetition==0
                            await runner.send(uid,token,scenario,step)
                            await runner.verify_turn_storage(uid,client.latest)
                            if index==0:
                                await runner.verify_greeting_commit(uid,gid,client.latest,language)
                        replay.require(await runner.raw_message_proofs(uid,legacy)==raw_proof,'old_plaintext_changed')
                        if repetition==0:
                            await runner.verify_history(uid,token,legacy)
                            if args.mode=='candidate':
                                reply=await client.get(replay.API+'/chat/greeting',params={'context':'home_enter'},
                                    headers={'Authorization':'Bearer '+token})
                                replay.require(reply.status_code==200 and reply.json().get('greeting_id') is None,
                                               'greeting_reissued_after_conversation')
                replay.require(await connection.fetch("SELECT key,value,updated_at FROM app_config WHERE key=ANY($1::text[]) ORDER BY key",
                    ['chat_content_encryption_state','context_retention_active_preparation'])==protected,'protected_configuration_changed')
                success=True
            except Exception as error:
                failure=failure_code(error)
            finally:
                cleaned=await runner.cleanup(state,journal)
    finally:
        await connection.close()
        if app_db._engine is not None:
            await app_db._engine.dispose()
    summary={'schema':'greeting-context-dev-smoke-v1','mode':args.mode,'success':success and cleaned,
             'completed_turns':sum(r['scenario']>0 and r['failure'] is None for r in reports),
             'failed_checks':sum(r['failure'] is not None for r in reports),'http_chat_attempts':len(responses),
             'cleanup':cleaned,'failure':failure,'semantic_review_pending':True,'semantic_quality_pass':None,
             'owned_accounts':len(state['users']),'global_encryption_writes':0}
    save(directory/'summary.json',summary)
    print(json.dumps({k:summary[k] for k in ('success','completed_turns','failed_checks','http_chat_attempts','cleanup')}),flush=True)
    return 0 if summary['success'] else 1


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode',choices=('baseline','candidate'),default='baseline')
    parser.add_argument('--env-file')
    parser.add_argument('--expected-sha')
    parser.add_argument('--output-directory')
    parser.add_argument('--cleanup-state')
    parser.add_argument('--execute-dev',action='store_true')
    args=parser.parse_args(argv)
    logging.disable(logging.CRITICAL)
    if not args.execute_dev:
        print(json.dumps({'validation_only':True,'planned_turns':9 if args.mode=='baseline' else 36,
                          'maximum_chat_calls':12 if args.mode=='baseline' else 45,'network_calls':0}))
        return 0
    try:
        replay.require(bool(args.env_file) and bool(args.expected_sha) and bool(args.output_directory or args.cleanup_state),
                       'execution_arguments_required')
        return asyncio.run(run(args))
    except BaseException:
        print(json.dumps({'success':False,'completed_turns':0,'failed_checks':1,'cleanup':False}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
