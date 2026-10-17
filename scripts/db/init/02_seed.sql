-- Fixed seed so every environment (and CI) gets the same shape of data.
SELECT setseed(0.42);

INSERT INTO shop.customers
    (id, first_name, last_name, email, phone, date_of_birth, country, signup_date)
SELECT
    g,
    (ARRAY['Aarav','Priya','Rohan','Ananya','Liam','Emma','Noah','Olivia',
           'Mateo','Sofia','Hiro','Yuki','Omar','Layla','Chen','Mei'])[1 + floor(random() * 16)::int],
    (ARRAY['Sharma','Patel','Singh','Kumar','Smith','Johnson',
           'Garcia','Tanaka','Khan','Wang','Brown','Silva'])[1 + floor(random() * 12)::int],
    'customer' || g || '@example.com',
    '+1-555-' || lpad((1000 + g)::text, 4, '0'),
    date '1960-01-01' + (random() * 15000)::int,
    (ARRAY['India','USA','UK','Germany','Brazil','Japan','Canada'])[1 + floor(random() * 7)::int],
    date '2022-01-01' + (random() * 1000)::int
FROM generate_series(1, 300) AS g;

INSERT INTO shop.employees (id, name, email, department, salary, hire_date)
SELECT
    g,
    'Employee ' || g,
    'employee' || g || '@shop.example.com',
    (ARRAY['Engineering','Sales','Support','Finance','Operations'])[1 + floor(random() * 5)::int],
    round((40000 + random() * 110000)::numeric, 2),
    date '2018-01-01' + (random() * 2500)::int
FROM generate_series(1, 20) AS g;

INSERT INTO shop.products (id, name, category, price, stock)
SELECT
    g,
    cat || ' item ' || g,
    cat,
    round((5 + random() * 195)::numeric, 2),
    (random() * 500)::int
FROM (
    SELECT g, (ARRAY['Electronics','Books','Clothing','Home','Toys'])[1 + (g % 5)] AS cat
    FROM generate_series(1, 40) AS g
) AS p(g, cat);

INSERT INTO shop.orders (id, customer_id, order_date, status)
SELECT
    g,
    1 + floor(random() * 300)::int,
    date '2024-01-01' + (random() * 650)::int,
    (ARRAY['pending','shipped','delivered','delivered','delivered','cancelled'])[1 + floor(random() * 6)::int]
FROM generate_series(1, 2000) AS g;

-- 1 to 3 line items per order (the same product can repeat within an order)
INSERT INTO shop.order_items (id, order_id, product_id, quantity, unit_price)
WITH picks AS (
    SELECT o.id AS order_id, k,
           1 + floor(random() * 40)::int AS product_id,
           1 + floor(random() * 4)::int  AS quantity
    FROM shop.orders o, generate_series(1, 3) AS k
    WHERE k <= 1 + (o.id % 3)
)
SELECT row_number() OVER (ORDER BY pk.order_id, pk.k), pk.order_id, pk.product_id, pk.quantity, pr.price
FROM picks pk
JOIN shop.products pr ON pr.id = pk.product_id;

UPDATE shop.orders o
SET total_amount = s.total
FROM (
    SELECT order_id, sum(quantity * unit_price) AS total
    FROM shop.order_items
    GROUP BY order_id
) s
WHERE s.order_id = o.id;

ANALYZE;