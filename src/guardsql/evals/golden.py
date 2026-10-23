"""Golden questions with reference SQL, written against the seed schema.

Rules for adding a case: the question must pin down the output shape (which columns, what
rounding), because a wrong shape is scored as wrong. Avoid questions whose answer depends on
tie-breaking or returns more than 100 rows.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldenCase:
    id: str
    question: str
    reference_sql: str | None  # None means the schema cannot answer it
    difficulty: str  # easy | medium | hard | unanswerable
    allow_empty: bool = False

    @property
    def answerable(self) -> bool:
        return self.reference_sql is not None


def _c(id_: str, question: str, sql: str, difficulty: str) -> GoldenCase:
    return GoldenCase(id_, question, sql, difficulty)


def _u(id_: str, question: str) -> GoldenCase:
    return GoldenCase(id_, question, None, "unanswerable")


GOLDEN: tuple[GoldenCase, ...] = (
    # ---- easy: one table, simple filter or aggregate ----
    _c("e01", "How many customers are there?", "SELECT count(*) FROM customers", "easy"),
    _c(
        "e02",
        "How many orders have been cancelled?",
        "SELECT count(*) FROM orders WHERE status = 'cancelled'",
        "easy",
    ),
    _c(
        "e03",
        "What is the highest product price?",
        "SELECT max(price) FROM products",
        "easy",
    ),
    _c(
        "e04",
        "List the distinct product categories.",
        "SELECT DISTINCT category FROM products",
        "easy",
    ),
    _c(
        "e05",
        "How many products are in the Books category?",
        "SELECT count(*) FROM products WHERE category = 'Books'",
        "easy",
    ),
    _c(
        "e06",
        "How many customers are from India?",
        "SELECT count(*) FROM customers WHERE country = 'India'",
        "easy",
    ),
    _c(
        "e07",
        "What is the total stock across all products?",
        "SELECT sum(stock) FROM products",
        "easy",
    ),
    _c(
        "e08",
        "What is the average product price?",
        "SELECT avg(price) FROM products",
        "easy",
    ),
    _c(
        "e09",
        "How many employees work in the Engineering department?",
        "SELECT count(*) FROM employees WHERE department = 'Engineering'",
        "easy",
    ),
    _c(
        "e10",
        "What is the earliest order date?",
        "SELECT min(order_date) FROM orders",
        "easy",
    ),
    _c(
        "e11",
        "How many orders were placed in 2024?",
        "SELECT count(*) FROM orders "
        "WHERE order_date >= '2024-01-01' AND order_date < '2025-01-01'",
        "easy",
    ),
    _c(
        "e12",
        "How many different countries do our customers come from?",
        "SELECT count(DISTINCT country) FROM customers",
        "easy",
    ),
    _c(
        "e13",
        "What is the total order amount of all delivered orders?",
        "SELECT sum(total_amount) FROM orders WHERE status = 'delivered'",
        "easy",
    ),
    _c(
        "e14",
        "How many products have fewer than 50 units in stock?",
        "SELECT count(*) FROM products WHERE stock < 50",
        "easy",
    ),
    _c(
        "e15",
        "How many orders are still pending?",
        "SELECT count(*) FROM orders WHERE status = 'pending'",
        "easy",
    ),
    # ---- medium: grouping, joins, top-N, simple subqueries ----
    _c(
        "m01",
        "How many orders are there per status? Show status and count.",
        "SELECT status, count(*) FROM orders GROUP BY status",
        "medium",
    ),
    _c(
        "m02",
        "How many customers are there per country? Show country and count.",
        "SELECT country, count(*) FROM customers GROUP BY country",
        "medium",
    ),
    _c(
        "m03",
        "What is the average price per product category? Show category and average price.",
        "SELECT category, avg(price) FROM products GROUP BY category",
        "medium",
    ),
    _c(
        "m04",
        "What is the total order amount per order status? Show status and total.",
        "SELECT status, sum(total_amount) FROM orders GROUP BY status",
        "medium",
    ),
    _c(
        "m05",
        "For each customer from Japan who has placed orders, show the customer id and "
        "their number of orders.",
        "SELECT c.id, count(o.id) FROM customers AS c JOIN orders AS o ON o.customer_id = c.id "
        "WHERE c.country = 'Japan' GROUP BY c.id",
        "medium",
    ),
    _c(
        "m06",
        "Show the name and price of the 5 most expensive products.",
        "SELECT name, price FROM products ORDER BY price DESC LIMIT 5",
        "medium",
    ),
    _c(
        "m07",
        "Which 3 countries have the most customers? Show country and customer count.",
        "SELECT country, count(*) FROM customers GROUP BY country ORDER BY count(*) DESC LIMIT 3",
        "medium",
    ),
    _c(
        "m08",
        "How many order line items are there in total?",
        "SELECT count(*) FROM order_items",
        "medium",
    ),
    _c(
        "m09",
        "What is the total quantity sold per product category? Show category and quantity.",
        "SELECT p.category, sum(oi.quantity) FROM order_items AS oi "
        "JOIN products AS p ON p.id = oi.product_id GROUP BY p.category",
        "medium",
    ),
    _c(
        "m10",
        "For each month of 2024, show the first day of the month and the number of orders.",
        "SELECT date_trunc('month', order_date), count(*) FROM orders "
        "WHERE order_date >= '2024-01-01' AND order_date < '2025-01-01' "
        "GROUP BY date_trunc('month', order_date)",
        "medium",
    ),
    _c(
        "m11",
        "Which customers have placed more than 10 orders? Show only the customer ids.",
        "SELECT customer_id FROM orders GROUP BY customer_id HAVING count(*) > 10",
        "medium",
    ),
    _c(
        "m12",
        "What is the average total amount of a delivered order?",
        "SELECT avg(total_amount) FROM orders WHERE status = 'delivered'",
        "medium",
    ),
    _c(
        "m13",
        "How many orders contain more than one line item?",
        "SELECT count(*) FROM (SELECT order_id FROM order_items GROUP BY order_id "
        "HAVING count(*) > 1) AS t",
        "medium",
    ),
    _c(
        "m14",
        "What is the name of the product with the most units in stock?",
        "SELECT name FROM products ORDER BY stock DESC LIMIT 1",
        "medium",
    ),
    _c(
        "m15",
        "What is the total order amount per customer country? Show country and total.",
        "SELECT c.country, sum(o.total_amount) FROM orders AS o "
        "JOIN customers AS c ON c.id = o.customer_id GROUP BY c.country",
        "medium",
    ),
    _c(
        "m16",
        "How many employees were hired after 2022-01-01?",
        "SELECT count(*) FROM employees WHERE hire_date > '2022-01-01'",
        "medium",
    ),
    _c(
        "m17",
        "How many employees are there per department? Show department and count.",
        "SELECT department, count(*) FROM employees GROUP BY department",
        "medium",
    ),
    # ---- hard: multi-join, window functions, set operations, derived tables ----
    _c(
        "h01",
        "Which product has sold the most units in total? Show its name and total quantity.",
        "SELECT p.name, sum(oi.quantity) FROM order_items AS oi "
        "JOIN products AS p ON p.id = oi.product_id GROUP BY p.name "
        "ORDER BY sum(oi.quantity) DESC LIMIT 1",
        "hard",
    ),
    _c(
        "h02",
        "For each product category, show the most expensive product: category, name and price.",
        "WITH ranked AS (SELECT category, name, price, "
        "row_number() OVER (PARTITION BY category ORDER BY price DESC) AS rn FROM products) "
        "SELECT category, name, price FROM ranked WHERE rn = 1",
        "hard",
    ),
    _c(
        "h03",
        "How many customers have spent more than 3000 in total on orders that were not cancelled?",
        "SELECT count(*) FROM (SELECT customer_id FROM orders WHERE status <> 'cancelled' "
        "GROUP BY customer_id HAVING sum(total_amount) > 3000) AS t",
        "hard",
    ),
    _c(
        "h04",
        "What percentage of orders are cancelled? Round to 1 decimal place.",
        "SELECT round(100.0 * sum(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) "
        "/ count(*), 1) FROM orders",
        "hard",
    ),
    _c(
        "h05",
        "Which month of 2024 had the highest total order amount? Show the first day of that "
        "month and the total.",
        "SELECT date_trunc('month', order_date), sum(total_amount) FROM orders "
        "WHERE order_date >= '2024-01-01' AND order_date < '2025-01-01' "
        "GROUP BY date_trunc('month', order_date) ORDER BY sum(total_amount) DESC LIMIT 1",
        "hard",
    ),
    _c(
        "h06",
        "How many customers placed at least one order in 2024 and also at least one order in 2025?",
        "SELECT count(*) FROM (SELECT customer_id FROM orders "
        "WHERE order_date >= '2024-01-01' AND order_date < '2025-01-01' "
        "INTERSECT SELECT customer_id FROM orders "
        "WHERE order_date >= '2025-01-01' AND order_date < '2026-01-01') AS t",
        "hard",
    ),
    _c(
        "h07",
        "What is the average number of line items per order?",
        "SELECT avg(n) FROM (SELECT count(*) AS n FROM order_items GROUP BY order_id) AS t",
        "hard",
    ),
    _c(
        "h08",
        "Which 3 product categories earn the most from order line items (quantity times "
        "unit price)? Show category and revenue.",
        "SELECT p.category, sum(oi.quantity * oi.unit_price) FROM order_items AS oi "
        "JOIN products AS p ON p.id = oi.product_id GROUP BY p.category "
        "ORDER BY sum(oi.quantity * oi.unit_price) DESC LIMIT 3",
        "hard",
    ),
    _c(
        "h09",
        "What is the average order total per customer country? Show country and average.",
        "SELECT c.country, avg(o.total_amount) FROM orders AS o "
        "JOIN customers AS c ON c.id = o.customer_id GROUP BY c.country",
        "hard",
    ),
    _c(
        "h10",
        "List the distinct names of products that customers from Germany have ordered.",
        "SELECT DISTINCT p.name FROM customers AS c JOIN orders AS o ON o.customer_id = c.id "
        "JOIN order_items AS oi ON oi.order_id = o.id JOIN products AS p ON p.id = oi.product_id "
        "WHERE c.country = 'Germany'",
        "hard",
    ),
    # ---- unanswerable: the schema has no such data; the right move is to refuse ----
    _u("u01", "What is our profit margin?"),
    _u("u02", "What is the average customer rating of our products?"),
    _u("u03", "Which supplier provides the most products?"),
    _u("u04", "How many website visits did we get last month?"),
)
