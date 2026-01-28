import sqlite3
from pathlib import Path
from typing import Optional, List, Tuple

DB_PATH = Path("shop.db")


def connect():
    con = sqlite3.connect(DB_PATH)
    return con


def init_db():
    with connect() as con:
        cur = con.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS products(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            price_cents INTEGER NOT NULL,
            stock INTEGER NOT NULL DEFAULT 0
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS cart(
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            PRIMARY KEY(user_id, product_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS orders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT,
            phone TEXT,
            address TEXT,
            pay_method TEXT,
            total_cents INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items(
            order_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            price_cents INTEGER NOT NULL,
            qty INTEGER NOT NULL
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """)

        # Миграции (добавляем новые колонки, если их ещё нет)
        def add_col(table: str, coldef: str):
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            except sqlite3.OperationalError:
                pass

        add_col("products", "photo_file_id TEXT")
        add_col("orders", "status TEXT DEFAULT 'new'")          # new / accepted / declined
        add_col("orders", "tg_username TEXT")
        add_col("orders", "tg_name TEXT")

        # Для таймера корзины: время последней активности
        add_col("cart", "updated_at TEXT")
        # Заполним пустые значения, если колонка появилась
        cur.execute("UPDATE cart SET updated_at = COALESCE(updated_at, datetime('now'))")

        con.commit()


# ---------- Products ----------
def add_product(category, title, price_cents, stock, photo_file_id=None):
    with connect() as con:
        con.execute(
            "INSERT INTO products(category,title,price_cents,stock,photo_file_id) VALUES(?,?,?,?,?)",
            (category, title, price_cents, stock, photo_file_id),
        )
        con.commit()


def list_categories():
    with connect() as con:
        rows = con.execute("SELECT DISTINCT category FROM products ORDER BY category").fetchall()
        return [r[0] for r in rows]


def list_products(category):
    with connect() as con:
        return con.execute(
            "SELECT id, title, price_cents, stock FROM products WHERE category=? ORDER BY id DESC",
            (category,),
        ).fetchall()


def get_product(pid):
    with connect() as con:
        return con.execute(
            "SELECT id, category, title, price_cents, stock, photo_file_id FROM products WHERE id=?",
            (pid,),
        ).fetchone()


# ---------- Cart ----------
def cart_items(user_id):
    with connect() as con:
        return con.execute("""
        SELECT c.product_id, p.title, p.price_cents, c.qty
        FROM cart c
        JOIN products p ON p.id=c.product_id
        WHERE c.user_id=?
        ORDER BY p.title
        """, (user_id,)).fetchall()


def cart_touch(cur, user_id: int):
    cur.execute("UPDATE cart SET updated_at=datetime('now') WHERE user_id=?", (user_id,))


def cart_add_reserve(user_id: int, pid: int, qty: int) -> int:
    """
    РЕЗЕРВ: уменьшает склад и кладёт в корзину.
    """
    with connect() as con:
        cur = con.cursor()
        cur.execute("SELECT stock FROM products WHERE id=?", (pid,))
        row = cur.fetchone()
        if not row:
            return 0
        stock = int(row[0])
        if stock <= 0:
            return 0

        add_qty = min(qty, stock)

        # уменьшаем склад
        cur.execute("UPDATE products SET stock = stock - ? WHERE id=?", (add_qty, pid))

        # upsert корзины
        cur.execute("SELECT qty FROM cart WHERE user_id=? AND product_id=?", (user_id, pid))
        r = cur.fetchone()
        if r:
            cur.execute(
                "UPDATE cart SET qty=?, updated_at=datetime('now') WHERE user_id=? AND product_id=?",
                (int(r[0]) + add_qty, user_id, pid),
            )
        else:
            cur.execute(
                "INSERT INTO cart(user_id, product_id, qty, updated_at) VALUES(?,?,?,datetime('now'))",
                (user_id, pid, add_qty),
            )

        # touch остальные позиции корзины пользователя
        cart_touch(cur, user_id)

        con.commit()
        return add_qty


def cart_remove_return(user_id: int, pid: int, qty: int) -> int:
    """
    Удаляет qty из корзины и ВОЗВРАЩАЕТ на склад.
    """
    with connect() as con:
        cur = con.cursor()
        cur.execute("SELECT qty FROM cart WHERE user_id=? AND product_id=?", (user_id, pid))
        row = cur.fetchone()
        if not row:
            return 0
        have = int(row[0])
        rem = min(qty, have)

        new_qty = have - rem
        if new_qty <= 0:
            cur.execute("DELETE FROM cart WHERE user_id=? AND product_id=?", (user_id, pid))
        else:
            cur.execute(
                "UPDATE cart SET qty=?, updated_at=datetime('now') WHERE user_id=? AND product_id=?",
                (new_qty, user_id, pid),
            )

        # вернуть на склад
        cur.execute("UPDATE products SET stock = stock + ? WHERE id=?", (rem, pid))

        cart_touch(cur, user_id)
        con.commit()
        return rem


def cart_clear_return(user_id: int):
    """
    Очищает корзину и возвращает всё на склад.
    """
    with connect() as con:
        cur = con.cursor()
        rows = cur.execute("SELECT product_id, qty FROM cart WHERE user_id=?", (user_id,)).fetchall()
        for pid, qty in rows:
            cur.execute("UPDATE products SET stock = stock + ? WHERE id=?", (int(qty), int(pid)))
        cur.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
        con.commit()


# --- Таймер корзины ---
def stale_cart_users(minutes: int = 30) -> List[int]:
    """
    Возвращает список user_id, у которых корзина не трогалась minutes минут.
    """
    with connect() as con:
        rows = con.execute("""
            SELECT DISTINCT user_id
            FROM cart
            WHERE updated_at <= datetime('now', ?)
        """, (f"-{int(minutes)} minutes",)).fetchall()
        return [int(r[0]) for r in rows]


def release_cart(user_id: int):
    """
    Возвращает товары из корзины на склад и очищает корзину.
    """
    with connect() as con:
        cur = con.cursor()
        rows = cur.execute("SELECT product_id, qty FROM cart WHERE user_id=?", (user_id,)).fetchall()
        for pid, qty in rows:
            cur.execute("UPDATE products SET stock = stock + ? WHERE id=?", (int(qty), int(pid)))
        cur.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
        con.commit()


# ---------- Orders ----------
def create_order(user_id, name, phone, address, pay_method, tg_username=None, tg_name=None):
    import datetime

    items = cart_items(user_id)
    if not items:
        return None

    total = 0
    for _pid, _title, price, qty in items:
        total += int(price) * int(qty)

    with connect() as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO orders(user_id,name,phone,address,pay_method,total_cents,created_at,status,tg_username,tg_name) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (user_id, name, phone, address, pay_method, total, datetime.datetime.utcnow().isoformat(),
             "new", tg_username, tg_name),
        )
        order_id = cur.lastrowid

        for pid, title, price, qty in items:
            cur.execute(
                "INSERT INTO order_items(order_id,product_id,title,price_cents,qty) VALUES(?,?,?,?,?)",
                (order_id, pid, title, price, qty),
            )

        # корзину просто удаляем (товар уже зарезервирован и остаётся в заказе)
        cur.execute("DELETE FROM cart WHERE user_id=?", (user_id,))
        con.commit()

    return order_id, total, items


def get_order(order_id: int):
    with connect() as con:
        return con.execute(
            "SELECT id, user_id, status, tg_username, tg_name, name, phone, address, pay_method, total_cents "
            "FROM orders WHERE id=?",
            (order_id,),
        ).fetchone()


def set_order_status(order_id: int, status: str):
    with connect() as con:
        con.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
        con.commit()


def restock_order(order_id: int):
    """
    Возвращает товары заказа обратно на склад (используем при отклонении).
    """
    with connect() as con:
        cur = con.cursor()
        rows = cur.execute("SELECT product_id, qty FROM order_items WHERE order_id=?", (order_id,)).fetchall()
        for pid, qty in rows:
            cur.execute("UPDATE products SET stock = stock + ? WHERE id=?", (int(qty), int(pid)))
        con.commit()


# ---------- Settings ----------
def set_setting(key: str, value: str):
    with connect() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()


def get_setting(key: str) -> Optional[str]:
    with connect() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def list_orders(status: str = "new", limit: int = 20):
    """
    Возвращает последние заказы по статусу.
    Поля: id, user_id, status, tg_username, tg_name, name, phone, address, pay_method, total_cents, created_at
    """
    with connect() as con:
        return con.execute(
            """
            SELECT id, user_id, status, tg_username, tg_name, name, phone, address, pay_method, total_cents, created_at
            FROM orders
            WHERE status=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (status, int(limit)),
        ).fetchall()


def order_items_full(order_id: int):
    """Возвращает items заказа: product_id, title, price_cents, qty"""
    with connect() as con:
        return con.execute(
            """
            SELECT product_id, title, price_cents, qty
            FROM order_items
            WHERE order_id=?
            ORDER BY title
            """,
            (int(order_id),),
        ).fetchall()


def recalc_order_total(order_id: int):
    with connect() as con:
        cur = con.cursor()
        row = cur.execute(
            "SELECT COALESCE(SUM(price_cents * qty), 0) FROM order_items WHERE order_id=?",
            (int(order_id),),
        ).fetchone()
        total = int(row[0] or 0)
        cur.execute("UPDATE orders SET total_cents=? WHERE id=?", (total, int(order_id)))
        con.commit()
        return total


def order_item_delta(order_id: int, product_id: int, delta: int):
    """
    Меняет количество товара в заказе на delta.
    delta = +1: забираем со склада (если есть)
    delta = -1: возвращаем на склад
    Если qty станет 0 — позиция удаляется.
    Возвращает (ok: bool, new_qty: int, new_total: int, reason: str)
    """
    with connect() as con:
        cur = con.cursor()

        # Проверим текущую qty
        row = cur.execute(
            "SELECT qty, price_cents FROM order_items WHERE order_id=? AND product_id=?",
            (int(order_id), int(product_id)),
        ).fetchone()
        if not row:
            return (False, 0, recalc_order_total(order_id), "item_not_found")

        qty = int(row[0])
        price = int(row[1])

        if delta > 0:
            # Нужно взять со склада
            st = cur.execute("SELECT stock FROM products WHERE id=?", (int(product_id),)).fetchone()
            if not st:
                return (False, qty, recalc_order_total(order_id), "product_not_found")
            stock = int(st[0])
            if stock < delta:
                return (False, qty, recalc_order_total(order_id), "no_stock")

            cur.execute("UPDATE products SET stock = stock - ? WHERE id=?", (int(delta), int(product_id)))
            new_qty = qty + int(delta)
            cur.execute(
                "UPDATE order_items SET qty=? WHERE order_id=? AND product_id=?",
                (new_qty, int(order_id), int(product_id)),
            )

        else:
            # Возврат на склад
            delta_abs = abs(int(delta))
            real = min(delta_abs, qty)
            new_qty = qty - real
            cur.execute("UPDATE products SET stock = stock + ? WHERE id=?", (real, int(product_id)))

            if new_qty <= 0:
                cur.execute("DELETE FROM order_items WHERE order_id=? AND product_id=?", (int(order_id), int(product_id)))
            else:
                cur.execute(
                    "UPDATE order_items SET qty=? WHERE order_id=? AND product_id=?",
                    (new_qty, int(order_id), int(product_id)),
                )

        con.commit()
        new_total = recalc_order_total(order_id)
        return (True, new_qty, new_total, "ok")


def cancel_order(order_id: int):
    """Отменить заказ: вернуть товары на склад, статус -> cancelled"""
    restock_order(int(order_id))
    set_order_status(int(order_id), "cancelled")
    return True


def products_all():
    """Все товары: id, category, title, price_cents, stock"""
    with connect() as con:
        return con.execute(
            "SELECT id, category, title, price_cents, stock FROM products ORDER BY category, title"
        ).fetchall()


def products_by_category(category: str):
    with connect() as con:
        return con.execute(
            "SELECT id, title, price_cents, stock FROM products WHERE category=? ORDER BY title",
            (category,),
        ).fetchall()


def product_set_stock(pid: int, stock: int) -> int:
    stock = max(0, int(stock))
    with connect() as con:
        con.execute("UPDATE products SET stock=? WHERE id=?", (stock, int(pid)))
        con.commit()
    return stock


def product_stock_delta(pid: int, delta: int) -> int:
    with connect() as con:
        cur = con.cursor()
        row = cur.execute("SELECT stock FROM products WHERE id=?", (int(pid),)).fetchone()
        if not row:
            return -1
        new_stock = max(0, int(row[0]) + int(delta))
        cur.execute("UPDATE products SET stock=? WHERE id=?", (new_stock, int(pid)))
        con.commit()
        return new_stock


def product_set_price(pid: int, price_cents: int) -> int:
    price_cents = max(0, int(price_cents))
    with connect() as con:
        con.execute("UPDATE products SET price_cents=? WHERE id=?", (price_cents, int(pid)))
        con.commit()
    return price_cents


def product_delete(pid: int) -> bool:
    with connect() as con:
        con.execute("DELETE FROM products WHERE id=?", (int(pid),))
        con.execute("DELETE FROM cart WHERE product_id=?", (int(pid),))  # на всякий случай
        con.commit()
    return True
