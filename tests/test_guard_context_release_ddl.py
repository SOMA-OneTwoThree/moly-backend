"""분리 실행은 검토된 최종 DDL과 같고 검증 scan을 metadata TX 밖에 둔다."""
from pathlib import Path


CHANGES = Path(__file__).resolve().parents[1] / 'db' / 'changes'
VALIDATIONS = {
    'messages': 'ALTER TABLE public.messages VALIDATE CONSTRAINT messages_kind_guardrails_check;',
    'registry': ('ALTER TABLE public.mem0_memory_registry\n'
                 '  VALIDATE CONSTRAINT mem0_memory_registry_context_priority_check;'),
    'candidates': ('ALTER TABLE public.mem0_ingest_candidates\n'
                   '  VALIDATE CONSTRAINT mem0_ingest_candidates_context_priority_check;'),
    'pressure': ('ALTER TABLE public.chat_contexts VALIDATE CONSTRAINT '
                 'chat_contexts_retention_pressure_positive_check;'),
}
SWAP = ('ALTER TABLE public.messages DROP CONSTRAINT messages_kind_check;\n'
        'ALTER TABLE public.messages\n'
        '  RENAME CONSTRAINT messages_kind_guardrails_check TO messages_kind_check;')


def code(source):
    return '\n'.join(line.strip() for line in source.splitlines()
                     if line.strip() and not line.lstrip().startswith('--'))


def test_split_execution_is_exactly_the_reviewed_final_ddl():
    reference = (CHANGES / 'guard_context_release.sql').read_text()
    for statement in (*VALIDATIONS.values(), SWAP):
        assert reference.count(statement) == 1
        reference = reference.replace(statement, '', 1)
    metadata = (CHANGES / 'guard_context_release_metadata.sql').read_text()
    assert code(metadata[metadata.index('DO $guardrails$'):]) == code(reference)
    for name, statement in VALIDATIONS.items():
        body = code((CHANGES / f'guard_context_release_validate_{name}.sql').read_text())
        assert body == code(statement)
        assert body.count(';') == 1
    swap = (CHANGES / 'guard_context_release_swap.sql').read_text()
    assert code(swap.split('-- REVIEWED_SWAP_BODY', 1)[1]) == code(SWAP)


def test_existing_table_locks_are_nowait_before_any_metadata_or_swap():
    metadata = code((CHANGES / 'guard_context_release_metadata.sql').read_text())
    prefix, _ = metadata.split('DO $guardrails$', 1)
    assert prefix.startswith('LOCK TABLE ')
    assert prefix.endswith('IN ACCESS EXCLUSIVE MODE NOWAIT;\n')
    for table in ('app_config', 'chat_contexts', 'conversation_checkpoints',
                  'mem0_ingest_candidates', 'mem0_memory_registry', 'messages',
                  'mem0_memory_sources', 'memory_pipeline_states',
                  'privacy_subject_barriers', 'profiles'):
        assert f'public.{table}' in prefix
    assert 'VALIDATE CONSTRAINT' not in metadata
    assert 'DROP CONSTRAINT messages_kind_check' not in metadata
    assert 'RENAME CONSTRAINT' not in metadata
    swap = code((CHANGES / 'guard_context_release_swap.sql').read_text())
    assert swap.startswith('LOCK TABLE public.messages IN ACCESS EXCLUSIVE MODE NOWAIT;')
    assert 'IF NOT FOUND' in swap and 'NOT new_constraint.convalidated' in swap
    assert "conname='messages_kind_check'" in swap
    assert "conname='messages_kind_guardrails_check'" in swap
    assert 'DROP CONSTRAINT IF EXISTS' not in swap
    assert 'VALIDATE CONSTRAINT' not in swap
