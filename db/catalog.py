"""Read the application-owned PostgreSQL catalog, without reading application rows."""
from __future__ import annotations

import asyncpg

SCHEMAS = ['public', 'vecs']

# Extension internals belong to Supabase/PostgreSQL, not to schema.sql.
_RELATIONS = """
SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=ANY($1::text[]) AND c.relkind IN ('r','p','S')
AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass
               AND d.objid=c.oid AND d.deptype='e')
"""
_FUNCTIONS = """
SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname=ANY($1::text[])
AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass
               AND d.objid=p.oid AND d.deptype='e')
"""
QUERIES = {
    'tables': f"""
        SELECT c.oid::regclass::text AS key, c.relkind, c.relrowsecurity,
               c.relforcerowsecurity, pg_get_userbyid(c.relowner) AS owner,
               ARRAY(SELECT unnest(c.reloptions) ORDER BY 1) AS options
        FROM pg_class c WHERE c.oid IN ({_RELATIONS}) AND c.relkind IN ('r','p')
    """,
    'columns': f"""
        SELECT a.attrelid::regclass::text || '.' || a.attname AS key,
               format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull AS not_null,
               pg_get_expr(d.adbin,d.adrelid) AS default, a.attidentity AS identity,
               a.attgenerated AS generated,
               CASE WHEN a.attcollation<>0 THEN a.attcollation::regcollation::text END AS collation
        FROM pg_attribute a LEFT JOIN pg_attrdef d
          ON d.adrelid=a.attrelid AND d.adnum=a.attnum
        WHERE a.attrelid IN ({_RELATIONS}) AND a.attnum>0 AND NOT a.attisdropped
    """,
    'constraints': f"""
        SELECT conrelid::regclass::text || '.' || conname AS key,
               pg_get_constraintdef(oid) AS definition, convalidated AS validated,
               condeferrable AS deferrable, condeferred AS deferred
        FROM pg_constraint WHERE conrelid IN ({_RELATIONS})
    """,
    'indexes': f"""
        SELECT i.indexrelid::regclass::text AS key, pg_get_indexdef(i.indexrelid) AS definition,
               i.indisvalid AS valid, i.indisready AS ready,
               i.indrelid::regclass::text AS table_name,
               i.indisunique AS unique, i.indisexclusion AS exclusion,
               ARRAY(SELECT unnest(c.reloptions) ORDER BY 1) AS options
        FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
        WHERE i.indrelid IN ({_RELATIONS})
    """,
    'triggers': f"""
        SELECT tgrelid::regclass::text || '.' || tgname AS key,
               pg_get_triggerdef(oid) AS definition, tgenabled AS enabled
        FROM pg_trigger WHERE NOT tgisinternal AND
          (tgrelid IN ({_RELATIONS}) OR
           (tgrelid=to_regclass('auth.users') AND tgname='on_auth_user_created'))
    """,
    'functions': f"""
        SELECT oid::regprocedure::text AS key, pg_get_functiondef(oid) AS definition,
               pg_get_userbyid(proowner) AS owner
        FROM pg_proc WHERE oid IN ({_FUNCTIONS})
    """,
    'policies': f"""
        SELECT polrelid::regclass::text || '.' || polname AS key,
               polcmd AS command, polpermissive AS permissive,
               ARRAY(SELECT CASE WHEN r=0 THEN 'PUBLIC' ELSE pg_get_userbyid(r) END
                     FROM unnest(polroles) r ORDER BY 1) AS roles,
               pg_get_expr(polqual,polrelid) AS using,
               pg_get_expr(polwithcheck,polrelid) AS with_check
        FROM pg_policy WHERE polrelid IN ({_RELATIONS})
    """,
    'relation_grants': f"""
        SELECT c.oid::regclass::text || ':' ||
               CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END ||
               ':' || a.privilege_type AS key, a.is_grantable AS grantable
        FROM pg_class c CROSS JOIN LATERAL
          aclexplode(COALESCE(c.relacl,acldefault(
            CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,c.relowner))) a
        WHERE c.oid IN ({_RELATIONS}) AND a.grantee<>c.relowner
    """,
    'column_grants': f"""
        SELECT a.attrelid::regclass::text || '.' || a.attname || ':' ||
               CASE WHEN x.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(x.grantee) END ||
               ':' || x.privilege_type AS key, x.is_grantable AS grantable
        FROM pg_attribute a CROSS JOIN LATERAL aclexplode(a.attacl) x
        WHERE a.attrelid IN ({_RELATIONS}) AND a.attnum>0 AND NOT a.attisdropped
    """,
    'function_grants': f"""
        SELECT p.oid::regprocedure::text || ':' ||
               CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE pg_get_userbyid(a.grantee) END ||
               ':' || a.privilege_type AS key, a.is_grantable AS grantable
        FROM pg_proc p CROSS JOIN LATERAL
          aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid IN ({_FUNCTIONS}) AND a.grantee<>p.proowner
    """,
    'sequences': f"""
        SELECT seqrelid::regclass::text AS key, format_type(seqtypid,NULL) AS type,
               seqstart AS start, seqincrement AS increment, seqmax AS max,
               seqmin AS min, seqcache AS cache, seqcycle AS cycle
        FROM pg_sequence WHERE seqrelid IN ({_RELATIONS})
    """,
}


async def read_catalog(conn: asyncpg.Connection) -> dict:
    catalog = {}
    async with conn.transaction(isolation='repeatable_read', readonly=True):
        await conn.execute("SET LOCAL search_path = ''")
        for section, query in QUERIES.items():
            entries = {}
            for row in await conn.fetch(query, SCHEMAS):
                # PostgreSQL's empty internal "char" arrives as b'\x00', which
                # would otherwise look truthy (e.g. a nonexistent identity default).
                item = {key: value.decode().rstrip('\x00') if isinstance(value, bytes) else value
                        for key, value in dict(row).items()}
                entries[item.pop('key')] = item
            catalog[section] = entries
    return catalog


def compare_catalog(expected: dict, actual: dict, *, strict: bool = False) -> list[str]:
    """Allow additive rollout objects, but never extra grants on an existing object."""
    problems = []
    for section, entries in expected.items():
        observed = actual.get(section, {})
        for key, definition in entries.items():
            if key not in observed:
                problems.append(f'missing {section}: {key}')
            elif observed[key] != definition:
                problems.append(f'changed {section}: {key}')
        for key in observed.keys() - entries.keys():
            extra_is_unsafe = strict
            if section == 'columns' and key.rsplit('.', 1)[0] in expected['tables']:
                column = observed[key]
                extra_is_unsafe |= (column['not_null'] and column['default'] is None
                                    and not column['identity'] and not column['generated'])
            if section == 'indexes':
                index = observed[key]
                extra_is_unsafe |= (index['table_name'] in expected['tables']
                                    and (index['unique'] or index['exclusion']))
            if section in {'constraints', 'triggers', 'policies'}:
                extra_is_unsafe |= key.rsplit('.', 1)[0] in expected['tables']
            if section.endswith('_grants'):
                object_name = key.split(':', 1)[0]
                kind = {'relation_grants': 'tables', 'column_grants': 'columns',
                        'function_grants': 'functions'}[section]
                extra_is_unsafe |= object_name in expected[kind]
                if section == 'relation_grants':
                    extra_is_unsafe |= object_name in expected['sequences']
            if extra_is_unsafe:
                problems.append(f'unexpected {section}: {key}')
    return sorted(problems)
