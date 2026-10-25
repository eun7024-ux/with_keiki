#!/usr/bin/env python3
"""위드케이키 회계 프로그램 — 단일 파일 서버 (Python 표준 라이브러리만 사용).

실행:  python3 server.py            → http://127.0.0.1:8700
옵션:  --host 0.0.0.0 --port 8700 --db 경로
환경변수 ACCOUNTING_PASSWORD 를 지정하면 브라우저 접속 시 비밀번호(Basic 인증)를 요구합니다.
엑셀(.xlsx) 가져오기에는 openpyxl 이 필요합니다 (pip install openpyxl). CSV 는 추가 설치 없이 동작합니다.
"""
import argparse
import base64
import contextlib
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import erp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DB_PATH = os.path.join(BASE_DIR, "data", "accounting.db")
INBOX_DIR = os.path.join(BASE_DIR, "inbox")

KINDS = ("sale", "purchase")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'customer',      -- customer | vendor | both
    biz_no TEXT DEFAULT '',
    contact TEXT DEFAULT '',
    memo TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    code TEXT DEFAULT '',
    name TEXT NOT NULL UNIQUE,
    unit TEXT DEFAULT '개',
    sale_price INTEGER NOT NULL DEFAULT 0,      -- 기본 판매단가(공급가, VAT 별도)
    cost_price INTEGER NOT NULL DEFAULT 0,      -- 원가 / 매입단가
    memo TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS client_prices (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    price INTEGER NOT NULL,
    UNIQUE (client_id, product_id)
);
CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('sale', 'purchase')),
    date TEXT NOT NULL,
    client_id INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    item_name TEXT DEFAULT '',
    qty REAL NOT NULL DEFAULT 1,
    unit_price INTEGER NOT NULL DEFAULT 0,
    supply INTEGER NOT NULL DEFAULT 0,          -- 공급가액
    vat INTEGER NOT NULL DEFAULT 0,             -- 부가세
    total INTEGER NOT NULL DEFAULT 0,           -- 합계
    unit_cost INTEGER,                          -- 매출 건의 개당 원가 (비우면 품목 원가 사용)
    paid INTEGER NOT NULL DEFAULT 0,            -- 입금/지급 완료 여부
    order_no TEXT DEFAULT '',
    dedupe_key TEXT,
    source TEXT NOT NULL DEFAULT 'manual',      -- manual | import | folder | api
    memo TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE UNIQUE INDEX IF NOT EXISTS entries_dedupe ON entries(kind, dedupe_key) WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS entries_date ON entries(kind, date);
CREATE TABLE IF NOT EXISTS expenses (
    id INTEGER PRIMARY KEY,
    date TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '기타',
    description TEXT DEFAULT '',
    amount INTEGER NOT NULL DEFAULT 0,          -- 공급가 기준 금액
    vat INTEGER NOT NULL DEFAULT 0,             -- 매입세액 공제 가능한 부가세
    memo TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS expenses_date ON expenses(date);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS import_log (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    source TEXT, filename TEXT, kind TEXT,
    inserted INTEGER, duplicates INTEGER, errors INTEGER, message TEXT
);
"""

DEFAULT_SETTINGS = {
    "company_name": "위드케이키",
    "vat_rate": "10",
    # cost_mode: purchase = 매출원가를 매입 공급가 합계로 계산 / product = 판매수량 × 품목 원가로 계산
    "cost_mode": "product",
    "expense_categories": "인건비,임차료,운반비,광고선전비,수수료,소모품비,통신비,복리후생비,세금과공과,기타",
}


# ───────────────────────── DB ─────────────────────────

def connect(path=None):
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextlib.contextmanager
def db(path=None):
    """요청 하나 동안 쓰는 연결: 성공하면 커밋, 예외면 롤백, 항상 닫는다."""
    conn = connect(path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db(path=None):
    path = path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with db(path) as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES ('api_key', ?)",
                     (secrets.token_urlsafe(24),))
        erp.init(conn)


def get_settings(conn):
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def rows(cur):
    return [dict(r) for r in cur]


# ───────────────────────── 값 파싱 ─────────────────────────

def parse_number(v):
    """'1,234,000원', '₩ 12,000', '(3,000)', 12000.0 → 숫자. 비어 있으면 None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    s = str(v).strip()
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^0-9.\-]", "", s)
    if s in ("", "-", ".", "-."):
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return -abs(n) if neg else n


def parse_date(v, default=None):
    if v is None or v == "":
        return default
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (int, float)) and 20000 < v < 80000:  # 엑셀 일련번호
        return (dt.date(1899, 12, 30) + dt.timedelta(days=int(v))).isoformat()
    s = str(v).strip()
    m = (re.match(r"^(\d{4})[-./년\s]+(\d{1,2})[-./월\s]+(\d{1,2})", s)
         or re.match(r"^(\d{4})(\d{2})(\d{2})", s))
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.match(r"^(\d{2})[-./](\d{1,2})[-./](\d{1,2})$", s)  # 26.10.24
        if not m:
            return default
        y, mo, d = 2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return default


def won(n):
    return int(round(n or 0))


# ───────────────────────── 거래처/품목 조회 ─────────────────────────

def find_or_create_client(conn, name, kind):
    name = (name or "").strip()
    if not name:
        return None
    r = conn.execute("SELECT id, type FROM clients WHERE name = ?", (name,)).fetchone()
    want = "customer" if kind == "sale" else "vendor"
    if r:
        if r["type"] not in (want, "both"):
            conn.execute("UPDATE clients SET type = 'both' WHERE id = ?", (r["id"],))
        return r["id"]
    return conn.execute("INSERT INTO clients(name, type) VALUES (?, ?)", (name, want)).lastrowid


def find_product(conn, name=None, code=None):
    if code:
        r = conn.execute("SELECT * FROM products WHERE code = ? AND code != ''", (str(code).strip(),)).fetchone()
        if r:
            return r
    if name:
        return conn.execute("SELECT * FROM products WHERE name = ?", (name.strip(),)).fetchone()
    return None


def lookup_price(conn, kind, client_id, product):
    if product is None:
        return None
    if kind == "sale" and client_id:
        r = conn.execute("SELECT price FROM client_prices WHERE client_id = ? AND product_id = ?",
                         (client_id, product["id"])).fetchone()
        if r:
            return r["price"]
    price = product["sale_price"] if kind == "sale" else product["cost_price"]
    return price or None


def build_entry(conn, kind, row, settings, default_date=None):
    """수기 입력·파일·API 공통: 원본 행(dict) → entries 테이블에 넣을 값. 실패 시 ValueError."""
    if kind not in KINDS:
        raise ValueError("kind 는 sale 또는 purchase")
    date = parse_date(row.get("date"), default_date)
    if not date:
        raise ValueError("날짜를 읽을 수 없음: %r" % (row.get("date"),))
    item = str(row.get("item") or "").strip()
    code = str(row.get("code") or "").strip()
    client_id = row.get("client_id") or find_or_create_client(conn, row.get("client"), kind)

    product = None
    if row.get("product_id"):
        product = conn.execute("SELECT * FROM products WHERE id = ?", (row["product_id"],)).fetchone()
    if product is None and (item or code):
        product = find_product(conn, item, code)
    if product is not None and not item:
        item = product["name"]

    qty = parse_number(row.get("qty"))
    unit_price = parse_number(row.get("unit_price"))
    supply = parse_number(row.get("supply"))
    vat = parse_number(row.get("vat"))
    total = parse_number(row.get("total"))
    rate = float(settings.get("vat_rate") or 10) / 100.0
    no_vat = str(row.get("vat_free") or "").lower() in ("1", "true", "yes", "y", "면세")

    if unit_price is None:
        unit_price = lookup_price(conn, kind, client_id, product)
    if qty is None:
        qty = 1 if unit_price is None or supply is None else (supply / unit_price if unit_price else 1)

    if supply is None:
        if unit_price is not None:
            supply = qty * unit_price
        elif total is not None:
            supply = total if no_vat else total / (1 + rate)
        else:
            raise ValueError("금액을 알 수 없음 (단가·공급가액·합계 중 하나 필요, 또는 품목 단가 등록)")
    if vat is None:
        if total is not None and not no_vat:
            vat = total - supply
        else:
            vat = 0 if no_vat else supply * rate
    if total is None:
        total = supply + vat
    if unit_price is None:
        unit_price = supply / qty if qty else supply

    if product is None and item:
        # 처음 보는 품목은 단가표에 자동 등록 (원가는 나중에 채우면 보고서에 바로 반영)
        pid = conn.execute(
            "INSERT INTO products(code, name, sale_price, cost_price) VALUES (?, ?, ?, ?)",
            (code, item, won(unit_price) if kind == "sale" else 0, won(unit_price) if kind == "purchase" else 0),
        ).lastrowid
        product = conn.execute("SELECT * FROM products WHERE id = ?", (pid,)).fetchone()
    elif product is not None and kind == "purchase" and not product["cost_price"] and unit_price:
        conn.execute("UPDATE products SET cost_price = ? WHERE id = ?", (won(unit_price), product["id"]))

    unit_cost = parse_number(row.get("unit_cost"))
    paid = row.get("paid")
    paid = 1 if str(paid).strip().lower() in ("1", "true", "yes", "y", "완료", "입금", "지급", "o") else 0
    return {
        "kind": kind, "date": date, "client_id": client_id,
        "product_id": product["id"] if product is not None else None,
        "item_name": item, "qty": qty, "unit_price": won(unit_price),
        "supply": won(supply), "vat": won(vat), "total": won(total),
        "unit_cost": won(unit_cost) if unit_cost is not None else None,
        "paid": paid, "order_no": str(row.get("order_no") or "").strip(),
        "memo": str(row.get("memo") or "").strip(),
    }


ENTRY_COLS = ("kind", "date", "client_id", "product_id", "item_name", "qty", "unit_price", "supply",
              "vat", "total", "unit_cost", "paid", "order_no", "memo")


def insert_entry(conn, e, source="manual", dedupe_key=None):
    """중복키가 이미 있으면 None 반환."""
    cols = ENTRY_COLS + ("source", "dedupe_key")
    vals = [e[c] for c in ENTRY_COLS] + [source, dedupe_key]
    cur = conn.execute(
        "INSERT OR IGNORE INTO entries(%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))), vals)
    return cur.lastrowid if cur.rowcount else None


def ingest_rows(conn, kind, raw_rows, source, default_date=None, filename=None):
    """여러 행을 한 번에 넣는다. 같은 행을 다시 넣어도 중복 등록되지 않는다."""
    settings = get_settings(conn)
    result = {"inserted": 0, "duplicates": 0, "errors": []}
    seen = {}
    for i, raw in enumerate(raw_rows):
        try:
            with conn:  # 행 단위 트랜잭션: 실패한 행이 자동 생성한 거래처/품목도 되돌림
                e = build_entry(conn, kind, raw, settings, default_date)
                if e["order_no"]:
                    base = "o|%s|%s" % (e["order_no"], e["item_name"])
                else:
                    base = "h|" + hashlib.sha1(json.dumps(
                        [e["date"], e["client_id"], e["item_name"], e["qty"], e["supply"]]).encode()).hexdigest()
                seen[base] = seen.get(base, 0) + 1
                key = "%s|%d" % (base, seen[base])
                if insert_entry(conn, e, source, key):
                    result["inserted"] += 1
                else:
                    result["duplicates"] += 1
        except ValueError as ex:
            result["errors"].append({"row": raw.get("_line", i + 1), "error": str(ex)})
    with conn:
        conn.execute(
            "INSERT INTO import_log(source, filename, kind, inserted, duplicates, errors, message) VALUES (?,?,?,?,?,?,?)",
            (source, filename or "", kind, result["inserted"], result["duplicates"], len(result["errors"]),
             "; ".join("%s행: %s" % (x["row"], x["error"]) for x in result["errors"][:5])))
    return result


# ───────────────────────── 파일 가져오기 ─────────────────────────

FIELD_SYNONYMS = {
    "order_no": ["주문번호", "발주번호", "거래번호", "전표번호", "주문코드", "승인번호", "orderno", "orderid", "order_no", "주문id"],
    "date": ["일자", "날짜", "거래일", "거래일자", "주문일", "주문일자", "주문일시", "판매일", "판매일자", "매출일", "매출일자",
             "매입일", "매입일자", "출고일", "출고일자", "발주일", "발주일자", "작성일자", "결제일", "date", "orderdate"],
    "client": ["거래처", "거래처명", "업체", "업체명", "상호", "상호명", "고객", "고객명", "고객사", "판매처", "매입처", "구매처",
               "발주처", "납품처", "공급받는자상호", "공급자상호", "주문자", "구매자", "client", "customer", "vendor", "company"],
    "code": ["품목코드", "상품코드", "제품코드", "sku", "바코드"],
    "item": ["품목", "품목명", "품명", "상품", "상품명", "제품", "제품명", "item", "product", "productname"],
    "qty": ["수량", "주문수량", "판매수량", "출고수량", "매입수량", "qty", "quantity", "ea"],
    "unit_price": ["단가", "판매단가", "매입단가", "공급단가", "개당가격", "price", "unitprice"],
    "supply": ["공급가액", "공급가", "공급금액", "판매금액", "매출금액", "매출액", "매입금액", "금액", "amount", "supply"],
    "vat": ["부가세", "세액", "부가가치세", "vat", "tax"],
    "total": ["합계", "합계금액", "총액", "총금액", "결제금액", "주문금액", "청구금액", "실결제금액", "total"],
    "paid": ["입금여부", "결제여부", "지급여부", "입금상태", "paid"],
    "memo": ["비고", "메모", "memo", "note", "적요"],
}
FIELD_LABELS = {"date": "일자", "client": "거래처", "code": "품목코드", "item": "품목", "qty": "수량",
                "unit_price": "단가", "supply": "공급가액", "vat": "부가세", "total": "합계",
                "order_no": "주문번호", "paid": "입금여부", "memo": "비고"}


def _norm_header(h):
    return re.sub(r"[\s_()\[\]·.\-/:]", "", str(h or "")).lower()


def guess_mapping(headers):
    norm = [_norm_header(h) for h in headers]
    mapping, used = {}, set()
    for field, syns in FIELD_SYNONYMS.items():  # 1) 정확히 일치
        for idx, h in enumerate(norm):
            if idx not in used and h in [_norm_header(s) for s in syns]:
                mapping[field] = idx
                used.add(idx)
                break
    for field, syns in FIELD_SYNONYMS.items():  # 2) 포함 관계 (긴 동의어부터)
        if field in mapping:
            continue
        for s in sorted((_norm_header(x) for x in syns), key=len, reverse=True):
            if len(s) < 2:
                continue
            hit = next((i for i, h in enumerate(norm) if i not in used and s in h), None)
            if hit is not None:
                mapping[field] = hit
                used.add(hit)
                break
    return mapping


def read_table(filename, data):
    """CSV/XLSX → 2차원 리스트."""
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise ValueError("엑셀 파일을 읽으려면 'pip install openpyxl' 을 먼저 실행하세요 (또는 CSV로 저장해서 올리세요).")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    if ext == ".xls":
        raise ValueError("구형 .xls 파일은 지원하지 않습니다. 엑셀에서 .xlsx 또는 CSV로 다시 저장해 주세요.")
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("파일 인코딩을 알 수 없습니다 (UTF-8 또는 CP949 CSV를 사용하세요).")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except csv.Error:
        dialect = csv.excel
    return [r for r in csv.reader(io.StringIO(text), dialect)]


def find_header_row(table):
    best, best_score = 0, -1
    for i, r in enumerate(table[:15]):
        score = len(guess_mapping(r))
        if score > best_score:
            best, best_score = i, score
    return best


def table_to_rows(table, mapping=None):
    if not table:
        raise ValueError("빈 파일입니다.")
    hi = find_header_row(table)
    headers = ["" if h is None else str(h).strip() for h in table[hi]]
    mapping = mapping if mapping is not None else guess_mapping(headers)
    out = []
    for n, r in enumerate(table[hi + 1:], start=hi + 2):
        if not any(c not in (None, "") and str(c).strip() for c in r):
            continue
        row = {"_line": n}
        for field, idx in mapping.items():
            if idx is not None and 0 <= int(idx) < len(r):
                v = r[int(idx)]
                row[field] = v.strip() if isinstance(v, str) else v
        # '합계' 같은 요약 행은 건너뜀
        label = " ".join(str(c) for c in r if c not in (None, ""))
        if re.match(r"^\s*(총\s*)?(합\s*계|소\s*계|total)\b", label, re.I) and not row.get("client"):
            continue
        out.append(row)
    return headers, mapping, out


def date_from_filename(name):
    m = re.search(r"(20\d{2})[-_.]?(\d{2})[-_.]?(\d{2})", name or "")
    return parse_date("%s-%s-%s" % m.groups()) if m else None


def import_file(conn, kind, filename, data, source, mapping=None, preview=False):
    table = read_table(filename, data)
    headers, mapping, raw_rows = table_to_rows(table, mapping)
    default_date = date_from_filename(filename) or dt.date.today().isoformat()
    if "date" not in mapping:
        note = "일자 열이 없어 %s 로 기록합니다." % default_date
    else:
        note = ""
    if preview:
        return {"headers": headers, "mapping": mapping, "rows": len(raw_rows), "note": note,
                "fields": FIELD_LABELS,
                "sample": [{k: (v if isinstance(v, (int, float, str)) or v is None else str(v))
                            for k, v in r.items()} for r in raw_rows[:8]]}
    if not any(f in mapping for f in ("supply", "total", "unit_price", "item", "code")):
        raise ValueError("금액 또는 품목 열을 찾지 못했습니다. 열 지정을 확인해 주세요.")
    if kind == "order":
        res = erp.ingest_orders(conn, raw_rows, source, default_date, filename)
    else:
        res = ingest_rows(conn, kind, raw_rows, source, default_date, filename)
    res["note"] = note
    return res


def scan_inbox(db_path=None, inbox=None):
    """inbox/주문, inbox/매출, inbox/매입 폴더에 떨어진 파일을 자동으로 가져와 done/ 또는 error/ 로 옮긴다."""
    inbox = inbox or INBOX_DIR
    results = []
    for kind, sub in (("order", "주문"), ("sale", "매출"), ("purchase", "매입")):
        d = os.path.join(inbox, sub)
        os.makedirs(os.path.join(d, "done"), exist_ok=True)
        os.makedirs(os.path.join(d, "error"), exist_ok=True)
        for name in sorted(os.listdir(d)):
            p = os.path.join(d, name)
            if not os.path.isfile(p) or name.startswith((".", "~$")):
                continue
            if os.path.splitext(name)[1].lower() not in (".csv", ".txt", ".tsv", ".xlsx", ".xlsm"):
                continue
            if time.time() - os.path.getmtime(p) < 5:  # 아직 저장/복사 중일 수 있음
                continue
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-")
            try:
                with open(p, "rb") as f:
                    data = f.read()
                with db(db_path) as conn:
                    res = import_file(conn, kind, name, data, "folder")
                dest = "done"
            except Exception as ex:  # noqa: BLE001 — 어떤 파일이든 감시 스레드는 계속 돌아야 함
                res = {"error": str(ex)}
                dest = "error"
                with db(db_path) as conn:
                    conn.execute("INSERT INTO import_log(source, filename, kind, inserted, duplicates, errors, message)"
                                 " VALUES ('folder', ?, ?, 0, 0, 1, ?)", (name, kind, str(ex)))
            shutil.move(p, os.path.join(d, dest, stamp + name))
            results.append({"file": name, "kind": kind, **res})
    return results


def inbox_watcher(interval, db_path=None):
    while True:
        try:
            for r in scan_inbox(db_path):
                print("[자동 가져오기]", r.get("file"), {k: v for k, v in r.items() if k != "file"}, flush=True)
        except Exception as ex:  # noqa: BLE001
            print("[자동 가져오기 오류]", ex, file=sys.stderr, flush=True)
        time.sleep(interval)


# ───────────────────────── 보고서 ─────────────────────────

def summary(conn, date_from, date_to):
    s = get_settings(conn)
    p = (date_from, date_to)

    def one(sql, args=p):
        return conn.execute(sql, args).fetchone()[0] or 0

    sales = one("SELECT SUM(supply) FROM entries WHERE kind='sale' AND date BETWEEN ? AND ?")
    sales_vat = one("SELECT SUM(vat) FROM entries WHERE kind='sale' AND date BETWEEN ? AND ?")
    purchases = one("SELECT SUM(supply) FROM entries WHERE kind='purchase' AND date BETWEEN ? AND ?")
    purchase_vat = one("SELECT SUM(vat) FROM entries WHERE kind='purchase' AND date BETWEEN ? AND ?")
    expenses = one("SELECT SUM(amount) FROM expenses WHERE date BETWEEN ? AND ?")
    expense_vat = one("SELECT SUM(vat) FROM expenses WHERE date BETWEEN ? AND ?")
    product_cost = one("""SELECT SUM(e.qty * COALESCE(e.unit_cost, p.cost_price, 0)) FROM entries e
                          LEFT JOIN products p ON p.id = e.product_id
                          WHERE e.kind='sale' AND e.date BETWEEN ? AND ?""")
    missing_cost = one("""SELECT COUNT(*) FROM entries e LEFT JOIN products p ON p.id = e.product_id
                          WHERE e.kind='sale' AND e.date BETWEEN ? AND ?
                          AND e.unit_cost IS NULL AND COALESCE(p.cost_price, 0) = 0""")
    cogs = purchases if s.get("cost_mode") == "purchase" else product_cost
    gross = sales - cogs
    net = gross - expenses

    # 월별 추이
    monthly = {}
    for r in conn.execute("""SELECT substr(e.date,1,7) m, e.kind, SUM(e.supply) amt,
                             SUM(CASE WHEN e.kind='sale' THEN e.qty * COALESCE(e.unit_cost, p.cost_price, 0) END) cost
                             FROM entries e LEFT JOIN products p ON p.id = e.product_id
                             WHERE e.date BETWEEN ? AND ? GROUP BY m, e.kind""", p):
        d = monthly.setdefault(r["m"], {"month": r["m"], "sales": 0, "purchases": 0, "expenses": 0, "product_cost": 0})
        if r["kind"] == "sale":
            d["sales"] = r["amt"] or 0
            d["product_cost"] = r["cost"] or 0
        else:
            d["purchases"] = r["amt"] or 0
    for r in conn.execute("SELECT substr(date,1,7) m, SUM(amount) amt FROM expenses WHERE date BETWEEN ? AND ? GROUP BY m", p):
        d = monthly.setdefault(r["m"], {"month": r["m"], "sales": 0, "purchases": 0, "expenses": 0, "product_cost": 0})
        d["expenses"] = r["amt"] or 0
    months = []
    for m in sorted(monthly):
        d = monthly[m]
        c = d["purchases"] if s.get("cost_mode") == "purchase" else d["product_cost"]
        d["cogs"] = won(c)
        d["product_cost"] = won(d["product_cost"])
        d["net"] = won(d["sales"] - c - d["expenses"])
        months.append(d)

    daily = rows(conn.execute("""SELECT date, SUM(supply) sales, COUNT(*) cnt FROM entries
                                 WHERE kind='sale' AND date BETWEEN ? AND ? GROUP BY date ORDER BY date DESC LIMIT 31""", p))
    top_clients = rows(conn.execute("""SELECT COALESCE(c.name,'(미지정)') name, SUM(e.supply) sales, COUNT(*) cnt
                                       FROM entries e LEFT JOIN clients c ON c.id = e.client_id
                                       WHERE e.kind='sale' AND e.date BETWEEN ? AND ?
                                       GROUP BY e.client_id ORDER BY sales DESC LIMIT 10""", p))
    top_products = rows(conn.execute("""SELECT COALESCE(NULLIF(e.item_name,''),'(미지정)') name, SUM(e.qty) qty, SUM(e.supply) sales,
                                        SUM(e.supply - e.qty * COALESCE(e.unit_cost, pr.cost_price, 0)) margin
                                        FROM entries e LEFT JOIN products pr ON pr.id = e.product_id
                                        WHERE e.kind='sale' AND e.date BETWEEN ? AND ?
                                        GROUP BY name ORDER BY sales DESC LIMIT 10""", p))
    expense_by_cat = rows(conn.execute("""SELECT category, SUM(amount) amount FROM expenses WHERE date BETWEEN ? AND ?
                                          GROUP BY category ORDER BY amount DESC""", p))
    # 미수금/미지급금은 기간과 무관하게 전체 기준
    receivables = rows(conn.execute("""SELECT COALESCE(c.name,'(미지정)') name, SUM(e.total) amount, COUNT(*) cnt, MIN(e.date) since
                                       FROM entries e LEFT JOIN clients c ON c.id = e.client_id
                                       WHERE e.kind='sale' AND e.paid = 0 GROUP BY e.client_id ORDER BY amount DESC""", ()))
    payables = conn.execute("SELECT SUM(total) FROM entries WHERE kind='purchase' AND paid = 0").fetchone()[0] or 0
    return {
        "from": date_from, "to": date_to, "cost_mode": s.get("cost_mode"),
        "sales": won(sales), "sales_vat": won(sales_vat), "purchases": won(purchases),
        "product_cost": won(product_cost), "cogs": won(cogs), "gross_profit": won(gross),
        "expenses": won(expenses), "net_profit": won(net),
        "gross_margin": round(gross / sales * 100, 1) if sales else None,
        "net_margin": round(net / sales * 100, 1) if sales else None,
        "vat_payable": won(sales_vat - purchase_vat - expense_vat),
        "missing_cost_count": missing_cost,
        "receivables_total": won(sum(r["amount"] or 0 for r in receivables)),
        "payables_total": won(payables),
        "monthly": months, "daily": daily, "top_clients": top_clients, "top_products": top_products,
        "expense_by_category": expense_by_cat, "receivables": receivables,
    }


# ───────────────────────── HTTP ─────────────────────────

ENTRY_SELECT = """SELECT e.*, c.name client_name,
                  e.qty * COALESCE(e.unit_cost, p.cost_price, 0) cost_amount
                  FROM entries e LEFT JOIN clients c ON c.id = e.client_id
                  LEFT JOIN products p ON p.id = e.product_id"""

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def month_range(today=None):
    today = today or dt.date.today()
    first = today.replace(day=1)
    nxt = (first + dt.timedelta(days=32)).replace(day=1)
    return first.isoformat(), (nxt - dt.timedelta(days=1)).isoformat()


class Handler(BaseHTTPRequestHandler):
    server_version = "WithKeikiAccounting/1.0"
    db_path = None
    password = None

    def log_message(self, fmt, *args):
        if os.environ.get("ACCOUNTING_VERBOSE"):
            super().log_message(fmt, *args)

    # --- 응답 도우미
    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(self, body, ctype, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            from urllib.parse import quote
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + quote(filename))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 30 * 1024 * 1024:
            raise ApiError(413, "요청이 너무 큽니다.")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "JSON 형식이 아닙니다.")

    def authorized(self, path):
        if path.startswith("/api/ingest"):
            return True  # API 키로 따로 검사
        if not self.password:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                _, pw = base64.b64decode(h[6:]).decode("utf-8").split(":", 1)
                return secrets.compare_digest(pw, self.password)
            except Exception:  # noqa: BLE001
                return False
        return False

    def handle_any(self, method):
        url = urlparse(self.path)
        path = url.path
        q = {k: v[-1] for k, v in parse_qs(url.query).items()}
        if not self.authorized(path):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="accounting", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            if path.startswith("/print/"):
                with db(self.db_path) as conn:
                    d_from, d_to = q.get("from") or month_range()[0], q.get("to") or month_range()[1]
                    page = erp.render_print(conn, path.strip("/").split("/")[1:], q, d_from, d_to)
                return self.send_bytes(page.encode("utf-8"), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "허용되지 않는 요청")
                return self.serve_static(path)
            with db(self.db_path) as conn:
                result = self.route(conn, method, path, q)
            if result is not None:
                self.send_json(result)
        except ApiError as ex:
            self.send_json({"error": str(ex)}, ex.status)
        except ValueError as ex:
            self.send_json({"error": str(ex)}, 400)
        except sqlite3.IntegrityError as ex:
            msg = str(ex)
            if "UNIQUE" in msg:
                msg = "이미 같은 이름(또는 항목)이 등록되어 있습니다."
            self.send_json({"error": msg}, 409)

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    def do_DELETE(self):
        self.handle_any("DELETE")

    def serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        full = os.path.normpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "없는 페이지")
        with open(full, "rb") as f:
            self.send_bytes(f.read(), MIME.get(os.path.splitext(full)[1], "application/octet-stream"))

    # --- 라우팅
    def route(self, conn, method, path, q):
        parts = path.strip("/").split("/")[1:]  # api/... 제거
        res = parts[0] if parts else ""
        rid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        d_from = q.get("from") or month_range()[0]
        d_to = q.get("to") or month_range()[1]

        if res == "summary" and method == "GET":
            return {**summary(conn, d_from, d_to), **erp.dashboard(conn)}

        if res == "settings":
            if method == "GET":
                return get_settings(conn)
            if method == "PUT":
                body = self.read_json()
                for k in ("company_name", "vat_rate", "cost_mode", "expense_categories") + tuple(erp.SETTINGS):
                    if k in body:
                        conn.execute("UPDATE settings SET value = ? WHERE key = ?", (str(body[k]), k))
                if body.get("regenerate_api_key"):
                    conn.execute("UPDATE settings SET value = ? WHERE key = 'api_key'", (secrets.token_urlsafe(24),))
                return get_settings(conn)

        if res == "clients":
            return self.crud(conn, method, rid, "clients", ("name", "type", "biz_no", "contact", "memo"),
                             "SELECT c.*, (SELECT SUM(total) FROM entries e WHERE e.client_id=c.id AND e.kind='sale' AND e.paid=0) receivable "
                             "FROM clients c ORDER BY name", required="name")
        if res == "products":
            if method in ("POST", "PUT"):
                self._body = self.read_json()
                if self._body.get("kind") not in erp.PRODUCT_KINDS:
                    self._body["kind"] = "product"
            return self.crud(conn, method, rid, "products",
                             ("code", "name", "unit", "kind", "sale_price", "cost_price", "safety_stock", "memo"),
                             "SELECT p.*, (SELECT SUM(b.qty * m.cost_price) FROM bom b JOIN products m ON m.id = b.material_id"
                             " WHERE b.product_id = p.id) bom_cost FROM products p ORDER BY p.name", required="name",
                             ints=("sale_price", "cost_price"), floats=("safety_stock",))
        if res == "client-prices":
            if method == "GET":
                return rows(conn.execute("""SELECT cp.*, c.name client_name, p.name product_name, p.sale_price
                                            FROM client_prices cp JOIN clients c ON c.id=cp.client_id
                                            JOIN products p ON p.id=cp.product_id ORDER BY c.name, p.name"""))
            if method == "POST":
                b = self.read_json()
                conn.execute("""INSERT INTO client_prices(client_id, product_id, price) VALUES (?,?,?)
                                ON CONFLICT(client_id, product_id) DO UPDATE SET price=excluded.price""",
                             (int(b["client_id"]), int(b["product_id"]), won(parse_number(b["price"]))))
                return {"ok": True}
            if method == "DELETE" and rid:
                conn.execute("DELETE FROM client_prices WHERE id = ?", (rid,))
                return {"ok": True}

        if res == "price" and method == "GET":  # 입력 화면에서 단가 자동 채움
            kind = q.get("kind", "sale")
            product = conn.execute("SELECT * FROM products WHERE id = ?", (q.get("product_id"),)).fetchone()
            return {"price": lookup_price(conn, kind, int(q.get("client_id") or 0) or None, product)}

        if res == "entries":
            return self.entries(conn, method, rid, q, d_from, d_to)

        if res == "expenses":
            if method == "GET":
                return rows(conn.execute("SELECT * FROM expenses WHERE date BETWEEN ? AND ? ORDER BY date DESC, id DESC",
                                         (d_from, d_to)))
            b = self.read_json() if method in ("POST", "PUT") else {}
            if method in ("POST", "PUT"):
                date = parse_date(b.get("date"))
                if not date:
                    raise ValueError("날짜를 입력하세요.")
                amount = parse_number(b.get("amount")) or 0
                vat = parse_number(b.get("vat"))
                vals = (date, b.get("category") or "기타", b.get("description") or "", won(amount),
                        won(vat or 0), b.get("memo") or "")
                if method == "POST":
                    conn.execute("INSERT INTO expenses(date, category, description, amount, vat, memo) VALUES (?,?,?,?,?,?)", vals)
                else:
                    conn.execute("UPDATE expenses SET date=?, category=?, description=?, amount=?, vat=?, memo=? WHERE id=?",
                                 vals + (rid,))
                return {"ok": True}
            if method == "DELETE" and rid:
                conn.execute("DELETE FROM expenses WHERE id = ?", (rid,))
                return {"ok": True}

        if res == "import" and method == "POST":
            b = self.read_json()
            kind = b.get("kind", "sale")
            if kind not in KINDS + ("order",):
                raise ValueError("kind 오류")
            try:
                data = base64.b64decode(b.get("content_b64") or "")
            except ValueError:
                raise ValueError("파일 내용을 읽을 수 없습니다.")
            mapping = b.get("mapping")
            if mapping is not None:
                mapping = {k: int(v) for k, v in mapping.items() if v not in (None, "", -1, "-1")}
            return import_file(conn, kind, b.get("filename") or "upload.csv", data, "import",
                               mapping, preview=bool(b.get("preview")))

        if res == "import-log" and method == "GET":
            return rows(conn.execute("SELECT * FROM import_log ORDER BY id DESC LIMIT 50"))

        if res == "scan-inbox" and method == "POST":
            return scan_inbox(self.db_path)

        if res == "ingest" and method == "POST":
            key = self.headers.get("X-API-Key") or q.get("key") or ""
            if not secrets.compare_digest(key, get_settings(conn).get("api_key", "")):
                raise ApiError(401, "API 키가 올바르지 않습니다.")
            b = self.read_json()
            kind = b.get("kind", "sale")
            if kind not in KINDS + ("order",):
                raise ValueError("kind 는 sale, purchase 또는 order")
            items = b.get("rows") if isinstance(b.get("rows"), list) else [b]
            if kind == "order":
                return erp.ingest_orders(conn, items, "api", dt.date.today().isoformat(), b.get("source_name") or "API")
            return ingest_rows(conn, kind, items, "api", dt.date.today().isoformat(), b.get("source_name") or "API")

        if res == "export" and method == "GET":
            return self.export(conn, q.get("type", "sale"), d_from, d_to)

        if res == "backup" and method == "GET":
            tmp = sqlite3.connect(":memory:")
            conn.backup(tmp)
            data = "\n".join(tmp.iterdump()).encode("utf-8")
            self.send_bytes(data, "application/sql", "accounting-backup-%s.sql" % dt.date.today().isoformat())
            return None

        result = erp.route(self, conn, method, parts, q, d_from, d_to)
        if result is not erp.NOT_FOUND:
            return result
        raise ApiError(404, "없는 API: %s %s" % (method, path))

    def crud(self, conn, method, rid, table, fields, list_sql, required=None, ints=(), floats=()):
        if method == "GET":
            return rows(conn.execute(list_sql))
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM %s WHERE id = ?" % table, (rid,))
            return {"ok": True}
        if method in ("POST", "PUT"):
            b = getattr(self, "_body", None) or self.read_json()
            if required and not str(b.get(required) or "").strip():
                raise ValueError("이름을 입력하세요.")
            vals = []
            for f in fields:
                v = b.get(f)
                if f in ints:
                    v = won(parse_number(v))
                elif f in floats:
                    v = parse_number(v) or 0
                else:
                    v = "" if v is None else str(v).strip()
                vals.append(v)
            if method == "POST":
                new = conn.execute("INSERT INTO %s(%s) VALUES (%s)" % (table, ",".join(fields), ",".join("?" * len(fields))),
                                   vals).lastrowid
                return {"ok": True, "id": new}
            if rid:
                conn.execute("UPDATE %s SET %s WHERE id = ?" % (table, ",".join(f + "=?" for f in fields)), vals + [rid])
                return {"ok": True}
        raise ApiError(405, "허용되지 않는 요청")

    def entries(self, conn, method, rid, q, d_from, d_to):
        if method == "GET":
            sql = ENTRY_SELECT + " WHERE e.kind = ? AND e.date BETWEEN ? AND ?"
            args = [q.get("kind", "sale"), d_from, d_to]
            if q.get("q"):
                sql += " AND (c.name LIKE ? OR e.item_name LIKE ? OR e.order_no LIKE ? OR e.memo LIKE ?)"
                args += ["%" + q["q"] + "%"] * 4
            if q.get("unpaid"):
                sql += " AND e.paid = 0"
            sql += " ORDER BY e.date DESC, e.id DESC LIMIT 2000"
            return rows(conn.execute(sql, args))
        if method == "DELETE" and rid:
            conn.execute("DELETE FROM entries WHERE id = ?", (rid,))
            return {"ok": True}
        if method == "POST" and rid is None and q.get("action") == "paid":
            b = self.read_json()
            ids = [int(i) for i in b.get("ids", [])]
            conn.executemany("UPDATE entries SET paid = ? WHERE id = ?", [(1 if b.get("paid", True) else 0, i) for i in ids])
            return {"ok": True, "updated": len(ids)}
        if method in ("POST", "PUT"):
            b = self.read_json()
            kind = b.get("kind") or q.get("kind") or "sale"
            e = build_entry(conn, kind, b, get_settings(conn))
            if method == "POST":
                return {"ok": True, "id": insert_entry(conn, e)}
            if rid:
                conn.execute("UPDATE entries SET %s WHERE id = ?" % ",".join(c + "=?" for c in ENTRY_COLS),
                             [e[c] for c in ENTRY_COLS] + [rid])
                return {"ok": True}
        raise ApiError(405, "허용되지 않는 요청")

    def export(self, conn, typ, d_from, d_to):
        buf = io.StringIO()
        w = csv.writer(buf)
        if typ in KINDS:
            w.writerow(["일자", "거래처", "품목", "수량", "단가", "공급가액", "부가세", "합계", "원가",
                        "입금/지급", "주문번호", "입력경로", "비고"])
            for r in conn.execute(ENTRY_SELECT + " WHERE e.kind=? AND e.date BETWEEN ? AND ? ORDER BY e.date, e.id",
                                  (typ, d_from, d_to)):
                w.writerow([r["date"], r["client_name"] or "", r["item_name"], r["qty"], r["unit_price"], r["supply"],
                            r["vat"], r["total"], won(r["cost_amount"]), "완료" if r["paid"] else "미완료",
                            r["order_no"], r["source"], r["memo"]])
            label = "매출" if typ == "sale" else "매입"
        elif typ == "expense":
            w.writerow(["일자", "계정", "내용", "금액", "부가세", "비고"])
            for r in conn.execute("SELECT * FROM expenses WHERE date BETWEEN ? AND ? ORDER BY date, id", (d_from, d_to)):
                w.writerow([r["date"], r["category"], r["description"], r["amount"], r["vat"], r["memo"]])
            label = "비용"
        elif typ == "pl":
            s = summary(conn, d_from, d_to)
            w.writerow(["항목", "금액"])
            for k, v in (("매출액", s["sales"]), ("매출원가", s["cogs"]), ("매출총이익", s["gross_profit"]),
                         ("판매비와관리비", s["expenses"]), ("영업이익(순이익)", s["net_profit"]),
                         ("매입 합계(공급가)", s["purchases"]), ("예상 납부 부가세", s["vat_payable"])):
                w.writerow([k, v])
            w.writerow([])
            w.writerow(["월", "매출", "매출원가", "비용", "순이익"])
            for m in s["monthly"]:
                w.writerow([m["month"], m["sales"], m["cogs"], m["expenses"], m["net"]])
            label = "손익계산서"
        else:
            raise ValueError("type 오류")
        data = buf.getvalue().encode("utf-8-sig")  # 엑셀에서 한글이 깨지지 않도록 BOM 포함
        self.send_bytes(data, "text/csv; charset=utf-8", "%s_%s_%s.csv" % (label, d_from, d_to))
        return None


erp.bind(sys.modules[__name__])


def make_server(host, port, db_path=None, password=None):
    handler = type("BoundHandler", (Handler,), {"db_path": db_path or DB_PATH, "password": password})
    return ThreadingHTTPServer((host, port), handler)


def main():
    global DB_PATH
    ap = argparse.ArgumentParser(description="위드케이키 회계 프로그램")
    ap.add_argument("--host", default="127.0.0.1", help="사내 다른 PC에서도 쓰려면 0.0.0.0")
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--scan-interval", type=int, default=60, help="inbox 폴더 확인 주기(초), 0이면 끔")
    args = ap.parse_args()
    DB_PATH = os.path.abspath(args.db)
    init_db(DB_PATH)
    password = os.environ.get("ACCOUNTING_PASSWORD") or None
    if args.host not in ("127.0.0.1", "localhost") and not password:
        print("⚠️  외부 접속을 열었는데 ACCOUNTING_PASSWORD 가 없습니다. 같은 네트워크의 누구나 장부를 볼 수 있습니다.")
    if args.scan_interval > 0:
        threading.Thread(target=inbox_watcher, args=(args.scan_interval, DB_PATH), daemon=True).start()
    srv = make_server(args.host, args.port, DB_PATH, password)
    print("회계 프로그램 실행 중 → http://%s:%d  (종료: Ctrl+C)" % ("127.0.0.1" if args.host == "0.0.0.0" else args.host, args.port))
    print("자동 가져오기 폴더:", os.path.join(INBOX_DIR, "매출"), "/", os.path.join(INBOX_DIR, "매입"))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n종료합니다.")


if __name__ == "__main__":
    main()
