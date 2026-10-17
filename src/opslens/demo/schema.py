"""Business schema. Monetary values use exact decimal amounts in CNY."""

SCHEMA_SQL = """
CREATE TABLE channels (
 channel_id VARCHAR PRIMARY KEY, channel_name VARCHAR NOT NULL,
 channel_type VARCHAR NOT NULL, is_paid BOOLEAN NOT NULL);
CREATE TABLE products (
 product_id VARCHAR PRIMARY KEY, sku VARCHAR UNIQUE NOT NULL,
 product_name VARCHAR NOT NULL, category VARCHAR NOT NULL, brand VARCHAR NOT NULL,
 unit_cost DECIMAL(12,2) NOT NULL CHECK(unit_cost >= 0),
 list_price DECIMAL(12,2) NOT NULL CHECK(list_price >= 0), is_active BOOLEAN NOT NULL);
CREATE TABLE orders (
 order_id VARCHAR PRIMARY KEY, customer_id VARCHAR NOT NULL,
 channel_id VARCHAR NOT NULL REFERENCES channels(channel_id), region VARCHAR NOT NULL,
 order_status VARCHAR NOT NULL CHECK(order_status IN ('paid','completed','cancelled')),
 created_at TIMESTAMP NOT NULL, paid_at TIMESTAMP, cancelled_at TIMESTAMP,
 currency VARCHAR NOT NULL CHECK(currency = 'CNY'),
 CHECK((order_status = 'cancelled' AND paid_at IS NULL AND cancelled_at >= created_at)
 OR (order_status IN ('paid','completed') AND paid_at >= created_at AND cancelled_at IS NULL)),
 CHECK((order_status = 'cancelled' AND cancelled_at IS NOT NULL)
 OR (order_status IN ('paid','completed') AND paid_at IS NOT NULL)));
CREATE TABLE order_items (
 order_item_id VARCHAR PRIMARY KEY, order_id VARCHAR NOT NULL REFERENCES orders(order_id),
 product_id VARCHAR NOT NULL REFERENCES products(product_id),
 quantity INTEGER NOT NULL CHECK(quantity > 0),
 unit_price DECIMAL(12,2) NOT NULL CHECK(unit_price >= 0),
 discount_amount DECIMAL(12,2) NOT NULL CHECK(discount_amount >= 0),
 CHECK(discount_amount <= quantity * unit_price));
CREATE TABLE refunds (
 refund_id VARCHAR PRIMARY KEY, order_id VARCHAR NOT NULL REFERENCES orders(order_id),
 order_item_id VARCHAR REFERENCES order_items(order_item_id),
 refund_amount DECIMAL(12,2) NOT NULL CHECK(refund_amount >= 0),
 refund_reason VARCHAR NOT NULL,
 refund_status VARCHAR NOT NULL CHECK(refund_status IN ('approved','rejected','pending')),
 created_at TIMESTAMP NOT NULL);
CREATE TABLE _demo_metadata (key VARCHAR PRIMARY KEY, value VARCHAR NOT NULL);
"""
