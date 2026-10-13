"""
config/stop_words.py

Editable list of generic English words excluded from Phase 9's annotation
meaning-overlap scoring (core/context_generator.py).

Bug this fixes: the meaning-overlap heuristic treats any word longer than
3 characters as potential business vocabulary. Without filtering, common
words like "their", "with", or "working" — which appear in almost any
natural-language question and often in annotation text too — count as a
"match" and inflate the relevance score of tables that have nothing to do
with the question.

If you notice a false-positive table selection in the logs (an
"annotation meaning overlap" entry containing a word that isn't real
business vocabulary — e.g. "their", "show", "using"), add it here. This
list intentionally does NOT affect alias matching or any other scoring
signal — only the free-text meaning-overlap check.
"""

STOP_WORDS = {
    # Articles, pronouns, demonstratives
    "the", "their", "theirs", "there", "this", "that", "these", "those",
    "them", "they", "your", "yours", "ours", "its",
    # Prepositions / conjunctions
    "with", "into", "from", "about", "over", "under", "between", "among",
    "and", "for", "than", "then", "to", "of", "in", "on", "at", "by",
    "as", "or", "a", "an",
    # Auxiliary / common verbs
    "are", "was", "were", "have", "has", "had", "will", "would", "could",
    "should", "does", "done", "being", "been", "is", "be", "do", "not",
    # Question / command words that appear in NL questions but say
    # nothing about which table is relevant
    "show", "list", "find", "display", "give", "get", "tell", "what",
    "when", "where", "which", "while", "how", "many", "much", "who",
    "using", "through", "together",
    # Generic quantifiers / fillers
    "each", "every", "some", "many", "more", "most", "only", "all",
    "just", "also", "here", "working", "work",
    # Generic naming words that appear constantly but identify nothing
    "name", "names", "value", "values",
}
