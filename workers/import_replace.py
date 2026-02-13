#!/usr/bin/env python3
import os, json, tempfile, zipfile
import psycopg2
from psycopg2.extras import execute_values
import requests

DATABASE_URL = os.getenv("DATABASE_URL")  # p.ex. postgres://...
SUPPLIER_NAME = os.getenv("SUPPLIER_NAME", "BIHR")
CATALOG_URL = os.getenv("CATALOG_URL")    # si descarregues el ZIP
LOCAL_JSON_PATH = os.getenv("LOCAL_JSON_PATH")  # si tens el JSON local

def load_products():
    if LOCAL_JSON_PATH and os.path.exists(LOCAL_JSON_PATH):
        j = json.load(open(LOCAL_JSON_PATH))
        return j.get("Products", [])
    if CATALOG_URL:
        r = requests.get(CATALOG_URL, timeout=120)
        r.raise_for_status()
        tmp = tempfile.NamedTemporaryFile(delete=False)
        tmp.write(r.content); tmp.flush(); tmp.close()
        zf = zipfile.ZipFile(tmp.name)
        inner = zf.namelist()[0]
        data = json.loads(zf.read(inner))
        return data.get("Products", [])
    raise SystemExit("No input provided (set LOCAL_JSON_PATH or CATALOG_URL)")

def parse_products(products):
    rows=[]
    for p in products:
        code = p.get("ProductCode") or p.get("SupplierProductCode") or p.get("NewPartNumber")
        qty = p.get("StockValue")
        level = p.get("StockLevel")
        in_stock = True if level == "InStock" else False
        rows.append((code, qty, in_stock, json.dumps(p)))
    return rows

def run_import(rows, supplier_id, conn):
    cur = conn.cursor()
    cur.execute("""
      CREATE TEMP TABLE tmp_stock (
        supplier_sku text,
        stock_quantity numeric,
        in_stock boolean,
        raw_value jsonb
      ) ON COMMIT DROP;
    """)
    execute_values(cur,
      "INSERT INTO tmp_stock (supplier_sku, stock_quantity, in_stock, raw_value) VALUES %s",
      rows, template="(%s, %s, %s, %s)"
    )
    cur.execute("""
      INSERT INTO supplier_sku (supplier_id, supplier_sku)
      SELECT %s, ts.supplier_sku
      FROM tmp_stock ts
      LEFT JOIN supplier_sku ss ON ss.supplier_sku = ts.supplier_sku AND ss.supplier_id = %s
      WHERE ss.id IS NULL
      ON CONFLICT DO NOTHING;
    """, (supplier_id, supplier_id))
    cur.execute("""
      CREATE TEMP TABLE tmp_stock_with_id AS
      SELECT ss.id AS supplier_sku_id, ts.stock_quantity, ts.in_stock, ts.raw_value
      FROM tmp_stock ts
      JOIN supplier_sku ss ON ss.supplier_sku = ts.supplier_sku AND ss.supplier_id = %s;
    """, (supplier_id,))
    cur.execute("BEGIN;")
    cur.execute("""
      DELETE FROM stock_current
      WHERE supplier_sku_id IN (SELECT supplier_sku_id FROM tmp_stock_with_id);
    """)
    cur.execute("""
      INSERT INTO stock_current (supplier_sku_id, last_updated, stock_quantity, in_stock, raw_value)
      SELECT supplier_sku_id, now(), stock_quantity::numeric, in_stock, raw_value
      FROM tmp_stock_with_id;
    """)
    cur.execute("COMMIT;")
    cur.execute("REFRESH MATERIALIZED VIEW vw_latest_stock_per_sku;")
    conn.commit()
    cur.close()

def main():
    products = load_products()
    rows = parse_products(products)
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT id FROM suppliers WHERE name=%s ORDER BY created_at DESC LIMIT 1", (SUPPLIER_NAME,))
    r = cur.fetchone()
    if r:
        supplier_id = r[0]
    else:
        cur.execute("INSERT INTO suppliers (name, connector_type, config) VALUES (%s,%s,%s) RETURNING id", (SUPPLIER_NAME,'api',json.dumps({})))
        supplier_id = cur.fetchone()[0]
        conn.commit()
    cur.close()
    run_import(rows, supplier_id, conn)
    conn.close()
    print('import done, rows', len(rows))

if __name__ == '__main__':
    main()
