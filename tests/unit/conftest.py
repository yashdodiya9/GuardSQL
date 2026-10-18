import pytest

from guardsql.guardrails.policy import SchemaPolicy


@pytest.fixture(scope="session")
def policy() -> SchemaPolicy:
    """Mirrors what the read-only role can see in the M1 database."""
    return SchemaPolicy(
        schema_name="shop",
        tables={
            "customers": ("id", "first_name", "last_name", "country", "signup_date"),
            "employees": ("id", "name", "department", "hire_date"),
            "products": ("id", "name", "category", "price", "stock"),
            "orders": ("id", "customer_id", "order_date", "status", "total_amount"),
            "order_items": ("id", "order_id", "product_id", "quantity", "unit_price"),
        },
        restricted_columns=frozenset({"email", "phone", "date_of_birth", "salary"}),
    )
