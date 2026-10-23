"""A static copy of the seed schema's policy, so the eval suites that need no database
(adversarial SQL, reference-SQL validity) can run offline and in CI. An integration test
checks that it still matches the live database."""

from __future__ import annotations

from guardsql.guardrails.policy import SchemaPolicy

RESTRICTED = frozenset({"email", "phone", "date_of_birth", "salary"})

SEED_POLICY = SchemaPolicy(
    schema_name="shop",
    tables={
        "customers": ("id", "first_name", "last_name", "country", "signup_date"),
        "employees": ("id", "name", "department", "hire_date"),
        "products": ("id", "name", "category", "price", "stock"),
        "orders": ("id", "customer_id", "order_date", "status", "total_amount"),
        "order_items": ("id", "order_id", "product_id", "quantity", "unit_price"),
    },
    restricted_columns=RESTRICTED,
)
