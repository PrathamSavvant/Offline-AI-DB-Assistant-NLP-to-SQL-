"""Deterministic, business-first table intent resolution.

This module runs before join discovery.  It answers a deliberately smaller
question than the RAG layer: which table is the subject of the request, and
which additional tables are explicitly required for an attribute, predicate,
or grouping?  Foreign-key proximity is intentionally not an intent signal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from config.stop_words import STOP_WORDS


IRREGULAR_SINGULARS = {
    "people": "person",
    "children": "child",
    "men": "man",
    "women": "woman",
}


@dataclass
class TableIntent:
    table: str
    entity_score: int = 0
    support_score: int = 0
    role: str = "context"
    evidence: List[str] = field(default_factory=list)
    matched_phrases: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class BusinessIntent:
    primary_table: Optional[str]
    required_tables: List[str]
    result_type: str
    confidence: float
    table_intents: List[TableIntent] = field(default_factory=list)
    ambiguities: List[Dict[str, Any]] = field(default_factory=list)
    ignored_tables: List[Dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "primary_table": self.primary_table,
            "required_tables": list(self.required_tables),
            "result_type": self.result_type,
            "confidence": self.confidence,
            "table_intents": [item.to_dict() for item in self.table_intents],
            "ambiguities": list(self.ambiguities),
            "ignored_tables": list(self.ignored_tables),
        }


class BusinessIntentResolver:
    """Resolve table purpose from schema and *approved* business language.

    Definitions are useful recall evidence, but are never allowed to create a
    required table by themselves.  This is the key safety property for large
    ERP databases where definitions naturally mention many related entities.
    """

    _COUNT = (r"\bhow many\b", r"\bcount\b", r"\bnumber of\b")
    _AGGREGATE = (r"\baverage\b", r"\bavg\b", r"\btotal\b", r"\bsum\b", r"\bminimum\b", r"\bmaximum\b")
    _SUPPORT_PREFIX = re.compile(r"\b(?:in|within|under|by|per|for|from|at|of)\s+$", re.IGNORECASE)

    def __init__(
        self,
        schema: Dict[str, Any],
        semantics: Optional[Dict[str, Any]] = None,
        defined_knowledge: Optional[Dict[str, Dict[str, Any]]] = None,
        annotations: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        self.tables = {table["name"]: table for table in schema.get("tables", [])}
        raw_semantics = semantics or {}
        self.semantics = raw_semantics.get("tables", raw_semantics)
        self.knowledge = defined_knowledge or {}
        self.annotations = annotations or {}
        self._catalog = {name: self._catalog_table(name) for name in self.tables}

    @staticmethod
    def _raw_words(value: Any) -> List[str]:
        return re.findall(r"[a-z0-9]+", str(value or "").casefold().replace("_", " "))

    @classmethod
    def _words(cls, value: Any) -> List[str]:
        words = cls._raw_words(value)
        expanded = list(words)
        for word in words:
            if word in IRREGULAR_SINGULARS:
                expanded.append(IRREGULAR_SINGULARS[word])
            elif word.endswith("ies") and len(word) > 4:
                expanded.append(word[:-3] + "y")
            elif word.endswith("s") and not word.endswith("ss") and len(word) > 3:
                expanded.append(word[:-1])
        return list(dict.fromkeys(expanded))

    @classmethod
    def _normalise_phrase(cls, value: Any) -> str:
        return " ".join(cls._raw_words(value))

    @staticmethod
    def _contains(question: str, phrase: str) -> Optional[re.Match[str]]:
        if not phrase:
            return None
        parts = phrase.split()
        alternatives = {parts[-1]}
        last = parts[-1]
        if last.endswith("ies") and len(last) > 4:
            alternatives.add(last[:-3] + "y")
        elif last.endswith("sis") and len(last) > 4:
            alternatives.add(last[:-2] + "es")
        elif last.endswith("s") and not last.endswith("ss") and len(last) > 3:
            alternatives.add(last[:-1])
        else:
            alternatives.add(last + "s")
        # Common verb inflections allow physical entity names such as
        # ``medication_dispense`` to match natural wording like "dispensed"
        # without requiring a database-specific synonym.
        if last.endswith("e") and len(last) > 3:
            alternatives.add(last + "d")
            alternatives.add(last[:-1] + "ing")
        elif len(last) > 3:
            alternatives.add(last + "ed")
            alternatives.add(last + "ing")
        for plural, singular in IRREGULAR_SINGULARS.items():
            if last == singular:
                alternatives.add(plural)
            elif last == plural:
                alternatives.add(singular)
        prefix = r"[\s_-]+".join(re.escape(part) for part in parts[:-1])
        ending = "(?:" + "|".join(re.escape(item) for item in sorted(alternatives)) + ")"
        pattern = r"\b" + (prefix + r"[\s_-]+" if prefix else "") + ending + r"\b"
        return re.search(pattern, question, re.IGNORECASE)

    @staticmethod
    def _unique(values: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
        seen = set()
        result = []
        for value, source in values:
            key = (value, source)
            if value and key not in seen:
                seen.add(key)
                result.append(key)
        return result

    def _catalog_table(self, table_name: str) -> Dict[str, Any]:
        table = self.tables[table_name]
        knowledge = self.knowledge.get(table_name, {}) if isinstance(self.knowledge, dict) else {}
        annotation = self.annotations.get(table_name, {})
        semantics = self.semantics.get(table_name, {})

        entity_phrases: List[Tuple[str, str]] = []
        entity_phrases.append((self._normalise_phrase(table_name), "schema table name"))
        for field, source in (
            (knowledge.get("business_entity"), "approved business entity"),
            (knowledge.get("business_name"), "approved business name"),
        ):
            if field:
                entity_phrases.append((self._normalise_phrase(field), source))
        for value in knowledge.get("aliases", []):
            entity_phrases.append((self._normalise_phrase(value), "approved alias"))
        for value in knowledge.get("synonyms", []):
            entity_phrases.append((self._normalise_phrase(value), "approved synonym"))
        for value in annotation.get("aliases", []):
            entity_phrases.append((self._normalise_phrase(value), "approved annotation alias"))

        request_phrases = [
            (self._normalise_phrase(value), "approved query phrase")
            for value in [*knowledge.get("common_phrases", []), *knowledge.get("query_phrases", [])]
        ]
        abbreviation_phrases = []
        for token, item in knowledge.get("abbreviations", {}).items():
            abbreviation_phrases.append((self._normalise_phrase(token), "approved abbreviation"))
            meaning = item.get("meaning") if isinstance(item, dict) else item
            if meaning:
                abbreviation_phrases.append((self._normalise_phrase(meaning), "approved abbreviation meaning"))

        column_phrases: List[Tuple[str, str, str]] = []
        approved_columns = knowledge.get("columns", {})
        for column in table.get("columns", []):
            name = column.get("name", "")
            readable = self._normalise_phrase(name)
            if readable and readable not in {"id", "created at", "updated at"}:
                column_phrases.append((readable, name, "schema column"))
            details = approved_columns.get(name, {})
            for value in [
                details.get("business_meaning"),
                *details.get("aliases", []),
                *details.get("synonyms", []),
                *details.get("common_phrases", []),
            ]:
                phrase = self._normalise_phrase(value)
                if phrase:
                    column_phrases.append((phrase, name, "approved column meaning"))

        predicates = []
        for predicate in knowledge.get("predicate_mappings", []):
            if predicate.get("approval_status") not in {"USER_VERIFIED", "USER_DEFINED", "APPROVED"}:
                continue
            phrase = self._normalise_phrase(predicate.get("phrase"))
            if phrase:
                predicates.append((phrase, predicate.get("column", ""), "approved predicate"))

        definition_words = set(self._words(knowledge.get("definition") or annotation.get("meaning") or ""))
        semantic_tags = [self._normalise_phrase(tag) for tag in semantics.get("business_tags", [])]
        return {
            "entity_phrases": self._unique(entity_phrases),
            "request_phrases": self._unique(request_phrases),
            "abbreviation_phrases": self._unique(abbreviation_phrases),
            "column_phrases": column_phrases,
            "predicates": predicates,
            "definition_words": definition_words,
            "semantic_tags": [tag for tag in semantic_tags if tag],
        }

    @classmethod
    def _result_type(cls, question: str) -> str:
        if any(re.search(pattern, question, re.IGNORECASE) for pattern in cls._COUNT):
            return "COUNT"
        if any(re.search(pattern, question, re.IGNORECASE) for pattern in cls._AGGREGATE):
            return "AGGREGATE"
        return "ENTITY"

    def _score_table(self, table_name: str, question: str, question_words: set[str]) -> TableIntent:
        catalog = self._catalog[table_name]
        intent = TableIntent(table=table_name)

        def add_entity(phrase: str, source: str, weight: int) -> None:
            match = self._contains(question, phrase)
            if not match:
                return
            score = weight + min(len(phrase.split()), 3) * 4
            prefix = question[max(0, match.start() - 18):match.start()]
            if self._SUPPORT_PREFIX.search(prefix):
                score -= 28
                intent.support_score += 18
                intent.evidence.append(f"{source}: {phrase} (filter/grouping role)")
            else:
                intent.evidence.append(f"{source}: {phrase}")
            intent.entity_score += max(score, 1)
            intent.matched_phrases.append(phrase)

        weights = {
            "schema table name": 92,
            "approved business entity": 120,
            "approved business name": 116,
            "approved alias": 112,
            "approved synonym": 108,
            "approved annotation alias": 104,
        }
        for phrase, source in catalog["entity_phrases"]:
            add_entity(phrase, source, weights.get(source, 90))
        for phrase, source in catalog["abbreviation_phrases"]:
            add_entity(phrase, source, 106)
        for phrase, source in catalog["request_phrases"]:
            if self._contains(question, phrase):
                intent.entity_score += 98
                intent.support_score += 30
                intent.evidence.append(f"{source}: {phrase}")
                intent.matched_phrases.append(phrase)

        for phrase, column, source in catalog["column_phrases"]:
            if self._contains(question, phrase):
                intent.support_score += 55 if source.startswith("approved") else 34
                intent.evidence.append(f"{source}: {column} ({phrase})")
                intent.matched_phrases.append(phrase)
        for phrase, column, source in catalog["predicates"]:
            if self._contains(question, phrase):
                intent.support_score += 85
                intent.evidence.append(f"{source}: {phrase} -> {column}")
                intent.matched_phrases.append(phrase)

        for tag in catalog["semantic_tags"]:
            if self._contains(question, tag):
                # Scanner-generated business tags are useful recall/context
                # evidence, but related/junction tables often inherit the
                # same tag.  They must not create a second SQL endpoint.
                intent.support_score += 8
                intent.evidence.append(f"schema business tag context: {tag}")
                intent.matched_phrases.append(tag)

        # Definitions only add a small confidence tiebreak.  They can never
        # assign a role or make a table required.
        overlap = (catalog["definition_words"] - STOP_WORDS) & question_words
        if overlap:
            intent.support_score += min(len(overlap), 3) * 3
            intent.evidence.append(f"definition context only: {', '.join(sorted(overlap))}")

        if intent.entity_score > 0:
            intent.role = "entity"
        if any(item.startswith(("schema column", "approved column", "approved predicate")) for item in intent.evidence):
            intent.role = "entity+support" if intent.entity_score > 0 else "support"
        return intent

    @staticmethod
    def _is_definition_only(intent: TableIntent) -> bool:
        return bool(intent.evidence) and all(item.startswith("definition context only:") for item in intent.evidence)

    @staticmethod
    def _is_shadowed_entity(intent: TableIntent, candidates: List[TableIntent]) -> bool:
        """True when a shorter table phrase is part of a longer named entity."""
        own_phrases = [set(phrase.split()) for phrase in intent.matched_phrases if phrase]
        if not own_phrases:
            return False
        has_support = any(
            item.startswith(("schema column", "approved column", "approved predicate"))
            for item in intent.evidence
        )
        if has_support:
            return False
        for other in candidates:
            if other is intent or other.entity_score < intent.entity_score:
                continue
            for own in own_phrases:
                for phrase in other.matched_phrases:
                    other_words = set(phrase.split())
                    if own and own < other_words:
                        return True
        return False

    def resolve(self, question: str, fallback_tables: Optional[List[str]] = None) -> BusinessIntent:
        normalized = " ".join(str(question or "").strip().split())
        question_words = set(self._words(normalized)) - STOP_WORDS
        intents = [self._score_table(name, normalized, question_words) for name in self.tables]
        meaningful = [item for item in intents if item.entity_score or item.support_score]
        ranked_entities = sorted(
            [item for item in meaningful if item.entity_score > 0],
            key=lambda item: (-item.entity_score, -item.support_score, item.table),
        )

        primary = ranked_entities[0].table if ranked_entities else None
        if primary is None:
            primary = next((name for name in (fallback_tables or []) if name in self.tables), None)

        required: List[str] = []
        if primary:
            required.append(primary)
        for item in sorted(meaningful, key=lambda row: (-row.entity_score, -row.support_score, row.table)):
            if item.table == primary or self._is_definition_only(item):
                continue
            if self._is_shadowed_entity(item, meaningful):
                continue
            # A second entity must be directly named/approved. A support-only
            # table must have a real column or approved predicate match.
            if item.entity_score >= 45 or item.support_score >= 34:
                required.append(item.table)

        required = list(dict.fromkeys(required))
        ambiguities: List[Dict[str, Any]] = []
        if len(ranked_entities) > 1:
            first, second = ranked_entities[:2]
            shared = set(first.matched_phrases) & set(second.matched_phrases)
            if shared and first.entity_score == second.entity_score and first.support_score == second.support_score:
                ambiguities.append({
                    "type": "business_entity",
                    "phrase": sorted(shared)[0],
                    "tables": sorted([first.table, second.table]),
                    "reason": "the same approved business term maps equally to multiple tables",
                })

        top_score = ranked_entities[0].entity_score if ranked_entities else 0
        next_score = ranked_entities[1].entity_score if len(ranked_entities) > 1 else 0
        if top_score:
            confidence = min(1.0, 0.55 + (top_score / 240.0) + max(top_score - next_score, 0) / 300.0)
        elif primary:
            confidence = 0.45
        else:
            confidence = 0.0
        if ambiguities:
            confidence = min(confidence, 0.35)

        ignored = [
            {"table": item.table, "reason": "definition context is not sufficient to select a table"}
            for item in meaningful if self._is_definition_only(item)
        ]
        return BusinessIntent(
            primary_table=primary,
            required_tables=required,
            result_type=self._result_type(normalized),
            confidence=round(confidence, 3),
            table_intents=sorted(meaningful, key=lambda row: (-row.entity_score, -row.support_score, row.table)),
            ambiguities=ambiguities,
            ignored_tables=ignored,
        )
