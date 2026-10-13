"""core/value_resolver.py

Value Resolver — Discover and index business values inside selected schema lookup columns.

Extracts low-cardinality values (enums, lookup codes, categories, statuses, group names)
from schema metadata, business dictionary, annotations, and database distinct queries.
Executes lightweight in-memory value resolution in < 100ms.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any, Dict, List, Optional, Set

from config.stop_words import STOP_WORDS as GLOBAL_STOP_WORDS
from config.settings import settings
from core.logger import get_logger

logger = get_logger(__name__)

LOOKUP_COLUMN_KEYWORDS = {
    "status", "state", "type", "category", "class", "kind", "group", "code",
    "grade", "unit", "dept", "department", "flag", "mode", "phase", "role",
    "name", "repair", "class_name", "type_name", "status_name", "cat", "sus"
}

CATEGORICAL_COLUMN_KEYWORDS = {
    "status", "state", "type", "category", "class", "kind", "sus", "repair_class", "grade", "mode", "phase"
}

EXCLUDED_COLUMN_KEYWORDS = {
    "remark", "description", "note", "comment", "log", "attachment", "detail",
    "message", "body", "text", "payload", "history", "address", "reason",
    "created_at", "updated_at", "timestamp", "date", "time", "password", "hash"
}

LOOKUP_LABELS = {"group", "type", "class", "category", "department", "unit", "status", "state"}
STOP_WORDS = {
    "in", "the", "for", "a", "an", "under", "over", "with", "by", "at",
    "from", "to", "of", "on", "and", "or", "is", "are", "show", "list",
    "find", "get", "display", "which", "what", "all", "any", "each",
} | GLOBAL_STOP_WORDS


@dataclass(frozen=True)
class ResolvedValue:
    value: Any
    table: str
    column: str
    confidence: float
    reason: str
    source: str = "Value Resolver"
    semantic_type: str = "UNKNOWN"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ValueIndex:
    """Lightweight in-memory Value Index mapping natural language fragments to (table, column, value) triples."""

    def __init__(self):
        # Map: lowercase_key -> List[ResolvedValue]
        self.exact_map: Dict[str, List[ResolvedValue]] = {}
        # Map: lowercase_token -> List[ResolvedValue]
        self.token_map: Dict[str, List[ResolvedValue]] = {}

    def add(self, key: str, value: Any, table: str, column: str, confidence: float, reason: str) -> None:
        norm_key = key.strip().lower()
        if not norm_key:
            return
        entry = ResolvedValue(
            value=value,
            table=table,
            column=column,
            confidence=confidence,
            reason=reason,
            source="Value Resolver",
        )
        self.exact_map.setdefault(norm_key, []).append(entry)
        for token in re.findall(r"[a-z0-9]+", norm_key):
            if len(token) >= 2 and token not in STOP_WORDS:
                self.token_map.setdefault(token, []).append(entry)

    def lookup(self, phrase: str) -> List[ResolvedValue]:
        norm = phrase.strip().lower()
        return self.exact_map.get(norm, [])


NON_VALUE_TOKENS = {
    "unique", "distinct", "name", "names", "manager", "managers", "employee", "employees",
    "worker", "workers", "record", "records", "table", "tables", "detail", "details",
    "list", "all", "each", "every", "total", "count", "show", "get", "which", "what",
    "data", "info", "information", "item", "items", "history", "average", "avg", "sum",
    "max", "min", "maximum", "minimum", "top", "bottom", "company", "system",
} | GLOBAL_STOP_WORDS


class ValueResolver:
    """Schema-driven, offline-friendly Value Resolver."""

    def __init__(
        self,
        schema: Dict[str, Any],
        dictionary: Any = None,
        semantics: Optional[Dict[str, Any]] = None,
        annotations: Optional[Dict[str, Any]] = None,
        database_adapter: Any = None,
        defined_knowledge: Any = None,
    ):
        self.schema = schema
        self.dictionary = dictionary
        self.semantics = semantics or {}
        self.annotations = annotations or {}
        self.database_adapter = database_adapter
        self.defined_knowledge = defined_knowledge
        self.tables = {table["name"]: table for table in schema.get("tables", [])}
        self.index = ValueIndex()
        # Only query-needed values are sampled. This bounded, expiring cache
        # never treats yesterday's live statuses as permanent schema facts.
        self._observed_value_cache: OrderedDict[tuple, tuple[float, List[Any]]] = OrderedDict()
        self._observed_cache_lock = Lock()
        self.build_index()

    @staticmethod
    def _words(value: str) -> Set[str]:
        return set(re.findall(r"[a-z0-9]+", str(value).replace("_", " ").lower()))

    def _semantic_type(self, table_name: str, column: Dict[str, Any]) -> str:
        name = column["name"].lower()
        words = self._words(name)
        if "status" in words or "state" in words:
            return "STATUS"
        if "group" in words or (table_name.rstrip("s").lower() == "group" and "name" in words):
            return "GROUP"
        if words & {"type", "category", "class", "kind", "grade", "mode", "phase"}:
            return "CATEGORY"
        if words & {"date", "time", "year", "month"}:
            return "DATE_TIME"
        if words & {"quantity", "amount", "count", "total", "number"}:
            return "QUANTITY"
        if name.endswith("_id") or words & {"code", "identifier", "number"}:
            return "IDENTIFIER"
        if "name" in words or "title" in words:
            return "NAME"
        return "ATTRIBUTE"

    def _observed_matches(self, table: str, column: str, phrase: str) -> List[Any]:
        """Read only matching categorical values when a database adapter is supplied."""
        key = (table, column, phrase.strip().lower())
        now = time.monotonic()
        with self._observed_cache_lock:
            cached = self._observed_value_cache.get(key)
            if cached is not None:
                expires_at, values = cached
                if now < expires_at:
                    self._observed_value_cache.move_to_end(key)
                    return list(values)
                del self._observed_value_cache[key]
        if self.database_adapter is None:
            return []
        adapter = self.database_adapter() if isinstance(self.database_adapter, type) or (callable(self.database_adapter) and not hasattr(self.database_adapter, "execute")) else self.database_adapter
        try:
            rows = adapter.execute(
                f"SELECT DISTINCT `{column}` AS value FROM `{table}` "
                f"WHERE `{column}` IS NOT NULL AND LOWER(`{column}`) LIKE %s LIMIT 20",
                (f"%{sorted(self._words(phrase) - {'under', 'waiting', 'awaiting', 'for'})[-1] if self._words(phrase) - {'under', 'waiting', 'awaiting', 'for'} else phrase.lower()}%",),
            )
            values = [row.get("value") for row in rows if row.get("value") is not None]
        except Exception as exc:
            logger.debug("Observed value lookup unavailable for %s.%s (%s)", table, column, type(exc).__name__)
            return []
        finally:
            if adapter is not self.database_adapter and hasattr(adapter, "close"):
                adapter.close()
        ttl = max(0, settings.VALUE_LOOKUP_CACHE_TTL_SECONDS)
        capacity = max(0, settings.VALUE_LOOKUP_CACHE_MAX_ENTRIES)
        # Do not retain empty results: a value newly added to a live workshop
        # database must be available on the next question.
        if values and ttl and capacity:
            with self._observed_cache_lock:
                self._observed_value_cache[key] = (time.monotonic() + ttl, list(values))
                self._observed_value_cache.move_to_end(key)
                while len(self._observed_value_cache) > capacity:
                    self._observed_value_cache.popitem(last=False)
        return values

    def _rank_state_candidates(self, phrase: str, selected: List[str]) -> List[ResolvedValue]:
        """Rank state-like values by semantic role, exact observed value, and table relevance."""
        phrase_words = self._words(phrase) - {"under", "waiting", "awaiting", "for"}
        ranked: List[tuple] = []
        for table_rank, table_name in enumerate(selected):
            columns = self.tables.get(table_name, {}).get("columns", [])
            has_status_column = any(self._semantic_type(table_name, column) == "STATUS" for column in columns)
            for column in columns:
                if not self._is_lookup_column(table_name, column):
                    continue
                role = self._semantic_type(table_name, column)
                # A state phrase should not become a class/type predicate just
                # because one token (for example "repair") happens to match.
                if role not in {"STATUS", "CATEGORY", "ATTRIBUTE"}:
                    continue
                # When the schema explicitly supplies a status/state field,
                # category/class fields are weaker evidence for a state phrase
                # and need not trigger extra database probes.
                if has_status_column and role != "STATUS":
                    continue
                name_words = self._words(column["name"])
                observed = self._observed_matches(table_name, column["name"], phrase)
                exact_values = [value for value in observed if phrase.lower() == str(value).lower()]
                token_values = [value for value in observed if phrase_words <= self._words(str(value))]
                values = exact_values or token_values
                if values:
                    value = values[0]
                    value_score = 70 if exact_values else 55
                    evidence = "observed database value"
                else:
                    value = phrase.title()
                    value_score = 0
                    evidence = "schema role"
                role_score = {"STATUS": 80, "CATEGORY": 20, "ATTRIBUTE": 5}[role]
                column_score = len(name_words & phrase_words) * 8
                score = role_score + value_score + column_score - table_rank
                ranked.append((score, table_name, column["name"], value, role, evidence))
        ranked.sort(key=lambda item: (-item[0], item[1], item[2], str(item[3])))
        if not ranked:
            return []
        best = ranked[0]
        # Keep ambiguity only for genuinely equal evidence; otherwise emit the
        # deterministic winner rather than asking the LLM to rediscover it.
        tied = [item for item in ranked if item[0] == best[0]]
        winners = tied if len(tied) > 1 else [best]
        return [ResolvedValue(
            value=item[3], table=item[1], column=item[2],
            confidence=round(min(1.0, 0.70 + item[0] / 250), 3),
            reason=f"state/status candidate ranked from {item[5]}",
            source="Value Resolver", semantic_type=item[4],
        ) for item in winners]

    def _is_lookup_column(self, table_name: str, column: Dict[str, Any]) -> bool:
        col_name = column["name"].lower()
        datatype = str(column.get("datatype", "")).lower()
        full_type = str(column.get("full_type", "")).lower()

        # Exclusions
        if any(kw in col_name for kw in EXCLUDED_COLUMN_KEYWORDS):
            return False
        if datatype in {"text", "longtext", "mediumtext", "blob", "json"}:
            return False

        # Inclusions
        if any(kw in col_name for kw in LOOKUP_COLUMN_KEYWORDS):
            return True
        if col_name.endswith(("_id", "_code", "_type", "_cat", "_status", "_class", "_group", "_name")):
            return True
        if full_type.startswith(("enum", "set")):
            return True
        if col_name in {"name", "title", f"{table_name.rstrip('s')}_name"}:
            return True

        return False

    def build_index(self) -> None:
        """Build/rebuild the lightweight in-memory Value Index."""
        t0 = time.perf_counter()
        logger.info("Building Value Index for %d tables...", len(self.tables))
        self.index = ValueIndex()

        for table_name, table in self.tables.items():
            for column in table.get("columns", []):
                if not self._is_lookup_column(table_name, column):
                    continue

                col_name = column["name"]
                full_type = str(column.get("full_type", ""))

                # 1. ENUM values from column schema
                if full_type.lower().startswith("enum"):
                    matches = re.findall(r"'([^']+)'", full_type)
                    for val in matches:
                        self.index.add(val, val, table_name, col_name, 1.0, "enum definition")

                # 2. Extract values from column comment metadata
                comment = column.get("comment", "")
                if comment:
                    for token in re.findall(r"\b[A-Za-z0-9_-]{2,}\b", comment):
                        if token.lower() not in STOP_WORDS:
                            self.index.add(token, token, table_name, col_name, 0.85, "column comment metadata")

        # 3. Incorporate Business Dictionary terms if present
        if self.dictionary is not None and hasattr(self.dictionary, "get_all_terms"):
            try:
                for term_info in self.dictionary.get_all_terms():
                    token = term_info.get("token")
                    table = term_info.get("table")
                    column = term_info.get("column")
                    value = term_info.get("value", token)
                    if table and column and table in self.tables:
                        phrase = term_info.get("expansion") or token
                        if phrase:
                            self.index.add(phrase, value, table, column, 0.95, "business dictionary mapping")
            except Exception as exc:
                logger.debug("Business dictionary term extraction skipped: %s", exc)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        logger.info("Value Index built successfully in %.2f ms.", elapsed_ms)

    def refresh(self) -> None:
        with self._observed_cache_lock:
            self._observed_value_cache.clear()
        self.build_index()

    def resolve(self, context: Dict[str, Any]) -> List[ResolvedValue]:
        """Resolve business values from question for selected tables in < 100ms."""
        t0 = time.perf_counter()

        question = context.get("question", "")
        selected = context.get("selected_tables", [])
        if not selected or not question:
            return []

        selected_set = set(selected)
        resolved: List[ResolvedValue] = []
        seen_keys: Set[tuple] = set()

        # Approved mappings are authoritative semantic memory. Conflicting
        # mappings are deliberately retained for PredicatePlanner to expose.
        if self.defined_knowledge is not None and hasattr(self.defined_knowledge, "predicate_candidates"):
            for table_name in selected:
                entry = self.defined_knowledge.table(table_name)
                for mapping in entry.get("predicate_mappings", []):
                    phrase = str(mapping.get("phrase") or "")
                    if not phrase or not self.contains_phrase(question, phrase):
                        continue
                    key = (table_name, mapping.get("column"), mapping.get("value"))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        resolved.append(ResolvedValue(mapping.get("value"), table_name, mapping.get("column", ""), float(mapping.get("confidence", 1.0)), "approved predicate knowledge", "Knowledge Layer", mapping.get("semantic_type", "UNKNOWN")))
                # A user-approved abbreviation with a target column is also
                # an exact predicate mapping. This is essential for opaque ERP
                # schemas: `CC` can reliably become commands.name = 'Central
                # Command' without asking the model to infer either the table
                # or the value. Abbreviations without a column remain retrieval
                # vocabulary only.
                for abbreviation, term in entry.get("abbreviations", {}).items():
                    term = term if isinstance(term, dict) else {"meaning": term}
                    meaning = str(term.get("meaning") or "").strip()
                    column_name = str(term.get("column") or "").strip()
                    if not meaning or not column_name:
                        continue
                    if not (
                        self.contains_phrase(question, str(abbreviation))
                        or self.contains_phrase(question, meaning)
                    ):
                        continue
                    if column_name not in {
                        column.get("name") for column in self.tables.get(table_name, {}).get("columns", [])
                    }:
                        continue
                    key = (table_name, column_name, meaning)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        resolved.append(ResolvedValue(
                            meaning, table_name, column_name, 1.0,
                            "approved abbreviation value", "Knowledge Layer",
                            self._semantic_type(
                                table_name,
                                next(column for column in self.tables[table_name].get("columns", []) if column.get("name") == column_name),
                            ),
                        ))
                # Important values are approved column semantics too.  They
                # are indexed only by the explicit phrase the user supplied;
                # no guessed value or inferred synonym becomes knowledge.
                for column_name, column in entry.get("columns", {}).items():
                    for phrase, value in column.get("important_values", {}).items():
                        if not phrase or not self.contains_phrase(question, str(phrase)):
                            continue
                        values = value if isinstance(value, list) else [value]
                        for approved_value in values:
                            key = (table_name, column_name, approved_value)
                            if key not in seen_keys:
                                seen_keys.add(key)
                                resolved.append(ResolvedValue(approved_value, table_name, column_name, 1.0,
                                    "approved important value", "Knowledge Layer", self._semantic_type(table_name,
                                    next((c for c in self.tables.get(table_name, {}).get("columns", []) if c.get("name") == column_name), {"name": column_name}))))

        # 1. Lookup label + value pairs (e.g. "OE group", "group OE")
        tokens = re.findall(r"\b[A-Za-z0-9_-]+\b", question)
        for i in range(len(tokens) - 1):
            t1, t2 = tokens[i], tokens[i + 1]
            val, label = None, None
            if t2.lower() in LOOKUP_LABELS and t1.lower() not in STOP_WORDS and len(t1) >= 2:
                val, label = t1, t2.lower()
            elif t1.lower() in LOOKUP_LABELS and t2.lower() not in STOP_WORDS and len(t2) >= 2:
                val, label = t2, t1.lower()

            if val and label and val.lower() not in NON_VALUE_TOKENS and val.lower() not in STOP_WORDS:
                # Unverified adjacent prose such as "department membership"
                # is not a value. This syntax is reserved for code-like
                # labels (OE group, TL unit); natural-language values should
                # come from approved knowledge or observed value indexes.
                if not (val.isupper() or any(char.isdigit() for char in val)):
                    continue
                for tbl_name in selected:
                    tbl_words = set(re.findall(r"[a-z0-9]+", tbl_name.lower().replace("_", " ")))
                    tbl_words_singular = {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in tbl_words}
                    if label in tbl_words or label in tbl_words_singular:
                        table_obj = self.tables.get(tbl_name, {})
                        name_cols = [c["name"] for c in table_obj.get("columns", []) if "name" in c["name"].lower()]
                        if name_cols:
                            col = name_cols[0]
                            key = (tbl_name, col, val.upper())
                            if key not in seen_keys:
                                seen_keys.add(key)
                                resolved.append(ResolvedValue(
                                    value=val.upper(),
                                    table=tbl_name,
                                    column=col,
                                    confidence=1.0,
                                    reason=f"{label} lookup value",
                                    source="Value Resolver",
                                    semantic_type="GROUP" if label == "group" else self._semantic_type(tbl_name, next(c for c in table_obj.get("columns", []) if c["name"] == col)),
                                ))

        # 2. Categorical / business concept phrases (e.g. "under repair")
        question_lower = question.lower()
        words = re.findall(r"[a-z0-9_-]+", question_lower)
        for n in range(min(4, len(words)), 0, -1):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                if phrase in STOP_WORDS or len(phrase) < 2:
                    continue

                matches = self.index.lookup(phrase)
                for m in matches:
                    if m.table in selected_set:
                        key = (m.table, m.column, m.value)
                        if key not in seen_keys:
                            seen_keys.add(key)
                            resolved.append(ResolvedValue(
                                value=m.value,
                                table=m.table,
                                column=m.column,
                                confidence=m.confidence,
                                reason="categorical business concept",
                                source="Value Resolver",
                                semantic_type=self._semantic_type(m.table, next(c for c in self.tables[m.table].get("columns", []) if c["name"] == m.column)),
                            ))

        # State phrases are ranked across schema-compatible columns.  This
        # deliberately runs after static enum/dictionary matches so observed
        # values can distinguish a full state ("UNDER REPAIR") from a weak
        # class/type token ("Repair").
        match_under = re.search(r"\b((?:under|waiting for|awaiting)\s+[A-Za-z][A-Za-z _-]{1,40}?)(?:\s+in\b|$)", question, re.IGNORECASE)
        if match_under:
            raw_val = match_under.group(1).strip()
            # Remove lower-evidence generic matches for this phrase; the
            # ranked state candidate below represents the complete condition.
            resolved = [r for r in resolved if r.source == "Knowledge Layer" or not (r.value and raw_val.lower() in str(r.value).lower())]
            seen_keys = {(r.table, r.column, r.value) for r in resolved}
            # A labelled lookup ("OE group") is a filter target, not the
            # entity whose state is being described. Exclude it from state
            # scoring using only its schema-derived label.
            lookup_tables = set()
            for i in range(len(tokens) - 1):
                label = tokens[i + 1].lower() if tokens[i + 1].lower() in LOOKUP_LABELS else tokens[i].lower() if tokens[i].lower() in LOOKUP_LABELS else None
                if label:
                    for table_name in selected:
                        table_words = self._words(table_name)
                        if label in table_words or label.rstrip("s") in {word.rstrip("s") for word in table_words}:
                            lookup_tables.add(table_name)
            state_tables = [table_name for table_name in selected if table_name not in lookup_tables] or selected
            raw_terms = set(self._words(raw_val)) - {"under", "waiting", "for", "awaiting"}
            approved_covers_phrase = any(
                item.source == "Knowledge Layer"
                and item.value
                and set(self._words(str(item.value)))
                and set(self._words(str(item.value))) <= raw_terms
                for item in resolved
            )
            if not approved_covers_phrase:
                for candidate in self._rank_state_candidates(raw_val, state_tables):
                    key = (candidate.table, candidate.column, candidate.value)
                    if key not in seen_keys:
                        seen_keys.add(key)
                        resolved.append(candidate)

        elapsed_ms = (time.perf_counter() - t0) * 1000
        # Operational values may be sensitive; diagnostics need timing and
        # counts, not the contents of a workshop database.
        logger.info("Value Resolver executed in %.2f ms, resolved %d value(s)", elapsed_ms, len(resolved))
        return resolved

    @staticmethod
    def contains_phrase(question: str, phrase: str) -> bool:
        return bool(re.search(r"\b" + re.escape(phrase.lower()).replace(r"\ ", r"\s+") + r"\b", question.lower()))
