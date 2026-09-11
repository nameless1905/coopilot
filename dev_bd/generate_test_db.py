"""
Генератор синтетической тестовой БД (PostgreSQL).

Схема: customers / products / orders / order_items / events —
универсальный "магазинный" домен, достаточно богатый, чтобы тестировать
джойны, агрегации, оконные функции и CTE (то, ради чего это всё пишется —
проверить query_qc_data / is_safe_select тул из Open WebUI).

Использование:
    pip install psycopg2-binary faker --break-system-packages
    python generate_test_db.py

По умолчанию подключается как суперпользователь admin (см. docker-compose.yml),
создаёт схему, наполняет данными и заводит отдельного readonly_user
с правами только на SELECT — тем самым DSN, который вписывается в Valves
Tool-а query_qc_data.
"""

import random
import datetime
import psycopg2
from psycopg2.extras import execute_values
from faker import Faker

# --- конфигурация ---
ADMIN_DSN = "postgresql://admin:admin_pass@localhost:5433/testdb"
READONLY_USER = "readonly_user"
READONLY_PASSWORD = "readonly_pass"

N_CUSTOMERS = 500
N_PRODUCTS = 60
N_ORDERS = 2000
N_EVENTS = 3000
SEED = 42

random.seed(SEED)
fake = Faker("ru_RU")
Faker.seed(SEED)

CATEGORIES = ["Электроника", "Одежда", "Дом и сад", "Спорт", "Книги", "Продукты"]
ORDER_STATUSES = ["completed", "pending", "cancelled", "refunded"]
EVENT_TYPES = ["login", "page_view", "add_to_cart", "search", "support_ticket"]

DDL = """
DROP TABLE IF EXISTS events, order_items, orders, products, customers CASCADE;

CREATE TABLE customers (
    id SERIAL PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    city TEXT NOT NULL,
    signup_date DATE NOT NULL
);

CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    price NUMERIC(10, 2) NOT NULL
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id),
    order_date TIMESTAMP NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE order_items (
    id SERIAL PRIMARY KEY,
    order_id INT NOT NULL REFERENCES orders(id),
    product_id INT NOT NULL REFERENCES products(id),
    quantity INT NOT NULL,
    unit_price NUMERIC(10, 2) NOT NULL
);

CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    customer_id INT REFERENCES customers(id),
    event_type TEXT NOT NULL,
    event_time TIMESTAMP NOT NULL,
    metadata JSONB
);

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_order_items_order ON order_items(order_id);
CREATE INDEX idx_events_customer ON events(customer_id);
CREATE INDEX idx_events_time ON events(event_time);
"""


def random_datetime_within(days_back: int) -> datetime.datetime:
    start = datetime.datetime.now() - datetime.timedelta(days=days_back)
    delta = datetime.timedelta(
        seconds=random.randint(0, days_back * 24 * 3600)
    )
    return start + delta


def main():
    conn = psycopg2.connect(ADMIN_DSN)
    conn.autocommit = True
    cur = conn.cursor()

    print("Создаю схему...")
    cur.execute(DDL)

    print(f"Генерирую {N_CUSTOMERS} клиентов...")
    customers = [
        (
            fake.name(),
            fake.unique.email(),
            fake.city(),
            fake.date_between(start_date="-3y", end_date="-1M"),
        )
        for _ in range(N_CUSTOMERS)
    ]
    customer_ids = [
        r[0]
        for r in execute_values(
            cur,
            "INSERT INTO customers (full_name, email, city, signup_date) VALUES %s RETURNING id",
            customers,
            fetch=True,
        )
    ]

    print(f"Генерирую {N_PRODUCTS} товаров...")
    products = [
        (
            fake.catch_phrase(),
            random.choice(CATEGORIES),
            round(random.uniform(200, 50000), 2),
        )
        for _ in range(N_PRODUCTS)
    ]
    product_rows = execute_values(
        cur,
        "INSERT INTO products (name, category, price) VALUES %s RETURNING id, price",
        products,
        fetch=True,
    )
    product_ids = [r[0] for r in product_rows]
    price_by_product = {r[0]: r[1] for r in product_rows}

    print(f"Генерирую {N_ORDERS} заказов и позиции к ним...")
    orders = []
    for _ in range(N_ORDERS):
        cust_id = random.choice(customer_ids)
        # немного перекос по статусам, чтобы отчёты были осмысленными
        status = random.choices(ORDER_STATUSES, weights=[70, 15, 10, 5])[0]
        orders.append((cust_id, random_datetime_within(365), status))
    order_ids = [
        r[0]
        for r in execute_values(
            cur,
            "INSERT INTO orders (customer_id, order_date, status) VALUES %s RETURNING id",
            orders,
            fetch=True,
        )
    ]

    order_items = []
    for order_id in order_ids:
        for _ in range(random.randint(1, 4)):
            prod_id = random.choice(product_ids)
            order_items.append(
                (order_id, prod_id, random.randint(1, 5), price_by_product[prod_id])
            )
    execute_values(
        cur,
        "INSERT INTO order_items (order_id, product_id, quantity, unit_price) VALUES %s",
        order_items,
    )

    print(f"Генерирую {N_EVENTS} событий активности...")
    events = []
    for _ in range(N_EVENTS):
        cust_id = random.choice(customer_ids)
        etype = random.choice(EVENT_TYPES)
        events.append(
            (
                cust_id,
                etype,
                random_datetime_within(90),
                psycopg2.extras.Json({"source": random.choice(["web", "mobile", "api"])}),
            )
        )
    execute_values(
        cur,
        "INSERT INTO events (customer_id, event_type, event_time, metadata) VALUES %s",
        events,
    )

    print("Завожу read-only пользователя...")
    cur.execute(
        f"SELECT 1 FROM pg_roles WHERE rolname = '{READONLY_USER}';"
    )
    if cur.fetchone():
        # роль переживает DROP TABLE ... CASCADE из DDL выше, поэтому перед
        # повторным DROP ROLE нужно сначала снять все её права/владения —
        # иначе Postgres откажет с DependentObjectsStillExist.
        cur.execute(f"DROP OWNED BY {READONLY_USER};")
        cur.execute(f"DROP ROLE {READONLY_USER};")
    cur.execute(
        f"CREATE ROLE {READONLY_USER} LOGIN PASSWORD '{READONLY_PASSWORD}';"
    )
    cur.execute(f"GRANT CONNECT ON DATABASE testdb TO {READONLY_USER};")
    cur.execute(f"GRANT USAGE ON SCHEMA public TO {READONLY_USER};")
    cur.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO {READONLY_USER};")
    cur.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO {READONLY_USER};"
    )

    cur.close()
    conn.close()

    print("\nГотово.")
    print(f"Admin DSN:    {ADMIN_DSN}")
    print(
        f"Readonly DSN: postgresql://{READONLY_USER}:{READONLY_PASSWORD}@localhost:5432/testdb"
    )
    print("\nЭтот readonly DSN и подставляйте в Valves.DB_DSN тула query_qc_data.")


if __name__ == "__main__":
    main()
