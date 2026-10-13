"""
core/mindmap_generator.py

Phase 5 — Database Mind Map

Builds a visual, hierarchical tree of the database:

    Database
    ├── Maintenance
    │      ├── work_orders
    │      └── work_unit
    ├── Inventory
    │      └── inventory_stock
    └── Uncategorized
           └── projects

Domains come from Phase 3 (TableGrouper), and tables within each domain are
ordered by importance from Phase 4 (ImportanceRanker) when available, so the
most central tables in each domain appear first.

This module touches neither the database nor the scanner — it's a pure
rendering step on top of Phase 3 + Phase 4 output, which keeps it fast to
regenerate (e.g. after tweaking domain_rules.py) without rescanning.
"""

from typing import Any, Dict, List, Optional

from core.logger import get_logger

logger = get_logger(__name__)


class MindMapGenerator:
    """
    Generates a text-based mind map tree from table groupings and
    (optionally) importance rankings.

    Usage:
        generator = MindMapGenerator(grouping_data, ranking_data)
        result = generator.build_all()
        print(result["markdown"])
    """

    def __init__(
        self,
        grouping_data: Dict[str, Any],
        ranking_data: Optional[Dict[str, Any]] = None,
        root_label: str = "Database",
    ):
        self.groups = grouping_data.get("groups", {})
        self.ranking_data = ranking_data
        self.root_label = root_label

        # Build a table -> rank lookup for ordering, if rankings were supplied.
        self._table_ranks: Dict[str, int] = {}
        if ranking_data:
            for entry in ranking_data.get("rankings", []):
                self._table_ranks[entry["table"]] = entry["rank"]

    # ------------------------------------------------------------------
    # ORDERING
    # ------------------------------------------------------------------

    def _order_tables(self, tables: List[str]) -> List[str]:
        """
        Order tables within a domain by importance rank (lower rank number
        = more important = appears first) when ranking data is available.
        Falls back to alphabetical order otherwise, so the mind map is
        always deterministic.
        """

        if self._table_ranks:
            return sorted(
                tables,
                key=lambda t: self._table_ranks.get(t, float("inf")),
            )
        return sorted(tables)

    def _order_domains(self) -> List[str]:
        """
        Order domains by their most important table's rank (so domains
        containing the overall most central tables surface first), falling
        back to alphabetical order if no ranking data is available.
        Uncategorized always sorts last regardless.
        """

        domain_names = [d for d in self.groups.keys() if d != "Uncategorized"]

        if self._table_ranks:
            def best_rank(domain: str) -> int:
                tables = self.groups[domain]
                ranks = [self._table_ranks.get(t, float("inf")) for t in tables]
                return min(ranks) if ranks else float("inf")

            domain_names.sort(key=best_rank)
        else:
            domain_names.sort()

        if "Uncategorized" in self.groups:
            domain_names.append("Uncategorized")

        return domain_names

    # ------------------------------------------------------------------
    # TREE STRUCTURE (for programmatic use by later phases)
    # ------------------------------------------------------------------

    def build_tree(self) -> Dict[str, Any]:
        """
        Build a nested dict representation of the mind map:

            {
                "name": "Database",
                "children": [
                    {"name": "Maintenance", "children": [
                        {"name": "work_orders", "children": []},
                        ...
                    ]},
                    ...
                ]
            }
        """

        domain_order = self._order_domains()

        children = []
        for domain in domain_order:
            table_children = [
                {"name": table, "children": []}
                for table in self._order_tables(self.groups[domain])
            ]
            children.append({"name": domain, "children": table_children})

        return {"name": self.root_label, "children": children}

    # ------------------------------------------------------------------
    # TEXT / MARKDOWN RENDERING
    # ------------------------------------------------------------------

    def render_ascii_tree(self) -> str:
        """
        Render the tree using box-drawing characters, matching the format
        from the project roadmap:

            Database
            ├── Maintenance
            │      ├── work_orders
            │      └── work_unit
            └── Inventory
                   └── inventory_stock
        """

        tree = self.build_tree()
        lines = [tree["name"]]

        def render_children(node_children: List[Dict[str, Any]], prefix: str) -> None:
            count = len(node_children)
            for i, child in enumerate(node_children):
                is_last = (i == count - 1)
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{child['name']}")

                extension = "       " if is_last else "│      "
                render_children(child["children"], prefix + extension)

        render_children(tree["children"], "")
        return "\n".join(lines)

    def render_markdown(self) -> str:
        """
        Render a full Markdown document: a title, the ASCII tree in a code
        block for visual clarity, and a nested bullet list underneath for
        anything that renders Markdown natively (e.g. GitHub, docs sites).
        """

        ascii_tree = self.render_ascii_tree()
        domain_order = self._order_domains()

        lines = ["# Database Mind Map", "", "```text", ascii_tree, "```", ""]

        lines.append("## Domain Breakdown")
        lines.append("")
        for domain in domain_order:
            tables = self._order_tables(self.groups[domain])
            lines.append(f"- **{domain}** ({len(tables)} table(s))")
            for table in tables:
                rank_note = ""
                if table in self._table_ranks:
                    rank_note = f" _(importance rank #{self._table_ranks[table]})_"
                lines.append(f"  - {table}{rank_note}")
            lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # ORCHESTRATION
    # ------------------------------------------------------------------

    def build_all(self) -> Dict[str, Any]:
        """
        Run the full Phase 5 generation and return everything the exporter
        needs:

            {
                "tree": { nested dict },
                "ascii": str,
                "markdown": str,
            }
        """

        logger.info("Starting Phase 5 mind map generation")

        tree = self.build_tree()
        ascii_tree = self.render_ascii_tree()
        markdown = self.render_markdown()

        logger.info(
            "Mind map generated: %d domain(s) at root",
            len(tree["children"]),
        )

        return {
            "tree": tree,
            "ascii": ascii_tree,
            "markdown": markdown,
        }