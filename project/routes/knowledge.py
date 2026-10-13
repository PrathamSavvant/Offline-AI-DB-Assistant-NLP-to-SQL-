"""Knowledge-management APIs unrelated to schema synchronization."""
from datetime import datetime
from pathlib import Path
import re

from flask import Blueprint, current_app, request

from core.schema_differ import SchemaDiffer
from core.serializer import safe_jsonify
from core.table_knowledge import TableKnowledgeRepository
from core.knowledge_impact import KnowledgeEffectivenessHarness
from core.knowledge_recommendations import KnowledgeRecommendationEngine
from core.logger import get_logger

bp = Blueprint("knowledge", __name__)
logger = get_logger(__name__)


def s(): return current_app.extensions["services"]
def synonyms(value): return [x.strip() for x in (value.split(",") if isinstance(value, str) else value or []) if x.strip()]
def _entry_data(data):
    return {
        "synonyms": synonyms(data.get("synonyms", [])),
        "aliases": synonyms(data.get("aliases", [])),
        "description": (data.get("description") or "").strip(),
        "example": (data.get("example") or "").strip(),
    }
def _refresh_dictionary(): s()["dictionary"].reload()


@bp.get("/api/semantic-relationships")
def semantic_relationships_list():
    return safe_jsonify(relationships=s()["semantic_relationships"].all())


@bp.post("/api/semantic-relationships")
def create_semantic_relationship():
    try:
        # Authoring and approval are separate actions. A newly proposed join
        # cannot become SQL authority in the same request.
        payload = {**(request.get_json(silent=True) or {}), "status": "pending"}
        item = s()["semantic_relationships"].create(payload, schema=s()["cache"].schema)
    except ValueError as exc:
        return safe_jsonify(error=str(exc)), 400
    s()["refresh_runtime_context"]()
    return safe_jsonify(relationship=item), 201


@bp.put("/api/semantic-relationships/<relationship_id>")
def update_semantic_relationship(relationship_id):
    try:
        item = s()["semantic_relationships"].update(
            relationship_id, request.get_json(silent=True) or {}, schema=s()["cache"].schema,
        )
    except KeyError:
        return safe_jsonify(error="Semantic relationship not found"), 404
    except ValueError as exc:
        return safe_jsonify(error=str(exc)), 400
    s()["refresh_runtime_context"]()
    return safe_jsonify(relationship=item)


@bp.delete("/api/semantic-relationships/<relationship_id>")
def disable_semantic_relationship(relationship_id):
    try:
        item = s()["semantic_relationships"].update(relationship_id, {"status": "disabled"})
    except KeyError:
        return safe_jsonify(error="Semantic relationship not found"), 404
    s()["refresh_runtime_context"]()
    return safe_jsonify(relationship=item)


def _sync_manual_profile(table_name, knowledge) -> None:
    """Persist a user edit into the profile without waiting for a schema crawl."""
    services = s()
    profile = services["cache"].table_profiles.get(table_name)
    if profile is None:
        return
    manual = profile.setdefault("manual_knowledge", {})
    manual.update({
        "business_entity": knowledge.get("business_entity", ""),
        "business_name": knowledge.get("business_name", ""),
        "definition": knowledge.get("definition", ""),
        "intended_usage": list(knowledge.get("intended_usage", [])),
        "exclusions": list(knowledge.get("exclusions", [])),
        "aliases": list(knowledge.get("aliases", [])),
        "synonyms": list(knowledge.get("synonyms", [])),
        "abbreviations": dict(knowledge.get("abbreviations", {})),
        "columns": dict(knowledge.get("columns", {})), "predicate_mappings": list(knowledge.get("predicate_mappings", [])),
        "record_grain": knowledge.get("record_grain", ""), "record_role": knowledge.get("record_role", ""),
        "current_record": dict(knowledge.get("current_record") or {}),
    })
    # Keep legacy readers coherent until their later retrieval refactor.
    profile["abbreviations"] = dict(manual["abbreviations"])
    TableKnowledgeRepository(services["cache"].knowledge_dir / "tables").save_profile(table_name, profile)
    services["cache"].inverted_index.update_manual_knowledge(table_name, knowledge)
    # The already-built request context keeps a snapshot for scoring. Refresh
    # that small mapping so the very next question sees the edit.
    context = services.get("context")
    if context is not None:
        context.rag_layer.lexical_retriever.defined_knowledge = services["defined_knowledge"].all()


def _knowledge_metrics(table_name, knowledge, schema_table):
    columns = schema_table.get("columns", [])
    column_knowledge = knowledge.get("columns", {})
    covered_columns = sum(1 for column in columns if column_knowledge.get(column.get("name"), {}).get("business_meaning"))
    checks = {
        "business_name": bool(knowledge.get("business_name")), "definition": bool(knowledge.get("definition")),
        "aliases": bool(knowledge.get("aliases")), "synonyms": bool(knowledge.get("synonyms")),
        "abbreviations": bool(knowledge.get("abbreviations")), "columns": bool(columns) and covered_columns == len(columns),
        "important_values": any(item.get("important_values") for item in column_knowledge.values()),
        "query_phrases": bool(knowledge.get("query_phrases") or knowledge.get("common_phrases")),
        "predicates": bool(knowledge.get("predicate_mappings")), "exclusions": bool(knowledge.get("exclusions")),
    }
    score = round(sum(checks.values()) / len(checks) * 100)
    return {"score": score, "checks": checks, "columns": {"covered": covered_columns, "total": len(columns)}}

def _recommendations():
    cache = s()["cache"]
    return KnowledgeRecommendationEngine(cache.schema, cache.relationships, cache.ranking, s()["defined_knowledge"])


def _defined_profile(table_name):
    services = s()
    schema_table = services["cache"].get_table(table_name)

    # [SCHEMA] diagnostic
    from core.db_context import db_context as _db_ctx
    logger.debug(
        "[SCHEMA] active_db=%s table_requested=%s table_found=%s scanned_count=%d",
        _db_ctx.active_database,
        table_name,
        schema_table is not None,
        len(services["cache"].get_table_names()),
    )

    if schema_table is None:
        return None
    defined = services["defined_knowledge"].table(table_name)
    cached_profile = services["cache"].table_profiles.get(table_name, {})

    # [KNOWLEDGE] diagnostic
    dk_ns = services["defined_knowledge"].path.parent.name
    pk_has_ctx = bool(services["pending_knowledge"].table_context(table_name))
    logger.debug(
        "[KNOWLEDGE] namespace=%s table=%s in_defined=%s in_pending_ctx=%s",
        dk_ns,
        table_name,
        services["defined_knowledge"].is_defined(defined),
        pk_has_ctx,
    )

    manual_abbrs = cached_profile.get("manual_knowledge", {}).get("abbreviations", {})
    needs_review = [
        entry for entry in manual_abbrs.values()
        if isinstance(entry, dict) and entry.get("status") == "needs_review"
    ]
    metrics = _knowledge_metrics(table_name, defined, schema_table)
    return {
        "table": table_name,
        "overview": {"type": schema_table.get("type"), "approx_row_count": schema_table.get("approx_row_count")},
        "knowledge": defined,
        "abbreviations": list(defined.get("abbreviations", {}).values()),
        "needs_review_abbreviations": needs_review,
        "manual_knowledge": cached_profile.get("manual_knowledge"),
        "system_knowledge": cached_profile.get("system_knowledge"),
        "relationships": services["cache"].get_relationships_for_table(table_name),
        "columns": schema_table.get("columns", []),
        "completeness": metrics,
        "priority": next((item for item in services["cache"].ranking.get("rankings", []) if item.get("table") == table_name), {}),
        "history": services["defined_knowledge"].history(table_name),
        "conflicts": [item for item in services["defined_knowledge"].conflicts() if any(candidate.get("table") == table_name for candidate in item["candidates"])],
    }


@bp.get("/api/knowledge")
def defined_knowledge_list():
    query = (request.args.get("search") or "").strip().lower()
    rows = []
    services = s()
    all_names = sorted(services["cache"].get_table_names())

    # [DB] + [SCHEMA] diagnostic
    from core.db_context import db_context as _db_ctx
    logger.info(
        "[DB] active_db=%s db_id=%s [SCHEMA] scanned_tables=%d [API] /api/knowledge search=%r",
        _db_ctx.active_database, _db_ctx.database_id, len(all_names), query,
    )

    for table in all_names:
        profile = _defined_profile(table)
        if not services["defined_knowledge"].is_defined(profile["knowledge"]):
            continue
        haystack = " ".join([table, profile["knowledge"].get("definition", ""), *profile["knowledge"].get("synonyms", [])]).lower()
        if not query or query in haystack:
            rows.append({
                "table": table, "defined": True,
                "definition": bool(profile["knowledge"].get("definition")),
                "synonym_count": len(profile["knowledge"].get("synonyms", [])),
                "abbreviation_count": len(profile["abbreviations"]),
            })
    logger.info("[API] /api/knowledge returning %d defined tables", len(rows))
    return safe_jsonify(tables=rows, total=len(rows))


@bp.get("/api/knowledge/pending")
def pending_knowledge():
    """Return pending-tables list enriched with per-table synonym/abbreviation pending counts."""
    services = s()
    raw_tables = services["pending_knowledge"].pending_tables(
        services["cache"].schema, services["defined_knowledge"],
        services["cache"].relationships, services["cache"].ranking,
    )
    raw_terms = services["pending_knowledge"].business_terms()
    requested_sort = (request.args.get("sort") or "priority").lower()
    source_filter = (request.args.get("source") or "").lower()
    confidence_filter = (request.args.get("confidence") or "").lower()
    query_tokens = [
        token for token in re.findall(
            r"[a-z0-9]+", (request.args.get("search") or "").lower()
        ) if token
    ]

    def matches(value):
        value_tokens = re.findall(r"[a-z0-9]+", str(value or "").lower())
        return bool(value_tokens) and all(
            any(word.startswith(token) for word in value_tokens)
            for token in query_tokens
        )

    # Build per-table pending abbreviation counts from business_terms
    pending_abbr_counts: dict = {}
    pending_abbr_by_table: dict = {}
    for term in raw_terms:
        tbl = term.get("table") or ""
        pending_abbr_counts[tbl] = pending_abbr_counts.get(tbl, 0) + 1
        pending_abbr_by_table.setdefault(tbl, []).append(term)

    # Enrich each pending table row
    enriched = []
    match_ranks = {}
    for row in raw_tables:
        name = row["table"]
        table_schema = services["cache"].get_table(name) or {}
        knowledge = services["defined_knowledge"].table(name)
        profile = services["cache"].table_profiles.get(name, {})
        manual = profile.get("manual_knowledge") or {}
        abbreviations = knowledge.get("abbreviations") or {}
        business_values = [
            knowledge.get("definition"), knowledge.get("business_name"),
            *(knowledge.get("synonyms") or []), *(knowledge.get("aliases") or []),
            profile.get("business_name"), profile.get("purpose"),
            *(profile.get("aliases") or []), manual.get("definition"),
            manual.get("business_name"), *(manual.get("synonyms") or []),
            *(manual.get("aliases") or []),
            services["pending_knowledge"].table_context(name),
        ]
        abbreviation_values = [
            *abbreviations.keys(),
            *(entry.get("meaning", "") if isinstance(entry, dict) else entry
              for entry in abbreviations.values()),
            *(row.get("unidentified_tokens") or []),
        ]
        columns = table_schema.get("columns") or []
        column_match = next((
            column.get("name", "") for column in columns
            if matches(column.get("name")) or matches(column.get("comment"))
        ), None) if query_tokens else None
        match = None
        if query_tokens:
            if matches(name):
                name_tokens = re.findall(r"[a-z0-9]+", name.lower())
                if name_tokens == query_tokens:
                    match = (0, "Exact table name")
                elif len(name_tokens) >= len(query_tokens) and all(
                    word.startswith(token)
                    for word, token in zip(name_tokens, query_tokens)
                ):
                    match = (1, "Table name")
                else:
                    match = (2, "Table name")
            elif any(matches(value) for value in business_values):
                match = (3, "Business meaning")
            elif any(matches(value) for value in abbreviation_values):
                match = (4, "Abbreviation or pending token")
            elif column_match is not None:
                match = (5, f"Column: {column_match}")
            elif matches(table_schema.get("comment")):
                match = (6, "Table comment")
            if match is None:
                continue
        item = {
            **row,
            "pending_synonym_count": 0,           # synonyms are user-entered; no auto-suggestions yet
            "pending_abbreviation_count": pending_abbr_counts.get(name, 0),
        }
        if match is not None:
            match_ranks[name] = match[0]
            item["search_match"] = match[1]
        if source_filter and source_filter not in str(row).lower():
            continue
        if confidence_filter and confidence_filter not in str(row).lower():
            continue
        enriched.append(item)

    # Keep the visible order strict even if a future repository adds another
    # presentation sort before this endpoint.
    sort_keys = {
        "table": lambda item: (item["table"].lower(),),
        "row_count": lambda item: (-item.get("approx_row_count", 0), item["table"].lower()),
        "priority": lambda item: (-item.get("unidentified_token_count", 0), item["table"].lower()),
        "confidence": lambda item: (-item.get("pending_abbreviation_count", 0), item["table"].lower()),
        "recently_discovered": lambda item: (item["table"].lower(),),
    }
    if query_tokens and requested_sort == "priority":
        enriched.sort(key=lambda item: (
            match_ranks[item["table"]], -item.get("unidentified_token_count", 0),
            item["table"].lower(),
        ))
    else:
        enriched.sort(key=sort_keys.get(requested_sort, sort_keys["priority"]))

    return safe_jsonify(
        pending_tables=enriched,
        pending_business_terms=raw_terms,
    )


@bp.get("/api/knowledge/<table_name>/pending")
def table_pending_suggestions(table_name):
    """Return pending business-term suggestions scoped to a single table (for review mode)."""
    profile = _defined_profile(table_name)
    if profile is None:
        logger.warning(
            "[API] /api/knowledge/%s/pending — table not found in schema (scanned tables: %s)",
            table_name,
            sorted(s()["cache"].get_table_names()),
        )
        return safe_jsonify(error=f"Unknown table '{table_name}'"), 404
    services = s()
    all_terms = services["pending_knowledge"].business_terms()
    table_terms = [t for t in all_terms if t.get("table") == table_name]
    pending_ctx = services["pending_knowledge"].table_context(table_name)
    logger.info(
        "[API] /api/knowledge/%s/pending — pending_terms=%d pending_ctx=%r namespace=%s",
        table_name, len(table_terms), pending_ctx[:60] if pending_ctx else "",
        services["pending_knowledge"].path.parent.name,
    )
    return safe_jsonify(table=table_name, pending_table_context=pending_ctx, pending_abbreviations=table_terms)


@bp.post("/api/knowledge/pending/table-context")
def save_pending_table_context():
    data = request.get_json(silent=True) or {}
    table = str(data.get("table") or "").strip()
    if _defined_profile(table) is None:
        return safe_jsonify(error=f"Unknown table '{table}'"), 404
    context = s()["pending_knowledge"].save_table_context(table, str(data.get("context") or ""))
    return safe_jsonify(table=table, pending_table_context=context)


@bp.get("/api/knowledge/<table_name>")
def defined_knowledge_profile(table_name):
    profile = _defined_profile(table_name)
    if profile is None:
        return safe_jsonify(error=f"Unknown table '{table_name}'"), 404
    return safe_jsonify(profile=profile)


@bp.post("/api/knowledge/table")
def update_defined_table_knowledge():
    data = request.get_json(silent=True) or {}
    table = str(data.get("table") or "").strip()
    profile = _defined_profile(table)
    if profile is None:
        return safe_jsonify(error=f"Unknown table '{table}'"), 404
    rule = data.get("current_record")
    if rule and (not isinstance(rule, dict) or rule.get("column") not in {column.get("name") for column in profile["columns"]}):
        return safe_jsonify(error="Current-record rule must use a real column on this table"), 400
    try:
        entry = s()["defined_knowledge"].update_table(table, data)
    except ValueError as exc:
        return safe_jsonify(error=str(exc)), 400
    _sync_manual_profile(table, entry)
    return safe_jsonify(table=table, knowledge=entry)


@bp.post("/api/knowledge/business-term")
def upsert_business_term():
    data = request.get_json(silent=True) or {}
    table = str(data.get("table") or "").strip()
    if _defined_profile(table) is None:
        return safe_jsonify(error=f"Unknown table '{table}'"), 404
    try:
        term = s()["defined_knowledge"].upsert_business_term(table, data)
    except ValueError as exc:
        return safe_jsonify(error=str(exc)), 400
    _sync_manual_profile(table, s()["defined_knowledge"].table(table))
    return safe_jsonify(table=table, abbreviation=term)


@bp.post("/api/knowledge/column")
def update_column_knowledge():
    data = request.get_json(silent=True) or {}; table = str(data.get("table") or "").strip(); column = str(data.get("column") or "").strip()
    profile = _defined_profile(table)
    if profile is None or column not in {item.get("name") for item in profile["columns"]}:
        return safe_jsonify(error="Unknown table or column"), 404
    entry = s()["defined_knowledge"].update_column(table, column, data); _sync_manual_profile(table, s()["defined_knowledge"].table(table))
    return safe_jsonify(table=table, column=column, knowledge=entry)


@bp.post("/api/knowledge/predicate")
def upsert_predicate_knowledge():
    data = request.get_json(silent=True) or {}; table = str(data.get("table") or "").strip()
    profile = _defined_profile(table)
    if profile is None or str(data.get("column") or "") not in {item.get("name") for item in profile["columns"]}:
        return safe_jsonify(error="Unknown table or column"), 404
    try: item = s()["defined_knowledge"].upsert_predicate(table, data)
    except ValueError as exc: return safe_jsonify(error=str(exc)), 400
    _sync_manual_profile(table, s()["defined_knowledge"].table(table))
    return safe_jsonify(table=table, predicate=item, conflicts=s()["defined_knowledge"].conflicts())


@bp.get("/api/knowledge/conflicts")
def knowledge_conflicts():
    return safe_jsonify(conflicts=s()["defined_knowledge"].conflicts())

@bp.get("/api/knowledge/recommendations")
def knowledge_recommendations():
    table = (request.args.get("table") or "").strip()
    engine = _recommendations()
    return safe_jsonify(tables=engine.prioritized_tables()) if not table else safe_jsonify(table=table, recommendations=engine.recommendations(table))

@bp.get("/api/knowledge/health")
def knowledge_health():
    engine = _recommendations(); rows = engine.prioritized_tables(); pending = s()["pending_knowledge"]
    feedback = pending.feedback_recommendations(); diagnostics = pending.query_diagnostics()
    conflicts = s()["defined_knowledge"].conflicts()
    overall = round(sum(row["completeness"]["score"] for row in rows) / len(rows)) if rows else 0
    high = [row for row in rows if row["priority"]["tier"] == "HIGH" and row["completeness"]["score"] < 100]
    stale = []
    for table, entry in s()["defined_knowledge"].all().items():
        live = s()["cache"].get_table(table)
        if live is None: stale.append({"table": table, "reason": "table removed"}); continue
        cols = {c.get("name") for c in live.get("columns", [])}
        for column in entry.get("columns", {}):
            if column not in cols: stale.append({"table": table, "column": column, "reason": "column removed"})
    return safe_jsonify(overall_coverage=overall, high_priority_gaps=len(high), pending_review=len(feedback) + len(pending.business_terms()), conflicts=len(conflicts), stale=stale, recent_query_issues=sum(bool(item.get("findings")) for item in diagnostics[-20:]), recommendations=feedback[-20:], diagnostics=diagnostics[-20:])

@bp.get("/api/knowledge/search")
def knowledge_search():
    query = (request.args.get("q") or "").lower(); table_filter = request.args.get("table") or ""
    engine = _recommendations(); results = []
    for row in engine.prioritized_tables():
        if table_filter and row["table"] != table_filter: continue
        text = " ".join([row["table"], str(row), *[str(x) for x in engine.recommendations(row["table"])]]).lower()
        if not query or all(token in text for token in re.findall(r"[a-z0-9]+", query)):
            results.append(row)
    return safe_jsonify(results=results)

@bp.post("/api/knowledge/recommendations/approve")
def approve_knowledge_recommendation():
    data = request.get_json(silent=True) or {}; table = str(data.get("table") or "").strip(); kind = str(data.get("type") or "").strip(); payload = data.get("payload") or {}
    profile = _defined_profile(table)
    if profile is None: return safe_jsonify(error="Unknown table"), 404
    columns = {x.get("name") for x in profile["columns"]}
    try:
        if kind in {"business_name", "table_definition"}:
            entry = s()["defined_knowledge"].update_table(table, {"business_name" if kind == "business_name" else "definition": data.get("value") or data.get("suggested_value") or "", "source": "USER_APPROVED", "approval_status": "APPROVED"})
        elif kind == "column_meaning":
            column = str(payload.get("column") or "");
            if column not in columns: return safe_jsonify(error="Stale column reference"), 409
            entry = s()["defined_knowledge"].update_column(table, column, {"business_meaning": data.get("value") or data.get("suggested_value") or "", "source": "USER_APPROVED", "approval_status": "APPROVED"})
        elif kind == "important_value":
            column, value = str(payload.get("column") or ""), payload.get("value")
            if column not in columns: return safe_jsonify(error="Stale column reference"), 409
            current = s()["defined_knowledge"].table(table).get("columns", {}).get(column, {})
            values = dict(current.get("important_values", {})); values[str(value)] = str(data.get("meaning") or value)
            entry = s()["defined_knowledge"].update_column(table, column, {**current, "important_values": values, "source": "USER_APPROVED", "approval_status": "APPROVED"})
        elif kind == "predicate":
            column = str(payload.get("column") or "")
            if column not in columns: return safe_jsonify(error="Stale column reference"), 409
            entry = s()["defined_knowledge"].upsert_predicate(table, {"phrase": data.get("phrase") or payload.get("phrase"), "column": column, "value": payload.get("value"), "source": "USER_APPROVED", "approval_status": "APPROVED"})
        else: return safe_jsonify(error="Unsupported recommendation type"), 400
    except ValueError as exc: return safe_jsonify(error=str(exc)), 400
    _sync_manual_profile(table, s()["defined_knowledge"].table(table))
    return safe_jsonify(table=table, approved=entry, conflicts=s()["defined_knowledge"].conflicts())


@bp.post("/api/knowledge/impact")
def knowledge_impact():
    question = str((request.get_json(silent=True) or {}).get("question") or "").strip()
    if not question: return safe_jsonify(error="'question' is required"), 400
    builder = current_app.extensions.get("knowledge_impact_builder")
    if builder is None: return safe_jsonify(error="Knowledge diagnostics unavailable"), 503
    return safe_jsonify(KnowledgeEffectivenessHarness(builder).compare(question))


@bp.delete("/api/knowledge/business-term/<table_name>/<abbreviation>")
def delete_business_term(table_name, abbreviation):
    if not s()["defined_knowledge"].delete_business_term(table_name, abbreviation):
        return safe_jsonify(error="Business term not found"), 404
    _sync_manual_profile(table_name, s()["defined_knowledge"].table(table_name))
    return safe_jsonify(deleted=abbreviation.upper(), table=table_name)


@bp.post("/api/knowledge/pending/approve")
def approve_pending_knowledge():
    data = request.get_json(silent=True) or {}
    logger.info("[API] request received for /api/knowledge/pending/approve (type=%s)", data.get("type"))
    if data.get("type") == "table_context":
        table = str(data.get("table") or "").strip()
        # The frontend may send the live textarea value as "context" so the
        # user does not need a prior "Save for review" step before accepting.
        inline_context = str(data.get("context") or "").strip() or None
        if inline_context is not None:
            logger.info("[KNOWLEDGE] inline context received for table=%s (length=%d)", table, len(inline_context))
        if _defined_profile(table) is None:
            logger.warning(
                "[API] /api/knowledge/pending/approve table_context — unknown table=%s", table
            )
            return safe_jsonify(error=f"Unknown table '{table}'"), 404
        try:
            entry = s()["pending_knowledge"].accept_table_context(
                table, s()["defined_knowledge"], inline_context=inline_context
            )
        except ValueError as exc:
            logger.warning("[API] response 400: %s", exc)
            return safe_jsonify(error=str(exc)), 400
        _sync_manual_profile(table, entry)
        # Rebuild the inverted-index snapshot so the very next RAG call
        # already sees the newly promoted definition.
        context = s().get("context")
        if context is not None:
            context.rag_layer.lexical_retriever.defined_knowledge = s()["defined_knowledge"].all()
        logger.info(
            "[KNOWLEDGE] pending/approved transition completed for table=%s (source=%s)",
            table, entry.get("source", "USER_APPROVED"),
        )
        logger.info("[API] response 200: table_context approved for table=%s", table)
        return safe_jsonify(table=table, knowledge=entry)
    if data.get("type") == "table":
        table = str(data.get("table") or data.get("id") or "").strip()
        if _defined_profile(table) is None:
            return safe_jsonify(error=f"Unknown table '{table}'"), 404
        entry = s()["defined_knowledge"].update_table(table, data)
        _sync_manual_profile(table, entry)
        s()["pending_knowledge"].reject("table", table)
        return safe_jsonify(table=table, knowledge=entry)
    pending_id = str(data.get("id") or "")
    pending_item = next((item for item in s()["pending_knowledge"].business_terms() if item["id"] == pending_id), {})
    try:
        term = s()["pending_knowledge"].approve_term(pending_id, s()["defined_knowledge"], data)
    except KeyError:
        return safe_jsonify(error="Pending business term not found"), 404
    except ValueError as exc:
        return safe_jsonify(error=str(exc)), 400
    table = str(data.get("table") or pending_item.get("table") or "").strip()
    if table:
        _sync_manual_profile(table, s()["defined_knowledge"].table(table))
    return safe_jsonify(abbreviation=term)


@bp.post("/api/knowledge/pending/reject")
def reject_pending_knowledge():
    data = request.get_json(silent=True) or {}
    item_type, item_id = str(data.get("type") or "business_term"), str(data.get("id") or "")
    if not item_id or not s()["pending_knowledge"].reject(item_type, item_id):
        return safe_jsonify(error="Pending knowledge item not found"), 404
    return safe_jsonify(rejected=item_id, type=item_type)


@bp.post("/api/knowledge/pending/bulk")
def bulk_pending_knowledge():
    data = request.get_json(silent=True) or {}; action = str(data.get("action") or "").lower(); items = data.get("items") or []
    if action not in {"approve", "reject"} or not isinstance(items, list):
        return safe_jsonify(error="'action' must be approve/reject and 'items' must be a list"), 400
    completed, errors = [], []
    for item in items:
        item = item if isinstance(item, dict) else {"id": item}
        try:
            if action == "reject":
                if s()["pending_knowledge"].reject(str(item.get("type") or "business_term"), str(item.get("id") or "")): completed.append(item)
                else: errors.append({"item": item, "error": "Not found"})
            else:
                term = s()["pending_knowledge"].approve_term(str(item.get("id") or ""), s()["defined_knowledge"], item)
                table = str(item.get("table") or "").strip()
                if table: _sync_manual_profile(table, s()["defined_knowledge"].table(table))
                completed.append({"item": item, "term": term})
        except (KeyError, ValueError) as exc: errors.append({"item": item, "error": str(exc)})
    return safe_jsonify(action=action, completed=completed, errors=errors)


def _attach_profile_abbreviation(token, entry, pending_entry=None) -> None:
    """Keep a newly approved business term immediately available per table.

    The global store remains a compatibility API during migration; profile
    ownership is updated here without waiting for a crawl or Flask restart.
    """
    pending_entry = pending_entry or s()["pending"].get_all().get(token.upper(), {})
    table_name = pending_entry.get("table")
    profiles = s()["cache"].table_profiles
    if not table_name or table_name not in profiles:
        return
    profile = profiles[table_name]
    profile.setdefault("abbreviations", {})[token.upper()] = entry
    TableKnowledgeRepository(s()["cache"].knowledge_dir / "tables").save_profile(table_name, profile)


@bp.get("/api/knowledge/<table_name>/needs-review")
def table_needs_review_abbreviations(table_name):
    profile = _defined_profile(table_name)
    if profile is None:
        return safe_jsonify(error=f"Unknown table '{table_name}'"), 404
    return safe_jsonify(
        table=table_name,
        needs_review_abbreviations=profile.get("needs_review_abbreviations", []),
    )


@bp.get("/api/knowledge/tables")
def table_knowledge_profiles():
    """Future-table-knowledge endpoint; reads cached profiles only."""
    profiles = s()["cache"].table_profiles
    tables = []
    for name, profile in sorted(profiles.items()):
        manual_abbrs = profile.get("manual_knowledge", {}).get("abbreviations", {})
        needs_review_count = sum(
            1 for entry in manual_abbrs.values()
            if isinstance(entry, dict) and entry.get("status") == "needs_review"
        )
        tables.append({
            "table": name,
            "business_name": profile.get("business_name", name),
            "purpose": profile.get("purpose", ""),
            "aliases": profile.get("aliases", []),
            "knowledge_status": "ready",
            "needs_review_count": needs_review_count,
        })
    return safe_jsonify(
        tables=tables,
        total=len(tables),
    )


@bp.get("/api/knowledge/tables/<table_name>")
def table_knowledge_profile(table_name):
    profile = s()["cache"].table_profiles.get(table_name)
    if profile is None:
        return safe_jsonify(error=f"Unknown table knowledge profile '{table_name}'"), 404
    return safe_jsonify(profile=profile)


@bp.route("/api/dictionary", methods=["GET", "POST"])
def dictionary():
    if request.method == "GET":
        query = (request.args.get("search") or "").strip()
        return safe_jsonify(entries=s()["dictionary"].search(query), total=len(s()["dictionary"].search(query)))
    data=request.get_json(silent=True) or {}; abbr=(data.get("abbreviation") or "").strip(); expansion=(data.get("expansion") or "").strip()
    if not abbr or not expansion: return safe_jsonify(error="Both 'abbreviation' and 'expansion' are required"),400
    try:
        entry=s()["dictionary"].set(abbr, expansion, **_entry_data(data)); _refresh_dictionary()
        return safe_jsonify(abbreviation=abbr.upper(), **entry)
    except ValueError as exc: return safe_jsonify(error=str(exc)),400


@bp.post("/api/dictionary/<abbreviation>")
def edit_dictionary(abbreviation):
    data=request.get_json(silent=True) or {}; expansion=(data.get("expansion") or "").strip()
    if not expansion: return safe_jsonify(error="'expansion' is required"),400
    try:
        entry=s()["dictionary"].set(abbreviation, expansion, **_entry_data(data)); _refresh_dictionary()
        return safe_jsonify(abbreviation=abbreviation.upper(), **entry)
    except ValueError as exc: return safe_jsonify(error=str(exc)),400


@bp.delete("/api/dictionary/<abbreviation>")
def delete_dictionary(abbreviation):
    if not s()["dictionary"].delete(abbreviation): return safe_jsonify(error=f"Unknown abbreviation '{abbreviation}'"),404
    _refresh_dictionary(); return safe_jsonify(deleted=abbreviation.upper())


@bp.post("/api/dictionary/<abbreviation>/synonym")
def add_synonym(abbreviation):
    value=((request.get_json(silent=True) or {}).get("synonym") or "").strip()
    if not value: return safe_jsonify(error="'synonym' is required"),400
    entry=s()["dictionary"].add_synonym(abbreviation,value)
    return (safe_jsonify(error=f"Unknown abbreviation '{abbreviation}'"),404) if entry is None else safe_jsonify(abbreviation=abbreviation.upper(), **entry)


@bp.get("/api/pending-dictionary")
def pending():
    query=(request.args.get("search") or "").lower()
    entries=[{"token": k, **v} for k,v in sorted(s()["pending"].get_all().items())]
    if query: entries=[entry for entry in entries if query in str(entry).lower()]
    return safe_jsonify(entries=entries, total=len(entries))


@bp.post("/api/pending-dictionary/<token>/resolve")
def resolve(token):
    data=request.get_json(silent=True) or {}; expansion=(data.get("expansion") or "").strip()
    if not expansion: return safe_jsonify(error="'expansion' is required"),400
    pending_entry = s()["pending"].get_all().get(token.upper(), {})
    entry=s()["pending"].resolve(token, expansion, s()["dictionary"], **_entry_data(data)); _attach_profile_abbreviation(token, entry, pending_entry); _refresh_dictionary()
    return safe_jsonify(abbreviation=token.upper(), **entry)


@bp.delete("/api/pending-dictionary/<token>")
def dismiss(token): s()["pending"].dismiss(token); return safe_jsonify(dismissed=token.upper())


@bp.post("/api/pending-dictionary/<token>/approve")
def approve(token): return resolve(token)


@bp.post("/api/pending-dictionary/<token>/reject")
def reject(token): return dismiss(token)


from core.db_context import db_context


@bp.get("/api/crawler/dashboard")
def dashboard():
    tables=s()["cache"].tables; path=db_context.get_knowledge_dir()/"schema.json"; diff=SchemaDiffer.load_latest_diff()
    return safe_jsonify(last_crawl=datetime.fromtimestamp(path.stat().st_mtime).isoformat() if path.exists() else None,total_tables=len(tables),total_columns=sum(len(t.get("columns",[])) for t in tables),total_relationships=len(s()["cache"].relationships.get("relationships",[])),new_tables=diff.get("added_tables",[]),new_columns=diff.get("added_columns",[]),unknown_abbreviations=len(s()["pending"].get_all()))


@bp.get("/api/schema-changes")
def schema_changes(): return safe_jsonify(latest=SchemaDiffer.load_latest_diff(),history=SchemaDiffer.load_history())
