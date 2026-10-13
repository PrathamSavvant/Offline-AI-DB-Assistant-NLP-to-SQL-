"""
core/inverted_index.py

Inverted token index for fast O(1) candidate table retrieval in large databases.
Maps normalized words/tokens to sets of candidate table names.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from config.stop_words import STOP_WORDS

GENERIC_COLUMNS = {"id", "created_at", "updated_at", "created", "updated", "status", "type", "name"}


def tokenize_text(text: str) -> List[str]:
    """Tokenize text into lowercase alpha-numeric words, removing stop words."""
    words = re.findall(r"\b[a-zA-Z0-9_]+\b", text.lower())
    tokens: Set[str] = set()
    for word in words:
        sub_words = word.split("_")
        for w in sub_words:
            w_strip = w.strip()
            if len(w_strip) >= 2 and w_strip not in STOP_WORDS:
                tokens.add(w_strip)
                # Singular/plural variants
                if w_strip.endswith("s") and not w_strip.endswith("ss") and len(w_strip) > 3:
                    tokens.add(w_strip[:-1])
                elif not w_strip.endswith("s"):
                    tokens.add(w_strip + "s")
    return list(tokens)


class InvertedTableIndex:
    """
    In-memory index mapping tokens to table names.
    Built at schema load time for fast lookup on large schemas (100+ tables).
    """

    def __init__(self) -> None:
        self.index: Dict[str, Set[str]] = {}
        self.table_tokens: Dict[str, Set[str]] = {}
        self.manual_tokens: Dict[str, Set[str]] = {}

    def build(
        self,
        schema: Dict[str, Any],
        grouping_data: Optional[Dict[str, Any]] = None,
        semantics_data: Optional[Dict[str, Any]] = None,
        table_profiles: Optional[Dict[str, Dict[str, Any]]] = None,
        annotations: Optional[Dict[str, Dict[str, Any]]] = None,
        defined_knowledge: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Build index from schema, metadata structures, and defined knowledge."""
        self.index.clear()
        self.table_tokens.clear()
        self.manual_tokens.clear()

        tables = schema.get("tables", [])
        grouping = grouping_data or {}
        table_to_domain = grouping.get("table_to_domain", {})
        domain_groups = grouping.get("domain_groups", {})
        semantics = (semantics_data or {}).get("tables", {})
        profiles = table_profiles or {}
        annos = annotations or {}
        defined = (defined_knowledge or {}).get("tables", {}) if isinstance(defined_knowledge, dict) and "tables" in defined_knowledge else (defined_knowledge or {})

        for table in tables:
            tname = table.get("name")
            if not tname:
                continue

            tokens: Set[str] = set()

            # 1. Table name & variants
            tokens.update(tokenize_text(tname))

            # 2. Columns (excluding generic ones)
            for col in table.get("columns", []):
                cname = col.get("name", "")
                if cname and cname.lower() not in GENERIC_COLUMNS:
                    tokens.update(tokenize_text(cname))

            # 3. Domain & grouping tags
            domain = table_to_domain.get(tname, "")
            if domain:
                tokens.update(tokenize_text(domain))
            for dom_name, dom_info in domain_groups.items():
                if tname in dom_info.get("tables", []):
                    tokens.update(tokenize_text(dom_name))

            # 4. Business semantics
            sem = semantics.get(tname, {})
            if sem.get("business_domain"):
                tokens.update(tokenize_text(sem["business_domain"]))
            for tag in sem.get("business_tags", []):
                tokens.update(tokenize_text(tag))

            # 5. Table profiles
            prof = profiles.get(tname, {})
            for alias in prof.get("aliases", []):
                tokens.update(tokenize_text(alias))
            if prof.get("table_purpose"):
                tokens.update(tokenize_text(prof["table_purpose"]))

            # 6. User Annotations
            ann = annos.get(tname, {})
            for alias in ann.get("aliases", []):
                tokens.update(tokenize_text(alias))
            if ann.get("meaning"):
                tokens.update(tokenize_text(ann["meaning"]))

            # 7. Defined Knowledge
            def_table = defined.get(tname, {}) if isinstance(defined, dict) else {}
            manual_tokens: Set[str] = set()
            if def_table.get("definition"):
                manual_tokens.update(tokenize_text(def_table["definition"]))
            for field in ("business_entity", "business_name", "intended_usage", "common_phrases", "query_phrases", "aliases"):
                value = def_table.get(field, [])
                for item in value if isinstance(value, list) else [value]:
                    manual_tokens.update(tokenize_text(str(item)))
            for syn in def_table.get("synonyms", []):
                manual_tokens.update(tokenize_text(syn))
            for abbr, abbr_info in def_table.get("abbreviations", {}).items():
                manual_tokens.update(tokenize_text(abbr))
                m_val = abbr_info.get("meaning", "") if isinstance(abbr_info, dict) else str(abbr_info)
                if m_val:
                    manual_tokens.update(tokenize_text(m_val))
                if isinstance(abbr_info, dict) and abbr_info.get("context"):
                    manual_tokens.update(tokenize_text(abbr_info["context"]))
            for column in def_table.get("columns", {}).values():
                for field in ("business_meaning", "aliases", "synonyms", "abbreviations", "common_phrases", "intended_usage"):
                    value = column.get(field, [])
                    for item in value if isinstance(value, list) else [value]:
                        manual_tokens.update(tokenize_text(str(item)))
            for predicate in def_table.get("predicate_mappings", []):
                if predicate.get("approval_status") in {"USER_VERIFIED", "USER_DEFINED", "APPROVED"}:
                    manual_tokens.update(tokenize_text(str(predicate.get("phrase") or "")))

            tokens.update(manual_tokens)
            self.manual_tokens[tname] = manual_tokens

            self.table_tokens[tname] = tokens
            for token in tokens:
                if token not in self.index:
                    self.index[token] = set()
                self.index[token].add(tname)

    def update_manual_knowledge(self, table_name: str, knowledge: Dict[str, Any]) -> None:
        """Reindex just one table's user-authored retrieval terms."""
        old = self.manual_tokens.get(table_name, set())
        for token in old:
            names = self.index.get(token)
            if names:
                names.discard(table_name)
                if not names:
                    del self.index[token]
        new: Set[str] = set()
        new.update(tokenize_text(str(knowledge.get("definition") or "")))
        for field in ("business_entity", "business_name", "intended_usage", "common_phrases", "query_phrases", "aliases"):
            value = knowledge.get(field, [])
            for item in value if isinstance(value, list) else [value]:
                new.update(tokenize_text(str(item)))
        for synonym in knowledge.get("synonyms", []):
            new.update(tokenize_text(str(synonym)))
        for abbreviation, entry in knowledge.get("abbreviations", {}).items():
            new.update(tokenize_text(str(abbreviation)))
            new.update(tokenize_text(str(entry.get("meaning", "") if isinstance(entry, dict) else entry)))
            if isinstance(entry, dict): new.update(tokenize_text(str(entry.get("context", ""))))
        for column in knowledge.get("columns", {}).values():
            new.update(tokenize_text(str(column.get("business_meaning") or "")))
            for field in ("aliases", "synonyms", "abbreviations", "common_phrases", "intended_usage"):
                for item in column.get(field, []): new.update(tokenize_text(str(item)))
        for predicate in knowledge.get("predicate_mappings", []):
            if predicate.get("approval_status") in {"USER_VERIFIED", "USER_DEFINED", "APPROVED"}:
                new.update(tokenize_text(str(predicate.get("phrase") or "")))
        self.manual_tokens[table_name] = new
        self.table_tokens.setdefault(table_name, set()).update(new)
        for token in new:
            self.index.setdefault(token, set()).add(table_name)

    def get_candidate_tables(self, question: str) -> Set[str]:
        """Return candidate tables matching any token in the question."""
        q_tokens = tokenize_text(question)
        candidates: Set[str] = set()
        for token in q_tokens:
            if token in self.index:
                candidates.update(self.index[token])
        return candidates
