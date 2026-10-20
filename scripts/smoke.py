"""Run an end-to-end defense rehearsal against a running compose stack."""

import json
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


BASE = os.getenv("API_URL", "http://127.0.0.1:8001")


def call(method, path, data=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = Request(BASE + path, data=json.dumps(data).encode() if data is not None else None,
                      headers=headers, method=method)
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as exc:
        response = exc
    with response:
        body = json.loads(response.read())
        assert response.headers.get("X-Request-Id"), "missing request ID"
        return response.status, body


def expect(method, path, expected_status, data=None, token=None, error=None):
    status, body = call(method, path, data, token)
    assert status == expected_status, (method, path, expected_status, status, body)
    if error:
        assert body["error_code"] == error, body
    return body


def main():
    suffix = uuid4().hex[:10]
    seller_email = f"seller-{suffix}@example.com"
    second_seller_email = f"seller-2-{suffix}@example.com"
    buyer_email = f"buyer-{suffix}@example.com"
    other_email = f"other-{suffix}@example.com"
    for email, role in ((seller_email, "SELLER"), (second_seller_email, "SELLER"),
                        (buyer_email, "USER"), (other_email, "USER")):
        expect("POST", "/auth/register", 201,
               {"email": email, "password": "demo-password-123", "role": role})
    def login(email, password="demo-password-123"):
        return expect("POST", "/auth/login", 200, {"email": email, "password": password})
    seller = login(seller_email)["access_token"]
    second_seller = login(second_seller_email)["access_token"]
    buyer_pair = login(buyer_email)
    buyer = buyer_pair["access_token"]
    other = login(other_email)["access_token"]
    admin = login("admin@example.com", "admin-demo-password")["access_token"]
    expect("POST", "/products", 403,
           {"name": "Forbidden", "price": 5, "stock": 1, "category": "books", "status": "ACTIVE"}, buyer,
           "ACCESS_DENIED")
    expect("POST", "/products", 400,
           {"name": "", "price": 0, "stock": -1, "category": "", "status": "ACTIVE"}, seller,
           "VALIDATION_ERROR")
    invalid_price = expect("POST", "/products", 400,
                           {"name": "Invalid price", "price": 0, "stock": 1,
                            "category": "books", "status": "ACTIVE"}, seller, "VALIDATION_ERROR")
    assert any("price" in item["field"] for item in invalid_price["details"]["violations"])
    product = expect("POST", "/products", 201,
                     {"name": "Book", "description": "Demo book", "price": 100.00,
                      "stock": 10, "category": "books", "status": "ACTIVE"}, seller)
    product_id = product["id"]
    assert expect("GET", "/products?category=books&status=ACTIVE&page=0&size=20", 200, token=buyer)["totalElements"] >= 1
    expect("PUT", f"/products/{product_id}", 403,
           {"name": "Stolen", "price": 100, "stock": 10, "category": "books", "status": "ACTIVE"},
           other, "ACCESS_DENIED")
    expect("PUT", f"/products/{product_id}", 403,
           {"name": "Stolen", "price": 100, "stock": 10, "category": "books", "status": "ACTIVE"},
           second_seller, "ACCESS_DENIED")
    expect("POST", "/orders", 403,
           {"items": [{"product_id": product_id, "quantity": 1}]}, seller, "ACCESS_DENIED")
    expect("POST", "/orders", 409,
           {"items": [{"product_id": product_id, "quantity": 11}]}, buyer, "INSUFFICIENT_STOCK")
    expect("POST", "/orders", 404,
           {"items": [{"product_id": str(uuid4()), "quantity": 1}]}, buyer, "PRODUCT_NOT_FOUND")
    inactive = expect("POST", "/products", 201,
                      {"name": "Draft", "price": 9, "stock": 2, "category": "books", "status": "INACTIVE"}, seller)
    expect("POST", "/orders", 409,
           {"items": [{"product_id": inactive["id"], "quantity": 1}]}, buyer, "PRODUCT_INACTIVE")
    now = datetime.now(timezone.utc)
    promo = expect("POST", "/promo-codes", 201,
                   {"code": "SAVE_" + suffix.upper(), "discount_type": "PERCENTAGE",
                    "discount_value": 10, "min_order_amount": 50, "max_uses": 2,
                    "valid_from": (now - timedelta(days=1)).isoformat(),
                    "valid_until": (now + timedelta(days=1)).isoformat()}, seller)
    high_min = expect("POST", "/promo-codes", 201,
                      {"code": "MIN_" + suffix.upper(), "discount_type": "FIXED_AMOUNT",
                       "discount_value": 10, "min_order_amount": 1000, "max_uses": 2,
                       "valid_from": (now - timedelta(days=1)).isoformat(),
                       "valid_until": (now + timedelta(days=1)).isoformat()}, seller)
    expect("POST", "/orders", 422,
           {"items": [{"product_id": product_id, "quantity": 1}], "promo_code": high_min["code"]},
           buyer, "PROMO_CODE_MIN_AMOUNT")
    created = expect("POST", "/orders", 201,
                     {"items": [{"product_id": product_id, "quantity": 2}], "promo_code": promo["code"]}, buyer)
    order_id = created["id"]
    assert Decimal(created["total_amount"]) == 180 and Decimal(created["discount_amount"]) == 20, created
    assert Decimal(created["items"][0]["price_at_order"]) == 100
    assert expect("GET", f"/products/{product_id}", 200, token=buyer)["stock"] == 8
    assert Decimal(expect("GET", f"/orders/{order_id}", 200, token=buyer)["items"][0]["price_at_order"]) == 100
    expect("GET", f"/orders/{order_id}", 403, token=other, error="ORDER_OWNERSHIP_VIOLATION")
    expect("POST", "/orders", 429,
           {"items": [{"product_id": product_id, "quantity": 1}]}, buyer,
           "ORDER_LIMIT_EXCEEDED")
    updated = expect("PUT", f"/orders/{order_id}", 200,
                     {"items": [{"product_id": product_id, "quantity": 3}]}, buyer)
    assert Decimal(updated["total_amount"]) == 270 and updated["items"][0]["quantity"] == 3, updated
    expect("PUT", f"/orders/{order_id}", 429,
           {"items": [{"product_id": product_id, "quantity": 1}]}, buyer, "ORDER_LIMIT_EXCEEDED")
    assert expect("GET", f"/products/{product_id}", 200, token=buyer)["stock"] == 7
    canceled = expect("POST", f"/orders/{order_id}/cancel", 200, token=buyer)
    assert canceled["status"] == "CANCELED"
    assert expect("GET", f"/products/{product_id}", 200, token=buyer)["stock"] == 10
    assert expect("GET", f"/products/{inactive['id']}", 200, token=buyer)["status"] == "INACTIVE"
    archived = expect("DELETE", f"/products/{inactive['id']}", 200, token=seller)
    assert archived["status"] == "ARCHIVED"
    assert expect("GET", f"/products/{inactive['id']}", 200, token=buyer)["status"] == "ARCHIVED"
    expect("POST", f"/orders/{order_id}/cancel", 409, token=buyer, error="INVALID_STATE_TRANSITION")
    expect("POST", "/auth/refresh", 200, {"refresh_token": buyer_pair["refresh_token"]})
    expect("GET", "/products", 401, error="TOKEN_INVALID")
    admin_order = expect("POST", "/orders", 201,
                         {"items": [{"product_id": product_id, "quantity": 1}]}, other)
    for status in ("PAYMENT_PENDING", "PAID", "SHIPPED", "COMPLETED"):
        admin_order = expect("POST", f"/orders/{admin_order['id']}/transition", 200,
                             {"status": status}, admin)
    assert admin_order["status"] == "COMPLETED"
    expect("POST", f"/orders/{admin_order['id']}/cancel", 409, token=other,
           error="INVALID_STATE_TRANSITION")
    print(json.dumps({"result": "PASS", "product_id": product_id,
                      "canceled_order_id": order_id, "completed_order_id": admin_order["id"],
                      "checks": ["auth", "roles", "validation", "CRUD", "soft delete",
                                 "pagination", "promo", "stock", "order update", "cancel",
                                 "rate limit", "state machine"]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
