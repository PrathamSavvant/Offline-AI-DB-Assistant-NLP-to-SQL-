"""Single source of truth for exported knowledge-base filenames.

Keep this module aligned with ``core.exporter`` whenever export names change.
Runtime validation and cache loading must refer here rather than duplicating
filename literals, otherwise a successful rebuild can be rejected later.
"""

# Complete runtime knowledge contract. Visualisation artifacts (graph/mindmap)
# are deliberately excluded because they are optional diagnostic output.
REQUIRED_KNOWLEDGE_FILES = (
    "schema.json",
    "relationships.json",
    "table_groups.json",
    "table_importance.json",
    "business_semantics.json",
    "joins.json",
    "database_summary.json",
)

# The cache consumes these directly. The remaining required files are used by
# other runtime services and are therefore validated by schema synchronization.
RUNTIME_CACHE_FILES = (
    "schema.json",
    "table_groups.json",
    "business_semantics.json",
    "table_importance.json",
)

# Pre-refactor names retained solely for an older exporter during upgrades.
# New names always take precedence when both exist.
LEGACY_KNOWLEDGE_FILE_ALIASES = {
    "table_groups.json": "grouping.json",
    "business_semantics.json": "semantics.json",
    "table_importance.json": "ranking.json",
}
