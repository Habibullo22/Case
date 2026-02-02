import time
import secrets
import random
from typing import Optional, List, Dict, Any

from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import aiosqlite

from db import DB_PATH, init_db

app = FastAPI()

# ====== SOZLAMA ======
ADMIN_TELEGRAM_ID = 5815294733  # seniki
ADMIN_TOKEN = "CHANGE_ME_SUPER_SECRET"  # admin panelga kirish uchun token
# =====================

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.on_event("startup")
async def _startup():
    await init_db()
    # demo seed data
    await seed_demo_data()


async def db_conn():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


def now() -> int:
    return int(time.time())


# --- Minimal auth: telegram_id query orqali (demo). Keyin Telegram initData verify qo‘shamiz.
async def get_user(request: Request, db=Depends(db_conn)):
    tg = request.headers.get("x-telegram-id")
    if not tg:
        raise HTTPException(401, "x-telegram-id header required")
    try:
        telegram_id = int(tg)
    except:
        raise HTTPException(400, "bad telegram id")

    row = await db.execute_fetchone("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
    if not row:
        await db.execute(
            "INSERT INTO users(telegram_id, username, balance, created_at) VALUES(?,?,?,?)",
            (telegram_id, None, 15, now())  # start bonus 15 coin
        )
        await db.commit()
        row = await db.execute_fetchone("SELECT * FROM users WHERE telegram_id=?", (telegram_id,))
    return dict(row)


async def require_admin(request: Request):
    tok = request.headers.get("x-admin-token")
    if tok != ADMIN_TOKEN:
        raise HTTPException(403, "admin token required")


# ====== PAGES ======
@app.get("/", response_class=HTMLResponse)
async def home():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    with open("static/admin.html", "r", encoding="utf-8") as f:
        return f.read()


# ====== API ======

@app.get("/api/me")
async def api_me(user=Depends(get_user)):
    return user

@app.get("/api/cases")
async def api_cases(db=Depends(db_conn), user=Depends(get_user)):
    rows = await db.execute_fetchall("SELECT * FROM cases ORDER BY id DESC")
    return [dict(r) for r in rows]

@app.get("/api/inventory")
async def api_inventory(db=Depends(db_conn), user=Depends(get_user)):
    rows = await db.execute_fetchall("""
      SELECT inv.id as inv_id, inv.status, inv.created_at,
             it.id as item_id, it.name, it.rarity, it.image, it.value
      FROM inventory inv
      JOIN items it ON it.id = inv.item_id
      WHERE inv.telegram_id = ?
      ORDER BY inv.id DESC
    """, (user["telegram_id"],))
    return [dict(r) for r in rows]

@app.post("/api/sell/{inv_id}")
async def api_sell(inv_id: int, db=Depends(db_conn), user=Depends(get_user)):
    row = await db.execute_fetchone("""
      SELECT inv.id, inv.status, it.value
      FROM inventory inv JOIN items it ON it.id=inv.item_id
      WHERE inv.id=? AND inv.telegram_id=?
    """, (inv_id, user["telegram_id"]))
    if not row:
        raise HTTPException(404, "not found")
    if row["status"] != "owned":
        raise HTTPException(400, "cannot sell this item")

    value = int(row["value"])
    await db.execute("UPDATE inventory SET status='sold' WHERE id=?", (inv_id,))
    await db.execute("UPDATE users SET balance = balance + ? WHERE telegram_id=?", (value, user["telegram_id"]))
    await db.commit()
    return {"ok": True, "added": value}

@app.post("/api/withdraw/{inv_id}")
async def api_withdraw(inv_id: int, payload: Dict[str, Any] = None, db=Depends(db_conn), user=Depends(get_user)):
    note = ""
    if payload and isinstance(payload, dict):
        note = (payload.get("note") or "")[:200]

    row = await db.execute_fetchone("""
      SELECT id, status FROM inventory
      WHERE id=? AND telegram_id=?
    """, (inv_id, user["telegram_id"]))
    if not row:
        raise HTTPException(404, "not found")
    if row["status"] != "owned":
        raise HTTPException(400, "cannot withdraw this item")

    await db.execute("UPDATE inventory SET status='withdraw_requested' WHERE id=?", (inv_id,))
    await db.execute("""
      INSERT INTO withdraw_requests(telegram_id, inventory_id, note, status, created_at)
      VALUES(?,?,?,?,?)
    """, (user["telegram_id"], inv_id, note, "pending", now()))
    await db.commit()
    return {"ok": True}

@app.post("/api/open/{case_id}")
async def api_open(case_id: int, db=Depends(db_conn), user=Depends(get_user)):
    # case exists?
    case = await db.execute_fetchone("SELECT * FROM cases WHERE id=?", (case_id,))
    if not case:
        raise HTTPException(404, "case not found")

    price = int(case["price"])
    urow = await db.execute_fetchone("SELECT balance FROM users WHERE telegram_id=?", (user["telegram_id"],))
    if int(urow["balance"]) < price:
        raise HTTPException(400, "not enough balance")

    # get weighted items
    rows = await db.execute_fetchall("""
      SELECT it.id, it.name, it.rarity, it.image, it.value, ci.weight
      FROM case_items ci
      JOIN items it ON it.id = ci.item_id
      WHERE ci.case_id=?
    """, (case_id,))
    if not rows:
        raise HTTPException(400, "case empty (no items)")

    items = [dict(r) for r in rows]
    weights = [max(1, int(r["weight"])) for r in rows]
    picked = random.choices(items, weights=weights, k=1)[0]

    # charge + add to inventory
    await db.execute("UPDATE users SET balance = balance - ? WHERE telegram_id=?", (price, user["telegram_id"]))
    await db.execute("""
      INSERT INTO inventory(telegram_id, item_id, status, created_at)
      VALUES(?,?,?,?)
    """, (user["telegram_id"], picked["id"], "owned", now()))
    await db.commit()

    # return result
    return {"case": dict(case), "drop": picked}


# ====== ADMIN API ======

@app.post("/api/admin/add_balance")
async def admin_add_balance(payload: Dict[str, Any], db=Depends(db_conn), _=Depends(require_admin)):
    telegram_id = int(payload.get("telegram_id"))
    amount = int(payload.get("amount"))
    if amount == 0:
        raise HTTPException(400, "amount required")
    await db.execute("INSERT OR IGNORE INTO users(telegram_id, username, balance, created_at) VALUES(?,?,?,?)",
                     (telegram_id, None, 0, now()))
    await db.execute("UPDATE users SET balance = balance + ? WHERE telegram_id=?", (amount, telegram_id))
    await db.commit()
    return {"ok": True}

@app.post("/api/admin/create_item")
async def admin_create_item(payload: Dict[str, Any], db=Depends(db_conn), _=Depends(require_admin)):
    name = (payload.get("name") or "").strip()
    rarity = (payload.get("rarity") or "common").strip()
    image = (payload.get("image") or "").strip()
    value = int(payload.get("value") or 0)
    if not name or value <= 0:
        raise HTTPException(400, "name/value required")
    cur = await db.execute("INSERT INTO items(name, rarity, image, value) VALUES(?,?,?,?)",
                           (name, rarity, image, value))
    await db.commit()
    return {"ok": True, "item_id": cur.lastrowid}

@app.post("/api/admin/create_case")
async def admin_create_case(payload: Dict[str, Any], db=Depends(db_conn), _=Depends(require_admin)):
    title = (payload.get("title") or "").strip()
    price = int(payload.get("price") or 0)
    cover = (payload.get("cover") or "").strip()
    if not title or price <= 0:
        raise HTTPException(400, "title/price required")
    cur = await db.execute("INSERT INTO cases(title, price, cover) VALUES(?,?,?)", (title, price, cover))
    await db.commit()
    return {"ok": True, "case_id": cur.lastrowid}

@app.post("/api/admin/add_case_item")
async def admin_add_case_item(payload: Dict[str, Any], db=Depends(db_conn), _=Depends(require_admin)):
    case_id = int(payload.get("case_id"))
    item_id = int(payload.get("item_id"))
    weight = int(payload.get("weight") or 1)
    if weight <= 0:
        weight = 1
    await db.execute("INSERT INTO case_items(case_id, item_id, weight) VALUES(?,?,?)",
                     (case_id, item_id, weight))
    await db.commit()
    return {"ok": True}

@app.get("/api/admin/withdraws")
async def admin_withdraws(db=Depends(db_conn), _=Depends(require_admin)):
    rows = await db.execute_fetchall("""
      SELECT wr.*, it.name, it.rarity, it.value, it.image
      FROM withdraw_requests wr
      JOIN inventory inv ON inv.id = wr.inventory_id
      JOIN items it ON it.id = inv.item_id
      ORDER BY wr.id DESC
      LIMIT 100
    """)
    return [dict(r) for r in rows]


# ====== DEMO SEED ======
async def seed_demo_data():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        c = await db.execute_fetchone("SELECT COUNT(*) as c FROM cases")
        if int(c["c"]) > 0:
            return

        # items
        items = [
            ("Desert Eagle | Purple", "rare", "", 1500),
            ("AK-47 | Redline", "epic", "", 6000),
            ("Knife | Gold", "legendary", "", 25000),
            ("Sticker | Common", "common", "", 200),
            ("USP | Blue", "uncommon", "", 700),
        ]
        item_ids = []
        for name, rarity, image, value in items:
            cur = await db.execute("INSERT INTO items(name, rarity, image, value) VALUES(?,?,?,?)",
                                   (name, rarity, image, value))
            item_ids.append(cur.lastrowid)

        # case
        cur = await db.execute("INSERT INTO cases(title, price, cover) VALUES(?,?,?)",
                               ("Armory Case", 3000, ""))
        case_id = cur.lastrowid

        # weights: common ko‘p, legendary kam
        weights = [30, 12, 1, 40, 25]
        for iid, w in zip(item_ids, weights):
            await db.execute("INSERT INTO case_items(case_id, item_id, weight) VALUES(?,?,?)",
                             (case_id, iid, w))

        await db.commit()
