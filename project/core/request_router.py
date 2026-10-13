"""
core/request_router.py

Lightweight pre-LangGraph request router.

Simple requests (schema browsing, relationship lookups, dictionary
reads, metadata) are handled DIRECTLY by Python modules — LangGraph is
NEVER invoked for them.

LangGraph is only reached when the request genuinely requires LLM
reasoning (NL→SQL generation, complex queries, SQL explanation).

Route decision returned as a string constant:
  DIRECT_SCHEMA        → return schema from cache
  DIRECT_TABLE_INFO    → return table info from cache
  DIRECT_RELATIONSHIPS → return relationships from cache
  DIRECT_DICTIONARY    → return dictionary entry
  DIRECT_METADATA      → return metadata counts
  LANGGRAPH            → hand off to LangGraph (NL→SQL, complex)
"""

import re
from dataclasses import dataclass, field
from typing import Dict

from core.logger import get_logger

logger = get_logger(__name__)

# Route constants
ROUTE_SCHEMA = "DIRECT_SCHEMA"
ROUTE_TABLE_INFO = "DIRECT_TABLE_INFO"
ROUTE_RELATIONSHIPS = "DIRECT_RELATIONSHIPS"
ROUTE_DICTIONARY = "DIRECT_DICTIONARY"
ROUTE_METADATA = "DIRECT_METADATA"
ROUTE_LANGGRAPH = "LANGGRAPH"
ROUTE_EXPLANATION = "EXPLANATION"
ROUTE_CONVERSATION = "CONVERSATION"
ROUTE_UNCLEAR = "UNCLEAR"

@dataclass(frozen=True)
class RouteResult:
    route: str
    params: Dict[str, str] = field(default_factory=dict)

class RequestRouter:
    """
    Classify incoming questions and route them to the fastest handler.
    All pattern matching is O(1) string ops — no LLM involved.
    """

    def route(self, question: str) -> RouteResult:
        """
        Returns (route_constant, params_dict).
        params_dict carries extracted entities (table name, etc.) for the handler.
        """
        q = question.strip()
        ql = q.lower()

        # Terminal non-database routes. These are intentionally deterministic:
        # greetings/help must never enter schema retrieval or the SQL model.
        conversational = (
            r"(?:hi|hello|hey|good\s+(?:morning|afternoon|evening))",
            r"(?:thanks|thank\s+you)(?:\s+(?:very\s+)?much)?",
            r"(?:who\s+are\s+you|what\s+can\s+you\s+do)",
        )
        if any(re.fullmatch(pattern + r"[!,.?\s]*", ql) for pattern in conversational):
            logger.info("Router: CONVERSATION for '%s'", q[:60])
            return RouteResult(ROUTE_CONVERSATION)

        # A request verb without a database target is not an analytical query.
        if re.fullmatch(
            r"(?:show\s+me|give\s+me\s+(?:some\s+)?data|tell\s+me\s+something)(?:\s+please)?[!,.?\s]*",
            ql,
        ):
            logger.info("Router: UNCLEAR for '%s'", q[:60])
            return RouteResult(ROUTE_UNCLEAR)

        # --- Schema / column listing ---
        if re.search(r"\b(show|list|display|get)\s+(the\s+)?(full\s+)?schema\b", ql):
            logger.info("Router: DIRECT_SCHEMA for '%s'", q[:60])
            return RouteResult(ROUTE_SCHEMA)

        if re.search(r"\b(show|list|display|get)\s+(all\s+)?tables?\b", ql):
            logger.info("Router: DIRECT_METADATA (list tables) for '%s'", q[:60])
            return RouteResult(ROUTE_METADATA)

        if re.search(r"\bhow many tables\b", ql):
            logger.info("Router: DIRECT_METADATA (count tables) for '%s'", q[:60])
            return RouteResult(ROUTE_METADATA)

        # SQL explanations must be classified before generic table explanations.
        if re.search(
            r"\b(?:explain\s+(?:the\s+|this\s+|last\s+|sql\s+|query\s+)*(?:sql|query)|"
            r"what\s+does\s+(?:(?:this|the|last)\s+)?sql(?:\s+query)?\s+do)\b",
            ql,
        ):
            logger.info("Router: EXPLANATION for '%s'", q[:60])
            return RouteResult(ROUTE_EXPLANATION)

        # --- Relationship lookups ---
        rel_match = re.search(
            r"\b(show|find|list|get|display)\s+(the\s+)?(foreign\s+keys?|relationships?|connections?)\s+(for|of|in|on)\s+(?:table\s+)?([a-z0-9_]+)\b",
            ql,
        )
        if rel_match:
            tname = rel_match.group(5)
            logger.info("Router: DIRECT_RELATIONSHIPS table=%s for '%s'", tname, q[:60])
            return RouteResult(ROUTE_RELATIONSHIPS, {"table": tname})

        if re.search(r"\b(show|find|list|display|get)\s+(all\s+)?(the\s+)?(database\s+)?(relationships?|foreign keys?)\b", ql):
            logger.info("Router: DIRECT_RELATIONSHIPS (all) for '%s'", q[:60])
            return RouteResult(ROUTE_RELATIONSHIPS)

        # --- Table explain / column info ---
        # Require either: "explain X", "describe X", "show columns for X",
        # or "what is X table" / "what is the X table" (must end in "table")
        # This avoids matching data questions like "what is the total revenue"
        explain_match = re.search(
            r"\b(explain|describe|show columns?\s+(?:for|of|in))\s+(?:table\s+|the\s+)?([a-z0-9_]+)(?:\s+table)?\b",
            ql,
        )
        what_is_table = re.search(
            r"\bwhat is\s+(?:the\s+)?([a-z0-9_]+)\s+table\b",
            ql,
        )
        if explain_match:
            target = explain_match.group(2)
            skip = {"all", "database", "schema", "relationships", "tables", "the", "me", "columns", "foreign", "keys", "last", "sql", "query"}
            if target not in skip:
                logger.info("Router: DIRECT_TABLE_INFO table=%s for '%s'", target, q[:60])
                return RouteResult(ROUTE_TABLE_INFO, {"table": target})
        if what_is_table:
            target = what_is_table.group(1)
            skip = {"all", "database", "schema", "relationships", "tables", "the"}
            if target not in skip:
                logger.info("Router: DIRECT_TABLE_INFO table=%s for '%s'", target, q[:60])
                return RouteResult(ROUTE_TABLE_INFO, {"table": target})

        # --- Dictionary lookups (read only) ---
        if re.search(r"\b(what does|what is|look up|find|search)\s+.{1,40}\s+(mean|stand for|abbreviat)\b", ql):
            logger.info("Router: DIRECT_DICTIONARY for '%s'", q[:60])
            return RouteResult(ROUTE_DICTIONARY)

        if re.search(r"\b(show|list|display)\s+(the\s+)?(business\s+)?dictionary\b", ql):
            logger.info("Router: DIRECT_DICTIONARY for '%s'", q[:60])
            return RouteResult(ROUTE_DICTIONARY)

        # --- Everything else → LangGraph (NL→SQL reasoning) ---
        logger.info("Router: LANGGRAPH for '%s'", q[:60])
        return RouteResult(ROUTE_LANGGRAPH)


# Global singleton
request_router = RequestRouter()
