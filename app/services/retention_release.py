"""Private preparation storage and exact release binding; no public promotion here."""
from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import text


class ReleaseError(ValueError):
    pass


def binding():
    from app.services.retention_preparation_context import current_binding
    return current_binding()


async def require_binding(session, value=None, *, lock=False):
    value = binding() if value is None else value
    if value is None:
        return None
    statement = text("SELECT state FROM retention_preparations WHERE id=:preparation_id"
                     + (" FOR SHARE" if lock else ""))
    state = await session.scalar(statement, {"preparation_id": value.preparation_id})
    allowed = {"building", "prepared"} if value.building else {"active"}
    if state not in allowed:
        raise ReleaseError("retention_preparation_unavailable")
    return value


async def resolve(session, *, lock=False, config=None):
    """Entry points capture this once; nested helpers only use that immutable binding."""
    value = binding()
    from app.services.retention_preparation_context import binding_captured
    if value is not None:
        return await require_binding(session, value, lock=lock)
    if config is None and binding_captured():
        return None
    from app.services import config_store
    from app.services.retention_preparation_context import Binding
    raw = config if config is not None else await config_store.get_config_values(
        session, ["context_retention_active_preparation"])
    identity = raw.get("context_retention_active_preparation")
    if identity is None:
        return None
    try:
        value = Binding(uuid.UUID(str(identity)), False)
    except (ValueError, TypeError) as exc:
        raise ReleaseError("retention_preparation_identity") from exc
    return await require_binding(session, value, lock=lock)


def params(values=None):
    result = dict(values or {})
    value = binding()
    if value is not None:
        result.update(preparation_id=value.preparation_id,
                      preparation_state="ready" if value.building else "published",
                      preparation_building=json.dumps(value.building))
    return result


def checkpoint_sql(statement):
    """Every v5 parent/coverage lookup uses the same exact release and state."""
    value = binding()
    if value is None:
        return statement
    sql = str(statement).replace(" AND c.preparation_id IS NULL", "")
    for alias in ("c", "latest"):
        for condition in (f"{alias}.publish_state='published'", f"{alias}.publish_state = 'published'"):
            visibility = (f"{alias}.publish_state=:preparation_state" if value.building else
                f"({alias}.publish_state='published' OR ({alias}.publish_state='ready' "
                f"AND {alias}.validation_json->'activation_sealed'='true'::jsonb))")
            sql = sql.replace(condition, f"({alias}.preparation_id=CAST(:preparation_id AS uuid) "
                              f"AND {visibility})")
    return text(sql)


def job_sql(statement):
    """Scope both immutable release ID and building/active execution lane."""
    value = binding()
    if value is None:
        return statement
    sql = str(statement)
    for alias in ("j", "active_job", "failed"):
        sql = sql.replace(f"AND ({alias}.payload->>'retention_preparation_id') IS NULL", "")
        token = f"{alias}.payload->'memory_generation'"
        if token in sql:
            at = sql.index(token)
            predicate = (f"({alias}.payload->>'retention_preparation_id')=CAST(CAST(:preparation_id AS uuid) AS text) AND "
                         f"{alias}.payload->'retention_preparation_building'=CAST(:preparation_building AS jsonb) AND ")
            sql = sql[:at] + predicate + sql[at:]
    if "FROM async_jobs WHERE" in sql:
        sql = sql.replace("(payload->>'retention_preparation_id') IS NULL AND", "")
        sql = sql.replace("FROM async_jobs WHERE", "FROM async_jobs WHERE "
            "(payload->>'retention_preparation_id')=CAST(CAST(:preparation_id AS uuid) AS text) AND "
            "payload->'retention_preparation_building'=CAST(:preparation_building AS jsonb) AND", 1)
    return text(sql)


def dedup_key(key):
    value = binding()
    if value is None:
        return key
    suffix = f":preparation:{value.preparation_id}:{int(value.building)}"
    if key.endswith(suffix):
        return key
    return key + suffix


async def contract_row(session, *, user_id, generation, lock=False):
    value = binding()
    if value is None:
        return None
    await require_binding(session, value, lock=lock)
    sql = text("""SELECT document_json,processed_through_message_id,render_hash,id,validation_json
      FROM retention_prepared_contracts WHERE preparation_id=:preparation_id
       AND user_id=:user_id AND memory_generation=:generation AND status=:preparation_state
      ORDER BY version DESC,id DESC LIMIT 1""" + (" FOR UPDATE" if lock else ""))
    if not value.building:
        sql = text(str(sql).replace("status=:preparation_state", "status IN ('ready','published')"))
    return (await session.execute(sql, params({"user_id": user_id, "generation": generation}))).first()


async def contract_cursor(session, *, user_id, generation):
    row = await contract_row(session, user_id=user_id, generation=generation)
    return int(row[1]) if row is not None else 0


async def store_contract(session, *, user_id, generation, locale, items, rendered, digest, through,
                         source_rows, after, draft_items=()):
    value = await require_binding(session, lock=True)
    if value is None:
        raise ReleaseError("retention_preparation_required")
    head_condition = "status=:preparation_state" if value.building else "status IN ('ready','published')"
    parent_sql = text("""SELECT document_json,processed_through_message_id,
      render_hash,id,validation_json FROM retention_prepared_contracts
      WHERE preparation_id=:preparation_id AND user_id=:user_id AND memory_generation=:generation
       AND status=:preparation_state ORDER BY version DESC,id DESC LIMIT 1 FOR UPDATE""")
    parent = (await session.execute(text(str(parent_sql).replace("status=:preparation_state", head_condition)),
       params({"user_id": user_id, "generation": generation}))).first()
    proof = contract_proof(value, generation=generation, locale=locale, after=after,
        through=through, rows=source_rows, document_hash=digest, parent=parent)
    parameters = params({"user_id": user_id, "generation": generation, "locale": locale,
        "document": json.dumps(items, ensure_ascii=False), "rendered": rendered,
        "digest": digest, "through": through, "validation": json.dumps(proof, sort_keys=True)})
    supersede = text("""UPDATE retention_prepared_contracts SET status='superseded'
      WHERE preparation_id=:preparation_id AND user_id=:user_id AND memory_generation=:generation
        AND status=:preparation_state""")
    await session.execute(text(str(supersede).replace("status=:preparation_state", head_condition)), parameters)
    insert_sql = text("""INSERT INTO retention_prepared_contracts
      (preparation_id,user_id,memory_generation,locale,version,document_json,rendered_text,
       render_hash,status,processed_through_message_id,published_at,validation_json)
      SELECT :preparation_id,:user_id,:generation,:locale,COALESCE(max(version),0)+1,
       CAST(:document AS jsonb),:rendered,:digest,:preparation_state,:through,
       CASE WHEN :preparation_state='published' THEN now() ELSE NULL END,CAST(:validation AS jsonb)
      FROM retention_prepared_contracts WHERE preparation_id=:preparation_id
        AND user_id=:user_id AND memory_generation=:generation AND locale=:locale RETURNING id""")
    identity = await session.scalar(insert_sql, parameters)
    if draft_items:
        from app.services import interaction_contract
        draft = json.dumps(draft_items, ensure_ascii=False, sort_keys=True)
        await session.scalar(insert_sql, {**parameters, "document": draft, "rendered": "",
            "digest": interaction_contract.document_hash(draft), "preparation_state": "draft",
            "validation": "{}"})
    return identity


def preparation_id():
    value = binding()
    return value.preparation_id if value is not None else None


async def execute(session, statement, values=None):
    return await session.execute(checkpoint_sql(job_sql(statement)), params(values))


async def scalar(session, statement, values=None):
    return await session.scalar(checkpoint_sql(job_sql(statement)), params(values))


CONTRACT_PROOF_SCHEMA = "retention-contract-segment-v1"
_CONTRACT_PROOF_KEYS = frozenset({"schema", "preparation_id", "generation", "locale",
    "after_message_id", "through_message_id", "source_message_ids", "source_hash",
    "previous_contract_id", "previous_document_hash", "previous_through_message_id", "document_hash"})


def contract_source_hash(rows):
    """Exact normal message identity; no raw body is persisted in the proof."""
    records, previous = [], 0
    for identity, sender, body, activity_date in rows:
        if (type(identity) is not int or identity <= previous or sender not in {"user", "moly"}
                or not isinstance(body, str)):
            raise ReleaseError("contract_proof_source_order")
        records.append([identity, sender, hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        str(activity_date) if activity_date is not None else None])
        previous = identity
    wire = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def contract_proof(value, *, generation, locale, after, through, rows, document_hash, parent):
    from app.services import interaction_contract
    rows = tuple(rows)
    if (type(after) is not int or type(through) is not int or not 0 <= after < through
            or any(not after < row[0] <= through for row in rows)):
        raise ReleaseError("contract_proof_source_boundary")
    if parent is None:
        if after != 0:
            raise ReleaseError("contract_proof_parent_missing")
        previous_id, previous_hash = None, None
    else:
        if (len(parent) < 5 or int(parent[1]) != after or not isinstance(parent[4], dict)
                or parent[4].get("schema") != CONTRACT_PROOF_SCHEMA
                or parent[4].get("through_message_id") != after
                or parent[4].get("document_hash") != parent[2]
                or interaction_contract.document_hash(json.dumps(parent[0], ensure_ascii=False,
                    sort_keys=True)) != parent[2]):
            raise ReleaseError("contract_proof_parent_changed")
        previous_id, previous_hash = str(parent[3]), parent[2]
    return {"schema": CONTRACT_PROOF_SCHEMA, "preparation_id": str(value.preparation_id),
        "generation": generation, "locale": locale, "after_message_id": after,
        "through_message_id": through, "source_message_ids": [row[0] for row in rows],
        "source_hash": contract_source_hash(rows), "previous_contract_id": previous_id,
        "previous_document_hash": previous_hash, "previous_through_message_id": after,
        "document_hash": document_hash}


async def validate_contract_chain(session, *, user_id, generation, through_id):
    """Read-only durable processing proof, independent of expiring async job payloads."""
    value = binding()
    if value is None or type(through_id) is not int or through_id < 0:
        return False
    try:
        await require_binding(session, value)
        rows = (await session.execute(text("""SELECT id,locale,document_json,render_hash,
          processed_through_message_id,status,validation_json FROM retention_prepared_contracts
          WHERE preparation_id=:preparation_id AND user_id=:user_id AND memory_generation=:generation
            AND status IN ('ready','published','superseded') ORDER BY version DESC,id DESC"""),
          params({"user_id": user_id, "generation": generation}))).all()
        indexed = {str(row[0]): row for row in rows}
        heads = [row for row in rows if row[5] in ({"ready"} if value.building else {"ready", "published"})]
        if not heads:
            return through_id == 0
        if len(heads) != 1 or int(heads[0][4]) < through_id:
            return False
        current, visited = heads[0], set()
        from app.services import interaction_contract
        while current is not None:
            identity, locale, document, digest, cursor, _status, proof = current
            if str(identity) in visited or not isinstance(proof, dict) or set(proof) != _CONTRACT_PROOF_KEYS:
                return False
            visited.add(str(identity))
            after, through = proof["after_message_id"], proof["through_message_id"]
            if (proof["schema"] != CONTRACT_PROOF_SCHEMA
                    or proof["preparation_id"] != str(value.preparation_id)
                    or type(proof["generation"]) is not int or proof["generation"] != generation
                    or proof["locale"] != locale or proof["document_hash"] != digest
                    or type(after) is not int or type(through) is not int or not 0 <= after < through
                    or through != cursor or type(proof["previous_through_message_id"]) is not int
                    or proof["previous_through_message_id"] != after
                    or not isinstance(proof["source_message_ids"], list)
                    or any(type(mid) is not int for mid in proof["source_message_ids"])
                    or interaction_contract.document_hash(json.dumps(document, ensure_ascii=False,
                        sort_keys=True)) != digest):
                return False
            sources = (await session.execute(text("""SELECT id,sender,content,activity_date FROM messages
              WHERE user_id=:user_id AND kind='normal' AND id>:after AND id<=:through ORDER BY id"""),
              {"user_id": user_id, "after": after, "through": through})).all()
            boundary_exists = await session.scalar(text("""SELECT EXISTS(SELECT 1 FROM messages
              WHERE user_id=:user_id AND id=:through)"""), {"user_id": user_id, "through": through})
            if (boundary_exists is not True
                    or proof["source_message_ids"] != [row[0] for row in sources]
                    or proof["source_hash"] != contract_source_hash(sources)):
                return False
            parent_id = proof["previous_contract_id"]
            if parent_id is None:
                return after == 0 and proof["previous_document_hash"] is None
            parent = indexed.get(parent_id)
            if parent is None or parent[4] != after or parent[3] != proof["previous_document_hash"]:
                return False
            current = parent
        return False
    except (ReleaseError, ValueError, TypeError, KeyError):
        return False
