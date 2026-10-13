"""
config/domain_rules.py

Editable keyword rules used by Phase 3 (Table Grouping) to classify tables
into business domains.

HOW IT WORKS
------------
Each domain maps to a list of keywords. A table is assigned to the first
domain whose keyword appears as a substring of the table name
(case-insensitive). Order matters: domains listed earlier are checked first,
so put more specific rules above more general ones.

If a table name doesn't match any keyword in any domain, it falls into the
"Uncategorized" domain automatically — you don't need to (and shouldn't)
list that here.

EDITING THIS FILE
-----------------
This is the file you'll come back to as you see real table names in your
knowledge/table_groups.json output. If tables are landing in the wrong
domain, or too many end up "Uncategorized", just adjust the keyword lists
below — no other code needs to change.

Example: if your database has tables like `wh_stock` and `wh_transfer` for
warehouse stock, and they're not matching "Inventory" below, just add
"wh_" to the Inventory keyword list.
"""

from typing import Dict, List

# Order matters — more specific domains should come before general ones.
DOMAIN_RULES: Dict[str, List[str]] = {
    "Maintenance": [
        "work_order", "work_unit", "maintenance", "repair", "breakdown",
        "service", "equipment", "asset", "machine",
    ],
    "Inventory": [
        "inventory", "stock", "product", "warehouse", "item", "material",
        "supplier", "purchase_order", "goods",
    ],
    "Finance": [
        "invoice", "payment", "transaction", "ledger", "account", "budget",
        "expense", "revenue", "tax", "billing",
    ],
    "Users": [
        "user", "employee", "staff", "person", "profile", "auth",
        "login", "role", "permission",
    ],
    "Administration": [
        "config", "setting", "audit", "log", "system", "admin",
        "notification", "session",
    ],
    "Sales": [
        "order", "customer", "client", "quote", "lead", "sales",
        "contract",
    ],
    "Departments": [
        "departments",
    ],
}

# Fallback domain name for tables that match nothing above. Do not add this
# as a key in DOMAIN_RULES — it's applied automatically.
UNCATEGORIZED_DOMAIN = "Uncategorized"