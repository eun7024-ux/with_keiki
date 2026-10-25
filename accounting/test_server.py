"""python3 -m unittest test_server  (accounting 폴더에서 실행)"""
import base64
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import server

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = os.path.join(HERE, "sample", "b2b_매출_20261024.csv")


class ServerCase(unittest.TestCase):
    """임시 DB로 서버를 띄우는 공통 준비 (test_erp 에서도 사용)"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        server.init_db(self.db)
        self.srv = server.make_server("127.0.0.1", 0, self.db)
        self.base = "http://127.0.0.1:%d/api/" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def call(self, path, body=None, method=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method or ("POST" if data else "GET"),
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def import_sample(self, encoding="utf-8", preview=False):
        with open(SAMPLE, encoding="utf-8") as f:
            raw = f.read().encode(encoding)
        return self.call("import", {"kind": "sale", "filename": os.path.basename(SAMPLE),
                                    "content_b64": base64.b64encode(raw).decode(), "preview": preview})


class AccountingTest(ServerCase):
    def test_parsers(self):
        self.assertEqual(server.parse_number("1,234,000원"), 1234000)
        self.assertEqual(server.parse_number("(3,000)"), -3000)
        self.assertIsNone(server.parse_number(""))
        for v in ("2026-10-24", "2026.10.24", "2026/10/24 13:00", "20261024", "2026년 10월 24일", "26.10.24", 46319):
            self.assertEqual(server.parse_date(v), "2026-10-24", v)
        self.assertIsNone(server.parse_date("합계"))

    def test_import_preview_and_mapping(self):
        st, p = self.import_sample(preview=True)
        self.assertEqual(st, 200, p)
        self.assertEqual(p["rows"], 4)  # 제목줄·합계줄 제외
        m = p["mapping"]
        self.assertEqual(p["headers"][m["order_no"]], "주문번호")
        self.assertEqual(p["headers"][m["client"]], "업체명")
        self.assertEqual(p["headers"][m["total"]], "결제금액")
        self.assertEqual(p["headers"][m["supply"]], "공급가액")

    def test_import_cp949_dedupe_and_summary(self):
        st, r = self.import_sample("cp949")
        self.assertEqual((st, r["inserted"], r["duplicates"], r["errors"]), (200, 4, 0, []))
        st, r = self.import_sample()  # 같은 파일 다시 → 전부 중복
        self.assertEqual((r["inserted"], r["duplicates"]), (0, 4))

        # 원가 입력 후 순이익 계산
        prods = {p["name"]: p for p in self.call("products")[1]}
        self.assertEqual(prods["딸깍키트"]["sale_price"], 15000)
        for name, cost in (("딸깍키트", 8000), ("꾸미 픽캡", 1500)):
            p = prods[name]
            self.call("products/%d" % p["id"], {**p, "cost_price": cost}, "PUT")
        self.call("expenses", {"date": "2026-10-24", "category": "운반비", "amount": 50000, "vat": 5000})
        st, s = self.call("summary?from=2026-10-01&to=2026-10-31")
        self.assertEqual(s["sales"], 810000)
        self.assertEqual(s["cogs"], 42 * 8000 + 60 * 1500)
        self.assertEqual(s["net_profit"], 810000 - 426000 - 50000)
        self.assertEqual(s["vat_payable"], 81000 - 5000)
        self.assertEqual(s["receivables_total"], 891000)
        self.assertEqual(s["missing_cost_count"], 0)
        self.assertEqual(s["monthly"][0]["net"], s["net_profit"])

        ids = [e["id"] for e in self.call("entries?kind=sale&from=2026-10-01&to=2026-10-31")[1]]
        self.call("entries?action=paid", {"ids": ids, "paid": True})
        self.assertEqual(self.call("summary?from=2026-10-01&to=2026-10-31")[1]["receivables_total"], 0)

    def test_manual_entry_uses_client_price(self):
        self.call("products", {"name": "딸깍키트", "sale_price": 15000, "cost_price": 8000})
        self.call("entries", {"kind": "sale", "date": "2026-10-25", "client": "행복문구", "item": "딸깍키트", "qty": 2})
        cid = self.call("clients")[1][0]["id"]
        pid = self.call("products")[1][0]["id"]
        self.call("client-prices", {"client_id": cid, "product_id": pid, "price": 13000})
        self.call("entries", {"kind": "sale", "date": "2026-10-25", "client": "행복문구", "item": "딸깍키트", "qty": 2})
        es = self.call("entries?kind=sale&from=2026-10-01&to=2026-10-31")[1]
        self.assertEqual(sorted((e["supply"], e["vat"], e["total"]) for e in es),
                         [(26000, 2600, 28600), (30000, 3000, 33000)])
        st, r = self.call("entries", {"kind": "sale", "date": "2026-10-25", "item": "없는품목"})
        self.assertEqual(st, 400)  # 금액을 알 수 없음

    def test_total_only_vat_split(self):
        self.call("entries", {"kind": "purchase", "date": "2026-10-25", "client": "원단상사", "item": "원단", "total": 110000})
        e = self.call("entries?kind=purchase&from=2026-10-01&to=2026-10-31")[1][0]
        self.assertEqual((e["supply"], e["vat"], e["total"]), (100000, 10000, 110000))

    def test_ingest_api_key(self):
        st, _ = self.call("ingest", {"kind": "sale", "rows": []}, headers={"X-API-Key": "wrong"})
        self.assertEqual(st, 401)
        key = self.call("settings")[1]["api_key"]
        rows = [{"date": "2026-10-25", "client": "하늘팬시", "item": "딸깍키트", "qty": 3, "unit_price": 15000, "order_no": "A-1"}]
        st, r = self.call("ingest", {"kind": "sale", "rows": rows}, headers={"X-API-Key": key})
        self.assertEqual((st, r["inserted"]), (200, 1))
        st, r = self.call("ingest", {"kind": "sale", "rows": rows}, headers={"X-API-Key": key})
        self.assertEqual(r["duplicates"], 1)

    def test_bad_row_rolls_back_auto_created_records(self):
        key = self.call("settings")[1]["api_key"]
        st, r = self.call("ingest", {"kind": "sale", "rows": [{"date": "2026-10-25", "client": "유령상사", "item": "유령품목"}]},
                          headers={"X-API-Key": key})
        self.assertEqual(len(r["errors"]), 1)
        self.assertEqual(self.call("clients")[1], [])
        self.assertEqual(self.call("products")[1], [])

    def test_inbox_folder(self):
        inbox = os.path.join(self.tmp.name, "inbox")
        server.scan_inbox(self.db, inbox)  # 폴더 생성
        dest = os.path.join(inbox, "매출", "daily.csv")
        with open(SAMPLE, "rb") as src, open(dest, "wb") as f:
            f.write(src.read())
        old = time.time() - 60
        os.utime(dest, (old, old))
        res = server.scan_inbox(self.db, inbox)
        self.assertEqual(res[0]["inserted"], 4)
        self.assertEqual(len(os.listdir(os.path.join(inbox, "매출", "done"))), 1)
        # 파일명에 날짜가 없어도 열의 일자를 사용
        self.assertEqual(self.call("summary?from=2026-10-24&to=2026-10-24")[1]["sales"], 810000)

    @unittest.skipUnless(__import__("importlib").util.find_spec("openpyxl"), "openpyxl 없음")
    def test_xlsx_import(self):
        import datetime, io, openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["거래일자", "거래처명", "품명", "수량", "단가"])
        ws.append([datetime.datetime(2026, 10, 3), "문구나라", "딸깍키트", 5, 14000])
        buf = io.BytesIO()
        wb.save(buf)
        st, r = self.call("import", {"kind": "sale", "filename": "a.xlsx", "content_b64": base64.b64encode(buf.getvalue()).decode()})
        self.assertEqual((st, r["inserted"]), (200, 1), r)
        e = self.call("entries?kind=sale&from=2026-10-01&to=2026-10-31")[1][0]
        self.assertEqual((e["date"], e["supply"], e["vat"]), ("2026-10-03", 70000, 7000))

    def test_export_csv_and_static(self):
        self.import_sample()
        with urllib.request.urlopen(self.base + "export?type=pl&from=2026-10-01&to=2026-10-31") as r:
            body = r.read().decode("utf-8-sig")
        self.assertIn("매출액,810000", body)
        with urllib.request.urlopen(self.base.replace("/api/", "/")) as r:
            self.assertIn("위드케이키", r.read().decode())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(self.base.replace("/api/", "/../server.py"))


if __name__ == "__main__":
    unittest.main()
