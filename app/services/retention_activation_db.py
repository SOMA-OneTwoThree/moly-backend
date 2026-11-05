"""Same-transaction server validation envelope; no publication or commit."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import uuid

from sqlalchemy import text

from app.services import retention_release_promotion as promotion

_BIND = re.compile(r"(?<!:):([A-Za-z_][A-Za-z_0-9]*)")
_VARIABLES = {"id": "v_id", "ids": "v_ids", "bounds": "v_bounds", "targets": "v_targets"}
_ERRORS = frozenset({"activation_busy", "live_preparation_required",
    "preparation_changed_since_audit", "active_preparation_pointer_conflict",
    "different_active_preparation_not_supported", "audited_rows_changed",
    "processing_changed_since_audit", "snapshot_metadata_unavailable", "prepared_artifacts_unsealed",
    "activation_read_committed_required"})


@dataclass(frozen=True)
class PreparedEnvelope:
    name: str
    statement: str
    payload_key: str
    preparation_id: uuid.UUID
    manifest_hash: str
    payload_hash: str
    driver_identity: object = field(repr=False, compare=False)
    transaction_identity: object = field(repr=False, compare=False)


def bind_variables(statement):
    """Only the fixed builder bindings become fixed PL/pgSQL variable names."""
    def replace(match):
        name = match[1]
        if name not in _VARIABLES:
            raise ValueError("unsupported_activation_binding")
        return _VARIABLES[name]
    return _BIND.sub(replace, statement)


def _inputs(token):
    if promotion.digest(token.manifest) != token.manifest_hash:
        raise promotion.Blocked("preparation_changed_since_audit")
    expected_ids = set(token.tail_contexts)
    if (set(token.source_revisions) != expected_ids
            or any(type(value) is not int or value < 0 for value in token.source_revisions.values())):
        raise promotion.Blocked("preparation_revision_unregistered")
    payload = {"id": str(token.preparation_id), "code_sha": token.code_sha,
        "registry_version": token.registry_version, "audit_versions_hash": promotion.digest(token.row_versions),
        "ids": list(token.tail_contexts),
        "audited_ids": [record[0] for record in token.user_metadata],
        "tail_bounds": {key: value["through"] for key, value in token.tail_contexts.items()},
        "revisions": {key: {"revision": revision,
            "through": token.manifest["users"].get(key, {}).get("through", 0)}
            for key, revision in token.source_revisions.items()}}
    return payload


def envelope_statement(token, table_name):
    if re.fullmatch(r"activation_receipt_[0-9a-f]{32}", table_name) is None:
        raise ValueError("invalid_activation_receipt_name")
    _inputs(token)
    payload_key = "moly.activation_payload_" + table_name.removeprefix("activation_receipt_")
    population = str(promotion._USERS)
    if _BIND.search(population):
        raise ValueError("unsupported_activation_binding")
    body = f"""
DECLARE
  v_payload jsonb := current_setting('{payload_key}')::jsonb;
  v_id uuid; v_ids uuid[]; v_audited uuid[];
  v_preparation record; v_registry jsonb; v_controls jsonb; v_new jsonb; v_revisions jsonb; v_revision_rows jsonb; v_dirty jsonb;
  v_tail jsonb; v_pointer jsonb;
BEGIN
  IF current_setting('transaction_isolation') <> 'read committed' THEN
    RAISE EXCEPTION 'activation_read_committed_required';
  END IF;
  PERFORM set_config('lock_timeout','150ms',true),
    set_config('statement_timeout','1000ms',true),
    set_config('idle_in_transaction_session_timeout','1000ms',true),
    set_config('work_mem','16MB',true);
  IF NOT pg_try_advisory_xact_lock(743021149) THEN
    RAISE EXCEPTION 'activation_busy';
  END IF;
  v_id := (v_payload->>'id')::uuid;
  SELECT COALESCE(array_agg(value::uuid),ARRAY[]::uuid[]) INTO v_ids
    FROM jsonb_array_elements_text(v_payload->'ids');
  SELECT COALESCE(array_agg(value::uuid),ARRAY[]::uuid[]) INTO v_audited
    FROM jsonb_array_elements_text(v_payload->'audited_ids');
  SELECT id,state,code_sha,manifest_json->>'format' AS format,
    manifest_json->>'code_sha' AS bound_code_sha,xmin::text AS row_version,
    (SELECT id FROM retention_preparations WHERE state='active') AS active_id
    INTO v_preparation FROM retention_preparations WHERE id=v_id FOR UPDATE NOWAIT;
  IF v_preparation.id IS NULL OR v_preparation.format IS DISTINCT FROM '{promotion.FORMAT}'
    OR v_preparation.code_sha IS DISTINCT FROM v_payload->>'code_sha'
    OR v_preparation.bound_code_sha IS DISTINCT FROM v_preparation.code_sha THEN
    RAISE EXCEPTION 'live_preparation_required';
  END IF;
  v_registry := to_jsonb(v_preparation);
  CREATE TEMP TABLE pg_temp.{table_name}(value jsonb NOT NULL) ON COMMIT DROP;
  IF v_preparation.state='active' THEN
    SELECT value INTO v_pointer FROM app_config
      WHERE key='context_retention_active_preparation' FOR SHARE;
    IF v_pointer IS DISTINCT FROM to_jsonb(v_id::text) THEN
      RAISE EXCEPTION 'active_preparation_pointer_conflict';
    END IF;
    INSERT INTO pg_temp.{table_name} VALUES(jsonb_build_object('registry',v_registry,'active_pointer',v_pointer,
      'controls',jsonb_build_array(jsonb_build_object('key','context_retention_active_preparation','value',v_pointer))));
    RETURN;
  END IF;
  IF v_preparation.state NOT IN ('building','prepared') OR v_preparation.row_version IS DISTINCT FROM v_payload->>'registry_version' THEN
    RAISE EXCEPTION 'preparation_changed_since_audit';
  END IF;
  IF v_preparation.active_id IS NOT NULL THEN RAISE EXCEPTION 'different_active_preparation_not_supported'; END IF;
  SELECT COALESCE(jsonb_agg(to_jsonb(c)),'[]'::jsonb) INTO v_controls FROM (
    SELECT key,value FROM app_config WHERE key IN
      ('context_retention_active_preparation','context_retention_v2_enabled','crisis_guard_enabled',
       'crisis_guard_policy_version','crisis_guard_timeout_s','crisis_guard_history_messages')
      ORDER BY key FOR UPDATE NOWAIT) c;
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(v_controls) c
    WHERE c->>'key'='context_retention_active_preparation'
      AND c->'value' IS DISTINCT FROM 'null'::jsonb) THEN
    RAISE EXCEPTION 'different_active_preparation_not_supported';
  END IF;
  WITH locked_clock AS MATERIALIZED (
    SELECT user_id,through_message_id,revision FROM retention_preparation_revisions
    WHERE preparation_id=v_id ORDER BY user_id FOR UPDATE NOWAIT
  ), event_counts AS MATERIALIZED (
    SELECT user_id,count(*) AS total FROM retention_preparation_revision_events
    WHERE preparation_id=v_id GROUP BY user_id
  ), locked_revisions AS MATERIALIZED (
    SELECT c.user_id,c.through_message_id,c.revision+COALESCE(e.total,0) AS revision
    FROM locked_clock c LEFT JOIN event_counts e ON e.user_id=c.user_id
  ) SELECT COALESCE(jsonb_object_agg(user_id::text,
      jsonb_build_object('revision',revision,'through',through_message_id)),'{{}}'::jsonb),
      COALESCE(jsonb_agg(to_jsonb(locked_revisions) ORDER BY user_id),'[]'::jsonb)
      INTO v_revisions,v_revision_rows FROM locked_revisions;
  IF v_revisions IS DISTINCT FROM v_payload->'revisions' THEN
    SELECT COALESCE(jsonb_agg(key ORDER BY key),'[]'::jsonb) INTO v_dirty FROM (
      SELECT COALESCE(e.key,a.key) AS key
      FROM jsonb_each(v_payload->'revisions') e FULL JOIN jsonb_each(v_revisions) a USING(key)
      WHERE e.value IS DISTINCT FROM a.value) changed;
    INSERT INTO pg_temp.{table_name} VALUES(jsonb_build_object(
      'registry',v_registry,'controls',v_controls,'dirty_subjects',v_dirty));
    RETURN;
  END IF;
  IF EXISTS(SELECT 1 FROM conversation_checkpoints WHERE preparation_id=v_id
    AND publish_state='ready'
    AND validation_json->'activation_sealed' IS DISTINCT FROM 'true'::jsonb) THEN
    RAISE EXCEPTION 'prepared_artifacts_unsealed';
  END IF;
  WITH new_ids AS MATERIALIZED (SELECT u.user_id FROM ({population}) u
    EXCEPT SELECT unnest(v_audited))
    SELECT COALESCE(jsonb_agg(to_jsonb(u)),'[]'::jsonb) INTO v_new
    FROM ({population}) u JOIN new_ids n ON n.user_id=u.user_id;
  SELECT COALESCE(jsonb_agg(to_jsonb(m) ORDER BY id),'[]'::jsonb) INTO v_tail FROM (
    SELECT user_id,id,sender,kind,content,encode(content_ciphertext,'hex') AS content_ciphertext_hex,content_chars FROM messages WHERE user_id=ANY(v_ids)
      AND id>COALESCE(((v_payload->'tail_bounds')->>user_id::text)::bigint,0)
      ORDER BY id FOR SHARE NOWAIT) m;
  INSERT INTO pg_temp.{table_name} VALUES(jsonb_build_object(
    'registry',v_registry,'controls',v_controls,'new_users',v_new,
    'verified_revisions',true,'revision_rows',v_revision_rows,'tail',v_tail));
END
"""
    tag = "$activation_" + table_name.removeprefix("activation_receipt_") + "$"
    if tag in body:
        raise ValueError("activation_literal_collision")
    return "DO " + tag + body + tag + ";"


async def prepare_envelope(session, token):
    """Upload only bound metadata before the critical source-lock deadline."""
    if not session.in_transaction():
        raise ValueError("activation_warmed_transaction_required")
    name = "activation_receipt_" + uuid.uuid4().hex
    payload_key = "moly.activation_payload_" + name.removeprefix("activation_receipt_")
    statement = envelope_statement(token, name)
    payload = _inputs(token)
    await session.execute(text("SELECT set_config(:payload_key,:payload,true)"),
        {"payload_key": payload_key,
         "payload": json.dumps(payload, separators=(",", ":"), default=str, ensure_ascii=False)})
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver = raw.driver_connection
    transaction = (connection.sync_connection.get_nested_transaction()
        or connection.sync_connection.get_transaction())
    if not driver.is_in_transaction() or transaction is None:
        raise ValueError("activation_warmed_transaction_required")
    return PreparedEnvelope(name, statement, payload_key, token.preparation_id, token.manifest_hash,
        promotion.digest(payload), driver, transaction)


async def validation_envelope(session, token, prepared):
    """Consume one prepared envelope in the same transaction; never commits."""
    if (not isinstance(prepared, PreparedEnvelope)
            or prepared.preparation_id != token.preparation_id
            or prepared.manifest_hash != token.manifest_hash
            or promotion.digest(token.manifest) != token.manifest_hash
            or prepared.payload_hash != promotion.digest(_inputs(token))
            or prepared.payload_key != "moly.activation_payload_" + prepared.name.removeprefix("activation_receipt_")
            or prepared.statement != envelope_statement(token, prepared.name)):
        raise promotion.Blocked("preparation_changed_since_audit")
    if not session.in_transaction():
        raise ValueError("activation_warmed_transaction_required")
    connection = await session.connection()
    raw = await connection.get_raw_connection()
    driver = raw.driver_connection
    transaction = (connection.sync_connection.get_nested_transaction()
        or connection.sync_connection.get_transaction())
    if (not driver.is_in_transaction() or driver is not prepared.driver_identity
            or transaction is None or transaction is not prepared.transaction_identity):
        raise ValueError("activation_prepared_transaction_changed")
    try:
        await driver.execute(prepared.statement)
    except Exception as error:
        if getattr(error, "sqlstate", None) == "P0001" and str(error) in _ERRORS:
            raise promotion.Blocked(str(error)) from None
        raise
    receipt = await session.scalar(text(f"SELECT value FROM pg_temp.{prepared.name}"))
    if isinstance(receipt, str):
        receipt = json.loads(receipt)
    if not isinstance(receipt, dict):
        raise promotion.Blocked("snapshot_metadata_unavailable")
    return receipt
