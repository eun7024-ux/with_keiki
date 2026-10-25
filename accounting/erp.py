"""ERP 기능: 주문·출고, 발주·입고, 재고, 생산(BOM), 문서 출력.

server.py 의 공통 함수(build_entry, insert_entry 등)를 bind() 로 넘겨받아 사용한다.
재고는 따로 저장하지 않고 '매입 수량 − 매출 수량 + 생산/실사 조정'으로 계산하므로,
주문을 거치지 않고 B2B 파일로 바로 넣은 매출도 재고에 그대로 반영된다.
"""
import datetime as dt
import html

core = None  # server 모듈 (bind 로 주입)
NOT_FOUND = object()

ORDER_KINDS = {"sales": "sale", "purchase": "purchase"}  # 주문 종류 → 확정 시 만들 장부 종류
OPEN_STATUS = {"sales": "received", "purchase": "ordered"}
DONE_STATUS = "done"
STATUS_LABELS = {"quote": "견적", "received": "접수", "ordered": "발주", "done": "완료", "cancelled": "취소"}
PRODUCT_KINDS = ("product", "material", "goods")  # 완제품 / 자재 / 상품(사서 그대로 파는 것)

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('sales', 'purchase')),   -- 수주 / 발주
    order_no TEXT NOT NULL,
    date TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    status TEXT NOT NULL,                       -- quote | received | ordered | done | cancelled
    due_date TEXT DEFAULT '',                   -- 납기(출고/입고 예정일)
    done_date TEXT DEFAULT '',                  -- 실제 출고/입고일
    courier TEXT DEFAULT '',
    tracking_no TEXT DEFAULT '',
    memo TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_no ON orders(kind, order_no);
CREATE TABLE IF NOT EXISTS order_lines (
    id INTEGER PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    item_name TEXT DEFAULT '',
    qty REAL NOT NULL DEFAULT 1,
    unit_price INTEGER NOT NULL DEFAULT 0,
    supply INTEGER NOT NULL DEFAULT 0,
    vat INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bom (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    material_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    qty REAL NOT NULL,                          -- 완제품 1개에 들어가는 자재 수량
    UNIQUE (product_id, material_id)
);
CREATE TABLE IF NOT EXISTS productions (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    qty REAL NOT NULL,
    memo TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stock_moves (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    qty REAL NOT NULL,                          -- + 입고 / − 출고
    reason TEXT NOT NULL,                       -- production | consume | adjust
    production_id INTEGER REFERENCES productions(id) ON DELETE CASCADE,
    memo TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS stock_moves_product ON stock_moves(product_id);
"""

SETTINGS = {
    # 문서(거래명세서·견적서·발주서)에 찍히는 회사 정보
    "biz_no": "", "ceo": "", "address": "", "phone": "", "biz_type": "", "bank_account": "",
    "doc_note": "",
}


def bind(module):
    global core
    core = module


def init(conn):
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(products)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN kind TEXT NOT NULL DEFAULT 'product'")
    if "safety_stock" not in cols:
        conn.execute("ALTER TABLE products ADD COLUMN safety_stock REAL NOT NULL DEFAULT 0")
    for k, v in SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


def today():
    return dt.date.today().isoformat()


# ───────────────────────── 재고 ─────────────────────────

def stock_levels(conn):
    """품목별 {product_id: {onhand, reserved, incoming}}"""
    lv = {}

    def add(pid, key, v):
        d = lv.setdefault(pid, {"onhand": 0.0, "reserved": 0.0, "incoming": 0.0})
        d[key] += v or 0

    for r in conn.execute("""SELECT product_id, SUM(CASE WHEN kind='purchase' THEN qty ELSE -qty END) q
                             FROM entries WHERE product_id IS NOT NULL GROUP BY product_id"""):
        add(r["product_id"], "onhand", r["q"])
    for r in conn.execute("SELECT product_id, SUM(qty) q FROM stock_moves GROUP BY product_id"):
        add(r["product_id"], "onhand", r["q"])
    for r in conn.execute("""SELECT o.kind, l.product_id, SUM(l.qty) q FROM order_lines l JOIN orders o ON o.id = l.order_id
                             WHERE l.product_id IS NOT NULL AND o.status IN ('received', 'ordered') GROUP BY o.kind, l.product_id"""):
        add(r["product_id"], "reserved" if r["kind"] == "sales" else "incoming", r["q"])
    return lv


def stock_table(conn):
    lv = stock_levels(conn)
    out = []
    for p in core.rows(conn.execute("SELECT * FROM products ORDER BY kind, name")):
        d = lv.get(p["id"], {"onhand": 0, "reserved": 0, "incoming": 0})
        avail = d["onhand"] - d["reserved"]
        safety = p.get("safety_stock") or 0
        if d["onhand"] < 0:
            state = "negative"
        elif avail < 0 or (safety and avail < safety):
            state = "low"
        elif safety and avail < safety * 1.5:
            state = "watch"
        else:
            state = "ok"
        out.append({**p, "onhand": d["onhand"], "reserved": d["reserved"], "incoming": d["incoming"],
                    "available": avail, "state": state, "value": core.won(max(d["onhand"], 0) * (p["cost_price"] or 0))})
    return out


def stock_history(conn, pid, limit=300):
    hist = core.rows(conn.execute(
        """SELECT e.date, CASE WHEN e.kind='purchase' THEN e.qty ELSE -e.qty END qty,
                  CASE WHEN e.kind='purchase' THEN '매입' ELSE '매출' END reason,
                  COALESCE(c.name, '') party, e.order_no ref, e.memo
           FROM entries e LEFT JOIN clients c ON c.id = e.client_id WHERE e.product_id = ?
           UNION ALL
           SELECT m.date, m.qty, CASE m.reason WHEN 'production' THEN '생산 입고' WHEN 'consume' THEN '생산 투입'
                  ELSE '재고 조정' END, '', CASE WHEN m.production_id IS NULL THEN '' ELSE '생산#' || m.production_id END, m.memo
           FROM stock_moves m WHERE m.product_id = ?
           ORDER BY 1, 3""", (pid, pid)))
    bal = 0
    for h in hist:
        bal += h["qty"]
        h["balance"] = bal
    return list(reversed(hist))[:limit]


def adjust_stock(conn, b):
    pid = int(b["product_id"])
    actual = core.parse_number(b.get("actual"))
    if actual is None:
        raise ValueError("실제 재고 수량을 입력하세요.")
    cur = stock_levels(conn).get(pid, {"onhand": 0})["onhand"]
    diff = actual - cur
    if diff:
        conn.execute("INSERT INTO stock_moves(date, product_id, qty, reason, memo) VALUES (?, ?, ?, 'adjust', ?)",
                     (core.parse_date(b.get("date"), today()), pid, diff, b.get("memo") or "재고 실사"))
    return {"ok": True, "diff": diff}


# ───────────────────────── 주문 (수주 / 발주) ─────────────────────────

def next_order_no(conn, kind, date):
    prefix = ("S" if kind == "sales" else "P") + "-" + date.replace("-", "") + "-"
    n = conn.execute("SELECT COUNT(*) FROM orders WHERE kind = ? AND order_no LIKE ?", (kind, prefix + "%")).fetchone()[0]
    while True:
        n += 1
        no = "%s%03d" % (prefix, n)
        if not conn.execute("SELECT 1 FROM orders WHERE kind = ? AND order_no = ?", (kind, no)).fetchone():
            return no


def build_lines(conn, kind, client_id, date, lines, settings):
    ek = ORDER_KINDS[kind]
    out = []
    for i, ln in enumerate(lines or []):
        if not any(str(ln.get(k) or "").strip() for k in ("item", "product_id", "code")):
            continue
        try:
            e = core.build_entry(conn, ek, {**ln, "client_id": client_id, "date": date}, settings)
        except ValueError as ex:
            raise ValueError("%d번째 줄: %s" % (i + 1, ex))
        out.append({k: e[k] for k in ("product_id", "item_name", "qty", "unit_price", "supply", "vat", "total")})
    if not out:
        raise ValueError("품목을 한 줄 이상 입력하세요.")
    return out


def save_order(conn, kind, b, oid=None, source="manual"):
    if kind not in ORDER_KINDS:
        raise ValueError("주문 종류 오류")
    settings = core.get_settings(conn)
    date = core.parse_date(b.get("date"), today())
    client_id = b.get("client_id") or core.find_or_create_client(conn, b.get("client"), ORDER_KINDS[kind])
    if not client_id:
        raise ValueError("거래처를 입력하세요.")
    lines = build_lines(conn, kind, client_id, date, b.get("lines"), settings)
    status = b.get("status") or OPEN_STATUS[kind]
    if status not in ("quote", OPEN_STATUS[kind]):
        status = OPEN_STATUS[kind]
    vals = (date, client_id, status, core.parse_date(b.get("due_date"), "") or "", b.get("memo") or "")
    if oid:
        o = get_order_row(conn, oid)
        if o["status"] == DONE_STATUS:
            raise ValueError("완료된 주문은 수정할 수 없습니다. 먼저 '완료 취소'를 하세요.")
        conn.execute("UPDATE orders SET date=?, client_id=?, status=?, due_date=?, memo=? WHERE id=?", vals + (oid,))
        conn.execute("DELETE FROM order_lines WHERE order_id = ?", (oid,))
    else:
        order_no = str(b.get("order_no") or "").strip() or next_order_no(conn, kind, date)
        oid = conn.execute("INSERT INTO orders(kind, order_no, date, client_id, status, due_date, memo, source)"
                           " VALUES (?,?,?,?,?,?,?,?)", (kind, order_no) + vals + (source,)).lastrowid
    for ln in lines:
        conn.execute("INSERT INTO order_lines(order_id, product_id, item_name, qty, unit_price, supply, vat, total)"
                     " VALUES (?,?,?,?,?,?,?,?)", (oid, ln["product_id"], ln["item_name"], ln["qty"], ln["unit_price"],
                                                   ln["supply"], ln["vat"], ln["total"]))
    return oid


def get_order_row(conn, oid):
    o = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
    if not o:
        raise core.ApiError(404, "주문을 찾을 수 없습니다.")
    return o


def get_order(conn, oid):
    o = dict(conn.execute("""SELECT o.*, c.name client_name, c.biz_no client_biz_no, c.contact client_contact
                             FROM orders o LEFT JOIN clients c ON c.id = o.client_id WHERE o.id = ?""", (oid,)).fetchone()
             or {})
    if not o:
        raise core.ApiError(404, "주문을 찾을 수 없습니다.")
    lv = stock_levels(conn)
    o["lines"] = core.rows(conn.execute("""SELECT l.*, p.unit, p.code FROM order_lines l
                                           LEFT JOIN products p ON p.id = l.product_id WHERE l.order_id = ? ORDER BY l.id""", (oid,)))
    for ln in o["lines"]:
        d = lv.get(ln["product_id"], {"onhand": 0, "reserved": 0})
        ln["onhand"] = d["onhand"]
        ln["available"] = d["onhand"] - d["reserved"]
    for k in ("supply", "vat", "total"):
        o[k] = sum(ln[k] for ln in o["lines"])
    return o


def list_orders(conn, kind, q, d_from, d_to):
    sql = """SELECT o.*, c.name client_name, COUNT(l.id) line_count, COALESCE(SUM(l.qty), 0) qty,
                    COALESCE(SUM(l.supply), 0) supply, COALESCE(SUM(l.total), 0) total,
                    (SELECT item_name FROM order_lines WHERE order_id = o.id ORDER BY id LIMIT 1) first_item
             FROM orders o LEFT JOIN clients c ON c.id = o.client_id LEFT JOIN order_lines l ON l.order_id = o.id
             WHERE o.kind = ?"""
    args = [kind]
    status = q.get("status") or ""
    if status == "open":  # 진행 중인 것은 기간과 관계없이 전부
        sql += " AND o.status IN ('quote', 'received', 'ordered')"
    else:
        sql += " AND o.date BETWEEN ? AND ?"
        args += [d_from, d_to]
        if status:
            sql += " AND o.status = ?"
            args.append(status)
    if q.get("q"):
        sql += " AND (c.name LIKE ? OR o.order_no LIKE ? OR o.memo LIKE ? OR o.tracking_no LIKE ?)"
        args += ["%" + q["q"] + "%"] * 4
    sql += " GROUP BY o.id ORDER BY CASE WHEN o.status IN ('quote','received','ordered') THEN 0 ELSE 1 END, " \
           "COALESCE(NULLIF(o.due_date, ''), o.date), o.id DESC LIMIT 1000"
    out = core.rows(conn.execute(sql, args))
    t = today()
    for o in out:
        o["overdue"] = bool(o["due_date"] and o["due_date"] < t and o["status"] in ("received", "ordered"))
    return out


def complete_order(conn, oid, b):
    """출고(수주) 또는 입고(발주) 완료 → 매출/매입 장부에 기록. 재고는 장부에서 자동 계산된다."""
    o = get_order_row(conn, oid)
    if o["status"] == DONE_STATUS:
        raise ValueError("이미 완료된 주문입니다.")
    if o["status"] == "cancelled":
        raise ValueError("취소된 주문입니다.")
    date = core.parse_date(b.get("date"), today())
    kind = ORDER_KINDS[o["kind"]]
    paid = 1 if b.get("paid") else 0
    lines = conn.execute("SELECT * FROM order_lines WHERE order_id = ? ORDER BY id", (oid,)).fetchall()
    for ln in lines:
        e = {"kind": kind, "date": date, "client_id": o["client_id"], "product_id": ln["product_id"],
             "item_name": ln["item_name"], "qty": ln["qty"], "unit_price": ln["unit_price"], "supply": ln["supply"],
             "vat": ln["vat"], "total": ln["total"], "unit_cost": None, "paid": paid, "order_no": o["order_no"],
             "memo": o["memo"] or ""}
        core.insert_entry(conn, e, "order", "order|%d|%d" % (oid, ln["id"]))
    conn.execute("UPDATE orders SET status = ?, done_date = ?, courier = ?, tracking_no = ? WHERE id = ?",
                 (DONE_STATUS, date, b.get("courier") or "", b.get("tracking_no") or "", oid))
    return {"ok": True, "entries": len(lines)}


def reopen_order(conn, oid):
    o = get_order_row(conn, oid)
    if o["status"] != DONE_STATUS:
        raise ValueError("완료된 주문만 되돌릴 수 있습니다.")
    n = conn.execute("DELETE FROM entries WHERE dedupe_key LIKE ?", ("order|%d|%%" % oid,)).rowcount
    conn.execute("UPDATE orders SET status = ?, done_date = '' WHERE id = ?", (OPEN_STATUS[o["kind"]], oid))
    return {"ok": True, "removed": n}


def set_status(conn, oid, status):
    o = get_order_row(conn, oid)
    if o["status"] == DONE_STATUS:
        raise ValueError("완료된 주문은 먼저 '완료 취소'를 하세요.")
    allowed = {"quote", "cancelled", OPEN_STATUS[o["kind"]]}
    if status not in allowed:
        raise ValueError("바꿀 수 없는 상태입니다.")
    conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, oid))
    return {"ok": True}


def ingest_orders(conn, raw_rows, source, default_date=None, filename=None):
    """B2B 주문 파일 → 수주(출고 대기). 같은 주문번호의 여러 줄은 한 주문으로 묶는다."""
    groups = {}
    for i, r in enumerate(raw_rows):
        r.setdefault("_line", i + 1)
        no = str(r.get("order_no") or "").strip()
        key = no or "%s|%s" % (core.parse_date(r.get("date"), default_date), str(r.get("client") or "").strip())
        groups.setdefault(key, []).append(r)
    res = {"inserted": 0, "duplicates": 0, "errors": [], "unit": "주문"}
    for key, grp in groups.items():
        first = grp[0]
        no = str(first.get("order_no") or "").strip()
        if no and conn.execute("SELECT 1 FROM orders WHERE kind='sales' AND order_no = ?", (no,)).fetchone():
            res["duplicates"] += 1
            continue
        try:
            with conn:
                date = core.parse_date(first.get("date"), default_date)
                if not date:
                    raise ValueError("날짜를 읽을 수 없음: %r" % (first.get("date"),))
                save_order(conn, "sales", {"date": date, "client": first.get("client"), "order_no": no,
                                           "memo": first.get("memo") or "", "lines": grp}, source=source)
                res["inserted"] += 1
        except ValueError as ex:
            res["errors"].append({"row": first["_line"], "error": str(ex)})
    with conn:
        conn.execute(
            "INSERT INTO import_log(source, filename, kind, inserted, duplicates, errors, message) VALUES (?,?,?,?,?,?,?)",
            (source, filename or "", "order", res["inserted"], res["duplicates"], len(res["errors"]),
             "; ".join("%s행: %s" % (x["row"], x["error"]) for x in res["errors"][:5])))
    return res


# ───────────────────────── 생산 / BOM ─────────────────────────

def bom_lines(conn, pid):
    lv = stock_levels(conn)
    out = core.rows(conn.execute("""SELECT b.*, m.name material_name, m.unit, m.cost_price FROM bom b
                                    JOIN products m ON m.id = b.material_id WHERE b.product_id = ? ORDER BY m.name""", (pid,)))
    for r in out:
        r["onhand"] = lv.get(r["material_id"], {"onhand": 0})["onhand"]
    return out


def production_plan(conn, pid, qty):
    plan = []
    for b in bom_lines(conn, pid):
        need = b["qty"] * qty
        plan.append({"material_id": b["material_id"], "name": b["material_name"], "unit": b["unit"],
                     "per": b["qty"], "need": need, "onhand": b["onhand"], "short": max(0, need - b["onhand"])})
    return plan


def add_production(conn, b):
    pid = int(b.get("product_id") or 0)
    qty = core.parse_number(b.get("qty"))
    if not pid or not qty or qty <= 0:
        raise ValueError("생산할 품목과 수량을 입력하세요.")
    date = core.parse_date(b.get("date"), today())
    memo = b.get("memo") or ""
    prid = conn.execute("INSERT INTO productions(date, product_id, qty, memo) VALUES (?,?,?,?)", (date, pid, qty, memo)).lastrowid
    conn.execute("INSERT INTO stock_moves(date, product_id, qty, reason, production_id, memo) VALUES (?,?,?,'production',?,?)",
                 (date, pid, qty, prid, memo))
    plan = production_plan(conn, pid, qty)
    for m in plan:
        conn.execute("INSERT INTO stock_moves(date, product_id, qty, reason, production_id, memo) VALUES (?,?,?,'consume',?,?)",
                     (date, m["material_id"], -m["need"], prid, memo))
    return {"ok": True, "id": prid, "short": [m for m in plan if m["short"] > 0]}


def list_productions(conn, d_from, d_to):
    out = core.rows(conn.execute("""SELECT pr.*, p.name product_name, p.unit FROM productions pr
                                    LEFT JOIN products p ON p.id = pr.product_id
                                    WHERE pr.date BETWEEN ? AND ? ORDER BY pr.date DESC, pr.id DESC""", (d_from, d_to)))
    for r in out:
        r["materials"] = core.rows(conn.execute("""SELECT p.name, -m.qty qty, p.unit FROM stock_moves m JOIN products p ON p.id = m.product_id
                                                   WHERE m.production_id = ? AND m.reason = 'consume'""", (r["id"],)))
    return out


# ───────────────────────── 대시보드 보강 ─────────────────────────

def dashboard(conn):
    t = today()
    so = conn.execute("""SELECT COUNT(DISTINCT o.id) n, COALESCE(SUM(l.supply), 0) amt,
                                COUNT(DISTINCT CASE WHEN o.due_date != '' AND o.due_date < ? THEN o.id END) overdue,
                                COUNT(DISTINCT CASE WHEN o.due_date = ? THEN o.id END) due_today
                         FROM orders o LEFT JOIN order_lines l ON l.order_id = o.id
                         WHERE o.kind = 'sales' AND o.status = 'received'""", (t, t)).fetchone()
    po = conn.execute("""SELECT COUNT(DISTINCT o.id) n, COALESCE(SUM(l.supply), 0) amt FROM orders o
                         LEFT JOIN order_lines l ON l.order_id = o.id WHERE o.kind = 'purchase' AND o.status = 'ordered'""").fetchone()
    stock = stock_table(conn)
    low = [{"name": s["name"], "available": s["available"], "safety_stock": s["safety_stock"], "unit": s["unit"],
            "incoming": s["incoming"], "state": s["state"]} for s in stock if s["state"] in ("low", "negative")]
    return {"open_orders": so["n"], "open_orders_amount": core.won(so["amt"]), "overdue_orders": so["overdue"],
            "due_today": so["due_today"], "open_pos": po["n"], "open_pos_amount": core.won(po["amt"]),
            "low_stock": low, "inventory_value": core.won(sum(s["value"] for s in stock))}


# ───────────────────────── API 라우팅 ─────────────────────────

def route(h, conn, method, parts, q, d_from, d_to):
    res = parts[0] if parts else ""
    rid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    sub = parts[2] if len(parts) > 2 else ""

    if res == "orders":
        kind = q.get("kind", "sales")
        if method == "GET":
            return get_order(conn, rid) if rid else list_orders(conn, kind, q, d_from, d_to)
        if method == "DELETE" and rid:
            o = get_order_row(conn, rid)
            if o["status"] == DONE_STATUS:
                raise ValueError("완료된 주문은 삭제할 수 없습니다. 먼저 '완료 취소'를 하세요.")
            conn.execute("DELETE FROM orders WHERE id = ?", (rid,))
            return {"ok": True}
        b = h.read_json()
        action = q.get("action")
        if method == "POST" and rid and action == "complete":
            return complete_order(conn, rid, b)
        if method == "POST" and rid and action == "reopen":
            return reopen_order(conn, rid)
        if method == "POST" and rid and action == "status":
            return set_status(conn, rid, b.get("status"))
        if method == "POST" and rid and action == "convert":  # 견적 → 주문 접수
            return set_status(conn, rid, OPEN_STATUS[get_order_row(conn, rid)["kind"]])
        if method == "POST" and not rid:
            try:
                return {"ok": True, "id": save_order(conn, b.get("kind") or kind, b)}
            except core.sqlite3.IntegrityError:
                raise ValueError("이미 같은 주문번호가 있습니다.")
        if method == "PUT" and rid:
            o = get_order_row(conn, rid)
            save_order(conn, o["kind"], b, rid)
            return {"ok": True}

    if res == "stock":
        if method == "GET" and rid and sub == "history":
            return stock_history(conn, rid)
        if method == "GET":
            return stock_table(conn)
        if method == "POST" and q.get("action") == "adjust":
            return adjust_stock(conn, h.read_json())

    if res == "bom":
        if method == "GET":
            return bom_lines(conn, int(q.get("product_id") or 0))
        if method == "POST":
            b = h.read_json()
            pid, mid = int(b["product_id"]), int(b["material_id"])
            if pid == mid:
                raise ValueError("자기 자신을 자재로 넣을 수 없습니다.")
            qty = core.parse_number(b.get("qty"))
            if not qty or qty <= 0:
                raise ValueError("수량을 입력하세요.")
            conn.execute("""INSERT INTO bom(product_id, material_id, qty) VALUES (?,?,?)
                            ON CONFLICT(product_id, material_id) DO UPDATE SET qty = excluded.qty""", (pid, mid, qty))
            return {"ok": True}
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM bom WHERE id = ?", (rid,))
            return {"ok": True}

    if res == "productions":
        if method == "GET" and q.get("plan"):
            return production_plan(conn, int(q.get("product_id") or 0), core.parse_number(q.get("qty")) or 0)
        if method == "GET":
            return list_productions(conn, d_from, d_to)
        if method == "POST":
            return add_production(conn, h.read_json())
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM productions WHERE id = ?", (rid,))
            return {"ok": True}

    return NOT_FOUND


# ───────────────────────── 문서 출력 ─────────────────────────

def hangul_amount(n):
    """1540000 → '일백오십사만'"""
    n = int(round(n))
    if n == 0:
        return "영"
    digits = "영일이삼사오육칠팔구"
    small = ["", "십", "백", "천"]
    big = ["", "만", "억", "조"]
    out, gi = "", 0
    neg = n < 0
    n = abs(n)
    while n:
        chunk, n = n % 10000, n // 10000
        if chunk:
            s = ""
            for i in range(4):
                d = chunk // (10 ** i) % 10
                if d:
                    s = digits[d] + small[i] + s
            out = s + big[gi] + out
        gi += 1
    return ("마이너스 " if neg else "") + out


DOC_CSS = """
*{box-sizing:border-box} body{font-family:'Noto Sans KR','Malgun Gothic','Apple SD Gothic Neo',sans-serif;color:#222;margin:0;background:#eee}
.page{width:210mm;min-height:270mm;margin:16px auto;background:#fff;padding:14mm 13mm;box-shadow:0 1px 6px rgba(0,0,0,.15)}
h1{text-align:center;font-size:26px;letter-spacing:12px;margin:0 0 4px} .sub{text-align:center;color:#666;font-size:12px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:12px} td,th{border:1px solid #444;padding:5px 6px}
th{background:#f1eef6;font-weight:600} .n{text-align:right;font-variant-numeric:tabular-nums} .c{text-align:center}
.parties{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px} .parties th{width:70px}
.vert{writing-mode:vertical-rl;width:26px!important;text-align:center;letter-spacing:6px}
.sum{margin:10px 0;font-size:14px;border:2px solid #444;padding:8px 12px;display:flex;justify-content:space-between}
.foot{margin-top:12px;font-size:12px;color:#444;white-space:pre-wrap} .meta{font-size:12px;margin-bottom:8px;display:flex;justify-content:space-between}
.bar{text-align:center;margin:12px} .bar button{font-size:15px;padding:8px 20px;cursor:pointer}
@media print{body{background:#fff}.page{margin:0;box-shadow:none;width:auto;min-height:0}.bar{display:none}@page{size:A4;margin:10mm}}
"""


def e(v):
    return html.escape(str(v if v is not None else ""))


def fmt(n):
    if isinstance(n, float) and not n.is_integer():
        return "{:,.2f}".format(n).rstrip("0").rstrip(".")
    return "{:,}".format(int(round(n or 0)))


def party_table(label, name, biz_no, ceo="", address="", contact="", biz_type=""):
    return """<table><tr><th class="vert" rowspan="5">%s</th><th>상호</th><td>%s</td></tr>
<tr><th>등록번호</th><td>%s</td></tr><tr><th>대표자</th><td>%s</td></tr>
<tr><th>주소</th><td>%s</td></tr><tr><th>연락처</th><td>%s%s</td></tr></table>""" % (
        e(label), e(name), e(biz_no), e(ceo), e(address), e(contact), (" / " + e(biz_type)) if biz_type else "")


def page(title, body):
    return """<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>%s</title>
<meta name="viewport" content="width=device-width, initial-scale=1"><style>%s</style></head><body>
<div class="bar"><button onclick="print()">인쇄 / PDF 저장</button></div>%s</body></html>""" % (e(title), DOC_CSS, body)


def render_order(conn, oid):
    o = get_order(conn, oid)
    s = core.get_settings(conn)
    me = party_table("공급자", s.get("company_name"), s.get("biz_no"), s.get("ceo"), s.get("address"), s.get("phone"), s.get("biz_type"))
    them = party_table("공급받는자" if o["kind"] == "sales" else "공급자", o.get("client_name"), o.get("client_biz_no"), "", "",
                       o.get("client_contact"))
    if o["kind"] == "purchase":
        title, sub = "발 주 서", "아래와 같이 발주합니다."
        me = party_table("발주처", s.get("company_name"), s.get("biz_no"), s.get("ceo"), s.get("address"), s.get("phone"))
        left, right = them, me
        meta = "발주번호 %s · 발주일 %s%s" % (e(o["order_no"]), e(o["date"]), (" · 납기 " + e(o["due_date"])) if o["due_date"] else "")
    elif o["status"] == "quote":
        title, sub = "견 적 서", "아래와 같이 견적합니다. (견적일로부터 30일간 유효)"
        left, right = them, me
        meta = "견적번호 %s · 견적일 %s" % (e(o["order_no"]), e(o["date"]))
    else:
        title, sub = "거 래 명 세 서", "공급받는자 보관용"
        left, right = them, me
        d = o["done_date"] or o["date"]
        meta = "주문번호 %s · 거래일 %s%s" % (e(o["order_no"]), e(d),
                                          (" · %s %s" % (e(o["courier"]), e(o["tracking_no"]))) if o["tracking_no"] else "")
    rows_html = "".join(
        "<tr><td class='c'>%d</td><td>%s</td><td class='c'>%s</td><td class='n'>%s</td><td class='n'>%s</td>"
        "<td class='n'>%s</td><td class='n'>%s</td></tr>" % (
            i + 1, e(ln["item_name"]), e(ln.get("unit") or ""), fmt(ln["qty"]), fmt(ln["unit_price"]), fmt(ln["supply"]), fmt(ln["vat"]))
        for i, ln in enumerate(o["lines"]))
    rows_html += "<tr><td>&nbsp;</td><td></td><td></td><td></td><td></td><td></td><td></td></tr>" * max(0, 8 - len(o["lines"]))
    body = """<div class="page"><h1>%s</h1><div class="sub">%s</div>
<div class="meta"><span>%s</span><span>작성일 %s</span></div>
<div class="parties">%s%s</div>
<div class="sum"><b>합계금액</b><span>금 %s원정 (₩%s)</span></div>
<table><tr><th style="width:34px">No</th><th>품목</th><th style="width:50px">단위</th><th style="width:60px">수량</th>
<th style="width:80px">단가</th><th style="width:95px">공급가액</th><th style="width:80px">세액</th></tr>%s
<tr><th colspan="3">합계</th><td class="n">%s</td><td></td><td class="n"><b>%s</b></td><td class="n"><b>%s</b></td></tr></table>
<div class="foot">%s%s</div></div>""" % (
        title, sub, meta, today(), left, right, hangul_amount(o["total"]), fmt(o["total"]), rows_html,
        fmt(sum(ln["qty"] for ln in o["lines"])), fmt(o["supply"]), fmt(o["vat"]),
        ("입금계좌: " + e(s.get("bank_account")) + "\n") if s.get("bank_account") and o["kind"] == "sales" else "",
        e(o["memo"] and ("비고: " + o["memo"] + "\n") or "") + e(s.get("doc_note") or ""))
    return page(title.replace(" ", "") + " " + o["order_no"], body)


def render_statement(conn, client_id, d_from, d_to):
    c = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not c:
        raise core.ApiError(404, "거래처를 찾을 수 없습니다.")
    s = core.get_settings(conn)
    items = core.rows(conn.execute("""SELECT e.*, p.unit FROM entries e LEFT JOIN products p ON p.id = e.product_id
                                      WHERE e.kind='sale' AND e.client_id = ? AND e.date BETWEEN ? AND ?
                                      ORDER BY e.date, e.id""", (client_id, d_from, d_to)))
    unpaid_all = conn.execute("SELECT COALESCE(SUM(total), 0) FROM entries WHERE kind='sale' AND client_id = ? AND paid = 0",
                              (client_id,)).fetchone()[0]
    tot = {k: sum(i[k] for i in items) for k in ("supply", "vat", "total")}
    paid = sum(i["total"] for i in items if i["paid"])
    rows_html = "".join(
        "<tr><td class='c'>%s</td><td>%s</td><td>%s</td><td class='n'>%s</td><td class='n'>%s</td><td class='n'>%s</td>"
        "<td class='n'>%s</td><td class='c'>%s</td></tr>" % (
            e(i["date"][5:]), e(i["order_no"]), e(i["item_name"]), fmt(i["qty"]), fmt(i["unit_price"]), fmt(i["supply"]),
            fmt(i["total"]), "완료" if i["paid"] else "미입금")
        for i in items) or "<tr><td colspan='8' class='c'>기간 내 거래가 없습니다.</td></tr>"
    body = """<div class="page"><h1>거 래 내 역 서</h1><div class="sub">%s ~ %s 정산</div>
<div class="parties">%s%s</div>
<table><tr><th>공급가액</th><th>세액</th><th>합계</th><th>입금 완료</th><th>기간 내 미입금</th><th>총 미수금(전체 기간)</th></tr>
<tr><td class="n">%s</td><td class="n">%s</td><td class="n"><b>%s</b></td><td class="n">%s</td><td class="n">%s</td><td class="n"><b>%s</b></td></tr></table>
<br><table><tr><th style="width:50px">일자</th><th style="width:110px">주문번호</th><th>품목</th><th style="width:55px">수량</th><th style="width:70px">단가</th>
<th style="width:85px">공급가액</th><th style="width:85px">합계</th><th style="width:55px">입금</th></tr>%s</table>
<div class="foot">%s%s</div></div>""" % (
        e(d_from), e(d_to), party_table("공급받는자", c["name"], c["biz_no"], "", "", c["contact"]),
        party_table("공급자", s.get("company_name"), s.get("biz_no"), s.get("ceo"), s.get("address"), s.get("phone")),
        fmt(tot["supply"]), fmt(tot["vat"]), fmt(tot["total"]), fmt(paid), fmt(tot["total"] - paid), fmt(unpaid_all), rows_html,
        ("입금계좌: " + e(s.get("bank_account")) + "\n") if s.get("bank_account") else "", e(s.get("doc_note") or ""))
    return page("거래내역서 %s %s~%s" % (c["name"], d_from, d_to), body)


def render_print(conn, parts, q, d_from, d_to):
    """/print/order/<id>, /print/statement?client_id=&from=&to="""
    kind = parts[0] if parts else ""
    if kind == "order" and len(parts) > 1 and parts[1].isdigit():
        return render_order(conn, int(parts[1]))
    if kind == "statement" and q.get("client_id"):
        return render_statement(conn, int(q["client_id"]), d_from, d_to)
    raise core.ApiError(404, "없는 문서입니다.")

