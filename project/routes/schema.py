from flask import Blueprint, current_app, request
from core.serializer import safe_jsonify

bp = Blueprint("schema", __name__)
def s(): return current_app.extensions["services"]

@bp.get('/api/schema-map')
def schema_map():
    from services.schema_map_service import build_schema_map
    services = s()
    with services['database_switch_lock']:
        if not services['cache'].is_loaded():
            return safe_jsonify(error='Connect to a database and complete a schema scan first.'), 503
        payload = build_schema_map(services['cache'], services['defined_knowledge'])
    response = safe_jsonify(**payload)
    response.headers['Cache-Control'] = 'no-store'
    return response

@bp.get("/api/tables")
def tables():
    context, annotations = s()["context"], s()["annotations"]
    rows = [{"name": n, "domain": context.table_to_domain.get(n, "Uncategorized"), "columns": [c["name"] for c in table.get("columns", [])], **annotations.get(n)} for n, table in context.tables_by_name.items()]
    return safe_jsonify(tables=sorted(rows, key=lambda item: item["name"]))
@bp.post("/api/tables/<table_name>/annotation")
def annotation(table_name):
    services = s(); context = services["context"]
    if table_name not in context.tables_by_name: return safe_jsonify(error=f"Unknown table '{table_name}'"), 404
    data = request.get_json(silent=True) or {}; aliases = data.get("aliases", [])
    if isinstance(aliases, str): aliases = [item.strip() for item in aliases.split(",") if item.strip()]
    entry = services["annotations"].set(table_name, (data.get("meaning") or "").strip(), aliases, enabled=bool(data.get("enabled", True)), priority=(data.get("priority") or "Medium").strip())
    context.update_annotations(services["annotations"].get_all())
    return safe_jsonify(table=table_name, annotation=entry)
