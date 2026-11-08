import sqlite3
import uuid
from datetime import datetime
from typing import List
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DB_FILE = "pos.db"
TAX_RATE = 0.0825  # 8.25% Texas State + Municipal Tax

app = FastAPI(title="Retail POS Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                emp_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                pin TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sku TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price_cents INTEGER NOT NULL,
                cost_cents INTEGER NOT NULL,
                taxable INTEGER DEFAULT 1,
                stock INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                receipt_number TEXT UNIQUE NOT NULL,
                subtotal_cents INTEGER NOT NULL,
                tax_cents INTEGER NOT NULL,
                total_cents INTEGER NOT NULL,
                cashier_id TEXT NOT NULL,
                status TEXT DEFAULT 'COMPLETED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS transaction_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price_cents INTEGER NOT NULL,
                line_total_cents INTEGER NOT NULL,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id),
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL,
                payment_type TEXT NOT NULL,
                tendered_cents INTEGER NOT NULL,
                change_due_cents INTEGER DEFAULT 0,
                FOREIGN KEY(transaction_id) REFERENCES transactions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
        """)

        cursor = conn.cursor()

        # Ensure 'status' column exists if upgrading from an older pos.db
        cols = [row["name"] for row in cursor.execute("PRAGMA table_info(transactions)").fetchall()]
        if "status" not in cols:
            cursor.execute("ALTER TABLE transactions ADD COLUMN status TEXT DEFAULT 'COMPLETED'")

        # Seed default operators if empty
        cursor.execute("SELECT COUNT(*) FROM users")
        if cursor.fetchone()[0] == 0:
            sample_users = [
                ("JJ-8042", "Jassiel Juarez", "1234", "MANAGER"),
                ("CS-1020", "Orlando Garcia", "0000", "CASHIER"),
            ]
            cursor.executemany("""
                INSERT INTO users (emp_id, name, pin, role)
                VALUES (?, ?, ?, ?)
            """, sample_users)

        # Seed initial inventory if empty
        cursor.execute("SELECT COUNT(*) FROM products")
        if cursor.fetchone()[0] == 0:
            sample_data = [
                ("012000000133", "Diet Mountain Dew 20oz", "beverage", 219, 110, 1, 48),
                ("028400064088", "Doritos Nacho Cheese 9.25oz", "snacks", 449, 230, 1, 24),
                ("049000000443", "Coca-Cola Original 2L", "beverage", 279, 140, 1, 32),
                ("078742351865", "Whole Milk 1 Gallon", "grocery", 368, 290, 0, 16),
                ("041220576321", "Fresh Bananas (1 lb)", "grocery", 68, 35, 0, 120),
                ("034000002405", "Reese's Peanut Butter Cups", "snacks", 189, 95, 1, 65),
            ]
            cursor.executemany("""
                INSERT INTO products (sku, name, category, price_cents, cost_cents, taxable, stock)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, sample_data)

        conn.commit()

init_db()

# --- API Models ---
class LoginPayload(BaseModel):
    pin: str

class UserCreatePayload(BaseModel):
    emp_id: str
    name: str
    pin: str
    role: str  # 'MANAGER' or 'CASHIER'

class CartItemPayload(BaseModel):
    sku: str
    quantity: int

class CheckoutPayload(BaseModel):
    items: List[CartItemPayload]
    payment_type: str  # 'CASH' or 'CARD'
    tendered_cents: int
    cashier_id: str = "JJ-8042"

class ProductCreatePayload(BaseModel):
    sku: str
    name: str
    category: str
    price_cents: int
    cost_cents: int = 100
    taxable: int = 1
    stock: int = 24

# --- Auth & Staff Endpoints ---
@app.post("/api/login")
def authenticate_cashier(payload: LoginPayload):
    with get_db() as conn:
        user = conn.execute(
            "SELECT emp_id, name, role FROM users WHERE pin = ?",
            (payload.pin.strip(),)
        ).fetchone()
        if not user:
            raise HTTPException(status_code=401, detail="Invalid Cashier PIN")
        return dict(user)

@app.get("/api/users")
def list_users():
    with get_db() as conn:
        rows = conn.execute("SELECT id, emp_id, name, pin, role FROM users ORDER BY role DESC, name ASC").fetchall()
        return [dict(row) for row in rows]

@app.post("/api/users")
def create_or_update_user(payload: UserCreatePayload):
    if len(payload.pin.strip()) != 4 or not payload.pin.strip().isdigit():
        raise HTTPException(status_code=400, detail="PIN must be exactly 4 digits")
    with get_db() as conn:
        try:
            conn.execute("""
                INSERT INTO users (emp_id, name, pin, role)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(emp_id) DO UPDATE SET
                    name = excluded.name,
                    pin = excluded.pin,
                    role = excluded.role
            """, (payload.emp_id.strip().upper(), payload.name.strip(), payload.pin.strip(), payload.role.upper()))
            conn.commit()
            return {"status": "ok", "emp_id": payload.emp_id.upper()}
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="That 4-digit PIN is already assigned to another user.")

@app.delete("/api/users/{emp_id}")
def delete_user(emp_id: str):
    if emp_id.upper() == "JJ-8042":
        raise HTTPException(status_code=400, detail="Cannot delete primary owner account (JJ-8042)")
    with get_db() as conn:
        conn.execute("DELETE FROM users WHERE emp_id = ?", (emp_id.upper(),))
        conn.commit()
        return {"status": "deleted"}

# --- Product & Inventory Endpoints ---
@app.get("/api/products")
def list_products():
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM products ORDER BY name ASC").fetchall()
        return [dict(row) for row in rows]

@app.get("/api/products/{sku}")
def get_product(sku: str):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM products WHERE sku = ?", (sku.strip(),)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="SKU not found")
        return dict(row)

@app.post("/api/products")
def create_or_restock_product(payload: ProductCreatePayload):
    with get_db() as conn:
        cursor = conn.cursor()
        existing = cursor.execute("SELECT id FROM products WHERE sku = ?", (payload.sku.strip(),)).fetchone()
        if existing:
            cursor.execute("""
                UPDATE products 
                SET stock = stock + ?, price_cents = ?, name = ?, category = ?, taxable = ?
                WHERE sku = ?
            """, (payload.stock, payload.price_cents, payload.name.strip(), payload.category, payload.taxable, payload.sku.strip()))
        else:
            cursor.execute("""
                INSERT INTO products (sku, name, category, price_cents, cost_cents, taxable, stock)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (payload.sku.strip(), payload.name.strip(), payload.category, payload.price_cents, payload.cost_cents, payload.taxable, payload.stock))
        conn.commit()
        return {"status": "ok", "sku": payload.sku}

# --- Checkout & Auditing Endpoints ---
@app.post("/api/checkout")
def process_checkout(payload: CheckoutPayload):
    if not payload.items:
        raise HTTPException(status_code=400, detail="Empty cart")

    with get_db() as conn:
        cursor = conn.cursor()
        line_items = []
        subtotal_cents = 0
        taxable_cents = 0

        for item in payload.items:
            cursor.execute("SELECT * FROM products WHERE sku = ?", (item.sku,))
            prod = cursor.fetchone()
            if not prod:
                raise HTTPException(status_code=404, detail=f"Item {item.sku} not found")
            if prod["stock"] < item.quantity:
                raise HTTPException(status_code=400, detail=f"Insufficient stock for {prod['name']}")

            line_total = prod["price_cents"] * item.quantity
            subtotal_cents += line_total
            if prod["taxable"]:
                taxable_cents += line_total

            line_items.append({
                "product_id": prod["id"],
                "sku": prod["sku"],
                "name": prod["name"],
                "unit_price_cents": prod["price_cents"],
                "quantity": item.quantity,
                "line_total_cents": line_total
            })

        tax_cents = round(taxable_cents * TAX_RATE)
        total_cents = subtotal_cents + tax_cents

        if payload.tendered_cents < total_cents:
            raise HTTPException(status_code=400, detail="Tendered amount is less than total")

        change_cents = payload.tendered_cents - total_cents if payload.payment_type == "CASH" else 0
        receipt_num = f"TXN-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

        try:
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute("""
                INSERT INTO transactions (receipt_number, subtotal_cents, tax_cents, total_cents, cashier_id, status)
                VALUES (?, ?, ?, ?, ?, 'COMPLETED')
            """, (receipt_num, subtotal_cents, tax_cents, total_cents, payload.cashier_id))
            txn_id = cursor.lastrowid

            for li in line_items:
                cursor.execute("""
                    INSERT INTO transaction_items (transaction_id, product_id, quantity, unit_price_cents, line_total_cents)
                    VALUES (?, ?, ?, ?, ?)
                """, (txn_id, li["product_id"], li["quantity"], li["unit_price_cents"], li["line_total_cents"]))

                cursor.execute("""
                    UPDATE products SET stock = stock - ? WHERE id = ?
                """, (li["quantity"], li["product_id"]))

            cursor.execute("""
                INSERT INTO payments (transaction_id, payment_type, tendered_cents, change_due_cents)
                VALUES (?, ?, ?, ?)
            """, (txn_id, payload.payment_type, payload.tendered_cents, change_cents))

            conn.commit()
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=str(e))

        return {
            "receipt_number": receipt_num,
            "subtotal_cents": subtotal_cents,
            "tax_cents": tax_cents,
            "total_cents": total_cents,
            "tendered_cents": payload.tendered_cents,
            "change_due_cents": change_cents,
            "items": line_items,
            "payment_type": payload.payment_type,
            "cashier_id": payload.cashier_id,
            "created_at": datetime.now().isoformat()
        }
import csv
import io
from fastapi.responses import StreamingResponse

@app.get("/api/reports/export-csv")
def export_transactions_csv():
    with get_db() as conn:
        cursor = conn.cursor()
        rows = cursor.execute("""
            SELECT t.receipt_number, t.cashier_id, t.subtotal_cents, t.tax_cents, t.total_cents, 
                   p.payment_type, p.tendered_cents, p.change_due_cents, t.status, t.created_at
            FROM transactions t
            LEFT JOIN payments p ON p.transaction_id = t.id
            ORDER BY t.id DESC
        """).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Receipt Number", "Cashier ID", "Subtotal ($)", "Tax ($)", 
        "Total ($)", "Payment Type", "Tendered ($)", "Change ($)", "Status", "Timestamp"
    ])

    for r in rows:
        writer.writerow([
            r["receipt_number"],
            r["cashier_id"],
            f"{r['subtotal_cents'] / 100:.2f}",
            f"{r['tax_cents'] / 100:.2f}",
            f"{r['total_cents'] / 100:.2f}",
            r["payment_type"] or "N/A",
            f"{(r['tendered_cents'] or 0) / 100:.2f}",
            f"{(r['change_due_cents'] or 0) / 100:.2f}",
            r["status"],
            r["created_at"]
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=pos_sales_report.csv"}
    )
@app.get("/api/reports/shift")
def shift_report():
    with get_db() as conn:
        cursor = conn.cursor()
        totals = cursor.execute("""
            SELECT 
                COUNT(*) as count,
                COALESCE(SUM(subtotal_cents), 0) as total_subtotal,
                COALESCE(SUM(tax_cents), 0) as total_tax,
                COALESCE(SUM(total_cents), 0) as grand_total
            FROM transactions
            WHERE status = 'COMPLETED'
        """).fetchone()

        cash_tendered = cursor.execute("""
            SELECT COALESCE(SUM(p.tendered_cents - p.change_due_cents), 0)
            FROM payments p
            JOIN transactions t ON t.id = p.transaction_id
            WHERE p.payment_type = 'CASH' AND t.status = 'COMPLETED'
        """).fetchone()[0]

        card_tendered = cursor.execute("""
            SELECT COALESCE(SUM(p.tendered_cents), 0)
            FROM payments p
            JOIN transactions t ON t.id = p.transaction_id
            WHERE p.payment_type != 'CASH' AND t.status = 'COMPLETED'
        """).fetchone()[0]

        recent_txns = cursor.execute("""
            SELECT id, receipt_number, total_cents, cashier_id, status, created_at
            FROM transactions
            ORDER BY id DESC LIMIT 10
        """).fetchall()

        return {
            "transaction_count": totals["count"],
            "total_sales_cents": totals["grand_total"],
            "cash_collected_cents": cash_tendered,
            "card_collected_cents": card_tendered,
            "recent_transactions": [dict(r) for r in recent_txns]
        }

@app.post("/api/transactions/{receipt_number}/void")
def void_transaction(receipt_number: str):
    with get_db() as conn:
        cursor = conn.cursor()
        txn = cursor.execute("SELECT id, status FROM transactions WHERE receipt_number = ?", (receipt_number,)).fetchone()
        if not txn:
            raise HTTPException(status_code=404, detail="Transaction not found")
        if txn["status"] == "VOIDED":
            raise HTTPException(status_code=400, detail="Transaction already voided")

        items = cursor.execute("SELECT product_id, quantity FROM transaction_items WHERE transaction_id = ?", (txn["id"],)).fetchall()
        try:
            cursor.execute("BEGIN TRANSACTION")
            cursor.execute("UPDATE transactions SET status = 'VOIDED' WHERE id = ?", (txn["id"],))
            for item in items:
                cursor.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (item["quantity"], item["product_id"]))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise HTTPException(status_code=500, detail=str(e))
        return {"status": "VOIDED", "receipt_number": receipt_number}