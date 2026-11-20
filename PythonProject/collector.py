"""
Команды:
  python collector.py init                   # загрузить справочник марок
  python collector.py add Kia K5 2021 2022   # добавить отслеживание (годы опциональны)
  python collector.py collect                # снять цены за сегодня (cron)
"""
import math, os, re, sqlite3, statistics, sys, time
from datetime import date
import requests

DB_PATH = os.environ.get("CAREX_DB", "carex.db")
API = "https://api.darvin.digital/api.php"
SITE = "https://nsk.carexkorea.ru"
PAGE_SIZE = 24
SLEEP_BETWEEN_REQUESTS = 0.7   # вежливая пауза между запросами

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (personal price tracker)",
                        "Referer": SITE + "/katalog/"})

def http_get(url, params=None, as_json=True, retries=3):
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json() if as_json else r.text
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))

def js_round(x):
    return math.floor(x + 0.5)   # JS Math.round (у Python round — банковский, отличается!)

def fetch_rates():
    xml = http_get(SITE + "/wp-admin/admin-ajax.php", {"action": "get_valute"})
    krw = float(re.search(r'ID="R01815".*?<Value>([\d,]+)</Value>', xml, re.S).group(1).replace(",", "."))
    eur = float(re.search(r'ID="R01239".*?<Value>([\d,]+)</Value>', xml, re.S).group(1).replace(",", "."))
    usd = float(http_get(SITE + "/wp-admin/admin-ajax.php", {"action": "fetch_first_rate"})["rate"])
    return krw, eur, usd

def site_price_rub(finish, recycling, usd_rub, usd_krw):
    """Формула 1-в-1 из catalog.js сайта (цена «под ключ», маршрут через Киргизию)."""
    calc_car = js_round((finish + 400000) / usd_krw)
    duty = js_round((finish / usd_krw) * 0.22 * usd_rub)
    decl = math.ceil(js_round(3000 * usd_rub) / 1000) * 1000
    docs = math.ceil(js_round(1000 * usd_rub) / 1000) * 1000
    delivery = math.ceil(js_round(1300 * usd_rub) / 1000) * 1000
    total = (calc_car * usd_rub + duty + 150000 + decl + docs
             + (recycling or 0) + 110000 + delivery)
    return math.ceil(total / 1000) * 1000

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS brands(
        marka_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS models(
        marka_id INTEGER, name TEXT, PRIMARY KEY(marka_id, name));
    CREATE TABLE IF NOT EXISTS watches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        marka_id INTEGER NOT NULL, marka_name TEXT NOT NULL,
        model_name TEXT NOT NULL, year_from INTEGER, year_to INTEGER,
        created_at TEXT DEFAULT (datetime('now')));
    CREATE TABLE IF NOT EXISTS listings(
        lot INTEGER PRIMARY KEY, marka_name TEXT, model_name TEXT,
        year INTEGER, month TEXT, mileage INTEGER, eng_v INTEGER,
        power_hp INTEGER, grade TEXT, kpp TEXT, vin TEXT,
        first_seen TEXT, last_seen TEXT);
    CREATE TABLE IF NOT EXISTS prices(
        watch_id INTEGER, lot INTEGER, day TEXT,
        price_krw INTEGER, price_rub INTEGER, recycling_rub INTEGER,
        mileage INTEGER, PRIMARY KEY(watch_id, lot, day));
    CREATE TABLE IF NOT EXISTS watch_days(
        watch_id INTEGER, day TEXT, cnt INTEGER,
        avg_rub INTEGER, med_rub INTEGER, min_rub INTEGER, max_rub INTEGER,
        avg_krw INTEGER, PRIMARY KEY(watch_id, day));
    CREATE TABLE IF NOT EXISTS rates(
        day TEXT PRIMARY KEY, krw_rub REAL, eur_rub REAL, usd_rub REAL);
    """)
    try:
        conn.execute("ALTER TABLE watches ADD COLUMN "
                     "preferential_recycling INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # колонка уже существует
    return conn

# ---------- справочники ----------
def refresh_brands(conn):
    html = http_get(SITE + "/katalog/", as_json=False)
    block = re.search(r'facetwp-facet-marka.*?<select.*?>(.*?)</select>', html, re.S)
    opts = re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', block.group(1))
    conn.executemany("INSERT OR REPLACE INTO brands VALUES(?,?)",
                     [(int(v), t.strip()) for v, t in opts])
    conn.commit()
    return len(opts)

def get_models(conn, marka_id):
    rows = conn.execute("SELECT name FROM models WHERE marka_id=? ORDER BY name",
                        (marka_id,)).fetchall()
    if rows:
        return [r["name"] for r in rows]
    data = http_get(API, {"method": "get_model", "marka_id": marka_id})
    names = sorted({m["MODEL_NAME"] for m in data if m.get("MODEL_NAME")})
    conn.executemany("INSERT OR REPLACE INTO models VALUES(?,?)",
                     [(marka_id, n) for n in names])
    conn.commit()
    return names

# ---------- курсы (рублёвая цена зависит от курса) ----------
def save_rates(conn, day):
    try:
        krw, eur, usd = fetch_rates()
        conn.execute("INSERT OR REPLACE INTO rates VALUES(?,?,?,?)", (day, krw, eur, usd))
        conn.commit()
        return krw, eur, usd
    except Exception as e:
        print("курсы не получены:", e)
        return None

# ---------- сбор цен ----------
def fetch_cars(marka_id, model, year_from, year_to, max_pages=None, preferential=False):
    params = dict(method="get_cars", marka_id=marka_id, model_id=model,
                  year_from=year_from or "", year_to=year_to or "",
                  mileage_from="", mileage_to="", engine_from="", engine_to="",
                  # особенность API: фильтр утильсбора применяется только при непустом sort
                  sort="FINISH-ASC" if preferential else "",
                  kpp="", time="", kuzov="", grade="",
                  preferential_recycling="true" if preferential else "")
    cnt = http_get(API, {**params, "method": "get_cars_count"})
    total = int(cnt[0]["TOTAL_COUNT"])
    pages = math.ceil(total / PAGE_SIZE)
    if max_pages:
        pages = min(pages, max_pages)
    print(f"  объявлений: {total}, страниц: {pages}")
    for page in range(1, pages + 1):
        data = http_get(API, {**params, "page": page})
        if not data:
            break
        yield from data
        time.sleep(SLEEP_BETWEEN_REQUESTS)

def collect_watch(conn, watch, day=None, max_pages=None):
    day = day or date.today().isoformat()
    print(f"[{day}] {watch['marka_name']} {watch['model_name']} "
          f"{watch['year_from'] or '*'}-{watch['year_to'] or '*'}")
    rates = save_rates(conn, day)
    if rates:
        krw, eur, usd = rates
        usd_krw = usd / krw * 1000 - 10  # ВАЖНО: из сырого курса, ДО +7
        usd_rub = usd + 7  # +7 только для рублёвых сумм
    else:
        usd_rub = usd_krw = None
    rub, krw, n = [], [], 0
    for item in fetch_cars(watch["marka_id"], watch["model_name"],
                           watch["year_from"], watch["year_to"], max_pages,
                           preferential=bool(watch["preferential_recycling"])):
        lot = int(item["LOT"])
        price_krw = int(item["FINISH"] or 0)
        recycling = item.get("PRICE_RECYCLING_COLLECTION_RUB") or 0
        if usd_rub:
            price_rub = site_price_rub(price_krw, recycling, usd_rub, usd_krw)
        else:
            price_rub = int(item.get("PRICE_FULL_RUB") or 0)  # запасной вариант без курсов
        if not price_krw:
            continue
        year = int((item.get("MONTH") or item.get("YEAR") or "0")[:4])
        conn.execute("""INSERT INTO listings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(lot) DO UPDATE SET last_seen=excluded.last_seen,
                                         mileage=excluded.mileage""",
            (lot, item.get("MARKA_NAME"), item.get("MODEL_NAME"), year,
             item.get("MONTH"), item.get("MILEAGE"), item.get("ENG_V"),
             item.get("HORSE_POWER"), item.get("GRADE"), item.get("KPP_TYPE"),
             item.get("VIN"), day, day))
        conn.execute("INSERT OR REPLACE INTO prices VALUES(?,?,?,?,?,?,?)",
            (watch["id"], lot, day, price_krw, price_rub,
             item.get("PRICE_RECYCLING_COLLECTION_RUB"), item.get("MILEAGE")))
        if price_rub:
            rub.append(price_rub)
        krw.append(price_krw)
        n += 1
    if rub:
        conn.execute("INSERT OR REPLACE INTO watch_days VALUES(?,?,?,?,?,?,?,?)",
            (watch["id"], day, len(rub), round(statistics.mean(rub)),
             round(statistics.median(rub)), min(rub), max(rub),
             round(statistics.mean(krw))))
    conn.commit()
    avg = round(statistics.mean(rub)) if rub else 0
    print(f"  сохранено: {n} авто, средняя {avg:,} руб.")

def collect_watch_id(watch_id, max_pages=None):
    conn = db()
    w = conn.execute("SELECT * FROM watches WHERE id=?", (watch_id,)).fetchone()
    if w:
        day = date.today().isoformat()
        save_rates(conn, day)
        collect_watch(conn, w, day, max_pages)
    conn.close()

def collect_all(max_pages=None):
    conn = db()
    day = date.today().isoformat()
    save_rates(conn, day)
    for w in conn.execute("SELECT * FROM watches").fetchall():
        try:
            collect_watch(conn, w, day, max_pages)
        except Exception as e:
            print("  ошибка:", e)
    conn.close()

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "collect"
    conn = db()
    if cmd == "init":
        print("марок загружено:", refresh_brands(conn))
    elif cmd == "add":
        args = [a for a in sys.argv[2:] if not a.startswith("--")]
        util = 1 if "--util" in sys.argv else 0
        brand, model = args[0], args[1]
        yf = int(args[2]) if len(args) > 2 else None
        yt = int(args[3]) if len(args) > 3 else None
        row = conn.execute("SELECT * FROM brands WHERE lower(name)=lower(?)",
                           (brand,)).fetchone()
        if not row:
            sys.exit("Марка не найдена, сначала: python collector.py init")
        models = get_models(conn, row["marka_id"])
        match = next((m for m in models if m.lower() == model.lower()), None)
        if not match:
            sys.exit(f"Модель не найдена. Доступные: {', '.join(models)}")
        cur = conn.execute("INSERT INTO watches(marka_id,marka_name,model_name,"
                           "year_from,year_to,preferential_recycling) VALUES(?,?,?,?,?,?)",
                           (row["marka_id"], row["name"], match, yf, yt, util))
        conn.commit()
        print("добавлено отслеживание id =", cur.lastrowid)
    elif cmd == "collect":
        conn.close()
        collect_all()
    conn.close()