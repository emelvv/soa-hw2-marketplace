"""Business handlers wired by generated/routes.py. No request or response DTOs are hand-written."""

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID

from psycopg.errors import UniqueViolation

from app import auth, db
from app.errors import fail


CENT = Decimal("0.01")


def value(item):
    return item.value if hasattr(item, "value") else item


def money(item) -> Decimal:
    return Decimal(str(item)).quantize(CENT, rounding=ROUND_HALF_UP)


def health(*, request, principal):
    return {"status": "ok"}


def register(*, request, principal, payload):
    email = str(payload.email).lower()
    try:
        with db.connection() as conn:
            row = conn.execute(
                "INSERT INTO users (email, password_hash, role) VALUES (%s, %s, %s::user_role) RETURNING id, email, role",
                (email, auth.password_hash(payload.password), value(payload.role)),
            ).fetchone()
            return row
    except UniqueViolation:
        fail("VALIDATION_ERROR", "Email is already registered", {"field": "email"})


def login(*, request, principal, payload):
    with db.connection() as conn:
        row = conn.execute(
            "SELECT id, role, password_hash FROM users WHERE email = %s",
            (str(payload.email).lower(),),
        ).fetchone()
    if row is None or not auth.password_matches(payload.password, row["password_hash"]):
        fail("TOKEN_INVALID", "Invalid credentials")
    return auth.token_pair(row["id"], row["role"])


def refresh(*, request, principal, payload):
    claims = auth.decode_token(payload.refresh_token, "refresh")
    with db.connection() as conn:
        row = conn.execute("SELECT id, role FROM users WHERE id = %s", (UUID(claims["sub"]),)).fetchone()
    if not row or row["role"] != claims["role"]:
        fail("REFRESH_TOKEN_INVALID", "User or role no longer valid")
    return auth.token_pair(row["id"], row["role"])


def _product(conn, product_id: UUID, lock: bool = False):
    sql = "SELECT * FROM products WHERE id = %s" + (" FOR UPDATE" if lock else "")
    row = conn.execute(sql, (product_id,)).fetchone()
    if row is None:
        fail("PRODUCT_NOT_FOUND", "Product not found", {"product_id": str(product_id)})
    return row


def _product_owner(principal, row):
    auth.require_role(principal, "SELLER", "ADMIN")
    if principal.role == "SELLER" and row["seller_id"] != principal.id:
        fail("ACCESS_DENIED", "Seller can manage only own products")


def list_products(*, request, principal, page, size, status, category):
    clauses = []
    params = []
    if status is not None:
        clauses.append("status = %s::product_status")
        params.append(value(status))
    if category is not None:
        clauses.append("category = %s")
        params.append(category)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with db.connection() as conn:
        total = conn.execute("SELECT count(*) AS n FROM products" + where, params).fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM products" + where + " ORDER BY created_at, id LIMIT %s OFFSET %s",
            [*params, size, page * size],
        ).fetchall()
    return {"items": rows, "totalElements": total, "page": page, "size": size}


def get_product(*, request, principal, id):
    with db.connection() as conn:
        return _product(conn, id)


def create_product(*, request, principal, payload):
    auth.require_role(principal, "SELLER", "ADMIN")
    seller_id = payload.seller_id if principal.role == "ADMIN" and payload.seller_id else principal.id
    if principal.role == "SELLER" and payload.seller_id and payload.seller_id != principal.id:
        fail("ACCESS_DENIED", "Seller cannot select another owner")
    with db.connection() as conn:
        owner = conn.execute("SELECT id FROM users WHERE id = %s AND role IN ('SELLER', 'ADMIN')", (seller_id,)).fetchone()
        if owner is None:
            fail("VALIDATION_ERROR", "seller_id must identify a seller", {"field": "seller_id"})
        return conn.execute(
            """INSERT INTO products (name, description, price, stock, category, status, seller_id)
               VALUES (%s, %s, %s, %s, %s, %s::product_status, %s) RETURNING *""",
            (payload.name, payload.description, money(payload.price), payload.stock,
             payload.category, value(payload.status), seller_id),
        ).fetchone()


def update_product(*, request, principal, id, payload):
    with db.connection() as conn:
        existing = _product(conn, id, lock=True)
        _product_owner(principal, existing)
        return conn.execute(
            """UPDATE products SET name=%s, description=%s, price=%s, stock=%s,
               category=%s, status=%s::product_status WHERE id=%s RETURNING *""",
            (payload.name, payload.description, money(payload.price), payload.stock,
             payload.category, value(payload.status), id),
        ).fetchone()


def delete_product(*, request, principal, id):
    with db.connection() as conn:
        existing = _product(conn, id, lock=True)
        _product_owner(principal, existing)
        return conn.execute(
            "UPDATE products SET status='ARCHIVED' WHERE id=%s RETURNING *", (id,),
        ).fetchone()


def create_promo_code(*, request, principal, payload):
    auth.require_role(principal, "SELLER", "ADMIN")
    if payload.valid_until <= payload.valid_from:
        fail("VALIDATION_ERROR", "valid_until must be after valid_from", {"field": "valid_until"})
    if value(payload.discount_type) == "PERCENTAGE" and money(payload.discount_value) > Decimal("100"):
        fail("VALIDATION_ERROR", "Percentage cannot exceed 100", {"field": "discount_value"})
    try:
        with db.connection() as conn:
            return conn.execute(
                """INSERT INTO promo_codes
                   (code, discount_type, discount_value, min_order_amount, max_uses, valid_from, valid_until, active)
                   VALUES (%s, %s::discount_type, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (payload.code, value(payload.discount_type), money(payload.discount_value),
                 money(payload.min_order_amount), payload.max_uses, payload.valid_from,
                 payload.valid_until, payload.active),
            ).fetchone()
    except UniqueViolation:
        fail("VALIDATION_ERROR", "Promo code already exists", {"field": "code"})


def _lock_user(conn, user_id: UUID):
    if conn.execute("SELECT 1 FROM users WHERE id=%s FOR UPDATE", (user_id,)).fetchone() is None:
        fail("TOKEN_INVALID", "User no longer exists")


def _check_cooldown(conn, user_id: UUID, kind: str):
    latest = conn.execute(
        """SELECT created_at FROM user_operations WHERE user_id=%s AND operation_type=%s::operation_type
           ORDER BY created_at DESC LIMIT 1""",
        (user_id, kind),
    ).fetchone()
    minutes = int(os.getenv("ORDER_COOLDOWN_MINUTES", "1"))
    if latest and datetime.now(timezone.utc) - latest["created_at"] < timedelta(minutes=minutes):
        fail("ORDER_LIMIT_EXCEEDED", f"Wait {minutes} minute(s) before another {kind.lower()}")


def _items(payload):
    items = [(item.product_id, item.quantity) for item in payload.items]
    if len({product_id for product_id, _ in items}) != len(items):
        fail("VALIDATION_ERROR", "Duplicate product_id in items", {"field": "items"})
    return items


def _locked_products(conn, product_ids):
    rows = {}
    for product_id in sorted(set(product_ids), key=str):
        rows[product_id] = _product(conn, product_id, lock=True)
    return rows


def _check_saleable(items, products, available):
    missing_stock = []
    for product_id, quantity in items:
        product = products[product_id]
        if product["status"] != "ACTIVE":
            fail("PRODUCT_INACTIVE", "Product is not active", {"product_id": str(product_id)})
        stock = available[product_id]
        if stock < quantity:
            missing_stock.append({"product_id": str(product_id), "requested": quantity, "available": stock})
    if missing_stock:
        fail("INSUFFICIENT_STOCK", "Insufficient stock", {"products": missing_stock})


def _gross(items, products):
    return sum((products[product_id]["price"] * quantity for product_id, quantity in items), Decimal("0.00"))


def _promo_discount(promo, gross):
    if promo["discount_type"] == "PERCENTAGE":
        discount = gross * min(promo["discount_value"], Decimal("70")) / Decimal("100")
    else:
        discount = min(promo["discount_value"], gross)
    return money(discount)


def _valid_promo(promo, gross, *, existing=False):
    now = datetime.now(timezone.utc)
    uses_ok = promo["current_uses"] <= promo["max_uses"] if existing else promo["current_uses"] < promo["max_uses"]
    return promo["active"] and uses_ok and promo["valid_from"] <= now <= promo["valid_until"] and gross >= promo["min_order_amount"]


def _order_response(conn, order_id: UUID):
    order = conn.execute("SELECT * FROM orders WHERE id=%s", (order_id,)).fetchone()
    order["items"] = conn.execute(
        "SELECT id, product_id, quantity, price_at_order FROM order_items WHERE order_id=%s ORDER BY id",
        (order_id,),
    ).fetchall()
    return order


def _order(conn, order_id: UUID, principal, lock=False):
    row = conn.execute("SELECT * FROM orders WHERE id=%s" + (" FOR UPDATE" if lock else ""), (order_id,)).fetchone()
    if row is None:
        fail("ORDER_NOT_FOUND", "Order not found", {"order_id": str(order_id)})
    if principal.role != "ADMIN" and row["user_id"] != principal.id:
        fail("ORDER_OWNERSHIP_VIOLATION", "Order belongs to another user")
    return row


def create_order(*, request, principal, payload):
    auth.require_role(principal, "USER", "ADMIN")
    items = _items(payload)
    with db.connection() as conn:
        _lock_user(conn, principal.id)
        _check_cooldown(conn, principal.id, "CREATE_ORDER")
        active = conn.execute(
            "SELECT 1 FROM orders WHERE user_id=%s AND status IN ('CREATED', 'PAYMENT_PENDING') LIMIT 1",
            (principal.id,),
        ).fetchone()
        if active:
            fail("ORDER_HAS_ACTIVE", "User already has an active order")
        products = _locked_products(conn, (product_id for product_id, _ in items))
        _check_saleable(items, products, {pid: row["stock"] for pid, row in products.items()})
        gross = _gross(items, products)
        promo = None
        discount = Decimal("0.00")
        if payload.promo_code:
            promo = conn.execute("SELECT * FROM promo_codes WHERE code=%s FOR UPDATE", (payload.promo_code,)).fetchone()
            if promo is None or not promo["active"] or promo["current_uses"] >= promo["max_uses"] or not (promo["valid_from"] <= datetime.now(timezone.utc) <= promo["valid_until"]):
                fail("PROMO_CODE_INVALID", "Promo code is unavailable")
            if gross < promo["min_order_amount"]:
                fail("PROMO_CODE_MIN_AMOUNT", "Order is below promo minimum", {"min_order_amount": str(promo["min_order_amount"])})
            discount = _promo_discount(promo, gross)
        order = conn.execute(
            """INSERT INTO orders (user_id, status, promo_code_id, total_amount, discount_amount)
               VALUES (%s, 'CREATED', %s, %s, %s) RETURNING id""",
            (principal.id, promo["id"] if promo else None, gross - discount, discount),
        ).fetchone()
        for product_id, quantity in items:
            conn.execute("UPDATE products SET stock=stock-%s WHERE id=%s", (quantity, product_id))
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, price_at_order) VALUES (%s,%s,%s,%s)",
                (order["id"], product_id, quantity, products[product_id]["price"]),
            )
        if promo:
            conn.execute("UPDATE promo_codes SET current_uses=current_uses+1 WHERE id=%s", (promo["id"],))
        conn.execute("INSERT INTO user_operations (user_id, operation_type) VALUES (%s,'CREATE_ORDER')", (principal.id,))
        return _order_response(conn, order["id"])


def get_order(*, request, principal, id):
    auth.require_role(principal, "USER", "ADMIN")
    with db.connection() as conn:
        _order(conn, id, principal)
        return _order_response(conn, id)


def update_order(*, request, principal, id, payload):
    auth.require_role(principal, "USER", "ADMIN")
    items = _items(payload)
    with db.connection() as conn:
        _lock_user(conn, principal.id)
        order = _order(conn, id, principal, lock=True)
        if order["status"] != "CREATED":
            fail("INVALID_STATE_TRANSITION", "Only CREATED orders can be updated")
        _check_cooldown(conn, principal.id, "UPDATE_ORDER")
        old_items = conn.execute("SELECT product_id, quantity FROM order_items WHERE order_id=%s", (id,)).fetchall()
        ids = [product_id for product_id, _ in items] + [old["product_id"] for old in old_items]
        products = _locked_products(conn, ids)
        available = {pid: row["stock"] for pid, row in products.items()}
        for old in old_items:
            available[old["product_id"]] += old["quantity"]
        _check_saleable(items, products, available)
        gross = _gross(items, products)
        promo_id = order["promo_code_id"]
        discount = Decimal("0.00")
        if promo_id:
            promo = conn.execute("SELECT * FROM promo_codes WHERE id=%s FOR UPDATE", (promo_id,)).fetchone()
            if promo and _valid_promo(promo, gross, existing=True):
                discount = _promo_discount(promo, gross)
            else:
                conn.execute("UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s", (promo_id,))
                promo_id = None
        for pid, stock in available.items():
            quantity = next((qty for item_pid, qty in items if item_pid == pid), 0)
            conn.execute("UPDATE products SET stock=%s WHERE id=%s", (stock - quantity, pid))
        conn.execute("DELETE FROM order_items WHERE order_id=%s", (id,))
        for product_id, quantity in items:
            conn.execute(
                "INSERT INTO order_items (order_id, product_id, quantity, price_at_order) VALUES (%s,%s,%s,%s)",
                (id, product_id, quantity, products[product_id]["price"]),
            )
        conn.execute(
            "UPDATE orders SET promo_code_id=%s, total_amount=%s, discount_amount=%s WHERE id=%s",
            (promo_id, gross - discount, discount, id),
        )
        conn.execute("INSERT INTO user_operations (user_id, operation_type) VALUES (%s,'UPDATE_ORDER')", (principal.id,))
        return _order_response(conn, id)


def cancel_order(*, request, principal, id):
    auth.require_role(principal, "USER", "ADMIN")
    with db.connection() as conn:
        order = _order(conn, id, principal, lock=True)
        if order["status"] not in {"CREATED", "PAYMENT_PENDING"}:
            fail("INVALID_STATE_TRANSITION", "Order cannot be canceled from this state")
        items = conn.execute("SELECT product_id, quantity FROM order_items WHERE order_id=%s", (id,)).fetchall()
        _locked_products(conn, (item["product_id"] for item in items))
        for item in items:
            conn.execute("UPDATE products SET stock=stock+%s WHERE id=%s", (item["quantity"], item["product_id"]))
        if order["promo_code_id"]:
            conn.execute("UPDATE promo_codes SET current_uses=current_uses-1 WHERE id=%s", (order["promo_code_id"],))
        conn.execute("UPDATE orders SET status='CANCELED' WHERE id=%s", (id,))
        return _order_response(conn, id)


def transition_order(*, request, principal, id, payload):
    auth.require_role(principal, "ADMIN")
    transitions = {
        "CREATED": "PAYMENT_PENDING",
        "PAYMENT_PENDING": "PAID",
        "PAID": "SHIPPED",
        "SHIPPED": "COMPLETED",
    }
    with db.connection() as conn:
        order = _order(conn, id, principal, lock=True)
        target = value(payload.status)
        if transitions.get(order["status"]) != target:
            fail("INVALID_STATE_TRANSITION", "Unsupported order transition", {"from": order["status"], "to": target})
        conn.execute("UPDATE orders SET status=%s::order_status WHERE id=%s", (target, id))
        return _order_response(conn, id)
