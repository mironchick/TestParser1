import os
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
import collector

DB_PATH = os.environ.get("CAREX_DB", "carex.db")
collector.DB_PATH = DB_PATH
app = FastAPI(title="Carex Price Tracker")

def db():
    return collector.db()

class WatchIn(BaseModel):
    marka_id: int
    model_name: str
    year_from: int | None = None
    year_to: int | None = None
    preferential_recycling: bool = False

@app.get("/")
def index():
    return FileResponse("static/index.html")

@app.get("/api/brands")
def brands():
    conn = db()
    rows = conn.execute("SELECT * FROM brands ORDER BY name").fetchall()
    if not rows:
        collector.refresh_brands(conn)
        rows = conn.execute("SELECT * FROM brands ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/models")
def models(marka_id: int):
    conn = db()
    names = collector.get_models(conn, marka_id)
    conn.close()
    return names

@app.get("/api/watches")
def watches():
    conn = db()
    out = []
    for w in conn.execute("SELECT * FROM watches ORDER BY id").fetchall():
        days = conn.execute("SELECT * FROM watch_days WHERE watch_id=? "
                            "ORDER BY day DESC LIMIT 2", (w["id"],)).fetchall()
        item = dict(w)
        item["latest"] = dict(days[0]) if days else None
        item["prev"] = dict(days[1]) if len(days) > 1 else None
        out.append(item)
    conn.close()
    return out

@app.post("/api/watches")
def add_watch(data: WatchIn, tasks: BackgroundTasks):
    conn = db()
    b = conn.execute("SELECT * FROM brands WHERE marka_id=?",
                     (data.marka_id,)).fetchone()
    if not b:
        raise HTTPException(404, "марка не найдена")
    names = collector.get_models(conn, data.marka_id)
    match = next((m for m in names if m.lower() == data.model_name.lower()), None)
    if not match:
        raise HTTPException(404, "модель не найдена")
    cur = conn.execute("INSERT INTO watches(marka_id,marka_name,model_name,"
                       "year_from,year_to,preferential_recycling) VALUES(?,?,?,?,?,?)",
                       (data.marka_id, b["name"], match, data.year_from, data.year_to,
                        int(data.preferential_recycling)))
    conn.commit()
    wid = cur.lastrowid
    conn.close()
    tasks.add_task(collector.collect_watch_id, wid)  # первый сбор — в фоне
    return {"id": wid}

@app.post("/api/watches/{wid}/refresh")
def refresh(wid: int, tasks: BackgroundTasks):
    tasks.add_task(collector.collect_watch_id, wid)
    return {"status": "started"}

@app.delete("/api/watches/{wid}")
def delete_watch(wid: int):
    conn = db()
    conn.execute("DELETE FROM watches WHERE id=?", (wid,))
    conn.execute("DELETE FROM prices WHERE watch_id=?", (wid,))
    conn.execute("DELETE FROM watch_days WHERE watch_id=?", (wid,))
    conn.commit()
    conn.close()
    return {"status": "ok"}

@app.get("/api/watches/{wid}/series")
def series(wid: int):
    conn = db()
    days = conn.execute("SELECT * FROM watch_days WHERE watch_id=? ORDER BY day",
                        (wid,)).fetchall()
    rates = conn.execute("SELECT * FROM rates ORDER BY day").fetchall()
    conn.close()
    return {"days": [dict(r) for r in days], "rates": [dict(r) for r in rates]}

@app.get("/api/watches/{wid}/changes")
def changes(wid: int):
    conn = db()
    days = [r["day"] for r in conn.execute(
        "SELECT day FROM watch_days WHERE watch_id=? ORDER BY day DESC LIMIT 2",
        (wid,))]
    if len(days) < 2:
        conn.close()
        return {"day": days[0] if days else None, "prev_day": None,
                "changed": [], "new": [], "gone": []}
    d1, d0 = days
    q = """SELECT p1.lot, p1.price_krw new_krw, p1.price_rub new_rub,
                  p0.price_krw old_krw, p0.price_rub old_rub,
                  l.year, l.month, l.mileage, l.grade, l.power_hp
           FROM prices p1
           LEFT JOIN prices p0 ON p0.watch_id=p1.watch_id
                              AND p0.lot=p1.lot AND p0.day=?
           JOIN listings l ON l.lot=p1.lot
           WHERE p1.watch_id=? AND p1.day=?"""
    changed, new = [], []
    for r in conn.execute(q, (d0, wid, d1)):
        r = dict(r)
        if r["old_krw"] is None:
            new.append(r)
        elif r["old_krw"] != r["new_krw"]:
            r["delta_krw"] = r["new_krw"] - r["old_krw"]
            r["delta_rub"] = (r["new_rub"] or 0) - (r["old_rub"] or 0)
            changed.append(r)
    gone = [dict(r) for r in conn.execute("""
        SELECT p0.lot, p0.price_rub old_rub, l.year, l.month, l.mileage
        FROM prices p0 LEFT JOIN prices p1
          ON p1.watch_id=p0.watch_id AND p1.lot=p0.lot AND p1.day=?
        JOIN listings l ON l.lot=p0.lot
        WHERE p0.watch_id=? AND p0.day=? AND p1.lot IS NULL""", (d1, wid, d0))]
    conn.close()
    changed.sort(key=lambda r: r["delta_krw"])
    return {"day": d1, "prev_day": d0, "changed": changed, "new": new, "gone": gone}