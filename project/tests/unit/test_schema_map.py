from threading import RLock
from types import SimpleNamespace

from flask import Flask

from routes.schema import bp
from services.schema_map_service import build_schema_map


def fixture():
    tables = [{'name': n, 'columns': [{'name': c} for c in ('id', 'tenant', 'parent')], 'foreign_keys': []}
              for n in ('opaque_001', 'opaque_002', 'isolated')]
    tables[0]['foreign_keys'] = [
        {'table': 'opaque_001', 'column': column, 'references_table': 'opaque_002',
         'references_column': column, 'constraint_name': 'composite', 'ordinal_position': i}
        for i, column in enumerate(('tenant', 'id'))
    ]
    tables[1]['foreign_keys'] = [{'table': 'opaque_002', 'column': 'parent', 'references_table': 'opaque_002',
                                'references_column': 'id', 'constraint_name': 'self'}]
    cache = SimpleNamespace(schema={'database': 'sample', 'tables': tables}, grouping={'groups': {'Operations': ['opaque_001']}},
                            semantic_relationships=[], is_loaded=lambda: True)
    knowledge = {'opaque_001': {'definition': 'Work orders', 'abbreviations': {'WO': {'meaning': 'Work order'}}}}
    return cache, SimpleNamespace(all=lambda: knowledge), knowledge


def test_map_preserves_composite_keys_self_references_and_isolated_tables():
    cache, defined, _ = fixture()
    result = build_schema_map(cache, defined)
    assert result['counts'] == {'tables': 3, 'relationships': 2, 'isolated': 1, 'abbreviations': 1}
    composite = next(e for e in result['edges'] if e['constraint'] == 'composite')
    assert composite['source_columns'] == composite['target_columns'] == ['tenant', 'id']
    assert any(e['source'] == e['target'] == 'opaque_002' for e in result['edges'])
    node = next(n for n in result['nodes'] if n['id'] == 'opaque_001')
    assert node['abbreviations'][0]['meaning'] == 'Work order'
    assert node['domain'] == 'Operations'


def test_map_excludes_pending_and_stale_relationships():
    cache, defined, knowledge = fixture()
    for status, column in [('approved', 'id'), ('pending', 'tenant'), ('approved', 'deleted')]:
        cache.semantic_relationships.append({'relationship_id': status + column, 'from_table': 'opaque_001',
                                            'from_column': column, 'to_table': 'opaque_002', 'to_column': 'id', 'status': status})
    knowledge['isolated'] = {'approval_status': 'PENDING', 'definition': 'Unapproved', 'abbreviations': {'BAD': {'meaning': 'Do not show'}}}
    result = build_schema_map(cache, defined)
    assert [e['constraint'] for e in result['edges'] if e['kind'] == 'semantic'] == ['approvedid']
    assert next(n for n in result['nodes'] if n['id'] == 'isolated')['definition'] == ''


def test_map_includes_all_300_tables_and_no_row_values():
    cache, defined, _ = fixture()
    cache.schema['tables'] = [{'name': f't_{i:03}', 'columns': [{'name': 'id', 'sample_values': ['private']}]} for i in range(300)]
    result = build_schema_map(cache, defined)
    assert len(result['nodes']) == 300
    assert result['counts']['isolated'] == 300
    assert 'private' not in str(result)


def test_map_api_refreshes_knowledge_and_handles_disconnected_cache():
    cache, defined, knowledge = fixture()
    app = Flask(__name__)
    app.extensions['services'] = {'cache': cache, 'defined_knowledge': defined, 'database_switch_lock': RLock()}
    app.register_blueprint(bp)
    client = app.test_client()
    first = client.get('/api/schema-map')
    assert first.status_code == 200 and first.headers['Cache-Control'] == 'no-store'
    knowledge['opaque_001']['abbreviations']['NEW'] = {'meaning': 'Newly approved'}
    assert client.get('/api/schema-map').json['counts']['abbreviations'] == 2
    cache.schema = {'database': 'another', 'tables': []}
    assert client.get('/api/schema-map').json['nodes'] == []
    cache.is_loaded = lambda: False
    assert client.get('/api/schema-map').status_code == 503
