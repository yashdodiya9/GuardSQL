CREATE SCHEMA shop;

CREATE TABLE shop.customers (
    id            integer PRIMARY KEY,
    first_name    text NOT NULL,
    last_name     text NOT NULL,
    email         text NOT NULL UNIQUE,   -- PII
    phone         text,                   -- PII
    date_of_birth date,                   -- PII
    country       text NOT NULL,
    signup_date   date NOT NULL
);

CREATE TABLE shop.employees (
    id         integer PRIMARY KEY,
    name       text NOT NULL,
    email      text NOT NULL UNIQUE,      -- PII
    department text NOT NULL,
    salary     numeric(10,2) NOT NULL,    -- sensitive
    hire_date  date NOT NULL
);

CREATE TABLE shop.products (
    id       integer PRIMARY KEY,
    name     text NOT NULL,
    category text NOT NULL,
    price    numeric(10,2) NOT NULL CHECK (price > 0),
    stock    integer NOT NULL CHECK (stock >= 0)
);

CREATE TABLE shop.orders (
    id           integer PRIMARY KEY,
    customer_id  integer NOT NULL REFERENCES shop.customers (id),
    order_date   date NOT NULL,
    status       text NOT NULL CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled')),
    total_amount numeric(12,2) NOT NULL DEFAULT 0
);

CREATE TABLE shop.order_items (
    id         integer PRIMARY KEY,
    order_id   integer NOT NULL REFERENCES shop.orders (id),
    product_id integer NOT NULL REFERENCES shop.products (id),
    quantity   integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(10,2) NOT NULL
);

CREATE INDEX ON shop.orders (customer_id);
CREATE INDEX ON shop.orders (order_date);
CREATE INDEX ON shop.order_items (order_id);
CREATE INDEX ON shop.order_items (product_id);

-- Descriptions are reused later to enrich the LLM prompt.
COMMENT ON TABLE shop.orders IS 'One row per customer order. total_amount = sum of its items.';
COMMENT ON COLUMN shop.orders.status IS 'pending, shipped, delivered, or cancelled';
COMMENT ON TABLE shop.order_items IS 'Line items: which products were in which order.';
COMMENT ON COLUMN shop.order_items.unit_price IS 'Price at time of purchase';
COMMENT ON TABLE shop.products IS 'Product catalog with current price and stock.';