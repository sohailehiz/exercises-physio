"""Physio exercise tracker. Run: pip install -r requirements.txt && python app.py"""
import os, sqlite3, datetime, hmac
try:  # optional: converts phone photos (HEIC), fixes rotation and shrinks big images
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        pillow_heif = None
except ImportError:
    Image = ImageOps = pillow_heif = None
from flask import Flask, Response, g, jsonify, request, render_template, send_from_directory

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data.db")
UP = os.path.join(BASE, "uploads")
os.makedirs(UP, exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # 25 MB upload limit

# Simple password gate for when the app is hosted online. Set APP_PASSWORD (and optionally APP_USER)
# as environment variables on the host. If APP_PASSWORD is not set (local use), there is no login.
APP_USER = os.environ.get("APP_USER", "me")
APP_PASSWORD = os.environ.get("APP_PASSWORD")


@app.before_request
def guard():
    if not APP_PASSWORD:
        return None
    a = request.authorization
    ok = a and hmac.compare_digest((a.username or "").encode(), APP_USER.encode()) \
        and hmac.compare_digest((a.password or "").encode(), APP_PASSWORD.encode())
    if not ok:
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Physio tracker"'})
    return None

# (name, cue, unit, placeholder target, pose key). Targets are placeholders: set real ones from your physio.
SEED = [
    ("Low lunge reach", "Kneel in a lunge, reach one arm up. Optional side bend, then bend back. Feel it at the front of the back hip. Keep legs aligned close to centre, not drifting away.", "reps", 10, "lunge"),
    ("Prone heel to buttock", "Lie face down. Pull the heel back 5 secs, release 5 secs. Each time push back a little more, within a comfortable range.", "reps", 10, "heel"),
    ("Lying heels together", "On your back, heels together, raise the knees out to the sides.", "reps", 10, "butterfly"),
    ("Glute bridge", "Feet flat, lift the hips and squeeze the glutes. Also try pressing through the heels.", "reps", 10, "bridge"),
    ("Knee to chest / figure four", "Pull the knees in toward the chest (V1), or cross one ankle over the opposite knee and pull (V2). Hold at the marked points.", "sec", 20, "kneechest"),
    ("Prone press-up", "Lie face down, hands in line with the shoulders, raise the upper back.", "reps", 10, "pressup"),
    ("Back extension over roller", "Roller or soft round pillow under the back, arms overhead, bend back slowly.", "reps", 10, "roller"),
    ("Seated hamstring reach", "Sit at the table edge, one leg straight. Move slowly and reach toward the heel. Adjust the gap to what is comfortable.", "reps", 10, "hamstring"),
    ("Seated figure four reach", "Sit with one leg crossed over the other and lean forward, hand sliding along the table.", "sec", 20, "figure4"),
]


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close(_):
    d = g.pop("db", None)
    if d:
        d.close()


def init():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS exercises(id INTEGER PRIMARY KEY, pos INTEGER, name TEXT, cue TEXT, unit TEXT, target INTEGER, pose TEXT, image TEXT)")
    if "active" not in [r[1] for r in c.execute("PRAGMA table_info(exercises)")]:
        c.execute("ALTER TABLE exercises ADD COLUMN active INTEGER DEFAULT 1")
    c.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)")
    cols = [r[1] for r in c.execute("PRAGMA table_info(entries)")]
    if cols and "session" not in cols:  # migrate older single-session data to session 1
        c.execute("ALTER TABLE entries RENAME TO entries_old")
    c.execute("CREATE TABLE IF NOT EXISTS entries(day TEXT, session INTEGER, exercise_id INTEGER, status TEXT, reps INTEGER, logged_at TEXT, PRIMARY KEY(day, session, exercise_id))")
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='entries_old'").fetchone():
        c.execute("INSERT INTO entries SELECT day, 1, exercise_id, status, reps, logged_at FROM entries_old")
        c.execute("DROP TABLE entries_old")
    if not c.execute("SELECT 1 FROM exercises").fetchone():
        c.executemany("INSERT INTO exercises(pos,name,cue,unit,target,pose) VALUES(?,?,?,?,?,?)",
                      [(i + 1,) + s for i, s in enumerate(SEED)])
    c.commit()
    c.close()


@app.get("/")
def index():
    return render_template("index.html")


def bundled():
    """Images committed to the repo in static/exercises/, named by exercise number: 1.jpg, 2.png, ..."""
    d = os.path.join(BASE, "static", "exercises")
    out = {}
    if os.path.isdir(d):
        for f in os.listdir(d):
            stem, ext = os.path.splitext(f)
            if ext.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif") and stem.isdigit():
                out[int(stem)] = "/static/exercises/" + f
    return out


@app.get("/api/day")
def day():
    rows = db().execute(
        "SELECT e.*, n.status, n.reps FROM exercises e "
        "LEFT JOIN entries n ON n.exercise_id=e.id AND n.day=? AND n.session=? WHERE e.active=1 ORDER BY e.pos",
        (request.args["date"], int(request.args.get("session", 1)))).fetchall()
    b = bundled()  # an image uploaded in the app wins over one bundled in the repo
    return jsonify([dict(r, img=("/uploads/" + r["image"]) if r["image"] else b.get(r["pos"])) for r in rows])


@app.post("/api/log")
def log():
    j = request.get_json()
    db().execute(
        "INSERT INTO entries VALUES(?,?,?,?,?,?) ON CONFLICT(day,session,exercise_id) DO UPDATE SET "
        "status=excluded.status, reps=excluded.reps, logged_at=excluded.logged_at",
        (j["date"], int(j.get("session", 1)), j["exercise_id"], j["status"], j.get("reps"),
         datetime.datetime.now().isoformat(timespec="seconds")))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/stats")
def stats():
    rows = db().execute("SELECT day, session, exercise_id, status, reps FROM entries ORDER BY day, session").fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/exercise")
def add_exercise():
    j = request.get_json()
    pos = db().execute("SELECT COALESCE(MAX(pos),0)+1 FROM exercises").fetchone()[0]
    db().execute("INSERT INTO exercises(pos,name,cue,unit,target,pose,active) VALUES(?,?,?,?,?,?,1)",
                 (pos, j["name"].strip(), j.get("cue", ""), j.get("unit", "reps"),
                  int(j.get("target") or 10), j.get("pose") or None))
    db().commit()
    return jsonify(ok=True)


@app.post("/api/exercise/<int:i>")
def edit_exercise(i):
    j = request.get_json()
    db().execute("UPDATE exercises SET name=?, cue=?, unit=?, target=? WHERE id=?",
                 (j["name"].strip(), j.get("cue", ""), j["unit"], int(j["target"]), i))
    db().commit()
    return jsonify(ok=True)


@app.delete("/api/exercise/<int:i>")
def del_exercise(i):  # archived, not deleted: past history is kept
    db().execute("UPDATE exercises SET active=0 WHERE id=?", (i,))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/settings")
def get_settings():
    r = db().execute("SELECT v FROM settings WHERE k='sessions'").fetchone()
    return jsonify(sessions=int(r["v"]) if r else 2)


@app.post("/api/settings")
def set_settings():
    n = max(1, min(int(request.get_json()["sessions"]), 12))
    db().execute("INSERT INTO settings VALUES('sessions',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (str(n),))
    db().commit()
    return jsonify(sessions=n)


@app.post("/api/reset")
def reset():
    j = request.get_json()
    db().execute("DELETE FROM entries WHERE day=? AND session=?", (j["date"], int(j["session"])))
    db().commit()
    return jsonify(ok=True)


@app.get("/api/sessions")
def sessions():
    rows = db().execute("SELECT session, COUNT(*) n FROM entries WHERE day=? GROUP BY session",
                        (request.args["date"],)).fetchall()
    return jsonify({str(r["session"]): r["n"] for r in rows})


@app.post("/api/target/<int:i>")
def target(i):
    j = request.get_json()
    db().execute("UPDATE exercises SET target=?, unit=? WHERE id=?", (int(j["target"]), j["unit"], i))
    db().commit()
    return jsonify(ok=True)


@app.post("/api/image/<int:i>")
def image(i):
    f = request.files["file"]
    ext = os.path.splitext(f.filename)[1].lower()
    heic = ext in (".heic", ".heif")
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif") and not heic:
        return jsonify(error="Use JPG, PNG, WebP, GIF or HEIC"), 400
    if heic and not pillow_heif:
        return jsonify(error="HEIC support is not installed on the server (pip install pillow-heif)."), 400
    stamp = int(datetime.datetime.now().timestamp())
    if ext == ".gif":  # keep the original file so the animation survives
        if f.stream.read(3) != b"GIF":
            return jsonify(error="That file is not a valid GIF."), 400
        f.stream.seek(0)
        name = f"ex{i}_{stamp}.gif"
        f.save(os.path.join(UP, name))
    elif Image:  # normalise everything else to a rotated, max-1600px JPEG
        try:
            im = Image.open(f.stream)
            im = ImageOps.exif_transpose(im) or im
            im.thumbnail((1600, 1600))
            name = f"ex{i}_{stamp}.jpg"
            im.convert("RGB").save(os.path.join(UP, name), "JPEG", quality=85)
        except Exception:
            return jsonify(error="Could not read that image file."), 400
    else:
        name = f"ex{i}_{stamp}{ext}"
        f.save(os.path.join(UP, name))
    db().execute("UPDATE exercises SET image=? WHERE id=?", (name, i))
    db().commit()
    return jsonify(image=name)


@app.delete("/api/image/<int:i>")
def no_image(i):
    db().execute("UPDATE exercises SET image=NULL WHERE id=?", (i,))
    db().commit()
    return jsonify(ok=True)


@app.get("/uploads/<path:n>")
def uploads(n):
    return send_from_directory(UP, n)


init()  # also runs under WSGI hosts such as PythonAnywhere

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
