import os
import json
import math
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, redirect, session, g, jsonify, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-prod")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, 'attendance.db')

BUNK_LIMIT = 80
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(error):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_db():
    conn = sqlite3.connect(DATABASE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS subjects (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            name          TEXT NOT NULL,
            semester      TEXT NOT NULL,
            total_classes INTEGER NOT NULL DEFAULT 0,
            attended      INTEGER NOT NULL DEFAULT 0,
            classes_held  INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS timetable (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            subject_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL,
            day_of_week INTEGER NOT NULL,
            UNIQUE(subject_id, day_of_week),
            FOREIGN KEY (subject_id) REFERENCES subjects(id),
            FOREIGN KEY (user_id)    REFERENCES users(id)
        )
    ''')
    # Store push subscriptions per user
    c.execute('''
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            subscription TEXT NOT NULL,
            UNIQUE(user_id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated

def get_subject_or_403(subject_id, user_id):
    return get_db().execute(
        "SELECT * FROM subjects WHERE id=? AND user_id=?", (subject_id, user_id)
    ).fetchone()

# ---------------------------------------------------------------------------
# Attendance Calculator
# Formula: attended / total_classes * 100
# ---------------------------------------------------------------------------

def compute_bunk_info(attended, classes_held, total_classes):
    if total_classes == 0:
        return dict(pct=0.0, realtime_pct=0.0, safe_to_bunk=0,
                    need_to_attend=0, max_possible=0.0, remaining=0,
                    status='safe', cooked=False, vibe='', vibe_detail='')

    # Main formula your college uses
    pct          = round(attended / total_classes * 100, 1)
    realtime_pct = round(attended / classes_held * 100, 1) if classes_held > 0 else 0.0
    remaining    = max(total_classes - classes_held, 0)

    min_needed     = math.ceil(BUNK_LIMIT / 100 * total_classes)
    still_need     = max(min_needed - attended, 0)
    safe_to_bunk   = max(remaining - still_need, 0)
    max_possible   = round((attended + remaining) / total_classes * 100, 1)
    cooked         = max_possible < BUNK_LIMIT
    need_to_attend = still_need if pct < BUNK_LIMIT and not cooked else 0

    if pct >= BUNK_LIMIT:
        status = 'safe'
    elif pct >= BUNK_LIMIT - 10:
        status = 'warning'
    else:
        status = 'danger'

    vibe, vibe_detail = get_vibe(pct, safe_to_bunk, need_to_attend, cooked)

    return dict(pct=pct, realtime_pct=realtime_pct, safe_to_bunk=safe_to_bunk,
                need_to_attend=need_to_attend, max_possible=max_possible,
                remaining=remaining, status=status, cooked=cooked,
                vibe=vibe, vibe_detail=vibe_detail)


def get_vibe(pct, safe_to_bunk, need_to_attend, cooked):
    if cooked:
        return "💀 COOKED", f"bro even if you attend every single remaining class you still can't hit {BUNK_LIMIT}%. it's over. take the L."
    if pct >= 95:
        return "👑 GOATED", f"you've attended basically everything. {safe_to_bunk} free bunks available. go touch some grass."
    elif pct >= 90:
        return "🔥 BUILT DIFFERENT", f"ngl you're locked in. {safe_to_bunk} free bunks. use them wisely (or don't lol)."
    elif pct >= BUNK_LIMIT + 5:
        return "✅ CHILLING", f"you're good. {safe_to_bunk} bunk{'s' if safe_to_bunk != 1 else ''} left before things get scary. spend them carefully bestie."
    elif pct >= BUNK_LIMIT:
        if safe_to_bunk == 0:
            return "⚠️ ON THIN ICE", "you're technically safe but one absent and you're cooked. do NOT bunk. i'm not joking."
        return "😬 BARELY SAFE", f"safe but by the skin of your teeth. {safe_to_bunk} bunk{'s' if safe_to_bunk != 1 else ''} left. treat them like gold."
    elif pct >= BUNK_LIMIT - 5:
        return "🚨 SOS", f"you're {round(BUNK_LIMIT - pct, 1)}% below the limit. attend the next {need_to_attend} classes or you're getting detained. NO EXCUSES."
    elif pct >= BUNK_LIMIT - 15:
        return "📵 TOUCH GRASS LATER", f"your parents are going to get a call. attend {need_to_attend} consecutive classes to recover. put the phone down."
    return "🪦 RIP BOZO", f"how did you even get here. you need {need_to_attend} classes in a row just to see daylight again. start praying."

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_all_subjects(user_id):
    db = get_db()
    rows = db.execute(
        "SELECT * FROM subjects WHERE user_id=? ORDER BY semester, name", (user_id,)
    ).fetchall()
    subjects = []
    for row in rows:
        info = compute_bunk_info(row['attended'], row['classes_held'], row['total_classes'])
        day_rows = db.execute(
            "SELECT day_of_week FROM timetable WHERE subject_id=? ORDER BY day_of_week", (row['id'],)
        ).fetchall()
        subjects.append({
            'id': row['id'], 'name': row['name'], 'semester': row['semester'],
            'total_classes': row['total_classes'], 'attended': row['attended'],
            'classes_held': row['classes_held'],
            'days_scheduled': [d['day_of_week'] for d in day_rows],
            **info,
        })
    return subjects

# ---------------------------------------------------------------------------
# PWA Routes
# ---------------------------------------------------------------------------

@app.route('/service-worker.js')
def service_worker():
    """Serve service worker from root scope (required for full app control)."""
    return send_from_directory(BASE_DIR, 'service-worker.js',
                               mimetype='application/javascript')

@app.route('/manifest.json')
def manifest():
    return send_from_directory(BASE_DIR, 'manifest.json',
                               mimetype='application/manifest+json')

@app.route('/push/subscribe', methods=['POST'])
@login_required
def push_subscribe():
    """Save push subscription for logged-in user."""
    subscription = request.get_json()
    if not subscription:
        return jsonify({'error': 'No subscription data'}), 400
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO push_subscriptions (user_id, subscription) VALUES (?, ?)",
        (session['user_id'], json.dumps(subscription))
    )
    db.commit()
    return jsonify({'status': 'subscribed'})

@app.route('/push/unsubscribe', methods=['POST'])
@login_required
def push_unsubscribe():
    db = get_db()
    db.execute("DELETE FROM push_subscriptions WHERE user_id=?", (session['user_id'],))
    db.commit()
    return jsonify({'status': 'unsubscribed'})

@app.route('/push/check', methods=['GET'])
@login_required
def push_check():
    """
    Called once daily by the client (via JS scheduler).
    Returns alert data if any subject needs attention.
    Client then shows the notification via the service worker.
    """
    subjects = get_all_subjects(session['user_id'])
    alerts = []
    today_dow = datetime.now().weekday()

    for s in subjects:
        # Alert if: below 80%, or only 1-2 bunks left AND class is today
        if s['cooked']:
            alerts.append({
                'name': s['name'],
                'pct':  s['pct'],
                'msg':  f"💀 {s['name']} is cooked — {s['pct']}%. max possible: {s['max_possible']}%",
                'urgent': True,
            })
        elif s['status'] == 'danger':
            alerts.append({
                'name': s['name'],
                'pct':  s['pct'],
                'msg':  f"🚨 {s['name']} is at {s['pct']}% — attend {s['need_to_attend']} classes to recover",
                'urgent': True,
            })
        elif s['status'] == 'warning':
            alerts.append({
                'name': s['name'],
                'pct':  s['pct'],
                'msg':  f"⚠️ {s['name']} is at {s['pct']}% — getting close to the limit",
                'urgent': False,
            })
        elif s['status'] == 'safe' and s['safe_to_bunk'] <= 1 and today_dow in s['days_scheduled']:
            alerts.append({
                'name': s['name'],
                'pct':  s['pct'],
                'msg':  f"⚠️ {s['name']} — only {s['safe_to_bunk']} bunk left. Don't skip today.",
                'urgent': False,
            })

    return jsonify({'alerts': alerts})

# ---------------------------------------------------------------------------
# Main Routes
# ---------------------------------------------------------------------------

@app.route('/')
@login_required
def index():
    today_dow  = datetime.now().weekday()
    today_name = DAYS[today_dow]
    subjects   = get_all_subjects(session['user_id'])
    todays_subjects = [s for s in subjects if today_dow in s['days_scheduled']]
    semesters = {}
    for s in subjects:
        semesters.setdefault(s['semester'], []).append(s)
    return render_template('index.html',
        semesters=semesters, todays_subjects=todays_subjects,
        today_name=today_name, days=DAYS, bunk_limit=BUNK_LIMIT)


@app.route('/add', methods=['POST'])
@login_required
def add_subject():
    name          = request.form.get('name', '').strip()
    semester      = request.form.get('semester', '').strip()
    total_classes = request.form.get('total_classes', '1').strip()
    days_selected = request.form.getlist('days')
    if not name or not semester:
        return redirect('/')
    try:
        total_classes = max(int(total_classes), 1)
    except ValueError:
        total_classes = 1
    db  = get_db()
    cur = db.execute(
        "INSERT INTO subjects (user_id, name, semester, total_classes) VALUES (?, ?, ?, ?)",
        (session['user_id'], name, semester, total_classes)
    )
    subject_id = cur.lastrowid
    for day in days_selected:
        try:
            dow = int(day)
            if 0 <= dow <= 6:
                db.execute(
                    "INSERT OR IGNORE INTO timetable (subject_id, user_id, day_of_week) VALUES (?, ?, ?)",
                    (subject_id, session['user_id'], dow)
                )
        except ValueError:
            pass
    db.commit()
    return redirect('/')


@app.route('/timetable/update/<int:subject_id>', methods=['POST'])
@login_required
def update_timetable(subject_id):
    subject = get_subject_or_403(subject_id, session['user_id'])
    if subject is None:
        return "Not found or access denied.", 403
    days_selected = request.form.getlist('days')
    db = get_db()
    db.execute("DELETE FROM timetable WHERE subject_id=? AND user_id=?", (subject_id, session['user_id']))
    for day in days_selected:
        try:
            dow = int(day)
            if 0 <= dow <= 6:
                db.execute(
                    "INSERT OR IGNORE INTO timetable (subject_id, user_id, day_of_week) VALUES (?, ?, ?)",
                    (subject_id, session['user_id'], dow)
                )
        except ValueError:
            pass
    db.commit()
    return redirect('/')


@app.route('/mark/<int:id>/<string:status>')
@login_required
def mark_attendance(id, status):
    if status not in ('present', 'absent'):
        return "Invalid status.", 400
    subject = get_subject_or_403(id, session['user_id'])
    if subject is None:
        return "Not found or access denied.", 403
    if subject['classes_held'] >= subject['total_classes']:
        return redirect('/')
    db = get_db()
    if status == "present":
        db.execute("UPDATE subjects SET attended=attended+1, classes_held=classes_held+1 WHERE id=?", (id,))
    else:
        db.execute("UPDATE subjects SET classes_held=classes_held+1 WHERE id=?", (id,))
    db.commit()
    return redirect('/')


@app.route('/delete/<int:id>', methods=['POST'])
@login_required
def delete_subject(id):
    subject = get_subject_or_403(id, session['user_id'])
    if subject is None:
        return "Not found or access denied.", 403
    db = get_db()
    db.execute("DELETE FROM timetable WHERE subject_id=?", (id,))
    db.execute("DELETE FROM subjects WHERE id=?", (id,))
    db.commit()
    return redirect('/')


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        if not username or not password:
            error = "Username and password are required."
        else:
            db = get_db()
            try:
                db.execute("INSERT INTO users (username, password) VALUES (?, ?)",
                           (username, generate_password_hash(password)))
                db.commit()
                return redirect('/login')
            except sqlite3.IntegrityError:
                error = "Username already exists."
    return render_template('register.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user['password'], password):
            session['user_id'] = user['id']
            return redirect('/')
        error = "Invalid username or password."
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    return redirect('/login')


if __name__ == '__main__':
    app.run(debug=os.environ.get("FLASK_DEBUG", "false").lower() == "true")