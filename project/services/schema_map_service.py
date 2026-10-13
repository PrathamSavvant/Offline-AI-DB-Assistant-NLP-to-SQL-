"""Read-only presentation of the complete cached schema and approved knowledge."""
from core.relationship_builder import RelationshipBuilder


def build_schema_map(cache, defined):
    schema = cache.schema
    tables = {table['name']: table for table in schema.get('tables', [])}
    knowledge = defined.all()
    domains = {name: domain for domain, names in cache.grouping.get('groups', {}).items() for name in names}
    nodes = []
    for name, table in sorted(tables.items()):
        entry = knowledge.get(name, {})
        if str(entry.get('approval_status', 'USER_DEFINED')).upper() not in {'USER_DEFINED', 'USER_VERIFIED', 'APPROVED'}:
            entry = {}
        abbreviations = []
        for token, value in sorted(entry.get('abbreviations', {}).items()):
            value = value if isinstance(value, dict) else {'meaning': value}
            if str(value.get('approval_status', 'APPROVED')).upper() not in {'USER_DEFINED', 'USER_VERIFIED', 'APPROVED'}:
                continue
            if value.get('meaning'):
                abbreviations.append({'token': token, 'meaning': value['meaning'], 'column': value.get('column', '')})
        nodes.append({
            'id': name, 'name': name, 'domain': domains.get(name, 'Uncategorized'),
            'definition': entry.get('definition', ''), 'abbreviations': abbreviations,
            'columns': [{'name': c['name'], 'type': c.get('full_type') or c.get('datatype', ''),
                         'primary_key': bool(c.get('is_primary_key')), 'nullable': c.get('nullable')}
                        for c in table.get('columns', [])],
            'row_count': table.get('approx_row_count'), 'table_type': table.get('type', 'TABLE'),
        })
    column_names = {n: {c['name'] for c in t.get('columns', [])} for n, t in tables.items()}
    edges = []
    for rel in RelationshipBuilder(schema, cache.semantic_relationships).get_relationships():
        source, target = rel['from_table'], rel['to_table']
        left, right = rel['from_columns'], rel['to_columns']
        # A stale relationship must never appear as a valid connection.
        if (source not in tables or target not in tables or not left or len(left) != len(right)
                or not set(left) <= column_names[source] or not set(right) <= column_names[target]):
            continue
        edges.append({'id': f'edge-{len(edges)}', 'source': source, 'target': target,
                      'source_columns': left, 'target_columns': right,
                      'kind': rel['relationship_type'], 'constraint': rel['constraint_name'],
                      'description': rel.get('description', '')})
    connected = {e[key] for e in edges for key in ('source', 'target')}
    return {'database': schema.get('database', ''), 'nodes': nodes, 'edges': edges,
            'counts': {'tables': len(nodes), 'relationships': len(edges),
                       'isolated': len(tables.keys() - connected),
                       'abbreviations': sum(len(n['abbreviations']) for n in nodes)}}
