"""
core/graph_visualizer.py

Phase 6 — Relationship Graph Visualization

Converts the Phase 2 relationship data into multiple graph description
formats so the database structure can be visualized in different tools:

    - Mermaid    (renders inline on GitHub, GitLab, Notion, most Markdown
                   viewers, and mermaid.live)
    - Graphviz   (.dot — render with `dot -Tpng graph.dot -o graph.png`,
                   or paste into https://dreampuf.github.io/GraphvizOnline)
    - PlantUML   (.puml — render with a local PlantUML jar, VS Code
                   extension, or http://www.plantuml.com/plantuml)
    - Draw.io    (.csv — import via Draw.io's Extras > Edit Diagram >
                   Edit Data, or File > Import)

This module only consumes the relationship list already built in Phase 2
(RelationshipBuilder.get_relationships()) — it does not touch the database
or rebuild the graph itself.
"""

from typing import Any, Dict, List

from core.logger import get_logger

logger = get_logger(__name__)


class GraphVisualizer:
    """
    Renders the database's FK relationships into several graph
    description formats for external visualization tools.

    Usage:
        visualizer = GraphVisualizer(relationship_data)
        result = visualizer.build_all()
    """

    def __init__(self, relationship_data: Dict[str, Any]):
        self.relationships: List[Dict[str, Any]] = relationship_data.get("relationships", [])
        self.isolated_tables: List[str] = relationship_data.get("isolated_tables", [])

    # ------------------------------------------------------------------
    # MERMAID
    # ------------------------------------------------------------------

    def generate_mermaid(self) -> str:
        """
        Generate a Mermaid flowchart. Each edge is labeled with the FK
        column doing the joining, e.g.:

            graph LR
                work_orders -->|unit_id| work_unit
                work_unit -->|group_id| work_unit_group
        """

        logger.info("Generating Mermaid graph")

        lines = ["graph LR"]

        for rel in self.relationships:
            lines.append(
                f"    {rel['from_table']} -->|{rel['from_column']}| {rel['to_table']}"
            )

        for table in self.isolated_tables:
            lines.append(f"    {table}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # GRAPHVIZ (DOT)
    # ------------------------------------------------------------------

    def generate_graphviz(self) -> str:
        """
        Generate a Graphviz DOT digraph. Nodes are boxed, edges labeled
        with the joining column.
        """

        logger.info("Generating Graphviz DOT graph")

        lines = [
            "digraph Database {",
            '    rankdir=LR;',
            '    node [shape=box, style=rounded, fontname="Helvetica"];',
            '    edge [fontname="Helvetica", fontsize=10];',
            "",
        ]

        for rel in self.relationships:
            lines.append(
                f'    "{rel["from_table"]}" -> "{rel["to_table"]}" '
                f'[label="{rel["from_column"]}"];'
            )

        for table in self.isolated_tables:
            lines.append(f'    "{table}";')

        lines.append("}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # PLANTUML
    # ------------------------------------------------------------------

    def generate_plantuml(self) -> str:
        """
        Generate a PlantUML entity diagram showing tables and their
        FK relationships.
        """

        logger.info("Generating PlantUML graph")

        entities = set()
        for rel in self.relationships:
            entities.add(rel["from_table"])
            entities.add(rel["to_table"])
        entities.update(self.isolated_tables)

        lines = ["@startuml", "left to right direction", ""]

        for entity in sorted(entities):
            lines.append(f"entity {entity}")

        lines.append("")

        for rel in self.relationships:
            lines.append(
                f'{rel["from_table"]} --> {rel["to_table"]} : {rel["from_column"]}'
            )

        lines.append("@enduml")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # DRAW.IO (CSV IMPORT FORMAT)
    # ------------------------------------------------------------------

    def generate_drawio_csv(self) -> str:
        """
        Generate a CSV file compatible with Draw.io's CSV import feature
        (Extras > Edit Diagram, or File > Import). Draw.io connects rows
        based on the special header directives below: each table becomes
        a node, and the "refs" column lists the tables it has outgoing
        foreign keys to (semicolon-separated), which Draw.io turns into
        connecting arrows.

        To use: open app.diagrams.net -> Extras -> Edit Diagram -> paste
        this entire file's contents -> Import.
        """

        logger.info("Generating Draw.io CSV graph")

        # Build table -> set of referenced tables (outgoing FKs only,
        # deduplicated, since Draw.io just needs the connection, not the
        # column detail).
        refs_by_table: Dict[str, set] = {}

        for rel in self.relationships:
            refs_by_table.setdefault(rel["from_table"], set())
            refs_by_table.setdefault(rel["to_table"], set())
            refs_by_table[rel["from_table"]].add(rel["to_table"])

        for table in self.isolated_tables:
            refs_by_table.setdefault(table, set())

        header = [
            "# label: %name%",
            "# style: rounded=1;whiteSpace=wrap;html=1;arcSize=10;fillColor=#dae8fc;strokeColor=#6c8ebf;",
            "# namespace: csvimport-",
            '# connect: {"from":"refs", "to":"id", "invert":true, "style":"curved=0;endArrow=blockThin;endFill=1;fontSize=10;"}',
            "# width: auto",
            "# height: auto",
            "# padding: 15",
            "# ignore: id,refs",
            "# nodespacing: 40",
            "# levelspacing: 100",
            "# edgespacing: 40",
            "# layout: horizontaltree",
            "##",
            "id,name,refs",
        ]

        rows = []
        for table, refs in sorted(refs_by_table.items()):
            refs_str = ";".join(sorted(refs))
            rows.append(f"{table},{table},{refs_str}")

        return "\n".join(header + rows)

    # ------------------------------------------------------------------
    # ORCHESTRATION
    # ------------------------------------------------------------------

    def build_all(self) -> Dict[str, str]:
        """
        Generate all four graph formats.

        Returns:
            {
                "mermaid": str,
                "graphviz": str,
                "plantuml": str,
                "drawio_csv": str,
            }
        """

        logger.info("Starting Phase 6 graph visualization generation")

        result = {
            "mermaid": self.generate_mermaid(),
            "graphviz": self.generate_graphviz(),
            "plantuml": self.generate_plantuml(),
            "drawio_csv": self.generate_drawio_csv(),
        }

        logger.info("Phase 6 complete: 4 graph format(s) generated")

        return result