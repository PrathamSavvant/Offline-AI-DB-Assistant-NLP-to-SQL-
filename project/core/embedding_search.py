"""
core/embedding_search.py

Embedding Retrieval — semantic similarity search over table/column
descriptions, using sentence-transformers (BAAI/bge-small-en-v1.5) with a
FAISS index.

Requirement #8 of the extension. This is the last of the three extra
signals (Dictionary, RapidFuzz, Embeddings) that feed into Phase 9's
Hybrid Retrieval alongside the existing keyword ranking — it never
replaces that ranking.

Both sentence-transformers and faiss are optional/heavy dependencies and
the model is downloaded on first use. To keep the rest of the pipeline
working with zero setup, this module:
  - loads the model lazily (only on first real query, not at import time)
  - catches import/load failures and simply reports "unavailable" rather
    than raising, so a missing/not-yet-downloaded model never breaks
    scanning, relationship building, or SQL generation.
The FAISS index is rebuilt from the current schema each time
`build_index()` is called (cheap relative to model inference) and cached
on disk under knowledge/embeddings/ so restarts don't require re-encoding
every table/column from scratch.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.db_context import db_context
from core.logger import get_logger
from config.settings import settings

logger = get_logger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"


class EmbeddingSearch:
    """
    Usage:
        embeddings = EmbeddingSearch(schema)
        embeddings.build_index()                 # once per crawl
        embeddings.search("who placed the order") -> [("orders", 0.81), ...]
    """

    def __init__(self, schema: Dict[str, Any], index_dir: Optional[Path] = None):
        self.schema = schema
        self.index_dir = index_dir if index_dir is not None else (db_context.get_knowledge_dir() / "embeddings")
        self._model = None
        self._faiss_index = None
        self._table_names: List[str] = []
        self.available = True
        self._load_error = None

    # ------------------------------------------------------------------
    # LAZY MODEL / INDEX LOADING
    # ------------------------------------------------------------------

    def _get_model(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
            # Production deployments should preload the model. local_files_only
            # prevents a user query from unexpectedly downloading from the web.
            self._model = SentenceTransformer(MODEL_NAME, local_files_only=True)
        except Exception as exc:  # broad: missing package, no internet, OOM, etc.
            self.available = False
            self._load_error = str(exc)
            logger.info("Embedding model unavailable (%s) — semantic search signal disabled", exc)
        return self._model

    @staticmethod
    def _table_document(table: Dict[str, Any], defined_knowledge: Optional[Dict[str, Any]] = None) -> str:
        """Flatten a table's structural info into one describable string."""
        col_names = ", ".join(c["name"] for c in table.get("columns", []))
        parts = [table["name"].replace("_", " "), col_names]
        if table.get("comment"):
            parts.append(table["comment"])
        if defined_knowledge:
            def_t = defined_knowledge.get(table["name"], {}) if isinstance(defined_knowledge, dict) else {}
            if def_t.get("definition"):
                parts.append(def_t["definition"])
            if def_t.get("synonyms"):
                parts.append(", ".join(def_t["synonyms"]))
            abbreviations = def_t.get("abbreviations", {})
            if abbreviations:
                parts.append(", ".join(
                    f"{token} {entry.get('meaning', '') if isinstance(entry, dict) else entry}"
                    for token, entry in abbreviations.items()
                ))
        return " | ".join(p for p in parts if p)

    def build_index(self, force: bool = False, defined_knowledge: Optional[Dict[str, Any]] = None) -> bool:
        """
        Encode every table into a vector and build a FAISS index. Returns
        False (without raising) if the embedding stack isn't available.
        Skips rebuilding if the schema fingerprint hasn't changed.
        """
        from core import schema_fingerprint
        current_fp = schema_fingerprint.compute(self.schema)
        fp_path = self.index_dir / "embeddings_fingerprint.json"

        if not force and fp_path.exists() and (self.index_dir / "tables.index").exists():
            stored_fp = schema_fingerprint.load(fp_path)
            if stored_fp == current_fp and self.load_index():
                logger.info("Embedding index is up-to-date with schema fingerprint — reusing cached index")
                return True

        model = self._get_model()
        if model is None:
            return False

        try:
            import faiss
            import numpy as np
        except Exception as exc:
            self.available = False
            self._load_error = str(exc)
            logger.warning("faiss unavailable (%s) — semantic search signal disabled", exc)
            return False

        tables = self.schema.get("tables", [])
        if not tables:
            return False

        documents = [self._table_document(t, defined_knowledge=defined_knowledge) for t in tables]
        self._table_names = [t["name"] for t in tables]

        vectors = model.encode(documents, normalize_embeddings=True, batch_size=32, show_progress_bar=False)
        vectors = np.asarray(vectors, dtype="float32")

        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        self._faiss_index = index

        self.index_dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(index, str(self.index_dir / "tables.index"))
        with open(self.index_dir / "table_names.json", "w", encoding="utf-8") as f:
            json.dump(self._table_names, f)
        schema_fingerprint.save(current_fp, fp_path)

        logger.info("Embedding index built for %d table(s)", len(self._table_names))
        return True

    def load_index(self) -> bool:
        """Load a previously-built index from disk instead of re-encoding."""
        try:
            import faiss
        except Exception as exc:
            self.available = False
            self._load_error = str(exc)
            return False

        index_path = self.index_dir / "tables.index"
        names_path = self.index_dir / "table_names.json"
        if not index_path.exists() or not names_path.exists():
            return False

        self._faiss_index = faiss.read_index(str(index_path))
        with open(names_path, "r", encoding="utf-8") as f:
            self._table_names = json.load(f)
        return True

    # ------------------------------------------------------------------
    # SEARCH
    # ------------------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """
        Return the top_k most semantically similar tables to `query`, as
        (table_name, similarity 0-1) pairs. Returns [] if the embedding
        stack isn't available — callers should treat that as "no signal",
        not an error.
        """

        if self._faiss_index is None:
            if not self.load_index() and not self.build_index():
                return []

        model = self._get_model()
        if model is None or self._faiss_index is None:
            return []

        try:
            import numpy as np
            query_vec = model.encode([query], normalize_embeddings=True)
            query_vec = np.asarray(query_vec, dtype="float32")
            scores, indices = self._faiss_index.search(query_vec, min(top_k, len(self._table_names)))
        except Exception as exc:
            logger.warning("Embedding search failed (%s)", exc)
            return []

        results = []
        for idx, score in zip(indices[0], scores[0]):
            if 0 <= idx < len(self._table_names):
                results.append((self._table_names[idx], float(score)))
        return results

    def score_for_table(self, query: str, table_name: str) -> float:
        """Convenience: similarity (0-1) between a query and one specific table."""
        for name, score in self.search(query, top_k=max(10, len(self._table_names) or 10)):
            if name == table_name:
                return max(0.0, score)
        return 0.0
