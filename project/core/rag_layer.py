"""
core/rag_layer.py

Modular RAG (Retrieval-Augmented Generation) Layer for Database Schemas.

Pipeline Stage:
    Schema Knowledge
          ↓
  ┌──────────────────────┐
  │       RAG Layer      │
  │                      │
  │ lexical retrieval    │
  │ vector retrieval     │
  │ reranking             │
  │ metadata filtering    │
  └──────────┬───────────┘
             ↓
      Relevant tables

This layer is strictly responsible for schema retrieval and ranking. It never
generates SQL. It executes 4 explicit stages:
  1. Metadata Filtering: Filter disabled tables, apply human priority weights and domain boundaries.
  2. Lexical Retrieval: Inverted token index, business dictionary expansion, keyword & RapidFuzz fuzzy matches.
  3. Vector Retrieval: FAISS dense embedding similarity search (offline-compatible).
  4. Reranking: Multi-signal score fusion, importance/centrality tiebreaks, and FK relationship expansion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from config.settings import settings
from config.stop_words import STOP_WORDS
from core.logger import get_logger
from core.table_annotations import DEFAULT_PRIORITY

logger = get_logger(__name__)

GENERIC_COLUMN_NAMES = {"id", "created_at", "updated_at", "created", "updated"}
MIN_FUZZY_GROUNDING_CONFIDENCE = 0.86

# Small language-only normalisation for common irregular English nouns.  This
# is deliberately independent of any database or ERP vocabulary: it lets
# schema/approved terms containing ``person`` match a question phrased with
# ``people`` without hard-coding a target table.
IRREGULAR_NOUNS = {
    "people": "person",
    "children": "child",
    "men": "man",
    "women": "woman",
}

SCORE_WEIGHTS = {
    "table_name_match": 5.0,
    "domain_match": 2.0,
    "business_tag_match": 2.0,
    "column_match": 2.0,
    "annotation_alias_match": 6.0,
    "annotation_meaning_word_match": 1.0,
    "dictionary_match": 3.0,
    "fuzzy_match": 3.0,
    "embedding_match": 3.0,
    "importance_tiebreak": 1.0,
    # Rewards a table that independently explains several query concepts
    # (entity + attribute + relationship), not merely one generic noun.
    "query_coverage": 1.0,
    "approved_alias_match": 9.0,
    "approved_entity_match": 11.0,
    "approved_phrase_match": 10.0,
    "approved_column_match": 7.0,
    "schema_self_relationship": 8.0,
    "exclusion_penalty": 8.0,
}

PRIORITY_WEIGHTS = {
    "Critical": 3.0,
    "High": 2.0,
    "Medium": 1.0,
    "Low": 0.0,
}


@dataclass
class RetrievalTrace:
    question: str
    normalized_terms: List[str] = field(default_factory=list)
    metadata_filtering: Dict[str, Any] = field(default_factory=dict)
    lexical_retrieval: Dict[str, Any] = field(default_factory=dict)
    candidate_union: Dict[str, Any] = field(default_factory=dict)
    vector_retrieval: Dict[str, Any] = field(default_factory=dict)
    reranking: List[Dict[str, Any]] = field(default_factory=list)
    relationship_expansion: Dict[str, Any] = field(default_factory=dict)
    selected_tables: List[str] = field(default_factory=list)
    excluded_tables: List[Dict[str, str]] = field(default_factory=list)
    timings_ms: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "question": self.question,
            "normalized_terms": self.normalized_terms,
            "metadata_filtering": self.metadata_filtering,
            "lexical_retrieval": self.lexical_retrieval,
            "candidate_union": self.candidate_union,
            "vector_retrieval": self.vector_retrieval,
            "reranking": self.reranking,
            "relationship_expansion": self.relationship_expansion,
            "selected_tables": self.selected_tables,
            "excluded_tables": self.excluded_tables,
            "timings_ms": self.timings_ms,
        }


class MetadataFilter:
    """Stage 1 / Boundary Filter: Apply tenant rules, enablement, and human priority."""

    def __init__(self, annotations: Optional[Dict[str, Dict[str, Any]]] = None):
        self.annotations = annotations or {}

    def is_enabled(self, table_name: str) -> bool:
        return bool(self.annotations.get(table_name, {}).get("enabled", True))

    def get_priority_weight(self, table_name: str) -> float:
        priority = self.annotations.get(table_name, {}).get("priority") or DEFAULT_PRIORITY
        return PRIORITY_WEIGHTS.get(priority, PRIORITY_WEIGHTS[DEFAULT_PRIORITY])

    def filter_tables(self, all_tables: Iterable[str]) -> Tuple[List[str], List[str]]:
        enabled: List[str] = []
        disabled: List[str] = []
        for name in all_tables:
            if self.is_enabled(name):
                enabled.append(name)
            else:
                disabled.append(name)
        return enabled, disabled


class LexicalRetriever:
    """Stage 2: Fast inverted index candidate lookup, dictionary expansion, token and fuzzy matching."""

    def __init__(
        self,
        tables_by_name: Dict[str, Dict[str, Any]],
        table_to_domain: Dict[str, str],
        semantics: Dict[str, Dict[str, Any]],
        annotations: Optional[Dict[str, Dict[str, Any]]] = None,
        dictionary: Optional[Any] = None,
        fuzzy_search: Optional[Any] = None,
        inverted_index: Optional[Any] = None,
        defined_knowledge: Optional[Any] = None,
    ):
        self.tables_by_name = tables_by_name
        self.table_to_domain = table_to_domain
        self.semantics = semantics
        self.annotations = annotations or {}
        self.dictionary = dictionary
        self.fuzzy_search = fuzzy_search
        self.inverted_index = inverted_index
        self.defined_knowledge = defined_knowledge
        self.entity_terms_by_table = {
            name: {
                self._stem(token)
                for token in re.findall(r"[a-z0-9]+", name.lower().replace("_", " "))
                if len(token) >= 3 and token not in STOP_WORDS
            }
            for name in tables_by_name
        }
        self.all_entity_terms = set().union(*self.entity_terms_by_table.values()) if self.entity_terms_by_table else set()

    @staticmethod
    def contains_term(question_lower: str, term: str) -> bool:
        pattern = r"\b" + re.escape(term.lower()).replace(r"\_", r"[\s_]") + r"\b"
        return re.search(pattern, question_lower) is not None

    @staticmethod
    def _stem(token: str) -> str:
        """Small deterministic normalizer for coverage only, not retrieval."""
        token = token.lower()
        for suffix in ("ion", "ment", "ing", "ed", "ly", "es", "s"):
            if token.endswith(suffix) and len(token) > len(suffix) + 2:
                return token[:-len(suffix)]
        return token

    def _query_coverage(self, table_name: str, question: str, definition: str) -> Dict[str, Any]:
        """Measure distinct query concepts supported by specific table evidence."""
        query_terms = {
            self._stem(token) for token in re.findall(r"[a-z0-9]+", question.lower())
            if len(token) >= 3 and token not in STOP_WORDS
        }
        table = self.tables_by_name.get(table_name, {})
        # Broad generated tags are deliberately excluded: they are shared by
        # many ERP tables and should not masquerade as entity coverage.
        evidence = " ".join([
            table_name.replace("_", " "),
            definition or "",
            " ".join(column.get("name", "").replace("_", " ") for column in table.get("columns", [])),
        ])
        evidence_terms = {self._stem(token) for token in re.findall(r"[a-z0-9]+", evidence.lower())}
        covered = sorted(query_terms & evidence_terms)
        return {"terms": covered, "count": len(covered)}

    @staticmethod
    def split_compound(term: str) -> List[str]:
        _FRAGMENTS = [
            "schedule", "manager", "project", "student", "library",
            "classroom", "enrollment", "department", "professor", "employee",
            "courses", "course", "issues", "issue", "books", "book",
            "class", "order", "orders", "client", "clients", "attendance",
            "managers", "rooms", "room", "grade", "grades",
        ]
        if "_" in term or len(term) <= 7:
            return []
        lower = term.lower()
        parts: List[str] = []
        remaining = lower
        while remaining:
            matched = False
            for frag in sorted(_FRAGMENTS, key=len, reverse=True):
                if remaining.startswith(frag):
                    parts.append(frag)
                    remaining = remaining[len(frag):]
                    matched = True
                    break
            if not matched:
                return []
        return parts if len(parts) >= 2 else []

    @classmethod
    def name_variants(cls, term: str) -> List[str]:
        variants = {term}
        if term.endswith("ies"):
            variants.add(term[:-3] + "y")
        elif term.endswith("es"):
            variants.add(term[:-2])
        if term.endswith("s") and not term.endswith("ss"):
            variants.add(term[:-1])
        else:
            variants.add(term + "s")

        parts = cls.split_compound(term)
        for part in parts:
            variants.add(part)
            if part.endswith("s") and not part.endswith("ss"):
                variants.add(part[:-1])
            else:
                variants.add(part + "s")
        return list(variants)

    def expand_question(self, question: str) -> str:
        phrases = [
            singular
            for plural, singular in IRREGULAR_NOUNS.items()
            if self.contains_term(question.lower(), plural)
        ]
        # Approved table vocabulary is database-scoped normalization. It is
        # additive: original user wording is never discarded.
        for entry in (self.defined_knowledge or {}).values() if isinstance(self.defined_knowledge, dict) else []:
            for abbr, item in entry.get("abbreviations", {}).items():
                meaning = item.get("meaning", "") if isinstance(item, dict) else str(item)
                if self.contains_term(question.lower(), abbr) or (meaning and self.contains_term(question.lower(), meaning)):
                    phrases.extend([abbr, meaning])
            for phrase in [*entry.get("aliases", []), *entry.get("synonyms", []), *entry.get("common_phrases", []), *entry.get("query_phrases", [])]:
                if self.contains_term(question.lower(), phrase): phrases.append(phrase)
        if self.dictionary is None:
            return question if not phrases else question + " " + " ".join(dict.fromkeys(phrases))
        try:
            resolved = self.dictionary.resolve_text(question)
        except Exception:
            logger.debug("Dictionary resolution failed", exc_info=True)
            return question
        for entry in resolved:
            phrases.extend([entry.get("expansion", ""), *entry.get("synonyms", []), *entry.get("aliases", [])])
        phrases = [p.strip() for p in phrases if isinstance(p, str) and p.strip()]
        return question if not phrases else question + " " + " ".join(dict.fromkeys(phrases))

    def get_candidate_subset(self, question: str, enabled_tables: List[str]) -> List[str]:
        """Pre-filter tables via Inverted Index when the schema is large (>20 tables)."""
        if self.inverted_index is None or len(enabled_tables) <= 20:
            return enabled_tables

        expanded_q = self.expand_question(question)
        candidates = self.inverted_index.get_candidate_tables(expanded_q)
        candidate_set = candidates.intersection(set(enabled_tables))
        if candidate_set:
            return sorted(candidate_set)
        # No token hit must not turn into a full schema scan.  Vector search
        # can still contribute candidates in the next stage.
        return []

    def score_table(self, table_name: str, question: str) -> Dict[str, Any]:
        """Score table based on lexical, token, column, annotation, and fuzzy matches."""
        question_lower = question.lower()
        keyword_score = 0.0
        matches: List[str] = []
        table = self.tables_by_name.get(table_name, {})
        semantics = self.semantics.get(table_name, {})
        manual_definition = ""

        # 1. Table name match
        readable_name = table_name.replace("_", " ")
        name_candidates = self.name_variants(table_name) + self.name_variants(readable_name)
        if any(self.contains_term(question_lower, c) for c in name_candidates):
            weight = SCORE_WEIGHTS["table_name_match"]
            keyword_score += weight
            matches.append(f"table_name (+{weight})")

        # 2. Domain match
        domain = self.table_to_domain.get(table_name, "")
        if domain and self.contains_term(question_lower, domain):
            weight = SCORE_WEIGHTS["domain_match"]
            keyword_score += weight
            matches.append(f"domain:{domain} (+{weight})")

        # 3. Business tags
        for tag in semantics.get("business_tags", []):
            if self.contains_term(question_lower, tag):
                weight = SCORE_WEIGHTS["business_tag_match"]
                keyword_score += weight
                matches.append(f"tag:{tag} (+{weight})")

        # 4. Column names
        for col in table.get("columns", []):
            col_name = col.get("name", "")
            if col_name.lower() in GENERIC_COLUMN_NAMES:
                continue
            if self.contains_term(question_lower, col_name.replace("_", " ")):
                weight = SCORE_WEIGHTS["column_match"]
                keyword_score += weight
                matches.append(f"column:{col_name} (+{weight})")

        # A possessive/hierarchical request can refer to a self-FK role rather
        # than to a separate similarly named table (for example a row's
        # parent/supervisor). This evidence is derived from schema FKs only.
        self_relationship_terms: List[str] = []
        for foreign_key in table.get("foreign_keys", []):
            if foreign_key.get("references_table") != table_name:
                continue
            relation = re.sub(r"(?:_id|_no|_key)$", "", str(foreign_key.get("column") or "").lower())
            relation_text = relation.replace("_", " ")
            relation_pattern = re.escape(relation_text)
            possessive = re.search(rf"\b(?:their|its|with)\s+{relation_pattern}s?\b", question_lower)
            hierarchy = relation in {"manager", "supervisor", "parent", "lead"} and re.search(
                r"\b(?:direct\s+reports?|reports?\s+to)\b", question_lower
            )
            missing_relation = re.search(
                rf"\b(?:without(?:\s+(?:a|an|any))?|no|not\s+have(?:\s+(?:a|an|any))?|do(?:es)?\s+not\s+have(?:\s+(?:a|an|any))?)\s+{relation_pattern}s?\b",
                question_lower,
            )
            if relation and (possessive or hierarchy or missing_relation):
                self_relationship_terms.append(relation)
        if self_relationship_terms:
            weight = SCORE_WEIGHTS["schema_self_relationship"]
            keyword_score += weight
            matches.append(f"schema_self_relation:{sorted(set(self_relationship_terms))} (+{weight})")

        # 5. User Annotations & Defined Knowledge
        annotation = self.annotations.get(table_name, {})
        for alias in annotation.get("aliases", []):
            if self.contains_term(question_lower, alias):
                weight = SCORE_WEIGHTS["annotation_alias_match"]
                keyword_score += weight
                matches.append(f"alias:{alias} (+{weight})")

        meaning = annotation.get("meaning", "")
        if meaning:
            meaning_words = {
                w for w in re.findall(r"[a-zA-Z]+", meaning.lower())
                if len(w) > 3 and w not in STOP_WORDS
            }
            question_words = {
                w for w in re.findall(r"[a-zA-Z]+", question_lower)
                if w not in STOP_WORDS
            }
            overlap = meaning_words & question_words
            if overlap:
                weight = min(len(overlap), 3) * SCORE_WEIGHTS["annotation_meaning_word_match"]
                keyword_score += weight
                matches.append(f"meaning_overlap:{sorted(overlap)} (+{weight})")

        # Defined knowledge terms if available
        if self.defined_knowledge is not None:
            def_entry = self.defined_knowledge.get(table_name, {}) if isinstance(self.defined_knowledge, dict) else {}
            manual_definition = str(def_entry.get("definition") or "")
            definition_words = {word for word in re.findall(r"[a-zA-Z0-9]+", str(def_entry.get("definition") or "").lower()) if len(word) >= 3} - STOP_WORDS
            question_words = {word for word in re.findall(r"[a-zA-Z0-9]+", question_lower) if len(word) >= 3} - STOP_WORDS
            definition_stems = {self._stem(word) for word in definition_words}
            question_stems = {self._stem(word) for word in question_words}
            normalized_overlap = definition_stems & question_stems
            definition_overlap = normalized_overlap
            other_entity_terms = self.all_entity_terms - self.entity_terms_by_table.get(table_name, set())
            definition_owned_terms = sorted(normalized_overlap - other_entity_terms)
            definition_related_terms = sorted(normalized_overlap & other_entity_terms)
            if definition_overlap:
                weight = min(len(definition_overlap), 3) * SCORE_WEIGHTS["annotation_meaning_word_match"]
                keyword_score += weight
                matches.append(f"manual_definition:{sorted(definition_overlap)} (+{weight})")
            for syn in def_entry.get("synonyms", []):
                syn_terms = {self._stem(word) for word in re.findall(r"[a-z0-9]+", str(syn).lower()) if word not in STOP_WORDS}
                if self.contains_term(question_lower, syn) or (syn_terms and syn_terms <= question_stems):
                    weight = SCORE_WEIGHTS["annotation_alias_match"]
                    keyword_score += weight
                    matches.append(f"defined_synonym:{syn} (+{weight})")
            for abbr, item in def_entry.get("abbreviations", {}).items():
                m_text = item.get("meaning", "") if isinstance(item, dict) else str(item)
                context_text = item.get("context", "") if isinstance(item, dict) else ""
                if self.contains_term(question_lower, abbr) or (m_text and self.contains_term(question_lower, m_text)) or (context_text and self.contains_term(question_lower, context_text)):
                    weight = SCORE_WEIGHTS["dictionary_match"]
                    keyword_score += weight
                    matches.append(f"defined_abbr:{abbr} (+{weight})")
            for alias in def_entry.get("aliases", []):
                alias_terms = {self._stem(word) for word in re.findall(r"[a-z0-9]+", str(alias).lower()) if word not in STOP_WORDS}
                if self.contains_term(question_lower, alias) or (alias_terms and alias_terms <= question_stems):
                    weight = SCORE_WEIGHTS["approved_alias_match"]; keyword_score += weight
                    matches.append(f"approved_alias:{alias} (+{weight})")
            for entity in (def_entry.get("business_entity", ""), def_entry.get("business_name", "")):
                if entity and self.contains_term(question_lower, entity):
                    weight = SCORE_WEIGHTS["approved_entity_match"]; keyword_score += weight
                    matches.append(f"approved_entity:{entity} (+{weight})")
            for phrase in [*def_entry.get("common_phrases", []), *def_entry.get("query_phrases", [])]:
                if self.contains_term(question_lower, phrase):
                    weight = SCORE_WEIGHTS["approved_phrase_match"]; keyword_score += weight
                    matches.append(f"approved_phrase:{phrase} (+{weight})")
            for column_name, column in def_entry.get("columns", {}).items():
                phrases = [column.get("business_meaning", ""), *column.get("aliases", []), *column.get("synonyms", []), *column.get("common_phrases", [])]
                if any(phrase and self.contains_term(question_lower, phrase) for phrase in phrases):
                    weight = SCORE_WEIGHTS["approved_column_match"]; keyword_score += weight
                    matches.append(f"approved_column:{column_name} (+{weight})")
            for predicate in def_entry.get("predicate_mappings", []):
                if predicate.get("approval_status") in {"USER_VERIFIED", "USER_DEFINED", "APPROVED"} and self.contains_term(question_lower, predicate.get("phrase", "")):
                    weight = SCORE_WEIGHTS["approved_phrase_match"]; keyword_score += weight
                    matches.append(f"approved_predicate:{predicate.get('phrase')} (+{weight})")
            exclusions = def_entry.get("exclusions", [])
            for exclusion in exclusions if isinstance(exclusions, list) else [exclusions]:
                if exclusion and self.contains_term(question_lower, exclusion):
                    keyword_score -= SCORE_WEIGHTS["exclusion_penalty"]
                    matches.append(f"exclusion:{exclusion} (-{SCORE_WEIGHTS['exclusion_penalty']})")

        # 6. Business dictionary matches
        if self.dictionary is not None:
            try:
                dict_hits = self.dictionary.lookup(question_lower) or []
                table_tokens = {table_name.upper()} | {
                    c["name"].upper() for c in table.get("columns", [])
                }
                if any(abbr in table_tokens for abbr in dict_hits):
                    weight = SCORE_WEIGHTS["dictionary_match"]
                    keyword_score += weight
                    matches.append(f"dictionary_match (+{weight})")
            except Exception:
                logger.debug("Dictionary check failed for '%s'", table_name, exc_info=True)

        # 7. Fuzzy search matches
        if self.fuzzy_search is not None:
            try:
                fuzzy_conf = self.fuzzy_search.score_for_table(question_lower, table_name)
                if fuzzy_conf > 0:
                    weight = round(fuzzy_conf * SCORE_WEIGHTS["fuzzy_match"], 2)
                    keyword_score += weight
                    matches.append(f"fuzzy_conf:{fuzzy_conf:.2f} (+{weight})")
            except Exception:
                logger.debug("Fuzzy search failed for '%s'", table_name, exc_info=True)

        coverage = self._query_coverage(table_name, question, manual_definition)
        coverage_score = round((coverage["count"] ** 2) * SCORE_WEIGHTS["query_coverage"], 4)
        if coverage_score:
            keyword_score += coverage_score
            matches.append(f"query_coverage:{coverage['terms']} (+{coverage_score})")

        return {
            "score": round(keyword_score, 4),
            "matches": matches,
            "score_components": {
                "table_name_score": sum(SCORE_WEIGHTS["table_name_match"] for item in matches if item.startswith("table_name")),
                "column_score": sum(SCORE_WEIGHTS["column_match"] for item in matches if item.startswith("column:")),
                "manual_knowledge_score": sum(float(item.rsplit("+", 1)[-1].rstrip(")")) for item in matches if item.startswith("manual_definition:")),
                "manual_definition_terms": sorted(normalized_overlap) if self.defined_knowledge is not None else [],
                "manual_owned_terms": definition_owned_terms if self.defined_knowledge is not None else [],
                "manual_related_entity_terms": definition_related_terms if self.defined_knowledge is not None else [],
                "synonym_score": sum(SCORE_WEIGHTS["annotation_alias_match"] for item in matches if item.startswith("defined_synonym:")),
                "abbreviation_score": sum(SCORE_WEIGHTS["dictionary_match"] for item in matches if item.startswith("defined_abbr:")),
                "semantic_score": sum(SCORE_WEIGHTS["business_tag_match"] for item in matches if item.startswith("tag:")),
                "approved_alias_score": sum(SCORE_WEIGHTS["approved_alias_match"] for item in matches if item.startswith("approved_alias:")),
                "approved_entity_score": sum(SCORE_WEIGHTS["approved_entity_match"] for item in matches if item.startswith("approved_entity:")),
                "approved_phrase_score": sum(SCORE_WEIGHTS["approved_phrase_match"] for item in matches if item.startswith(("approved_phrase:", "approved_predicate:"))),
                "approved_column_score": sum(SCORE_WEIGHTS["approved_column_match"] for item in matches if item.startswith("approved_column:")),
                "self_relationship_score": sum(SCORE_WEIGHTS["schema_self_relationship"] for item in matches if item.startswith("schema_self_relation:")),
                "self_relationship_terms": sorted(set(self_relationship_terms)),
                "table_entity_terms": sorted(self.entity_terms_by_table.get(table_name, set())),
                "temporal_scope": (
                    "history"
                    if (
                        any(term in set(re.findall(r"[a-z0-9]+", (table_name + " " + manual_definition + " " + " ".join(def_entry.get("aliases", []))).lower())) for term in {"historical", "history", "past", "previous", "former"})
                        or "no longer current" in manual_definition.lower()
                    )
                    else "current"
                    if any(term in set(re.findall(r"[a-z0-9]+", (table_name + " " + manual_definition + " " + " ".join(def_entry.get("aliases", []))).lower())) for term in {"current", "active", "present"})
                    else ""
                ) if self.defined_knowledge is not None else "",
                "exclusion_penalty": -sum(SCORE_WEIGHTS["exclusion_penalty"] for item in matches if item.startswith("exclusion:")),
                "query_coverage_score": coverage_score,
                "query_coverage_terms": coverage["terms"],
                "value_evidence_score": 0.0,
                "relationship_evidence_score": 0.0,
            },
        }


class VectorRetriever:
    """Stage 3: Offline-compatible dense vector similarity retrieval."""

    def __init__(self, embedding_search: Optional[Any] = None):
        self.embedding_search = embedding_search

    def search(self, question: str, top_k: int = 20) -> Dict[str, float]:
        if self.embedding_search is None:
            return {}
        try:
            hits = self.embedding_search.search(question.lower(), top_k=top_k)
            return {name: float(score) for name, score in hits if score > 0.0}
        except Exception:
            logger.warning("Vector retrieval failed; continuing with lexical retrieval", exc_info=True)
            return {}


class Reranker:
    """Stage 4: Multi-signal fusion, importance tiebreaks, and FK relationship expansion."""

    def __init__(
        self,
        importance: Dict[str, Dict[str, Any]],
        relationship_graph: Dict[str, List[Dict[str, Any]]],
        candidate_limit: int = 12,
        neighbour_limit: int = 2,
    ):
        self.importance = importance
        self.relationship_graph = relationship_graph
        self.candidate_limit = max(1, candidate_limit)
        self.neighbour_limit = max(0, neighbour_limit)

    def _neighbours(self, table_name: str, enabled_set: Set[str]) -> Iterable[str]:
        for edge in self.relationship_graph.get(table_name, []):
            neighbour = edge.get("to_table")
            if neighbour in enabled_set:
                yield neighbour

    @staticmethod
    def _grounding_reasons(matches: List[str]) -> List[str]:
        """Return semantic evidence labels; ranking-only signals are excluded."""
        labels: List[str] = []
        direct = {
            "table_name": "exact_schema_match",
            "column": "exact_column_match",
            "alias": "approved_alias_match",
            "manual_definition": "approved_table_context_match",
            "meaning_overlap": "approved_table_context_match",
            "defined_synonym": "approved_synonym_match",
            "defined_abbr": "approved_abbreviation_match",
            "approved_alias": "approved_alias_match",
            "approved_entity": "approved_entity_match",
            "approved_phrase": "approved_phrase_match",
            "approved_column": "approved_column_match",
            "approved_predicate": "approved_predicate_match",
            "dictionary_match": "approved_dictionary_match",
            "domain": "schema_domain_match",
            "tag": "schema_business_concept_match",
            "query_coverage": "deterministic_schema_term_match",
            "schema_self_relation": "verified_self_fk_match",
        }
        for match in matches:
            prefix = str(match).split(":", 1)[0].split(" ", 1)[0]
            if prefix in direct:
                labels.append(direct[prefix])
                continue
            if prefix == "fuzzy_conf":
                found = re.search(r"fuzzy_conf:([0-9.]+)", str(match))
                if found and float(found.group(1)) >= MIN_FUZZY_GROUNDING_CONFIDENCE:
                    labels.append("strong_fuzzy_schema_match")
        return list(dict.fromkeys(labels))

    @staticmethod
    def _strong_endpoint_evidence(record: Dict[str, Any]) -> bool:
        """True when evidence describes this table, not merely a neighbour.

        Free-text definitions often mention related entities. In a 300-table
        ERP schema those mentions are valuable candidate-recall evidence, but
        they are not sufficient to make every referenced table an endpoint.
        """
        components = record.get("score_components", {})
        distinctive_owned_terms = set(components.get("manual_owned_terms", [])) - {
            "current", "active", "present", "past", "previous", "historical", "history",
        }
        return bool(
            components.get("table_name_score", 0)
            or components.get("column_score", 0)
            or components.get("approved_alias_score", 0)
            or components.get("approved_entity_score", 0)
            or components.get("approved_phrase_score", 0)
            or components.get("approved_column_score", 0)
            or components.get("self_relationship_score", 0)
            or components.get("synonym_score", 0)
            or components.get("abbreviation_score", 0)
            or distinctive_owned_terms
        )

    @staticmethod
    def _singular(token: str) -> str:
        if token.endswith("ies") and len(token) > 4:
            return token[:-3] + "y"
        if token.endswith("s") and not token.endswith("ss") and len(token) > 3:
            return token[:-1]
        return token

    def rerank(
        self,
        tables_to_score: List[str],
        lexical_scores: Dict[str, Dict[str, Any]],
        vector_scores: Dict[str, float],
        metadata_filter: MetadataFilter,
        top_n: int,
        knowledge_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
        question: str = "",
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        """Combine all signals and rank candidate tables."""
        enabled_set = set(tables_to_score)
        scored_records: Dict[str, Dict[str, Any]] = {}

        for name in tables_to_score:
            lex_info = lexical_scores.get(name, {"score": 0.0, "matches": []})
            lex_score = float(lex_info["score"])
            vec_sim = float(vector_scores.get(name, 0.0))
            vec_score = round(vec_sim * SCORE_WEIGHTS["embedding_match"], 4)

            imp_val = float(self.importance.get(name, {}).get("score", 0.0))
            imp_contrib = round(imp_val * SCORE_WEIGHTS["importance_tiebreak"], 4)
            prio_contrib = metadata_filter.get_priority_weight(name)

            # Combined score
            combined = lex_score + vec_score + imp_contrib + prio_contrib
            matches = list(lex_info["matches"])
            if vec_score > 0:
                matches.append(f"embedding_sim:{vec_sim:.2f} (+{vec_score})")
            if imp_contrib > 0:
                matches.append(f"importance_tiebreak (+{imp_contrib})")
            if prio_contrib > 1.0 or prio_contrib == 0.0:
                matches.append(f"priority_weight (+{prio_contrib})")

            # Importance, priority and vector similarity may order grounded
            # candidates, but cannot manufacture semantic relevance.  A table
            # must have inspectable schema/approved-knowledge evidence.
            grounding_reasons = self._grounding_reasons(matches)
            has_signal = (lex_score > 0.0 or vec_score > 0.0)
            grounded = bool(grounding_reasons) and lex_score > 0.0

            scored_records[name] = {
                "table": name,
                "lexical_score": lex_score,
                "vector_score": vec_score,
                "vector_similarity": vec_sim,
                "importance_score": imp_contrib,
                "priority_score": prio_contrib,
                "combined_score": round(combined, 4),
                "has_signal": has_signal,
                "grounded": grounded,
                "grounding_reasons": grounding_reasons,
                "matches": matches,
                "score_components": {
                    **lex_info.get("score_components", {}),
                    "vector_score": vec_score,
                    "importance_score": imp_contrib,
                    "priority_score": prio_contrib,
                },
            }

        # Step 1: Filter to tables that have actual query relevance signal
        matched_candidates = [rec for rec in scored_records.values() if rec["grounded"]]
        matched_candidates.sort(key=lambda r: (-r["combined_score"], r["table"]))
        bounded_candidates = matched_candidates[: self.candidate_limit]

        def direct_endpoint_evidence(record: Dict[str, Any]) -> bool:
            components = record.get("score_components", {})
            return bool(
                components.get("table_name_score", 0)
                or components.get("column_score", 0)
                or components.get("semantic_score", 0)
                or components.get("approved_alias_score", 0)
                or components.get("approved_entity_score", 0)
                or components.get("approved_phrase_score", 0)
                or components.get("approved_column_score", 0)
                or components.get("self_relationship_score", 0)
                or components.get("synonym_score", 0)
                or components.get("abbreviation_score", 0)
            )

        def definition_only_single_term(record: Dict[str, Any]) -> bool:
            components = record.get("score_components", {})
            return bool(
                components.get("manual_definition_terms")
                and len(components.get("query_coverage_terms", [])) <= 1
                and not direct_endpoint_evidence(record)
            )

        # A specific schema/semantic entity at least as strong as a candidate
        # supported by one free-text word wins before alphabetical tie-breaking.
        # This prevents broad context words from becoming extra SQL endpoints.
        authoritative_scores = [
            item["combined_score"] for item in bounded_candidates
            if direct_endpoint_evidence(item)
        ]
        if authoritative_scores:
            best_authoritative_score = max(authoritative_scores)
            bounded_candidates = [
                item for item in bounded_candidates
                if not (
                    definition_only_single_term(item)
                    and best_authoritative_score >= item["combined_score"]
                )
            ]

        # Step 2: Prune bystander candidates that have only weak/incidental column overlap
        # when a dominant primary entity with strong direct matches is already present.
        question_terms = set(re.findall(r"[a-z0-9]+", question.lower()))
        desired_scope = (
            "history" if question_terms & {"historical", "history", "past", "previous", "former"}
            else "current" if question_terms & {"current", "currently", "active", "present"}
            else ""
        )
        pruned_candidates = []
        if bounded_candidates:
            top_rec = bounded_candidates[0]
            top_score = top_rec["combined_score"]
            top_comps = top_rec.get("score_components", {})
            has_strong_top = self._strong_endpoint_evidence(top_rec)
            top_is_direct_schema_entity = bool(
                top_comps.get("table_name_score", 0)
                or top_comps.get("semantic_score", 0)
            )
            strong_names = {
                item["table"] for item in bounded_candidates
                if self._strong_endpoint_evidence(item)
            }
            for rec in bounded_candidates:
                if rec == top_rec:
                    pruned_candidates.append(rec)
                    continue
                comps = rec.get("score_components", {})
                rec_strong = self._strong_endpoint_evidence(rec)
                rec_is_definition_only_single_term = definition_only_single_term(rec)
                structural_temporal_entity = bool(
                    desired_scope
                    and comps.get("temporal_scope") == desired_scope
                    and len(set(self._neighbours(rec["table"], enabled_set)) & strong_names) >= 2
                    and comps.get("manual_definition_terms")
                )
                # Definition text that only repeats another table's entity
                # name is relationship context. Join planning may introduce
                # that table later, but retrieval must not promote it now.
                if has_strong_top and not rec_strong and not structural_temporal_entity:
                    continue
                # A broad word in free-text context (for example "company"
                # inside a department description) must not create a second
                # endpoint when the question directly names another schema
                # entity.  Multi-concept definitions, explicit aliases,
                # columns and approved phrases remain eligible, preserving
                # opaque-table retrieval for large ERP databases.
                if (
                    top_is_direct_schema_entity
                    and rec_is_definition_only_single_term
                    and rec["combined_score"] < top_score
                    and not structural_temporal_entity
                ):
                    continue
                pruned_candidates.append(rec)
        else:
            pruned_candidates = bounded_candidates

        self_fk_owners = [
            record for record in pruned_candidates
            if record.get("score_components", {}).get("self_relationship_score", 0)
        ]
        if self_fk_owners:
            owner_names = {record["table"] for record in self_fk_owners}
            relationship_tokens = {
                token
                for owner in self_fk_owners
                for token in owner.get("score_components", {}).get("self_relationship_terms", [])
            }
            pruned_candidates = [
                record for record in pruned_candidates
                if record["table"] in owner_names
                or not ({self._singular(token) for token in re.findall(r"[a-z0-9]+", record["table"].replace("_", " "))} & relationship_tokens)
            ]

        # When approved definitions distinguish current from historical
        # versions of the same business entity, honor that distinction before
        # endpoint selection. This is generic sibling disambiguation, not a
        # table-name allowlist.
        if desired_scope:
            temporal_terms = {"historical", "history", "past", "previous", "former", "current", "active", "present"}
            scoped = []
            for record in pruned_candidates:
                components = record.get("score_components", {})
                scope = components.get("temporal_scope", "")
                own_terms = set(components.get("table_entity_terms", [])) - temporal_terms
                conflicting_sibling = any(
                    other is not record
                    and other.get("score_components", {}).get("temporal_scope") == desired_scope
                    and own_terms
                    and own_terms & (set(other.get("score_components", {}).get("table_entity_terms", [])) - temporal_terms)
                    for other in pruned_candidates
                )
                if scope and scope != desired_scope and conflicting_sibling:
                    continue
                scoped.append(record)
            pruned_candidates = scoped

        # Prefer the longest directly named entity phrase. For example,
        # "office sections" must not independently select both
        # `office_sections` and the substring table `offices`.
        direct_named = [
            record for record in pruned_candidates
            if record.get("score_components", {}).get("table_name_score", 0)
        ]
        shadowed_names = set()
        question_lower = question.lower()
        for shorter in direct_named:
            short_terms = {
                self._singular(token) for token in re.findall(r"[a-z0-9]+", shorter["table"].replace("_", " "))
            }
            for longer in direct_named:
                if shorter is longer:
                    continue
                long_terms = {
                    self._singular(token) for token in re.findall(r"[a-z0-9]+", longer["table"].replace("_", " "))
                }
                readable_long = " ".join(re.findall(r"[a-z0-9]+", longer["table"].replace("_", " ")))
                if short_terms < long_terms and readable_long.rstrip("s") in question_lower:
                    shadowed_names.add(shorter["table"])
        if shadowed_names:
            pruned_candidates = [record for record in pruned_candidates if record["table"] not in shadowed_names]

        # FK edges are used later to connect already-retrieved entities. They
        # are never a reason to introduce an unrelated neighbour here.
        seed_names = [r["table"] for r in pruned_candidates[: min(top_n, len(pruned_candidates))]]
        final_candidates = list(seed_names)

        # Step 3: Final selection picking connected candidates where possible
        ranked_pool = [
            scored_records.get(name, {
                "table": name,
                "lexical_score": 0.0,
                "vector_score": 0.0,
                "vector_similarity": 0.0,
                "importance_score": 0.0,
                "priority_score": 1.0,
                "combined_score": 1.0,
                "has_signal": False,
                "matches": ["relationship_expansion"],
            }) for name in final_candidates
        ]
        ranked_pool.sort(key=lambda r: (-r["combined_score"], r["table"]))

        selected: List[Dict[str, Any]] = []
        while ranked_pool and len(selected) < top_n:
            selected_names = {s["table"] for s in selected}
            connected = [
                r for r in ranked_pool
                if not selected or any(r["table"] in set(self._neighbours(ex, enabled_set)) for ex in selected_names)
            ]
            chosen = connected[0] if connected else ranked_pool[0]
            selected.append(chosen)
            ranked_pool.remove(chosen)

        for idx, rec in enumerate(matched_candidates, start=1):
            rec["rank"] = idx

        diagnostic_records = sorted(
            scored_records.values(), key=lambda r: (-r["combined_score"], r["table"])
        )

        rel_expansion_info = {"seeds": seed_names, "neighbors_added": {}, "policy": "join_bridges_only"}

        return selected, diagnostic_records, rel_expansion_info


class RAGLayer:
    """
    Composite RAG Layer encapsulating:
      1. Metadata Filtering
      2. Lexical Retrieval
      3. Vector Retrieval
      4. Reranking & Relationship Expansion
    """

    def __init__(
        self,
        schema: Dict[str, Any],
        grouping_data: Dict[str, Any],
        semantics_data: Dict[str, Any],
        ranking_data: Dict[str, Any],
        relationship_graph: Dict[str, List[Dict[str, Any]]],
        annotations: Optional[Dict[str, Dict[str, Any]]] = None,
        dictionary: Optional[Any] = None,
        fuzzy_search: Optional[Any] = None,
        embedding_search: Optional[Any] = None,
        inverted_index: Optional[Any] = None,
        defined_knowledge: Optional[Any] = None,
        table_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
        candidate_limit: int = 12,
        neighbour_limit: int = 2,
    ):
        self.tables_by_name = {t["name"]: t for t in schema.get("tables", [])}
        self.table_to_domain = grouping_data.get("table_to_domain", {})
        self.semantics = semantics_data.get("tables", {})
        self.importance = {r["table"]: r for r in ranking_data.get("rankings", [])}
        self.relationship_graph = relationship_graph
        self.annotations = annotations or {}
        self.table_profiles = table_profiles or {}

        # 4 Sub-Components
        self.metadata_filter = MetadataFilter(self.annotations)
        self.lexical_retriever = LexicalRetriever(
            self.tables_by_name,
            self.table_to_domain,
            self.semantics,
            annotations=self.annotations,
            dictionary=dictionary,
            fuzzy_search=fuzzy_search,
            inverted_index=inverted_index,
            defined_knowledge=defined_knowledge,
        )
        self.vector_retriever = VectorRetriever(embedding_search)
        self.reranker = Reranker(
            self.importance,
            self.relationship_graph,
            candidate_limit=candidate_limit,
            neighbour_limit=neighbour_limit,
        )

    def retrieve_relevant_tables(
        self,
        question: str,
        top_n: int = 3,
    ) -> Tuple[List[Dict[str, Any]], RetrievalTrace]:
        """
        Execute the 4-stage retrieval and return (selected_tables, retrieval_trace).
        """
        started = time.perf_counter()
        all_table_names = list(self.tables_by_name.keys())
        expanded_question = self.lexical_retriever.expand_question(question)
        normalized_terms = sorted(set(re.findall(r"[a-z0-9_]+", expanded_question.lower())) - STOP_WORDS)

        # Stage 1: Metadata Filtering
        enabled_tables, disabled_tables = self.metadata_filter.filter_tables(all_table_names)
        if not enabled_tables:
            logger.error("All tables are disabled in metadata filtering.")
            trace = RetrievalTrace(
                question=question,
                metadata_filtering={"total": len(all_table_names), "enabled": 0, "disabled": disabled_tables},
                selected_tables=[],
            )
            return [], trace

        # Stage 2: Lexical Candidate Subsetting & Scoring
        lexical_started = time.perf_counter()
        candidates_to_score = self.lexical_retriever.get_candidate_subset(question, enabled_tables)
        # Stage 3: Vector Similarity Retrieval
        vector_started = time.perf_counter()
        vector_scores = self.vector_retriever.search(question, top_k=settings.EMBEDDING_TOP_K)
        vector_ms = (time.perf_counter() - vector_started) * 1000
        vector_candidates = [name for name in vector_scores if name in set(enabled_tables)]
        lexical_candidates = sorted(candidates_to_score)
        union_candidates = sorted(set(lexical_candidates) | set(vector_candidates))
        # Candidate preservation: score only the already-indexed union with
        # the existing lexical machinery before applying the fixed budget.
        # Alphabetical order is a tie-breaker, never a survival criterion.
        preservation_scores = {
            name: self.lexical_retriever.score_table(name, expanded_question)
            for name in union_candidates
        }
        prioritized_candidates = sorted(
            union_candidates,
            key=lambda name: (
                -float(preservation_scores[name].get("score", 0.0)),
                -float(vector_scores.get(name, 0.0)),
                name,
            ),
        )
        candidates_to_score = prioritized_candidates[:settings.MAX_RETRIEVAL_CANDIDATES]
        omitted_by_budget = prioritized_candidates[settings.MAX_RETRIEVAL_CANDIDATES:]
        lexical_scores = {name: preservation_scores[name] for name in candidates_to_score}
        lexical_ms = max(0.0, (time.perf_counter() - lexical_started) * 1000 - vector_ms)

        # Stage 4: Multi-Signal Reranking & FK Expansion
        rerank_started = time.perf_counter()
        selected, rerank_records, rel_expansion = self.reranker.rerank(
            candidates_to_score,
            lexical_scores,
            vector_scores,
            self.metadata_filter,
            top_n=top_n,
            knowledge_profiles=self.table_profiles,
            question=expanded_question,
        )

        selected_table_names = [s["table"] for s in selected]
        rerank_ms = (time.perf_counter() - rerank_started) * 1000

        trace = RetrievalTrace(
            question=question,
            normalized_terms=normalized_terms,
            metadata_filtering={
                "total_tables": len(all_table_names),
                "enabled_count": len(enabled_tables),
                "disabled_tables": disabled_tables,
            },
            lexical_retrieval={
                "subset_evaluated": len(candidates_to_score),
                "matched_count": sum(1 for s in lexical_scores.values() if s["score"] > 0),
                "top_lexical": [
                    {"table": t, "score": s["score"], "matches": s["matches"]}
                    for t, s in sorted(lexical_scores.items(), key=lambda x: -x[1]["score"]) if s["score"] > 0
                ][:6],
            },
            candidate_union={
                "expanded_question": expanded_question,
                "lexical_candidates": lexical_candidates,
                "vector_candidates": sorted(vector_candidates),
                "union_before_budget": union_candidates,
                "scored_candidates": candidates_to_score,
                "total_candidates_before_cutoff": len(union_candidates),
                "preserved_candidates": [
                    {
                        "table": name,
                        "preserved": True,
                        "preservation_reason": preservation_scores[name].get("matches", []) or ["vector candidate"],
                        "candidate_priority": preservation_scores[name].get("score", 0.0),
                    }
                    for name in candidates_to_score
                ],
                "candidates_sent_to_reranker": candidates_to_score,
                "omitted_by_candidate_budget": [
                    {
                        "table": name,
                        "reason": "lower existing retrieval evidence at candidate budget",
                        "candidate_priority": preservation_scores[name].get("score", 0.0),
                    }
                    for name in omitted_by_budget
                ],
            },
            vector_retrieval={
                "enabled": self.vector_retriever.embedding_search is not None,
                "hits_count": len(vector_scores),
                "scores": vector_scores,
            },
            reranking=rerank_records[:10],
            relationship_expansion=rel_expansion,
            selected_tables=selected_table_names,
            excluded_tables=(
                [
                    {"table": record["table"], "reason": "no sufficiently strong semantic grounding"}
                    for record in rerank_records if not record.get("grounded")
                ]
                + [
                    {"table": name, "reason": "no retrieval signal"}
                    for name in enabled_tables if name not in candidates_to_score
                ]
            )[:settings.MAX_RETRIEVAL_CANDIDATES],
            timings_ms={"lexical": round(lexical_ms, 3), "vector": round(vector_ms, 3), "reranking": round(rerank_ms, 3), "total": round((time.perf_counter() - started) * 1000, 3)},
        )

        logger.info(
            "RAG Layer retrieved %d table(s) for question %r: %s",
            len(selected_table_names), question, selected_table_names,
        )

        return selected, trace
