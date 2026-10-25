"""python3 -m unittest test_erp  (accounting 폴더에서 실행)"""
import base64
import os
import sqlite3
import urllib.request

import server
from test_server import SAMPLE, ServerCase


class ErpTest(ServerCase):
    def setup_products(self):
        ids = {}
        for name, kind, sp, cp, safety in (("딸깍키트", "product", 15000, 0, 20), ("케이스", "material", 0, 1200, 0),
                                           ("키캡", "material", 0, 300, 0)):
            ids[name] = self.call("products", {"name": name, "kind": kind, "sale_price": sp, "cost_price": cp,
                                               "safety_stock": safety})[1]["id"]
        return ids

    def stock(self):
        return {s["name"]: s for s in self.call("stock")[1]}

    def test_order_ship_creates_sales_and_moves_stock(self):
        ids = self.setup_products()
        self.call("stock?action=adjust", {"product_id": ids["딸깍키트"], "actual": 100, "date": "2026-10-01"})
        st, r = self.call("orders", {"kind": "sales", "date": "2026-10-20", "client": "행복문구", "due_date": "2026-10-22",
                                     "lines": [{"item": "딸깍키트", "qty": 30}, {"item": "", "qty": ""}]})
        self.assertEqual(st, 200, r)
        oid = r["id"]
        o = self.call("orders/%d" % oid)[1]
        self.assertTrue(o["order_no"].startswith("S-20261020-"))
        self.assertEqual((len(o["lines"]), o["supply"], o["vat"]), (1, 450000, 45000))
        s = self.stock()["딸깍키트"]
        self.assertEqual((s["onhand"], s["reserved"], s["available"]), (100, 30, 70))
        self.assertEqual(self.call("summary?from=2026-10-01&to=2026-10-31")[1]["open_orders"], 1)

        st, r = self.call("orders/%d?action=complete" % oid, {"date": "2026-10-21", "courier": "CJ", "tracking_no": "1234"})
        self.assertEqual(st, 200, r)
        sales = self.call("entries?kind=sale&from=2026-10-01&to=2026-10-31")[1]
        self.assertEqual([(e["date"], e["supply"], e["order_no"], e["source"]) for e in sales],
                         [("2026-10-21", 450000, o["order_no"], "order")])
        s = self.stock()["딸깍키트"]
        self.assertEqual((s["onhand"], s["reserved"]), (70, 0))
        self.assertEqual(self.call("orders/%d?action=complete" % oid, {})[0], 400)  # 두 번 출고 불가
        self.assertEqual(self.call("orders/%d" % oid, {"lines": [{"item": "딸깍키트", "qty": 1}]}, "PUT")[0], 400)

        self.call("orders/%d?action=reopen" % oid, {})
        self.assertEqual(self.call("entries?kind=sale&from=2026-10-01&to=2026-10-31")[1], [])
        self.assertEqual(self.stock()["딸깍키트"]["onhand"], 100)

    def test_quote_convert_and_cancel(self):
        self.setup_products()
        oid = self.call("orders", {"kind": "sales", "client": "하늘팬시", "status": "quote",
                                   "lines": [{"item": "딸깍키트", "qty": 5}]})[1]["id"]
        self.assertEqual(self.stock()["딸깍키트"]["reserved"], 0)  # 견적은 재고를 잡지 않음
        self.call("orders/%d?action=convert" % oid, {})
        self.assertEqual(self.stock()["딸깍키트"]["reserved"], 5)
        self.call("orders/%d?action=status" % oid, {"status": "cancelled"})
        self.assertEqual(self.stock()["딸깍키트"]["reserved"], 0)

    def test_purchase_order_receive_and_low_stock(self):
        ids = self.setup_products()
        oid = self.call("orders", {"kind": "purchase", "date": "2026-10-02", "client": "부품상사",
                                   "lines": [{"product_id": ids["케이스"], "qty": 500}]})[1]["id"]
        self.assertEqual(self.stock()["케이스"]["incoming"], 500)
        low = {x["name"] for x in self.call("summary")[1]["low_stock"]}
        self.assertIn("딸깍키트", low)  # 안전재고 20, 현재 0
        self.call("orders/%d?action=complete" % oid, {"date": "2026-10-03"})
        p = self.call("entries?kind=purchase&from=2026-10-01&to=2026-10-31")[1][0]
        self.assertEqual((p["supply"], p["client_name"]), (600000, "부품상사"))  # 원가 1200 자동 적용
        self.assertEqual(self.stock()["케이스"]["onhand"], 500)

    def test_production_consumes_bom(self):
        ids = self.setup_products()
        self.call("bom", {"product_id": ids["딸깍키트"], "material_id": ids["케이스"], "qty": 1})
        self.call("bom", {"product_id": ids["딸깍키트"], "material_id": ids["키캡"], "qty": 4})
        self.assertEqual(self.call("bom", {"product_id": ids["딸깍키트"], "material_id": ids["딸깍키트"], "qty": 1})[0], 400)
        self.call("stock?action=adjust", {"product_id": ids["케이스"], "actual": 100})
        self.call("stock?action=adjust", {"product_id": ids["키캡"], "actual": 200, "date": "2026-10-01"})
        plan = self.call("productions?plan=1&product_id=%d&qty=60" % ids["딸깍키트"])[1]
        self.assertEqual({p["name"]: p["short"] for p in plan}, {"케이스": 0, "키캡": 40})
        st, r = self.call("productions", {"date": "2026-10-10", "product_id": ids["딸깍키트"], "qty": 50})
        self.assertEqual((st, r["short"]), (200, []))
        s = self.stock()
        self.assertEqual((s["딸깍키트"]["onhand"], s["케이스"]["onhand"], s["키캡"]["onhand"]), (50, 50, 0))
        prod = {p["name"]: p for p in self.call("products")[1]}
        self.assertEqual(prod["딸깍키트"]["bom_cost"], 1200 + 4 * 300)
        hist = self.call("stock/%d/history" % ids["키캡"])[1]
        self.assertEqual([(h["reason"], h["qty"], h["balance"]) for h in hist], [("생산 투입", -200, 0), ("재고 조정", 200, 200)])
        self.call("productions/%d" % r["id"], method="DELETE")
        self.assertEqual(self.stock()["키캡"]["onhand"], 200)

    def test_import_b2b_file_as_orders(self):
        with open(SAMPLE, "rb") as f:
            raw = f.read()
        body = {"kind": "order", "filename": os.path.basename(SAMPLE), "content_b64": base64.b64encode(raw).decode()}
        st, r = self.call("import", body)
        self.assertEqual((st, r["inserted"], r["duplicates"], r["errors"]), (200, 3, 0, []))  # 4줄 → 주문 3건
        self.assertEqual(self.call("import", body)[1]["duplicates"], 3)
        orders = self.call("orders?kind=sales&status=open")[1]
        two = next(o for o in orders if o["order_no"] == "B2B-24-0002")
        self.assertEqual((two["line_count"], two["supply"], two["client_name"]), (2, 250000, "하늘팬시"))
        self.assertEqual(self.call("entries?kind=sale&from=2026-10-01&to=2026-10-31")[1], [])  # 출고 전엔 매출 아님

    def test_print_documents(self):
        self.setup_products()
        self.call("settings", {"biz_no": "123-45-67890", "ceo": "홍길동", "bank_account": "국민 000-00"}, "PUT")
        oid = self.call("orders", {"kind": "sales", "client": "행복문구", "lines": [{"item": "딸깍키트", "qty": 2}]})[1]["id"]
        base = self.base.replace("/api/", "/print/")
        with urllib.request.urlopen(base + "order/%d" % oid) as r:
            page = r.read().decode()
        self.assertIn("거 래 명 세 서", page)
        self.assertIn("금 삼만삼천원정", page)
        self.assertIn("123-45-67890", page)
        self.call("orders/%d?action=complete" % oid, {"date": "2026-10-05"})
        cid = self.call("clients")[1][0]["id"]
        with urllib.request.urlopen(base + "statement?client_id=%d&from=2026-10-01&to=2026-10-31" % cid) as r:
            self.assertIn("33,000", r.read().decode())

    def test_hangul_amount(self):
        h = server.erp.hangul_amount
        self.assertEqual(h(1540000), "일백오십사만")
        self.assertEqual(h(100000000), "일억")
        self.assertEqual(h(33000), "삼만삼천")
        self.assertEqual(h(1001), "일천일")

    def test_upgrade_old_database(self):
        old = os.path.join(self.tmp.name, "old.db")
        conn = sqlite3.connect(old)
        conn.executescript(server.SCHEMA.replace("CREATE TABLE IF NOT EXISTS orders", "CREATE TABLE IF NOT EXISTS x_orders"))
        conn.execute("INSERT INTO products(name, sale_price) VALUES ('기존품목', 1000)")
        conn.commit()
        conn.close()
        server.init_db(old)
        with server.db(old) as c:
            p = dict(c.execute("SELECT * FROM products").fetchone())
        self.assertEqual((p["kind"], p["safety_stock"]), ("product", 0))

