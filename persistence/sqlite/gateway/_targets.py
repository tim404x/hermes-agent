"""Native, downgrade-compatible expression index for point retirement across routing scopes."""

import json

ROUTE_TARGET_SQL = "CASE WHEN json_valid(entry_json) THEN json_extract(entry_json,'$.session_id') END"

_INDEX_TARGET = ("CASE WHEN typeof(entry_json)='blob' THEN x'00' WHEN json_valid(entry_json) "
                 "THEN json_quote(json_extract(json_patch('{}',entry_json),'$.session_id')) "
                 "WHEN instr(entry_json,'NaN') OR instr(entry_json,'Infinity') THEN x'00' END")


def route_namespace_sql(key_expr: str) -> str:
    """Slice UTF-8 bytes so embedded NULs survive SQLite's substr."""
    tail = f"substr(CAST({key_expr} AS BLOB), 7)"
    colon = f"instr({tail}, x'3a')"
    return (f"CASE WHEN substr({key_expr}, 1, 6) = 'agent:' AND {colon} > 1 "
            f"THEN CAST(substr({tail}, 1, {colon} - 1) AS TEXT) ELSE 'main' END")


ROUTE_NAMESPACE_SQL = route_namespace_sql('session_key')
TARGET_INDEX = f'CREATE INDEX idx_gateway_routing_target ON gateway_routing({_INDEX_TARGET}, {ROUTE_NAMESPACE_SQL})'


def _point_predicates(sid, namespace):
    fields = '' if namespace is None else f' AND {ROUTE_NAMESPACE_SQL}=?'
    args = () if namespace is None else (namespace,)
    return f'{_INDEX_TARGET}=json_quote(?){fields}', (sid, *args), f"{_INDEX_TARGET}=x'00'{fields}", args


def _legacy_targets(conn, sid, where, args):
    """Python's historical decoder also accepted non-finite numbers and UTF-16/32 byte strings."""
    for scope, key, text in conn.execute(f'SELECT scope,session_key,entry_json FROM gateway_routing WHERE {where}', args):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict) and value.get('session_id') == sid:
            yield scope, key


def target_keys(conn, sid):
    point, args, legacy, extra = _point_predicates(sid, None)
    yield from conn.execute(f'SELECT scope,session_key FROM gateway_routing WHERE {point}', args)
    yield from _legacy_targets(conn, sid, legacy, extra)


def delete_target(conn, sid, namespace=None):
    point, args, legacy, extra = _point_predicates(sid, namespace)
    count = conn.execute(f'DELETE FROM gateway_routing WHERE {point}', args).rowcount
    keys = list(_legacy_targets(conn, sid, legacy, extra))
    conn.executemany('DELETE FROM gateway_routing WHERE scope=? AND session_key=?', keys)
    return count + len(keys)


def install_routing_index(cursor, definition):
    name = definition.split()[2]
    row = cursor.execute("SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (name,)).fetchone()
    if row is None or row[0] != definition:
        if row is not None:
            cursor.execute(f'DROP INDEX {name}')
        cursor.execute(definition)
