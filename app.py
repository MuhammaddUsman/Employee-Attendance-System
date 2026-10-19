from flask import Flask, render_template_string, request, jsonify, redirect, url_for, send_file, abort, session, Response
from datetime import datetime, timedelta
import json
import os
import csv
import re
import io
import time
import hmac
import base64
import urllib.request
from functools import wraps
from zoneinfo import ZoneInfo

app = Flask(__name__)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=bool(os.environ.get('VERCEL')),
)

app.secret_key = os.environ.get('SECRET_KEY', 'dev_secret_change_me')

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')
TIMEZONE = ZoneInfo(os.environ.get('TIMEZONE', 'Asia/Karachi'))

# Storage: Upstash Redis (persistent, used on Vercel) if configured,
# otherwise a local JSON file (fine for local development).
REDIS_URL = os.environ.get('KV_REST_API_URL') or os.environ.get('UPSTASH_REDIS_REST_URL')
REDIS_TOKEN = os.environ.get('KV_REST_API_TOKEN') or os.environ.get('UPSTASH_REDIS_REST_TOKEN')
REDIS_KEY = 'attendance_data'
DATA_FILE = os.path.join('/tmp' if os.environ.get('VERCEL') else '.', 'attendance_data.json')


P_EMP, P_ATT = 'att:employees', 'att:attendance'
K_CNT, K_MIG = 'att:emp_counter', 'att:migrated'
_migrated = False


def now_local():
    return datetime.now(TIMEZONE)


def _use_redis():
    return bool(REDIS_URL and REDIS_TOKEN)


def _cmd(*args):
    req = urllib.request.Request(
        REDIS_URL.rstrip('/'),
        data=json.dumps(list(args)).encode('utf-8'),
        headers={'Authorization': f'Bearer {REDIS_TOKEN}', 'Content-Type': 'application/json'},
        method='POST',
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        out = json.loads(resp.read().decode('utf-8'))
    if isinstance(out, dict) and out.get('error'):
        raise RuntimeError(out['error'])
    return out.get('result')


def _emp_num(eid):
    m = re.fullmatch(r'EMP(\d+)', eid or '')
    return int(m.group(1)) if m else 0


# ---- local JSON file (development only) ----
def _local_load():
    d = {}
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, 'r') as f:
                d = json.load(f)
        except Exception:
            d = {}
    if isinstance(d.get('attendance'), list) or (isinstance(d.get('employees'), dict) and P_EMP not in d):
        old, d = d, {P_EMP: {}, P_ATT: {}, K_CNT: {}}   # convert the old single-blob format
        for eid, e in (old.get('employees') or {}).items():
            d[P_EMP][eid] = json.dumps(e)
        for r in old.get('attendance') or []:
            d[P_ATT].setdefault(f"{r['date']}:{r['employee_id']}", json.dumps(r))
        d[K_CNT]['v'] = str(max([_emp_num(e) for e in d[P_EMP]] or [0]))
    for k in (P_EMP, P_ATT, K_CNT):
        d.setdefault(k, {})
    return d


def _local_save(d):
    with open(DATA_FILE, 'w') as f:
        json.dump(d, f, indent=1)


# ---- one-time move from the old single-key storage to per-record storage ----
def ensure_migrated():
    global _migrated
    if _migrated or not _use_redis():
        return
    if _cmd('GET', K_MIG) is None:
        raw = _cmd('GET', REDIS_KEY)           # the old blob is kept as a backup
        if raw:
            old = json.loads(raw)
            emps = old.get('employees') or {}
            if emps:
                args = []
                for eid, e in emps.items():
                    args += [eid, json.dumps(e)]
                _cmd('HSET', P_EMP, *args)
            recs = {}
            for r in old.get('attendance') or []:
                recs.setdefault(f"{r['date']}:{r['employee_id']}", json.dumps(r))
            if recs:
                args = []
                for k, v in recs.items():
                    args += [k, v]
                _cmd('HSET', P_ATT, *args)
            _cmd('SETNX', K_CNT, str(max([_emp_num(e) for e in emps] or [0])))
        _cmd('SET', K_MIG, '1')
    _migrated = True


# ---- hash primitives (same behaviour on Redis and on the local file) ----
def h_getall(name):
    if _use_redis():
        ensure_migrated()
        res = _cmd('HGETALL', name) or []
        if isinstance(res, dict):
            return res
        return {res[i]: res[i + 1] for i in range(0, len(res), 2)}
    return dict(_local_load()[name])


def h_get(name, field):
    if _use_redis():
        ensure_migrated()
        return _cmd('HGET', name, field)
    return _local_load()[name].get(field)


def h_set(name, field, value):
    if _use_redis():
        ensure_migrated()
        _cmd('HSET', name, field, value)
        return
    d = _local_load()
    d[name][field] = value
    _local_save(d)


def h_setnx(name, field, value):
    # atomic "create only if it does not exist"
    if _use_redis():
        ensure_migrated()
        return _cmd('HSETNX', name, field, value) == 1
    d = _local_load()
    if field in d[name]:
        return False
    d[name][field] = value
    _local_save(d)
    return True


def h_del(name, *fields):
    if not fields:
        return
    if _use_redis():
        ensure_migrated()
        _cmd('HDEL', name, *fields)
        return
    d = _local_load()
    for f in fields:
        d[name].pop(f, None)
    _local_save(d)


def next_counter():
    if _use_redis():
        ensure_migrated()
        return int(_cmd('INCR', K_CNT))
    d = _local_load()
    n = int(d[K_CNT].get('v', '0')) + 1
    d[K_CNT]['v'] = str(n)
    _local_save(d)
    return n


# ---- employees and records ----
def rec_key(date, emp_id):
    return f'{date}:{emp_id}'


def store_get_employee(eid):
    v = h_get(P_EMP, eid)
    return json.loads(v) if v else None


def store_set_employee(eid, emp):
    h_set(P_EMP, eid, json.dumps(emp))


def store_create_employee(eid, emp):
    return h_setnx(P_EMP, eid, json.dumps(emp))


def store_delete_employee(eid):
    h_del(P_EMP, eid)
    h_del(P_ATT, *[k for k in h_getall(P_ATT) if k.endswith(':' + eid)])


def store_get_record(date, eid):
    v = h_get(P_ATT, rec_key(date, eid))
    return json.loads(v) if v else None


def store_set_record(rec):
    h_set(P_ATT, rec_key(rec['date'], rec['employee_id']), json.dumps(rec))


def store_create_record(rec):
    return h_setnx(P_ATT, rec_key(rec['date'], rec['employee_id']), json.dumps(rec))


def store_delete_record(date, eid):
    h_del(P_ATT, rec_key(date, eid))


def load_data():
    employees = {k: json.loads(v) for k, v in h_getall(P_EMP).items()}
    attendance = [json.loads(v) for v in h_getall(P_ATT).values()]
    attendance.sort(key=lambda r: (r['date'], r.get('clock_in') or ''))
    return {'employees': employees, 'attendance': attendance}


BASE_TEMPLATE = r'''
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
    <title>{{ company_name }} - Attendance</title>
    <link rel="manifest" href="/manifest.webmanifest">
    <meta name="theme-color" content="#0f2547">
    <link rel="icon" type="image/png" href="/icon-192.png">
    <link rel="apple-touch-icon" href="/icon-192.png">
    <meta name="mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="{{ company_name[:12] }}">
    <style>
        :root{--navy:#0f2547;--blue:#2563eb;--bg:#f1f5f9;--text:#0f172a;--muted:#64748b;--line:#e2e8f0;--green:#15803d;--red:#b91c1c}
        *{margin:0;padding:0;box-sizing:border-box}
        body{font-family:'Inter','Segoe UI',system-ui,-apple-system,Roboto,Arial,sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.5}
        .topbar{background:var(--navy);position:sticky;top:0;z-index:10;box-shadow:0 1px 4px rgba(0,0,0,.25)}
        .topbar-inner{max-width:1200px;margin:0 auto;padding:0 24px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;min-height:60px}
        .brand{color:#fff;font-weight:700;font-size:1.1rem;display:flex;align-items:center;gap:10px}
        .brand-mark{width:32px;height:32px;border-radius:8px;background:var(--blue);display:inline-flex;align-items:center;justify-content:center;font-size:1rem}
        .nav{display:flex;gap:4px;flex-wrap:wrap;padding:8px 0}
        .nav a{color:#cbd5e1;text-decoration:none;padding:8px 14px;border-radius:8px;font-weight:500;font-size:.92rem;transition:background .15s,color .15s}
        .nav a:hover{background:rgba(255,255,255,.1);color:#fff}
        .nav a.active{background:rgba(255,255,255,.18);color:#fff}
        .container{max-width:1200px;margin:0 auto;padding:28px 24px 40px}
        .footer{text-align:center;color:var(--muted);font-size:.82rem;padding:0 24px 28px}
        .page-title{font-size:1.25rem;font-weight:700;color:var(--text);margin-bottom:16px}
        .card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:24px;margin-bottom:20px;box-shadow:0 1px 2px rgba(15,23,42,.05);overflow-x:auto}
        .stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px;margin-bottom:20px}
        .stat-card{background:#fff;border:1px solid var(--line);border-left:4px solid var(--blue);border-radius:12px;padding:16px 20px}
        .stat-card .num{font-size:1.9rem;font-weight:700;line-height:1.1}
        .stat-card .lbl{color:var(--muted);font-size:.78rem;margin-top:4px;text-transform:uppercase;letter-spacing:.06em}
        .form-group{margin-bottom:18px}
        .form-group label,.card label{display:block;margin-bottom:6px;font-weight:600;color:#334155;font-size:.9rem}
        .form-group input,.form-group select,.card input[type=date],.card input[type=month]{width:100%;padding:10px 12px;border:1px solid #cbd5e1;border-radius:8px;font-size:.95rem;background:#fff;font-family:inherit}
        .card input[type=date],.card input[type=month]{width:auto;display:inline-block}
        .card form label{display:inline-block;margin-bottom:0}
        input:focus,select:focus{outline:none;border-color:var(--blue);box-shadow:0 0 0 3px rgba(37,99,235,.15)}
        .btn{display:inline-block;padding:10px 20px;background:var(--blue);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:.92rem;font-weight:600;text-decoration:none;font-family:inherit;transition:background .15s}
        .btn:hover{background:#1d4ed8}
        .btn-success{background:var(--green)}.btn-success:hover{background:#166534}
        .btn-danger{background:var(--red)}.btn-danger:hover{background:#991b1b}
        table{width:100%;border-collapse:collapse;margin-top:8px;min-width:560px}
        table th{background:#f8fafc;color:var(--muted);padding:12px 14px;text-align:left;font-weight:600;text-transform:uppercase;font-size:.74rem;letter-spacing:.05em;border-bottom:2px solid var(--line)}
        table td{padding:12px 14px;border-bottom:1px solid var(--line);color:#1e293b}
        table tbody tr:nth-child(even){background:#fafbfd}
        table tbody tr:hover{background:#f1f5f9}
        .status-in,.status-out{font-weight:600;font-size:.8rem;padding:4px 12px;border-radius:999px;display:inline-block}
        .status-in{color:#166534;background:#dcfce7}
        .status-out{color:#475569;background:#e2e8f0}
        .clock-display{text-align:center;font-size:3rem;font-weight:700;color:var(--navy);margin:6px 0 20px;font-variant-numeric:tabular-nums}
        .date-display{text-align:center;color:var(--muted);font-weight:500;margin-bottom:4px}
        .success-msg,.error-msg{padding:12px 16px;border-radius:8px;margin-bottom:18px;font-weight:500;font-size:.92rem}
        .success-msg{background:#f0fdf4;color:#166534;border:1px solid #bbf7d0}
        .error-msg{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
        .button-group{display:flex;gap:12px;flex-wrap:wrap}
        .button-group .btn{flex:1;min-width:150px;padding:12px 20px}
        .topbar{padding-top:env(safe-area-inset-top)}
        @media(max-width:640px){.container{padding:18px 14px 30px}.card{padding:16px}.clock-display{font-size:2.3rem}.topbar-inner{padding:10px 14px 0}}
    </style>
</head>
<body>
    {% set p = request.path %}
    <div class="topbar"><div class="topbar-inner">
        <div class="brand"><span class="brand-mark">{{ company_name[:1] }}</span>{{ company_name }}</div>
        <div class="nav">
            <a href="/" class="{{ 'active' if p == '/' }}">Clock In/Out</a>
            <a href="/employees" class="{{ 'active' if p == '/employees' }}">Employees</a>
            {% if is_admin %}
            <a href="/records" class="{{ 'active' if p == '/records' }}">Attendance Records</a>
            <a href="/reports" class="{{ 'active' if p == '/reports' }}">Reports</a>
            <a href="/export">Export Excel</a>
            {% endif %}
            {% if is_admin %}<a href="/logout">Logout</a>{% else %}<a href="/login" class="{{ 'active' if p == '/login' }}">Admin Login</a>{% endif %}
        </div>
    </div></div>
    <div class="container">
        {% block content %}{% endblock %}
    </div>
    <div class="footer">&copy; {{ company_name }} &middot; Attendance Management System<span id="install-box" style="display:none;"> &middot; <a href="#" id="install-link">Install app on this device</a></span><span id="ios-hint" style="display:none;"> &middot; To install: tap Share, then &ldquo;Add to Home Screen&rdquo;</span></div>
    <script>
        function updateClock() {
            const now = new Date();
            const timeStr = now.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            const dateStr = now.toLocaleDateString('en-US', { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
            const clockEl = document.getElementById('live-clock');
            const dateEl = document.getElementById('live-date');
            if (clockEl) {
                clockEl.textContent = timeStr;
            }
            if (dateEl) {
                dateEl.textContent = dateStr;
            }
        }
        setInterval(updateClock, 1000);
        updateClock();
    </script>
<script>
        if ('serviceWorker' in navigator) {
            window.addEventListener('load', function () {
                navigator.serviceWorker.register('/sw.js').catch(function () {});
            });
        }
        var deferredPrompt = null;
        window.addEventListener('beforeinstallprompt', function (e) {
            e.preventDefault();
            deferredPrompt = e;
            document.getElementById('install-box').style.display = 'inline';
        });
        document.getElementById('install-link').addEventListener('click', function (ev) {
            ev.preventDefault();
            if (deferredPrompt) {
                deferredPrompt.prompt();
                deferredPrompt = null;
                document.getElementById('install-box').style.display = 'none';
            }
        });
        (function () {
            var ios = /iPad|iPhone|iPod/.test(navigator.userAgent);
            var standalone = window.navigator.standalone || window.matchMedia('(display-mode: standalone)').matches;
            if (ios && !standalone) { document.getElementById('ios-hint').style.display = 'inline'; }
        })();
    </script>
</body>
</html>
'''

LOGIN_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
    <div class="card" style="max-width:480px;margin:0 auto;">
        <h2 class="page-title">Admin Login</h2>
        {% if message %}
            <div class="error-msg">{{ message }}</div>
        {% endif %}
        <form method="POST" action="/login">
            <input type="hidden" name="next" value="{{ next or '' }}">
            <div class="form-group">
                <label>Admin Password:</label>
                <input type="password" name="password" required placeholder="Enter admin password">
            </div>
            <div style="display:flex;gap:10px;">
                <button type="submit" class="btn btn-success">Login</button>
                <a href="/" class="btn">Back</a>
            </div>
        </form>
    </div>
''')


HOME_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
    <div class="stats-grid">
        <div class="stat-card"><div class="num">{{ total_employees }}</div><div class="lbl">Total Employees</div></div>
        <div class="stat-card"><div class="num">{{ working_now }}</div><div class="lbl">Working Now</div></div>
        <div class="stat-card"><div class="num">{{ completed_today }}</div><div class="lbl">Completed Today</div></div>
    </div>
    <div class="card">
        <h2 class="page-title">Clock In/Out</h2>
        <div class="date-display" id="live-date"></div>
        <div class="clock-display" id="live-clock"></div>
        {% if message %}
        <div class="{% if success %}success-msg{% else %}error-msg{% endif %}">
            {{ message }}
        </div>
        {% endif %}
        <form method="POST" action="/clock">
            <div class="form-group">
                <label>Select Employee:</label>
                <select name="employee_id" required>
                    <option value="">-- Choose Employee --</option>
                    {% for emp_id, emp in employees.items() %}
                    <option value="{{ emp_id }}">{{ emp.name }} ({{ emp.department }})</option>
                    {% endfor %}
                </select>
            </div>
            <div class="button-group">
                <button type="submit" name="action" value="in" class="btn btn-success">Clock In</button>
                <button type="submit" name="action" value="out" class="btn btn-danger">Clock Out</button>
            </div>
        </form>
    </div>
    
    <div class="card">
        <h3 class="page-title">Today's Activity</h3>
        <table>
            <thead>
                <tr>
                    <th>Employee</th>
                    <th>Department</th>
                    <th>Clock In</th>
                    <th>Clock Out</th>
                    <th>Status</th>
                </tr>
            </thead>
            <tbody>
                {% for record in today_records %}
                <tr>
                    <td><strong>{{ record.employee_name }}</strong></td>
                    <td>{{ record.department }}</td>
                    <td>{{ record.clock_in }}</td>
                    <td>{{ record.clock_out or '-' }}</td>
                    <td>
                        <span class="{% if record.clock_out %}status-out{% else %}status-in{% endif %}">
                            {% if record.clock_out %}Clocked Out{% else %}Working{% endif %}
                        </span>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
''')


EMPLOYEES_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
    <div class="card">
        <h2 class="page-title">Add New Employee</h2>
        {% if message %}
        <div class="success-msg">{{ message }}</div>
        {% endif %}
        {% if is_admin %}
        <form method="POST" action="/add_employee">
            <div class="form-group">
                <label>Employee Name:</label>
                <input type="text" name="name" required placeholder="Enter full name">
            </div>
            <div class="form-group">
                <label>Department:</label>
                <input type="text" name="department" required placeholder="e.g., IT, HR, Sales">
            </div>
            <div class="form-group">
                <label>Position:</label>
                <input type="text" name="position" required placeholder="e.g., Manager, Developer">
            </div>
            <button type="submit" class="btn">Add Employee</button>
        </form>
        {% else %}
        <p style="color:#64748b;">Only an admin can add employees. <a href="/login?next=/employees">Admin login</a></p>
        {% endif %}
    </div>
    
    <div class="card">
        <h3 class="page-title">Employee List</h3>
        <table>
            <thead>
                <tr>
                    <th>ID</th>
                    <th>Name</th>
                    <th>Department</th>
                    <th>Position</th>
                    <th>Join Date</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for emp_id, emp in employees.items() %}
                <tr>
                    <td><strong>{{ emp_id }}</strong></td>
                    <td>{{ emp.name }}</td>
                    <td>{{ emp.department }}</td>
                    <td>{{ emp.position }}</td>
                    <td>{{ emp.join_date }}</td>
                    <td>
                        {% if is_admin %}
                            <form method="POST" action="/delete_employee" style="display:inline;" onsubmit="return confirm('Delete this employee and all of their attendance records?');">
                                <input type="hidden" name="emp_id" value="{{ emp_id }}">
                                <button type="submit" class="btn btn-danger" style="margin-left:6px;padding:7px 12px;">Delete</button>
                            </form>
                        {% else %}
                            <!-- Non-admin: no delete option -->
                            <span style="color:#718096;font-weight:600;">—</span>
                        {% endif %}
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
''')


RECORDS_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
    <div class="card">
        <h2 class="page-title">Attendance Records</h2>
        {% if message %}<div class="success-msg">{{ message }}</div>{% endif %}
        <p style="margin-bottom:16px;"><a href="/edit_record" class="btn">+ Add / Correct Record</a></p>
        <form method="GET" action="/records" style="margin-bottom:16px;">
            <label>Filter by date:</label>
            <input type="date" name="date" value="{{ filter_date or '' }}" style="padding:8px;border-radius:8px;border:1px solid #e2e8f0;margin-left:8px;">
            <button class="btn" style="margin-left:8px;">Apply</button>
            <a href="/records" class="btn" style="margin-left:8px;">Clear</a>
        </form>
        <form method="GET" action="/export" style="margin-bottom:16px;">
            <label>Download monthly Excel sheet:</label>
            <input type="month" name="month" value="{{ current_month }}" style="padding:8px;border-radius:8px;border:1px solid #e2e8f0;margin-left:8px;">
            <button class="btn" style="margin-left:8px;">Download</button>
        </form>
        <table>
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Employee</th>
                    <th>Department</th>
                    <th>Clock In</th>
                    <th>Clock Out</th>
                    <th>Time Worked</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                {% for record in records %}
                <tr>
                    <td>{{ record.date }}</td>
                    <td>{{ record.employee_name }}</td>
                    <td>{{ record.department }}</td>
                    <td>{{ record.clock_in }}</td>
                    <td>{{ record.clock_out or '-' }}</td>
                    <td>{{ record.hours_worked }}</td>
                    <td>
                        <a href="/edit_record?date={{ record.date }}&emp_id={{ record.employee_id }}" class="btn" style="padding:6px 12px;">Edit</a>
                        <form method="POST" action="/delete_record" style="display:inline;" onsubmit="return confirm('Delete this record?');">
                            <input type="hidden" name="date" value="{{ record.date }}">
                            <input type="hidden" name="emp_id" value="{{ record.employee_id }}">
                            <button type="submit" class="btn btn-danger" style="padding:6px 12px;">Delete</button>
                        </form>
                    </td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
''')


EDIT_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
    <div class="card" style="max-width:560px;margin:0 auto;">
        <h2 class="page-title">{{ 'Edit Record' if editing else 'Add / Correct Record' }}</h2>
        {% if message %}<div class="error-msg">{{ message }}</div>{% endif %}
        <form method="POST" action="/save_record">
            <div class="form-group">
                <label>Employee:</label>
                {% if editing %}
                <input type="hidden" name="emp_id" value="{{ emp_id }}">
                <input type="text" value="{{ employees[emp_id].name if emp_id in employees else emp_id }}" disabled>
                {% else %}
                <select name="emp_id" required>
                    <option value="">-- Choose Employee --</option>
                    {% for eid, e in employees.items() %}
                    <option value="{{ eid }}" {{ 'selected' if eid == emp_id }}>{{ e.name }} ({{ e.department }})</option>
                    {% endfor %}
                </select>
                {% endif %}
            </div>
            <div class="form-group">
                <label>Date:</label>
                {% if editing %}
                <input type="hidden" name="date" value="{{ date }}">
                <input type="text" value="{{ date }}" disabled>
                {% else %}
                <input type="date" name="date" value="{{ date }}" required>
                {% endif %}
            </div>
            <div class="form-group">
                <label>Clock In:</label>
                <input type="time" step="1" name="clock_in" value="{{ clock_in }}" required>
            </div>
            <div class="form-group">
                <label>Clock Out (leave empty if still working):</label>
                <input type="time" step="1" name="clock_out" value="{{ clock_out }}">
            </div>
            <div style="display:flex;gap:10px;">
                <button type="submit" class="btn btn-success">Save</button>
                <a href="/records" class="btn" style="background:#64748b;">Cancel</a>
            </div>
        </form>
    </div>
''')


REPORTS_TEMPLATE = BASE_TEMPLATE.replace('{% block content %}{% endblock %}', '''
    <div class="card">
        <h2 class="page-title">Weekly Attendance Report</h2>
        <table>
            <thead>
                <tr>
                    <th>Employee</th>
                    <th>Department</th>
                    <th>Days Present</th>
                    <th>Total Time</th>
                    <th>Average Time/Day</th>
                </tr>
            </thead>
            <tbody>
                {% for emp_id, report in weekly_report.items() %}
                <tr>
                    <td>{{ report.name }}</td>
                    <td>{{ report.department }}</td>
                    <td>{{ report.days_present }}</td>
                    <td>{{ report.total_text }}</td>
                    <td>{{ report.avg_text }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
''')


def hours_between(date_str, clock_in, clock_out):
    """Hours worked on a record. Returns None if the times are invalid
    (for example clock-out earlier than clock-in)."""
    try:
        a = datetime.strptime(f"{date_str} {clock_in}", '%Y-%m-%d %H:%M:%S')
        b = datetime.strptime(f"{date_str} {clock_out}", '%Y-%m-%d %H:%M:%S')
    except Exception:
        return None
    hours = (b - a).total_seconds() / 3600
    if hours < 0:
        return None
    return hours


def format_duration(hours, signed=False):
    """7.5 -> '7h 30m'. Under one minute -> '36s'. signed=True adds +/-."""
    if hours is None:
        return ''
    secs = round(abs(hours) * 3600)
    if 0 < secs < 60 and not signed:
        return f'{secs}s'
    h, m = divmod(round(secs / 60), 60)
    text = f'{h}h {m:02d}m'
    if signed:
        text = ('-' if hours < 0 else '+') + text if (h or m) else text
    return text


def parse_bool_param(val):
    if val is None:
        return None
    return str(val) in ('1', 'true', 'True', 'yes', 'Yes')


def parse_time(text):
    text = (text or '').strip()
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(text, fmt).strftime('%H:%M:%S')
        except ValueError:
            pass
    return None


def valid_date(text):
    try:
        datetime.strptime(text or '', '%Y-%m-%d')
        return True
    except ValueError:
        return False


def safe_next(url):
    if url and url.startswith('/') and not url.startswith('//') and '\\' not in url:
        return url
    return None


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            if request.method == 'GET':
                target = request.full_path if request.query_string else request.path
            else:
                target = url_for('employees')
            return redirect(url_for('login', next=target))
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_company():
    return {'company_name': COMPANY_NAME}


@app.route('/login', methods=['GET', 'POST'])
def login():
    nxt = safe_next(request.values.get('next'))
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        if hmac.compare_digest(pwd.encode('utf-8'), ADMIN_PASSWORD.encode('utf-8')):
            session['is_admin'] = True
            return redirect(nxt or url_for('employees'))
        time.sleep(1)  # slows down password guessing
        return render_template_string(LOGIN_TEMPLATE, message='Invalid password', next=nxt, is_admin=False)
    return render_template_string(LOGIN_TEMPLATE, next=nxt, is_admin=session.get('is_admin', False))


@app.route('/logout')
def logout():
    session.pop('is_admin', None)
    return redirect(url_for('index'))


@app.route('/')
def index():
    data = load_data()
    today = now_local().strftime('%Y-%m-%d')
    today_records = [r for r in data['attendance'] if r['date'] == today]
    today_records = sorted(today_records, key=lambda r: r.get('clock_in', ''), reverse=False)
    message = request.args.get('message')
    success = parse_bool_param(request.args.get('success'))
    return render_template_string(HOME_TEMPLATE,
                                 employees=data['employees'],
                                 today_records=today_records,
                                 message=message,
                                 success=success,
                                 total_employees=len(data['employees']),
                                 working_now=sum(1 for r in today_records if not r.get('clock_out')),
                                 completed_today=sum(1 for r in today_records if r.get('clock_out')),
                                 is_admin=session.get('is_admin', False))


@app.route('/clock', methods=['POST'])
def clock():
    employee_id = request.form.get('employee_id', '')
    action = request.form.get('action')

    emp = store_get_employee(employee_id) if employee_id else None
    if not emp:
        return redirect(url_for('index', message='Employee not found', success=0))

    now = now_local()
    today = now.strftime('%Y-%m-%d')

    if action == 'in':
        rec = {
            'employee_id': employee_id,
            'employee_name': emp['name'],
            'department': emp['department'],
            'date': today,
            'clock_in': now.strftime('%H:%M:%S'),
            'clock_out': None,
        }
        if store_create_record(rec):   # atomic: a second click cannot create a duplicate
            return redirect(url_for('index', message=f'{emp["name"]} clocked in successfully!', success=1))
        existing = store_get_record(today, employee_id) or {}
        msg = 'Already clocked out for today.' if existing.get('clock_out') else 'Already clocked in!'
        return redirect(url_for('index', message=msg, success=0))

    if action == 'out':
        rec = store_get_record(today, employee_id)
        if not rec:
            return redirect(url_for('index', message='No clock-in record found!', success=0))
        if rec.get('clock_out'):
            return redirect(url_for('index', message='Already clocked out!', success=0))
        rec['clock_out'] = now.strftime('%H:%M:%S')
        store_set_record(rec)
        return redirect(url_for('index', message=f'{emp["name"]} clocked out successfully!', success=1))

    return redirect(url_for('index', message='Invalid action', success=0))


@app.route('/employees')
def employees():
    data = load_data()
    message = request.args.get('message')
    return render_template_string(EMPLOYEES_TEMPLATE, employees=data['employees'], message=message, is_admin=session.get('is_admin', False))


@app.route('/add_employee', methods=['POST'])
@admin_required
def add_employee():
    name = request.form.get('name', '').strip()
    department = request.form.get('department', '').strip()
    position = request.form.get('position', '').strip()

    if not name or not department or not position:
        return redirect(url_for('employees', message='All fields are required'))
    emp = {
        'name': name,
        'department': department,
        'position': position,
        'join_date': now_local().strftime('%Y-%m-%d'),
    }
    for _ in range(10):
        emp_id = f'EMP{next_counter():03d}'
        if store_create_employee(emp_id, emp):
            return redirect(url_for('employees', message=f'Employee {name} added successfully!'))
    return redirect(url_for('employees', message='Could not create the employee, please try again'))


@app.route('/delete_employee', methods=['POST'])
@admin_required
def delete_employee():
    emp_id = request.form.get('emp_id', '')
    if not store_get_employee(emp_id):
        return redirect(url_for('employees', message='Employee not found'))
    store_delete_employee(emp_id)
    return redirect(url_for('employees', message=f'Employee {emp_id} deleted'))


@app.route('/records')
@admin_required
def records():
    data = load_data()
    filter_date = request.args.get('date')
    records = list(data['attendance'])

    if filter_date:
        records = [r for r in records if r['date'] == filter_date]

    for record in records:
        if record.get('clock_out'):
            h = hours_between(record['date'], record.get('clock_in'), record['clock_out'])
            record['hours_worked'] = format_duration(h) if h is not None else 'Check times'
        else:
            record['hours_worked'] = 'In Progress'

    records = list(reversed(records))
    return render_template_string(RECORDS_TEMPLATE, records=records, filter_date=filter_date,
                                  current_month=now_local().strftime('%Y-%m'),
                                  message=request.args.get('message'),
                                  is_admin=session.get('is_admin', False))


def render_edit(emp_id, date, clock_in, clock_out, message=None):
    data = load_data()
    editing = bool(emp_id and valid_date(date) and store_get_record(date, emp_id))
    return render_template_string(EDIT_TEMPLATE, employees=data['employees'], editing=editing,
                                  emp_id=emp_id, date=date, clock_in=clock_in, clock_out=clock_out,
                                  message=message, is_admin=True)


@app.route('/edit_record')
@admin_required
def edit_record():
    emp_id = request.args.get('emp_id', '')
    date = request.args.get('date', '')
    rec = store_get_record(date, emp_id) if (emp_id and valid_date(date)) else None
    return render_edit(emp_id, date or now_local().strftime('%Y-%m-%d'),
                       (rec or {}).get('clock_in') or '', (rec or {}).get('clock_out') or '')


@app.route('/save_record', methods=['POST'])
@admin_required
def save_record():
    emp_id = request.form.get('emp_id', '')
    date = request.form.get('date', '')
    ci_raw = request.form.get('clock_in', '')
    co_raw = request.form.get('clock_out', '')
    emp = store_get_employee(emp_id) if emp_id else None
    ci = parse_time(ci_raw)
    co = parse_time(co_raw) if co_raw.strip() else None

    err = None
    if not emp:
        err = 'Please choose an employee.'
    elif not valid_date(date):
        err = 'Please enter a valid date.'
    elif not ci:
        err = 'Please enter a valid clock-in time.'
    elif co_raw.strip() and not co:
        err = 'Please enter a valid clock-out time.'
    elif co and hours_between(date, ci, co) is None:
        err = 'Clock-out must be later than clock-in.'
    if err:
        return render_edit(emp_id, date, ci_raw, co_raw, err)

    rec = store_get_record(date, emp_id) or {}
    rec.update({'employee_id': emp_id, 'employee_name': emp['name'], 'department': emp['department'],
                'date': date, 'clock_in': ci, 'clock_out': co})
    store_set_record(rec)
    return redirect(url_for('records', message=f'Record saved for {emp["name"]} on {date}'))


@app.route('/delete_record', methods=['POST'])
@admin_required
def delete_record():
    store_delete_record(request.form.get('date', ''), request.form.get('emp_id', ''))
    return redirect(url_for('records', message='Record deleted'))


@app.route('/reports')
@admin_required
def reports():
    data = load_data()
    week_ago_dt = now_local() - timedelta(days=7)
    week_ago = week_ago_dt.strftime('%Y-%m-%d')

    weekly_report = {}
    for record in data['attendance']:
        if record['date'] >= week_ago and record.get('clock_out'):
            emp_id = record['employee_id']
            if emp_id not in weekly_report:
                emp = data['employees'].get(emp_id, {})
                weekly_report[emp_id] = {
                    'name': emp.get('name', 'Unknown'),
                    'department': emp.get('department', 'Unknown'),
                    'days_present': 0,
                    'total_hours': 0.0
                }
            hours = hours_between(record['date'], record.get('clock_in'), record['clock_out']) or 0.0

            weekly_report[emp_id]['days_present'] += 1
            weekly_report[emp_id]['total_hours'] += hours

    for emp_id in weekly_report:
        days = weekly_report[emp_id]['days_present']
        weekly_report[emp_id]['avg_hours'] = weekly_report[emp_id]['total_hours'] / days if days > 0 else 0.0
        weekly_report[emp_id]['total_text'] = format_duration(weekly_report[emp_id]['total_hours'])
        weekly_report[emp_id]['avg_text'] = format_duration(weekly_report[emp_id]['avg_hours'])

    return render_template_string(REPORTS_TEMPLATE, weekly_report=weekly_report, is_admin=session.get('is_admin', False))

COMPANY_NAME = os.environ.get('COMPANY_NAME', 'Employee Attendance System')
DURATION_FMT = '[h]"h "mm"m"'
WORK_HOURS_PER_DAY = float(os.environ.get('WORK_HOURS_PER_DAY', '8'))


def build_export_rows(data, filter_date=None, month=None):
    rows = []
    for r in sorted(data['attendance'], key=lambda x: (x['date'], x.get('clock_in') or '')):
        if filter_date and r['date'] != filter_date:
            continue
        if month and not r['date'].startswith(month):
            continue
        if r.get('clock_out'):
            h = hours_between(r['date'], r.get('clock_in'), r['clock_out'])
            hours, status = (h, 'Completed') if h is not None else (None, 'Check times')
        else:
            hours, status = None, 'In Progress'
        try:
            day_name = datetime.strptime(r['date'], '%Y-%m-%d').strftime('%A')
        except Exception:
            day_name = ''
        rows.append({
            'date': r['date'], 'day': day_name, 'emp_id': r['employee_id'],
            'name': r.get('employee_name', ''), 'dept': r.get('department', ''),
            'clock_in': r.get('clock_in') or '', 'clock_out': r.get('clock_out') or '',
            'hours': hours, 'status': status,
        })
    return rows


def build_excel(rows, filter_date, month=None, report_title='Attendance Sheet'):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    NAVY, BAND, WHITE = '1F3864', 'F2F5FA', 'FFFFFF'
    thin = Side(style='thin', color='BFC7D5')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_font = Font(name='Calibri', bold=True, color=WHITE, size=11)
    head_fill = PatternFill('solid', fgColor=NAVY)
    center = Alignment(horizontal='center', vertical='center')
    left = Alignment(horizontal='left', vertical='center', indent=1)

    if month:
        period = datetime.strptime(month, '%Y-%m').strftime('%B %Y')
    elif filter_date:
        period = datetime.strptime(filter_date, '%Y-%m-%d').strftime('%d %B %Y')
    elif rows:
        d1 = datetime.strptime(rows[0]['date'], '%Y-%m-%d').strftime('%d %b %Y')
        d2 = datetime.strptime(rows[-1]['date'], '%Y-%m-%d').strftime('%d %b %Y')
        period = d1 if d1 == d2 else f'{d1} - {d2}'
    else:
        period = 'No records'
    generated = now_local().strftime('%d %B %Y, %I:%M %p')

    def title_block(ws, title, last_col):
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
        ws.merge_cells(start_row=3, start_column=1, end_row=3, end_column=last_col)
        ws['A1'] = COMPANY_NAME
        ws['A1'].font = Font(name='Calibri', bold=True, size=18, color=WHITE)
        for c in range(1, last_col + 1):
            ws.cell(row=1, column=c).fill = PatternFill('solid', fgColor=NAVY)
        ws['A2'].font = Font(name='Calibri', bold=True, size=14, color=NAVY)
        ws['A2'] = title
        ws['A2'].font = Font(name='Calibri', bold=True, size=13, color='444444')
        ws['A3'] = f'Period: {period}     |     Generated: {generated}'
        ws['A3'].font = Font(name='Calibri', italic=True, size=10, color='666666')
        ws.row_dimensions[1].height = 34
        for r in (1, 2, 3):
            ws.cell(row=r, column=1).alignment = left

    def header_row(ws, row, headers):
        for i, h in enumerate(headers, 1):
            c = ws.cell(row=row, column=i, value=h)
            c.font, c.fill, c.alignment, c.border = head_font, head_fill, center, border
        ws.row_dimensions[row].height = 22

    def page_setup(ws, header_row_num):
        ws.page_setup.orientation = 'landscape'
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        ws.print_title_rows = f'{header_row_num}:{header_row_num}'
        ws.oddFooter.center.text = 'Page &P of &N'
        ws.sheet_view.showGridLines = False

    wb = Workbook()
    wb.properties.title = report_title
    wb.properties.creator = COMPANY_NAME

    # ---------------- Sheet 1: Daily attendance ----------------
    ws = wb.active
    ws.title = 'Attendance'
    ws.sheet_properties.tabColor = NAVY
    headers = ['Date', 'Day', 'Employee ID', 'Employee Name', 'Department',
               'Clock In', 'Clock Out', 'Time Worked', 'Status']
    title_block(ws, report_title, len(headers))
    HR = 5
    header_row(ws, HR, headers)

    status_style = {
        'Completed': ('1E7B34', 'E6F4EA'),
        'In Progress': ('9A6700', 'FFF4CE'),
        'Check times': ('B42318', 'FDE8E8'),
    }
    r = HR
    for i, row in enumerate(rows):
        r += 1
        vals = [
            datetime.strptime(row['date'], '%Y-%m-%d').date(), row['day'], row['emp_id'],
            row['name'], row['dept'],
            datetime.strptime(row['clock_in'], '%H:%M:%S').time() if row['clock_in'] else None,
            datetime.strptime(row['clock_out'], '%H:%M:%S').time() if row['clock_out'] else None,
            round(row['hours'] * 60) / 1440 if row['hours'] is not None else None, row['status'],
        ]
        for col, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=col, value=v)
            c.border = border
            c.font = Font(name='Calibri', size=11)
            c.alignment = left if col in (4, 5) else center
            if i % 2 == 1:
                c.fill = PatternFill('solid', fgColor=BAND)
        ws.cell(row=r, column=1).number_format = 'dd-mmm-yyyy'
        ws.cell(row=r, column=6).number_format = 'hh:mm AM/PM'
        ws.cell(row=r, column=7).number_format = 'hh:mm AM/PM'
        ws.cell(row=r, column=8).number_format = DURATION_FMT
        fg, bg = status_style[row['status']]
        sc = ws.cell(row=r, column=9)
        sc.font = Font(name='Calibri', bold=True, size=11, color=fg)
        sc.fill = PatternFill('solid', fgColor=bg)

    first, last = HR + 1, r
    if rows:
        r += 1
        ws.cell(row=r, column=1, value='TOTAL')
        ws.cell(row=r, column=8, value=f'=SUBTOTAL(109,H{first}:H{last})')
        ws.cell(row=r, column=9, value=f'=SUBTOTAL(103,A{first}:A{last})&" records"')
        for col in range(1, len(headers) + 1):
            c = ws.cell(row=r, column=col)
            c.font = Font(name='Calibri', bold=True, size=11, color=NAVY)
            c.fill = PatternFill('solid', fgColor='DCE3F0')
            c.border = Border(top=Side(style='medium', color=NAVY), bottom=thin, left=thin, right=thin)
            c.alignment = center
        ws.cell(row=r, column=8).number_format = DURATION_FMT
        ws.auto_filter.ref = f'A{HR}:{get_column_letter(len(headers))}{last}'
    ws.freeze_panes = ws.cell(row=HR + 1, column=1)
    for i, w in enumerate([14, 13, 14, 24, 18, 12, 12, 14, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    page_setup(ws, HR)

    # ---------------- Sheet 2: Summary per employee ----------------
    ws2 = wb.create_sheet('Summary')
    ws2.sheet_properties.tabColor = '2563EB'
    summary = {}
    for row in rows:
        e = summary.setdefault(row['emp_id'], {'name': row['name'], 'dept': row['dept'],
                                               'days': 0, 'hours': 0.0})
        if row['status'] == 'Completed':
            e['days'] += 1
            e['hours'] += row['hours']
    sh = ['Employee ID', 'Employee Name', 'Department', 'Days Present', 'Total Time',
          'Average / Day', f'Expected ({WORK_HOURS_PER_DAY:g}h/day)', 'Difference']
    title_block(ws2, report_title.replace('Attendance Sheet', 'Employee Summary'), len(sh))
    header_row(ws2, HR, sh)

    def summary_row(r, vals, diff_hours, fill=None, bold=False, color=None):
        for col, v in enumerate(vals, 1):
            c = ws2.cell(row=r, column=col, value=v)
            c.border = border
            c.font = Font(name='Calibri', size=11, bold=bold, color=color)
            c.alignment = left if col in (2, 3) else center
            if fill:
                c.fill = PatternFill('solid', fgColor=fill)
        d = ws2.cell(row=r, column=8)
        d.font = Font(name='Calibri', bold=True, size=11,
                      color='1E7B34' if diff_hours >= 0 else 'B42318')

    r = HR
    tot_days = tot_hours = 0
    for i, (eid, e) in enumerate(sorted(summary.items())):
        r += 1
        expected = e['days'] * WORK_HOURS_PER_DAY
        avg = e['hours'] / e['days'] if e['days'] else 0
        tot_days += e['days']
        tot_hours += e['hours']
        summary_row(r, [eid, e['name'], e['dept'], e['days'], format_duration(e['hours']),
                        format_duration(avg), format_duration(expected),
                        format_duration(e['hours'] - expected, signed=True)],
                    e['hours'] - expected, fill=BAND if i % 2 else None)
    if summary:
        r += 1
        tot_expected = tot_days * WORK_HOURS_PER_DAY
        summary_row(r, ['TOTAL', '', '', tot_days, format_duration(tot_hours),
                        format_duration(tot_hours / tot_days if tot_days else 0),
                        format_duration(tot_expected),
                        format_duration(tot_hours - tot_expected, signed=True)],
                    tot_hours - tot_expected, fill='DCE3F0', bold=True, color=NAVY)
        ws2.cell(row=r, column=8).font = Font(
            name='Calibri', bold=True, size=11,
            color='1E7B34' if tot_hours - tot_expected >= 0 else 'B42318')
    ws2.freeze_panes = ws2.cell(row=HR + 1, column=1)
    for i, w in enumerate([14, 24, 18, 14, 14, 16, 26, 14], 1):
        ws2.column_dimensions[get_column_letter(i)].width = w
    page_setup(ws2, HR)

    out = io.BytesIO()
    wb.save(out)
    out.seek(0)
    return out


@app.route('/export')
@admin_required
def export_file():
    data = load_data()
    filter_date = request.args.get('date')
    fmt = request.args.get('format', 'xlsx')
    month = request.args.get('month')

    # Default: the current month. Use ?month=all for every record.
    if filter_date:
        month = None
    elif month == 'all':
        month = None
    elif not month or not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month):
        month = now_local().strftime('%Y-%m')

    rows = build_export_rows(data, filter_date, month)

    if month:
        month_label = datetime.strptime(month, '%Y-%m').strftime('%B %Y')
        report_title = f'{month_label} Attendance Sheet'
    elif filter_date:
        day_label = datetime.strptime(filter_date, '%Y-%m-%d').strftime('%d %B %Y')
        report_title = f'Attendance Sheet - {day_label}'
    else:
        report_title = 'Attendance Sheet - All Records'
    base_name = report_title

    if fmt != 'csv':
        try:
            output = build_excel(rows, filter_date, month, report_title)
            return send_file(
                output,
                mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                as_attachment=True, download_name=base_name + '.xlsx')
        except ImportError:
            pass  # openpyxl not installed -> fall back to CSV below

    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(['Date', 'Day', 'Employee ID', 'Employee Name', 'Department',
                     'Clock In', 'Clock Out', 'Time Worked', 'Status'])
    for x in rows:
        writer.writerow([x['date'], x['day'], x['emp_id'], x['name'], x['dept'],
                         x['clock_in'], x['clock_out'],
                         format_duration(x['hours']), x['status']])
    output = io.BytesIO()
    output.write(b'\xef\xbb\xbf')  # BOM so Excel reads the file correctly
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)
    return send_file(output, mimetype='text/csv', as_attachment=True,
                     download_name=base_name + '.csv')


ICONS = {
    192: "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",
    512: "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAABasUlEQVR42u2dd4BdVdW31977lFunpffeSU8QEkoIEDoI0kUQBEQRUEGKqICKoi+ogCgCHwqoVFF6DxBIBEI6IXXSK0mm3XruOXvv748zMyQRIeacO3Nn5ve8+cM3ZCaTc89ez1prN1Y+5BgCAADQ8eB4BAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAIAAAAAAQAAAAAABAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAOjgGHgEoN3AGGvMazjb63eIiDQprb84J2KMdv+ipi9RSu/1OwBAAAC0TqxnjDFG/v9ppYnI9TyltCbtOAU/SivPa47+xLlhfvEL77keKdXsAG4YRMQY2bbFiHHOTP93OCNNmrTWpLWGFQAEAECRwz1jWmmllet5juMqpZSUpLSwTCLq0qnCMg0hxIgh/ThnhmFMHDvMEIKIpFRVlclRwwYSado9w98DTcSWrlhTU5sSghORJ+W8RSs8z1NKL1u1XkpZcL0du+qISBZc4owLwTm3bdM0BGeccea7AEoAbWNklQ85Bk8BlGbE54wRkdLKdT2n4Hqe1FJxQ0SjdmV5csiA3tGIPWbk4EQiOmbEoGQiNnhA70QsyjkrS8ZD/5EaUhmldDqbW712UyqdXbysOp3OLf54dS7vrFq7qbY+lcs5ypNMcMMQtmWapsEZJyKlNXwAIAAAviDoc84YMa2163l5p+C6Hmlt2VaXThWD+vXq07PruAOGjDtgSKfKsv59e1SWJ//bt1JKE2narXFPRI3doi9Ck6bdvqhpOoE1zyv8J7X1qXUbtu6qbVj40aqFH63auOWT6vWbd+yqKzgFYsw0jYhtmYbBGNOklYIMAAQAQHOmz5lSulBwc3lHSSkMo3NV+bBBfYYP6T925KCxo4YMG9ync1XF3pFak9Z+z79xsrexZNhr7jck/KitqXG+QWtNjPinhcoe7KypW7F646KlqxZ9XL181boV1Rt31tRLz+NCRCO2ZZn+PxmVAYAAQEeM+4JzTdp1vVy+4BVc0zZ7du88esSgqZNHTxo3fNigvn16dt0zMSellFa6eTKgGFF+v93Q3PpnnPH/KBY2bvlkRfWGDxcunz13yZJl1Vu27XQd17DMaMQyTYMRk0rBBAACAO0ZzhnnXErlFAq5nMM571RZPnbUoOmHTJwyafSYkYMqK/bo6kipNGk/xy6dcL+PSvDze0bMn09uprYutfjj6jkfLpn57rxFS6t31dYrpaJR27YsIbhSao+2FQAQAGjzcZ9xqVQu7zh5JxqL9O3ZbdrUCcdNP2j86CF9e3Vr/pNKKaU1I8b4ZzVW2izKFwJpzhjnn/pgw+btC5asemnme2/Nnr9hy/ZcNm9H7GjEFpwrDRMACAC0/Xy/Me5HIyOH9p9xxJdOnjF1zMhBsWikOVmWSvlL7Fk7CvqfVxworUkLzpv/vdlcfvHH1c++OvvVN9//eOW6XK7JBKgJAAQA2tIr1Zjk6kw2v1fcnzxuuBDC/2NSKiJqZ5n+/lUGRNTcJpJSzl24fC8TxGMRIqYwTwAgAFDCKT/nnDkFN53Occ7Gjhp83PSDTtoz7ntS8uZtXWC3ssDvEhmfClLOXbj8uVdnvzTzvUVLVyulE4mobZlKaaUUnhiAAEBpvENEQgilVSabLziF7t06nXj01FOOPfTowyfblrln3OcI+19kgsa1rc0mcArua2/Pfebld55/bfa27bss24rHIpxxKSXKAQABgNZ7exgTnLue15DKWKY5fvTQs0898owTj+jVowvifugm2Lx1x5PPv/nYP99YsGRlwXXLknHTMLB+FEAAoKXhnDNGuXwhl81VVZZ/+bjDzj3tqGlTJvi9bL+/7/8ZENwEfs+n+dm+NWf+359+/V8vzaqprY/GotGI1fxnAIAAQBERgjNiqUzW9eSgfj2/ftZxZ5w0feigPs0p/+6LW0CoJtBSqeaCYGX1xiefm/mXx1+qXr/FNEQyHtOkffUCAAGA8EO/1tSQykgpJ40dduXFZ375uEOTiVhjys+If85xOSAsDRAppUg3FgSpdPZfL71z1wNPfLhohRCiLBlnjKABAAGAkEN/fSotOD9m2oHnnHr06ScdYZkGEUkpGUPkbwWU0lorf3lVwfWeeu7NR//52itvfSCVKk8moAEAAYCg+NtW/dA/Y9qB37v0rCMPnej/JylVB9m6VdIFgdZK6eZtBG+8M++39z3+apMGiDA3ACAAsF+hn3OWSme1phnTJjeHfqW1VopzgchfShogpSRrmnZv0sBcxiiZiGHrAIAAwD6/E4wJwdPpnFMoTBw7/NYbLj1m2oGNPQfSgnM8opKl+VANInrlrQ9u/OV98xYtty0rkYhKiQWjAAIAn4thiHy+kMlkJ40bccXFp5/z5aNM00Dob6MacF3v0X+9fvcDT324cFk8HotELM+TeD4AAgB7I4SQUtbXp/r07n75had9/5tnmf40r1II/W1UA/4H57reb/70+D1/fnrjpm3l5Un/g8bzAUQkIp0G4yl09CyAMcMQdfUp2zS+fvYJf73nJ8dO/5IQXMpP28pthcZbu4iU9jfTklKkdOMv/z9+3i9Ncrc/rxt35DbdJLkvV0qWDJwxTaSkNAzjkAPHnP3lo9LZ/EfLqhtSmXgsgm4QQAUAyDBELu9kM/ljjzzop9dePHnccCKSUnIhSjzY+bFeN4VpxogxKvZ61GavMEaMiLE2YAVfA/6C0bkLl//k1w+8/MZ7sXgkGrHREYIAIIAOin9oc21N/aABfW665qKvnXFMY+gv1a28ereUnDP6zL6UUtSQU6TZqi2eJ0lpWrTG9ST5/6AVm7zatDY4/Wf6y4g8RZUJNqy34f9dhqCxA03OyBA0pKdBTJdF+X/7S1WTgRqtUIpPTyvVuG/gkSdfueX2B6vXbqysKvcPmsZwgABAx0r8U+mcYHT+WcfffM1F3btWKaWJNC+xdr/2WzeaGN87u1eKatNqzTZZk9LLN3pba9WGT2RdRm2tUZzTjrrGa1QKHmmt/S8V4gvUpjX5h2xqIsaYZfi9FOpSwZWiHlW8Is77dhU9KvnwPkZVkg3sLioTez8zpUkrIkaclZwMlFJEjHO27ZOam29/8OHHX5SakokoSgEIAHSoxL9h9KjBv/3pFUceOomIPCmbD5kplaBPtOdlupQr6E07VfUWb+kGuXS9u2WX2laratLKk+R6WnAmOAlOpsGIyBTkt2Z218a+NL53D9mNN3FpciURketpqUgqkkqbBjMEVSV490resxMf1c8c1VcM6mn07syj1h5R39+QW1IyaP6433jnw+/95O4lS1dXVpWhFIAAQPtP/NOZHGl9+UVfufmai5KJmOdJIVq/56OJtPLz7j1C9pZdqnqrt3CNN2+Vu2ab3FarUjktlTY4MwwyBTNFY+/FD+5+m2gfY/3/ME5YY5uo8X+wxk6UK8mV2vPIU1pwloyy7pV8YHcxcYg5bqAxqIfRsxPfozLQxIgYb/1pA621lMowRCqdvfn2B+958B/EWCKOUgACAO028afamvrdE38plRCt2fPRTbO4u/8UdWn94Sp3zjJ3QbW7eousSSlPkuBkmWQZTPBP468OO9Dvhxh8K/g/klRU8HTBJanIEFSV5IN7ivGDzCkjzElDzIoE270saP7CVqT5BditFCgnHCABAYB2lvhnc3nPlVdcfHpj4t+q5zY35/vNcV8q+mid98FK9+0lhRWbvK01quCRbZBtMUMQY0S6MYMu9RHlVzCMtCZPklPQjkeWQT2q+LDexuGjrQOHmgf0N3b/h7duTdB8yrRfCtz9wFOGKWLRCEoBCAC0k+hfU9vQr3f339xyxanHH9a6ib8fxJv/8pyjP97ovTqv8ObiwsrNXs4hIShiMsskzkipT1s6bXJ0+T7gpDQVXMq7WkqK2jS0l3HEGGvGRGtkHyNqs09NUPxlrF9YCvzzxVnfv+nu9Zu2VVWWwQEQAGjbbR+tdV1N/cnHH3bf7dd261LVWom/P6/bPBEqJS1c6/7jXeftJYUtNTKbp4hFEYv54a9NZPr7WRkQKU35gs4XKBahnlXi8NHWVw6xxw0w/Tn4vR5Uq5QC23fUXHrNr599cVZFVTljmBmGAEDbTPyzubyU6qZrLvrhlV8jItm0G6gVU/7lG72nZztvLSms3OTlXYpazDJJ8Mal9B1FzIw4J6mo4FKuoCMmDe1tTBttnTbVHt7HaN2CoPkl+cVdj9xy+4NCcLSDIADQ9qL/rtqGAX163P+b6448ZKJSilr2anZNpNSnmezOBvXa/MJT7+aXrPPqMipqMT/fb5fJ/v9aFvg1Qa6gK+J8dH/j9EMiR0+wOpfxTwuClp0hUFqT1pzzN96dd8n3f7V249ZOaAdBAKBtZJecaaK62oaTjz20se3jScNoucTfj1nNKf+yjd7Dr+deX1DYsFOZgmI2M0THyvf3vSbwJGUd7Urq25kfNd46/6joiN0KghbuC/mvTWM76OV3KirLGJHCxwYBgJJFCFEoFPJO4epvnXPbjZdRy8737h76XY/eWlx4ZGZuzsduOq9jNrPNxrIAfK6/iRE5LmUdnYiwKSPNr02PThvj37/Z0hpofnmuv/XeO/74aMS2LMvCSaIQAChFDEOkUtlEPHrnz6867/RjpFSMt1DbZ/fQ73d7Hno9t2itxxnFI0zwjt7q+Z+HJSPOSCrK5LXSNHaAccFR0ea+UEtqQGmtlRaC//WpV6760Z3pTC6ZjKEdBAGA0sI0jLr61LDBfR/+/Y8njB7aYm2fvUL/X2fm/zozt3GHilgUsxlS/lAKgqyj8wXq04WfNz163vRIy2vAf53mL1l5/nd+tmL1horypOt5+HQgAFAa0d80dn5Sc9jU8c889KuK8kSLRX+p9g79G3bIRIRHLHT5Q9UAI84pX6B0XvXtIvbSQMt0+PyXqq4+fcoF182avaBz1yrXhQPaPLgQpq33ChjnvGZHzTcvOu2hu35Uloz7B7y0QOjnjDijXQ3qvpdy1/6/1PMfOFKxshhj/gYufDYhlllESpMQlIiwVI7eWOQ8976TdWhITxGPsOaPo8i1CJdSxaL2GSdNr6lPvfPu/Fg8ho8GAgCt2B9gWutMJveDK8773c+usixTKV3sKV/VdHSPJ+mRmbnL/5B6ce4eoR8U9eE3a2DmIudf7zkRix3QzzRE4+dS1I4Q50wpbdvmiUdPzbve23MWmKbBOcPsDgQAWj76cylVJpN74DfXX/Otc6RUjBEvZh7Y3O5njJ5737nyj6m/v5WXkhD6W0UD8QhL5+jleYXX5hcqE3x4b4OxT8+YK17FqbVWSs84fHLfXt2eeu5NwzCEEBoSgABAy31sghcKrpTyvjuuv+Cs41zPMwxR1AMepCLOiTNauMa7+v7UPc9na1KqLMY4R+hvTQ1ELba1Rj3/gTNvtTeoh9Gzivsa4MV0AOfM9bwJY4b169Pj2VfeVUqbpgEHtEUwCdwWo7/IZnOVFcl//vmXX5owsthTvko3JpU1KXXXs9lH3sjnHF0exwqf0qkFiRHVZ3TUZl87MnLlybGqJPcP0SvqxID/4r0//+NTL7yhti4Vi0WxRQAVAGiJ6F9Rnnzx77dPGju82NHfX2TCGD35Tv47f2h4bb4btVnUZlJhXX+p4F+N4J8q+u5S98W5Tnmcj+pnFLsU4Jx7nuzbq9v0Qyf944W3U+mMbVuoAyAA0BLRf/wBQ4oa/f1bzjmjddvllfem7no263qsLMawpatkNUBEiSirz9C//p1fss4bP8isSvLmj7J4DujVvfORcAAEANpN9PcTf6nokTdy3/lDw0frZackx0xv6dO8TGjpBvmvOfl4hI3ub/ofJYcDAASA6P/F/QQiwWnddnnpXQ0PvJwTgsVtJhH625QGYjYrePT8+87cle6Bw8yqJPf35RWjFIADIADQHqK/v9SHMXro9dx3/tCwaots7iGAtoXWxDklomz1FvnPOfl4hI0fZBZvVgAOgABA247+niRD0I569e17Gv74Qk5wFkPi3/ZLgajFCi698EHho/XeIaOsRJR5kngRNgvCARAACD3682w2X+zo39z2eXdp4YI76hes9qrKGDEk/u2nFIhH2NJ18vm5zog+Rv9uokjtoM9ygAkHQABgP4dToeBWlCWKGv1VU9vnrmeyV9+fyjk6GWM47rf9lQLxCKvP6KfedQRnBw03/Sn9ojrg6RfezuYcw8AesdIFG8FKNvozKZXrerOeuad46/391T476tU1D6RenOtUJTlDx78dv1SMNFFNSh0/2b794mSXcl6kw0T91/XDRcsPO+Vy0zSE4LhKDBUA2Gcts8ZT3u7/zfXHTT+oqNF//mr33F/VL6j2OpVxnOLZvvE/3GSULV0vn3/fmTjE7NVJFGNa2K8Devfs2qdXt6eefdM0TcYYnj8EAPZ1/GQy2Qd++8MLzjy2GNG/+Vi3R9/Kf/OuhmwHa/twRkIQY41HXDBOghOxjrLBrbkd9MSsfLcKMWaAUYwj5HwHjB89tF+fHk8//yYmhCEAsE+YplHzya7rvnvB1Zed5XqeaRghj/+mY91+/HD6F49nYjYzjY6y2kdwYkS5gm7IUsEjT1LBI8eldE5LRabBBO8QGlCaTINxxp59z6nP6CPHWcWYEuCcu543YfTQnOO+9vqcZHlCYSdhqTUbMAdQatF/5yc1l1102h9/dY3nSSFEuGPSb/s0ZPUVf2h4ZX6hIsF0xzjawc/3G3La4DSyr3HwCPOAfkbPzoIRNWT1krXeBysL81d7qZzuOEecMkaMUV1aHzPBuvvbZWUxFvqUgNYkpTQM8a3rbr/3wadxjxgEAP4rhiHq69NTJo9+6bE7IpbFGIXbOfWH94ZP5BV/TM1ZVuhSzjtI24dzcj1yXH3EGOvS46IHj7SMzwpzyzd6f34t9/Rsx3F1PNJRqiJ/88eUEdbd30r27SqK4ACtNeULhePOvnrO3CXl5QncKQ8BgP/oTgiRTmeHDOj97vN/rChLKKXDvd3F3+f10Trvq7+u35VS5THmdoxhKDjlCzpisZvPS5x1WKTZhXsEKSLRdMH6vNXu1felVm2RfkbcIepOQfVZ3SnJ/3Zt+QH9Df9VCRH/Za5rSB9y4rdWrd2USMRwcHSpjA7MAZRGispc14tGrGceuq1/nx5SqnBvdvSH9PzV7vm3N9RlVDLaUaZ8OaOCR8kYf+y6iunjLKmImk453esXY6Q0SUW9O4tTp0TeX+mu3SajFusgUwJRm6Vy+sW5hS8NM3t1FuHuFmaM+fcJT5k8+rF/vVZwPcPgmBKGAMCnIySTzT3w2xuOPmyyJ6UhwkzA/Oj/xDv5i+9scD2KWh0lsW28nl7rx2+oGDPQcCUZ4vPmORkjzkkqitrshMn2K/OcT+qUZXYUB9gmyzr02Kx8z05idH8jXAdwzjwpe3bv3Ld39yeefcO2LQgAAgBERIYhanbV33DV+VddcobneUaoy36ao/93/5SKmMw0OtCRzoJTfVb//ILEMRNtV5K5b1bljKSiqMUmDjafmu10nPdQazIMYsSee9/p06UYDuCe540dOdgpuK+9+UEiEcPuMAgA0V/U1Dacctyh999xnedJIYwQ5313j/6JCBO8A+3y5ZyyeT26v/Gri5K66S77ff1aRp6kHlV8S418b7mXiLIO8tz8B2Ua7NniOIAxLqU8+vDJC5euXvjRqng8Cge08jDBI2jVIMWzOad/nx733XGd1ppzXrzozzvY4W6cyHH1N46N+kv7/9cHyxlpTZccGyuLMa8jLVxUmjijRIR990+pJ97JG4JCnC5ijDjnWuv77riuf58e2ZzDOUIQBNBR0VpLTz74uxu6dqoId9lPB4/+jJHjUa9O4ogxln/Q6X4UEJpoYA8xaaiZKWjRkQZKUR3AOVNKd+1U8eDvbpCexPZgCKCDYhiirqb+p9ddPG3KeM+TIS776eDR38/fcwU9vI/RuYzr/b0RVyliRAcONV1Xd7STbIrqACG458lpU8b/9LqL62rqi3ezKYAASjf619Q2nHLC4dd+56v+VklE/3ArACn1Af0NP47v9zchogP6G4bBOmCeWlQHGIaQUl77na+ecsLhNbUNcAAE0JEeOue5nNOvV7f7br+WtGYs5Nz/yXfy372340Z/H62pIh5C3l4eY7zDnBP3Xx1wb+rJ8OcDOGl93+3X9uvVLYfJAAigQ1EouHfe+t2unSuVUmG1/qUiQ9CiNd4PHkjFO3b0b9RhGAteO/gxxr4D4hH2gwdSi9Z4hqCwNpFwzpRSXTtX3nnrdwsFFzEBAugQGIaoran77jfPOvmYQ/zj3sKK/oLTR+u8r91ezzkTAve6UCihe+MOJVWH1oDSJARxzr52e/1H6zzBQ3OAEMLz5MnHHPLdb55VW1OHRhAE0N4fN+fpTG70qCE3XX1hiOc9KEWC06ad6txf19dnlG0Sjt0Njt/2Wf+JlFJ39DpAkW1SfUad++v6TTuVCO+0VCG4lOqmqy8cPWpIOpNDIwgCaO9jSaq7f/G9RDxKFM5hn1oTMarL6MvvaahJqY5zjGXLYBp4Bo0lZjzCalLq8nsa6jI6rPtz/CGQiEfv/sX3FF5cCKAd4zd/rrr0zMMPHhfWuk//bi/O6Kp7G+YsK5TjPvfi1AGAiDxJ5TE2Z1nhqnsb/BmmUB6Ovyr08IPHXXXpmWgEQQDt9EFznk43Nn+UCq35IzUJTjc9kn5lXqFLOXcR/UExcSV1KeevzCvc9EhacJIh2VEIrlRTIyiNRhAE0E7xmz9ah9P8kYoMTk/Myt/7YrYigdwftFAdUJFg976YfWJW3ghpQpgxpnVjIwhPGAJobxiGqN1Vd9G5J4TY/PGX/Sxc4137YKo8jtPVQcuhNZXH+bUPphauCW1RUHMj6KJzT6jdhUYQBNBeYIzl887Agb1vuuYipVQo5a3SxDntalCX3FnPiHGOVjVoUQFwTozYJXfW72pQPKRTZjnnSqmbrrlo4MDe+bzDOvjSKwigfSAEz2RyP7/h0m6dK7UO58Q3rYkRXX1/atNOFbWx6BO0NEpR1KZNO9XV96dYSFPlnDOtdbfOlT+/4dJMJicEohME0Pajf21d6tgjDz7r5OlShrPty5MkOP3+uewLc51KtP5BK+FJqkywF+Y6v38uK3g4p0QIIaSUZ508/dgjD66tS8EBEEDbRkqViEVuu/EyznloE7+C5ixzf/VkpirJEf1B6zqgKsl/9WRmzjI3rFMiGGOc89tuvCwRi0jsDIAA2i6GIerrUuedcezYUYOllMG7//6S/50N6rv3NhgcPVLQ+jBGBmffvbdhZ4MK5fgpzrmUcuyoweedcWx9XQqzwRBAGx0YLJ8v9Ond7SdXX6hUOEd++kfbX/NAasMOtP5BSeBPBmzYoa55IMVC2x7MldI/ufrCPr275fMFZDoQQNtDCJ5JZ6+69MxunSu1DuHIT3/d59/fyr/4gVOVROsflAqepKoke/ED5+9v5UNZFco501p161x51aVnZtJZzARAAG3tsXKeTucmjh9xxTdOD2Xpp9IkOG3cIW99NF0W42iNgpJCKiqL8VsfTW/cIQUPpxGklLriG6dPHD8Ce4MhgDYnAOY4ztWXnW2ZhtY6YA2rNWlNnqTv35+qy2jTwKp/UFpoTaZBdRn9/ftTnmx8Y4PAGNNaW6Zx9WVnO44T4nXZAAIoevqfSmUnjRtx2gmHK6U4DzqL5af/T8zKv7W4UB7HYZ+gRIuA8jh7a3HhiVn5kIoAoZQ67YTDJ40bkUplUQRAAG0oIdK/+sm3bcvc7xvJ94r+G3bIWx9Ll8fR/AEl7gB+62PpDWE0gvwpZdsyf/WTb2vUvBBAm0AI3tCQPnb6l6ZPnRDKlS/+gc8/fjhdk9aWQPMHlHLeQ5agmrT+8cPpUA6L9q+LmT51wrHTv9TQkMZsMATQBsYA5/wHl381rJRKcHrm386L/qZfpP+gtPEUVSbYi3OdZ/7thHh55A8u/yrnOPEQAmgj6f+hXxoT/NB/v31Un9W3PZFORtH8AW0DqSgZ5bc9ka7P6uA7A/yrAg790hgUARBAW0r/g2cr/r7fu57Jrt2uIhaaP6DNjIKIRWu3q7ueyYayN9h/81EEQAClnv6nUqGl/0oRZ7R0vffnV7MVcWz7Am0JT1JFnP351ezS9R5nQbes714EpFIoAiCAkoQx5nny/DOPIyIVOO3RRIzRbU9kcgUSOA0FtL18iHIFuu2JDGMUPGv3B9T5Zx7neRInQ0AApfccOU+lsxPHjTjx6Cla64DHPvtzvy9/6MxcVKiIM4n0H7Q1pKSKOJu5qPDyhyHMBgshtNYnHj1l4rgRqTT2BEAAJZj+u97Vl50dsS2lVJAcxZ/79STd8XSWc0LPE7RRNBHndMfTWU9SwNlgxkgpFbGtqy8723M9FAEQQGlF/1zeGdS/1ynHHqK1Dpie+HO/j8/KL1nnJSMMR36CNopSlIywJeu8x2flg88Gc8611qcce8ig/r1yuDASAigdhOC5bO7Cc06IRSNKqSCvptbEGdWm9R1PZ2I2k8j/QVtGaorZ7I6nM7VpzYMWAUwpFYtGLjznhFwWF0ZCACWD68rKyvKzTz2KiAKe+680MUZ/m5nbtFNi6Sdo6/hLQjftlH+bmWMs+OEQnIjOPvWoyspy18XMGARQAhiGSDWkv3LC4QP69JAy0Ln/zen/X17PJSLY+QXaRRGgKBHhf3k9F7wI4JxJqQb06fGVEw5PNaRxWRgE0PoopU3L+NoZx4bwrZrS/w2fIP0H7aoI2PBJOEWAz9fOONa0DKUwQiCA1n18nGcyuYljh0+ZfIDWOkhfco/0Hwc/gHZWBETDKQKE4FrrKZMPmDh2eCaDi2IggFYWACs4hXNPO9oQQgaL2X76/1ek/6D9FgF/DaMIkFIZQpx72tEFp4CLYiCAVoMx5jhu9+6dTz/pCD83CZ7+P4T0H7TfIuChkIoAIjr9pCO6d+/sOC7Wg0IArZb+ZzLZk2ZM7d6lSspAqz+lIsbo9QXOhh1I/0H7LQJ2yNcXOIwF2hjMGJNSde9SddKMqZlMFkUABNCKNQA7/cQjgo4NIsEpX9D/75VcxGKY2QLtEqUpYrH/90ouX9AijC3up594BGOMCAKAAFol/c/mxh4w5NCDxgTc/asUMUbvLXcXrnHjNrb+gnYqAEVxmy1c47633GXBjgj1dwUfetCYsQcMyWRzKAIggJYXAHdy+ZOOnhqN2FIGOvzH/9q/vJYTeI9Be0dw9pfXcs2v/X4PGSlVNGKfdPRUJ5fHWiAIoKWRUkWi0RNnTCWiIN1//9z/1VvkO0sL8QjD9C9oz6NGUTzC3llaWL1FBrwnwB90J86YGolGJYYNBNCy6T/L5ZwDhg8YO2pw0OX/1Jj+p3MaGxtBu8cQlM5pvwgIMg3gbwgYO2rwAcMH5HIOukAQQEsKgDv5/HFHHmSZRpDsQ2vinGrT+pX5ThxnP4COUgTwV+Y7tWnNeaAFb1IqyzSOO/IgJ48uEATQki+xVJFo9KQZh1Cw/o9UxIhene9s+ERGTKz+BO0frSli0oZP5KvzHUZB14MS0UkzDkEXCAJoyfQ/tP6Pn7U89W7eNBiCP+goDiAyDfbUu/nmIbB/oAsEAbSKAMLp//jTv6u2yEVrvJiN6V/QYQpoRTGbLVrjrQo8FYwuEATQ4q+vVNFY7ORjgvZ//A1fT8/O16UVpn9Bh8IQVJdWT8/ONw+E/cMfgCcfc0g0FkMXCAIoOowxxyn069Nt5LABRBSk6hSCXI/eXFSI2Bybv0CHQimK2PzNRQXXIxEg+/EH4MhhA/r16eY4BZwLBAEUFyF4Pu9MnzohFrGllPv9wvnTv8s2ess2elGLcPwD6FgC0BS1Gt//IFPBjDEpZSxiT586IZ93cE8kBFBctNaM8+OPmuK/fgG/2xOz8o6r8dKCjphLcXJc/cSsfPCynIiOP2oK41xjIR0EUDwYY64rO1eVjx89hAL0f7RuPP3trcWFiIXl/6AjIhVFLP7W4kLj2XD7G7r9YTh+9JDOVeWuK9EFggCK9rA4y+Xy40YN7t6lSqn9P//Zb/is2CQ375K2geX/oGMW02QbtHmXXLFJUoCpYMaYUqp7l6pxowbncnksBoUAilgBeK47/dBJnPMg95H6Ef+VeU4mrwXW/4COihCUyetX5jnNg2I/MyqlOefTD53kubgfBgIoGkpp07YPmjiKgi0AFYIKHr2B9T+gow8oitj8jUWFQrC1QP5gPGjiKNO2cVM8BFCs9L9QcHt17zxm5CAKMAHgr/9ZtdlbscmLmlj/AzqwADRFTVqxyVu1OdBaIH8wjhk5qFf3zoUCigAIoBhPirNc3hk9YmBFeTLIBIBf6v57mZtz0P8BHR0hKOfofy9zKUAXyJ8GqChPjh4xMJfHmRAQQHEqAC3l1APHMKIgZaZ/AuLbSwpCMEz/gg6O1iQEe3tJwT8Zd/+LCaUZ0dQDx2iJhUAQQDHKVaW5YUwaN5wCTABoTZxRKqeXb/IiJq7/BR1+WGmKmGz5Ji+V05wFKgKIaNK44dwwMA0AAYSf/nue7FxVPmxQ3yAC8N/MeavdLbuUhQWgABWAJsugLbvUvNUuBVsMSkTDBvXtXFXueSgCIICwBZDLO8MH9+3ZrbPWOsgWMCKavdR1PY2zCwEgIs7J9fTspYGmAThnWuue3ToPH9w3l3cgAAgg3HeUea47ctgAzpkKsHJTcFKaFlS7Fvo/ADSVxZbJFlS7SpMINA2gOGcjhw3wXBfzwBBA2EUA0bhRQ4IkKUoTY7S9Vq3c7EVMzAAD0FgWR0y2crO3vVYxtv9dIH9AjRs1BLEfAggZKbVp26NHDKJgM8BEVL1V1qS0ITABAEDjuDAE1aR09VZJFHQeePSIQaZtS4nRBQGElfsz5nle104VQwb2pmBnwBHRgmrXk5gAAGC3MMTJk3pBddBpACIaMrB3104VnudhGgACCE0AeacwqH/PyvKE1nq/Xyw/6C+odjl2AACwZ27EBfMFsN+5EWNMa11ZnhjUv2cel8NAAOEJgKQn+/buLoTY7yXG/g6AfEGv2SptAwIAYI/RYRtszVaZLwTaDaCUFkL07d1dehLxHwIIrQIgrcYd4M8A768AiIhoa43aUqNMgSOAANgtcGsyBW2pUVtrVPNg2S+RaCIad8AQ0goVAAQQ0tupNBNGKDPAKzbLVE7jCngA9sIQlMrpFZvDmQdmAvuBIYDwBBCN2p2ryolov7MK/51eut5VUiM1AeA/YjcpqZeuD3gqHBFR56ryaBTnQkMA4byXzPO8ThXJgf16BqkA/Kmt5RslZoAB+MwMiQu2fKOkYPPARDSwX89OFUksBIIAwhGAU3AHD+gVi0aU2v8lQIxRwaNNO6WJGWAAPksApsE27ZQFb//rbMaYUjoWjQwe0MvBxQAQQDiVqVLxWNQ0xH7PTmlNjCid0/4MMOI/AHuPEWqcB07nNAt0Q6Q2DRGPRZVSiP8QQAgVgJZy1LABFGAJkN+NXLdd1qUV9gAD8JlJkiGoLq3WbQ90R7w/SEcNG4CLASCAsF5NXVGWIAoauHfUK9cjnFLVktVbiXwTsE/BiJHr0Y56FWy0EhFVlCU08iwIIDhKaWGaI4cNoMBrQFdv8TwsAWqZ0E/EGXkyhG/lSeKM8KG1jLA9qVdv8SjwStCRwwYI08RCIAggHCrKk8GzyI07JeeYAW6JRFITZRzdKRlC3O6UZBlHa0Lp1gKlNnHONu6UwQuvgAMWAgCN2YTneV06VQzo2yNIBeB/3eadCmfAFRvBqeBRQ1b/4uvJc6ZF/c7y/uHP1pwzLfqLrycbsrrgBTqtHuxTPOK0eacKIgB/kA7o26MLjoSDAIKjlI7YZlkyvt8vpb8EyHH1rpQyOJYAFTf65xydiLL7ryq74uSYEEETScZICLri5Nj9V5UloiznaDigiBUAkcFpV0o57v4vBPI/8bJkPGKjBQQBhPFScsaD3ALmv5SOS1trlIFdYMV7lRk5LpXF+N+uLT9uku3KcBr3jMiVdNwk+2/XlpfFuOOiF1S0sabJEGxrjXLcoOZWSnHGMdQggIAFKXMcZ/jQfuXJuFKBjpdSCoGj6Diuvv2S5Oj+huuRGd6BS6Yg16PR/Y3bL0k6rmaMBC/dX4Zo/CV4m3zlOKMg6RZjTClVnowPH9rPcRzcDfn5GHgEX5iVCM79o8b3M/RrEoxWbvF21KuYjauAi/MeC9pRr276amLGBMuVZIb9XpsGuZJmTLBuOCv+o4fSySgr0SuntL/2SROREMw0KGoxzkiqtjHWLIN21KuVW7wDh5r+wNlvDQjOUW1DAKEElxCSSaVwBHSxEJzqMvr4yfblJ8WkCjP336sOUJouPym2tUat3iqjZsl9oP6M99iBpuCkNa3Y7K3aLFdu8vIuJWNMcFJtQQNKh/NzhjJsIYAODWNMed6EscOISKlA9zi2iRSsjSIVJSLslvMSjIq7YN9vJ9x6QaKtPBlP0qK17pOz8k/PcTI5nYiyNvEeBvwh/aE6Yeywf77wFlYBfcErjUewD+V/IE36deiiNW7BxVXAxajPqD6jTptq9+8mpKIWeMJ+MVeivxR5svGXUmQImjjYvO2i5Au3VB40wqxNl/oqJs6p4OpFawIdCh3KsIUAQHMED6HUD2VXKti7RCNyPaqI828eF9PUQvt1OSfOSvXXbpPAvguVIqloSC/x2PUVF82I7mpQpX8fUSiDBUdBQAAhFaRhlM2oRIuBEJTK6ZMPsgd0F6pF0v+2N8I5+d1/xugXX098/ejozpJ3QCiDRaLrCgEETiKIcV5VWRbKOATF6MYITid+yUa2ty+vn1J0y9cSk4ea6VxJdyND+dmqKssYFgJBAAGrSGEYY4LdBux/3bINnsAusLDzxIJHPTvx8YNMxrDN4ouGOiNNFLXYLV9NKE2luSVdaxKCLdvgUeDTIMaMGCQMA40gCCAoXuBtwERUm8Y5oOFHtLyrR/U1klGmNJpsX4zgJBVNGmoeOc5qyJXohDBjVJvWFLgR5Cm0gCCAUN7IML6JgSddhEjheXr0AIMxwmDf9xSbiE6dEinlc3JCGSzIByCAUhp4eARFiGWcUecyvMP/WxFARAePMDuVcdcr0bIJgwUCAOCLBWAYbERfg7DI6n8pmzRRZYL36yoKHp4bBABAWwaL/fZDnJZBXSu4pzTiPwQAAOiIGgAAAgBtG9zQ8r/CiDxJmbzGwlmA0QPabCBj5HnaXzOOfHbfE3/GKJ3X1VulbeBwcggAtFTaBUIXgNK0swGTAP8DSpPWtHiNu6NemUaJihODBQIopaQpjG/iIUwVIZk1DLZkrac1Ttr438T53PuO65XuaRChDBbUNhBAOBjBBoqfZFUmcA5E+MlsxGRLN3ipnOYMXaB9eGKKBKf1n8hn3nPKYlyW5Am1WlNlglHgtp6BpAACCJwrMel5i5dVU4DTZf2vG9HXkBKnQYQcKSyDtuxSC6pdrXHh2hdnxP4j+r+nMqmcNkQp5siMkZTa39ux3wLwh+riZdXS83AhDAQQ6HXUStXUNoSSfIHwX19OUtHz7zsY5l8oS0+SIegvr+eemJWviJf01WChDJaa2gatFF4MCCAoIoyVhmhQFAMpKRllz77nrN0mOYdlPzvx92O9KejxWfkfPZSuTPIS3z0XymARWCC8D+DWtH2pA0LIIgzcUF2c6GYatKtB/eml7G0XJlsmrLWtvceCk+DkSbrticy9L2ZjdhtIiUMZLGj+QADh4HpesBeRiGjsQNMyGVLU0PEklcf507Ody46P9e/WEveCta3MMp3TL89z7n8pt3itVxYPYXK1qChFlsnGDjQp8PlOAYctBABIa80NY/6iFUTEg+2bRD1a1Ihcn9U3/TX9l6vLdXHfByJGc1e4UpX6MWpa04pN3tIN3uyl7trt0hRUkWBtpXYJOFj8oTp/0QqOC2EggDByzBCWy/k3iYNiIBVVxNmLc517nst+56SYK8ksQsPN/7a/fy7744fTZbE2EExdSYwoarGKONO6zXSu/KvtS2TYQgAdHcZIKhUkj/Dj/tCeRpdyXpvWJbv9so1Lmjol+S8fzwztZcyYYLkemaG+2v43fHV+4ZePZ7pVctYWZvX9GkW1ndDv3/HZpZwP7Wk0D5z9rt0llgDtS3TCI/gclNK2bS9fub4+leGcB9IAx0L1omOb7Jr7U0vWeaZBbnj5nyvJNGjJOu+a+1O2yUiTVKR0qf+SiqRqe9mGCravW2vNOa9PZZavXG/btsKogwACZSVESiseeDOwbVKPKu5hL1gxA4dtUkNWffXX9S/NdUxBSgWVrtKkFJmCXprrfPXX9Q1ZZZsQeRErAE/qHlXcNoN6i3OutMJQgwACPyDO8o7bkMrQ/lb9/h1Mtsk6JbmncNBVEZGKojZL5/QldzXc9UzWn3eR+6UBP4P2+9F3PZO95K6GdE5HbYb7Z4qabHmKOiW5bTK9v6uA/EHakMrkHZdj2g0CCJa5a8MwduyqW7thKwU+DaJXZ45loC3gAMugRIT9/NH0BXfUr9osRZMG9qUfops6J5yR4LRqs7zgjvqfP5pORJhl4Pax4pdxinp15hT4HIi1G7bu2FVnYBXQF4FJ4H2irj4VTCRERH06C6U09qcUPYhoIqLO5fz1BYV/L6v98sH2xcfGhvYSzf9VfdYiTv9IUc5IMCKilZvlAy9n//VvJ5PXncu5hxUlLVABMFJK9+ksKPAce8ABCwGAphKJM+m6H69Ye9KMqfudTfjhZnBPwxA4E7SF8CT5izUfej3/zHvOiQfax022Jw42KxOM//dForVpPW+1+9Jc5/kPnLq0Lo+zshhD9G+pgpsMwQb3NCjALjB/kH68Yq10Xc6x9RICCCExYXUNaQq8NbFLOTcNTCG2HH7HpirJPEl/ezP/+Kx890o+qp8xZoDZqYwN7/3py798k7erQS9e6y5d722rVZ6kZJRVJZnfDgItVrqZBnUp58FGKxFRXUMapTYEEEpWopkQS1espQCni/hzUf27iYoET/sn8UIDLagBzqgywTRRTUq/vqDw0lyHcbb7ZjFXklbaNJhtskSUMSKF0N/SaRZ5kioSvH83QQE2AfiDdOmKtUwITABAACGUpZzzTDbnelLs72JQfyFQIsp6VvGlG6QpcF1Ry36IzSdiGmSbjDGm9R4OZoz83/RnCEArCIDIldSziiei+78EyP9Oricz2RznHPH/i3NTPIIvrABsy1y9dnM2l+ecBVkIZBnUu7NwPWwFaE2dS0We3HsnV/NvImS0YgXgerp3Z2EZgZYAcc6yufzqtZtty0QFAAGEIADDMHbVpdas30IBVoL6eeXwPkJhLxgAnyUAJfXwPoICXAjjD88167fsqkthDSgEENIz4iyXc3bW1FOA1Wl+0B/Vz+RYCATAZxVnXLBR/QIdBO2PrJ019bmcg11gEEBoAtDSWxLsZmD/nR7WSySjWFYIwN74K6+G9RIUeA3okmXVWnoQAAQQVm6iifGFH62iAAuB/C/rUcV7VnFX4mhoAHYLQ6xxBrhHFW8eLPuVZjEiWvjRKmIc/R8IILTiVBhiw6ZtUsr9TisYI6UpYrGBPYSDeWAA9hwdjqcH9hARiym9/xUA50xKuWHTNmEIxH8IILQKIGJb1eu21NanGWMB54HHDzIxDwzA3umR1OMHmRRsBpgxVlufrl63JWJbqAAggNAEYBjGJ7vqVq3ZRET7fcK4H/THDzINgR3qAOyRGxmC+QLY79zIH5ir1mz6BMfAQQDhIgRzHSeUeeBBPYR/OAGKAACoaQ9wVZIN6hHODLDrOEJgdEEA4dYBRAuXrgrygnJGWlO3Sj60l5F30QUCoFEAeVcP7WV0q+RaBzkEgoho4dJVyPwhgNBLVG2Y5scr1iqlg9wO5p9LM36QWXA1FgIB4CdGBVePH2T61zbs//fhXCn98Yq1hmniJkgIINT0X+toxF6+esOW7TsZYwGnAaaOMk0D0wAA+NkVmQabOiroBABjbMv2nctXb4hGbEwAQAAhC8AwxM6a+hXVGyjANICf9U8cbPbsxAsepgFAR4cxKnjUsxOfONikAIeA+kNyRfWGnTX1hoFzQCGA0J8UZ8rzPly4nILNAytNySgb3tvIowsEMKwY5V09vLeRjAbaAeAPyQ8XLlce9gBDAMUpApgQsz9YrImCvGH+fYSHj7YkdgMAVACMpNSHj7YYC3QQN+dME83+YDGuAYAAioJSOhqxlyxbU1ef4pwHXAx68AgzajOJQ4FAx0ZKitrs4BEBz4DTnPO6+tSSZWuiERszwBBAUSoAyzI3b9u5+ONqCrAdTHDSREN6GcN6GzkXhwKBDhx9GOVcGtbbGNLL0ERif6ORPxgXf1y9edtOC9cAQADFelicuY7z3rylFGAawM96LIOOHGvlHcXxCYCOO6Ao76gjx1qWQUGqYX8wvjdvqevgFGgIoJhFgGGaM9/5UCkV5D3zS91jJtrxCLpAoOMiJcUj7JiJNgW6A5I4Z0qpme98aJhI/yGAoqGUjkYjC5eu3rajJsg0gO+OYb1Fr07CwWJQ0CFhjByPenUSw3oHugXenwDYtqNm4dLV0WgEEwAQQBErANMUO2vqFyxZRcFOhZOKIhabNsbKF5TAhwA6HoJTvqCmjbEiFpMq6BlwC5as2llTb5pYAgQBFDdtYVqpF1+f4xsh4Hc787CIbTKJLcGg4yEV2SY787BI8MSMiF58fY5WiqGahgCK+9ZKFYnYM2fPz+YdEWDFsb8WaEQfY0QfI1fAWiDQweIOo1yh8f0Psv5Hay2EyOadmbPnRyK2RDIFARQVrbVtW+s3bv94xVoK0AUiIinJNOgIrAUCHTDucMo76oixlhls/Y8/AD9esXb9xu02LoGBAFoAIXgum332lXcp2GJQP+s/bWqkIsFxTTzoUHiSKhL8tKkRokDlrz8An33l3Vw2KzCZBgG0AEopOxJ56Y33Cq4X5J3jnJSmIT3F2IFG1tF4e0FHSaE4ZR09dqAxpKdQmoKUv0Lwguu99MZ7diSicL4uBNAiAtDRqP3R8rWLlq5mjAVpO/pv7OmHRFxPYxYAdBAYkevp0w+JEAU6/0dKxRhbtHT1R8vXRqM4AQICaLEURvB8Lvfcq0G7QP5U8IwJdt+uIu9iQwDoANGfUd6lvl3FjAl2kOnf5qH33Kvv5nM59H8ggJYsAsLpAvknIFYm2DET7EweGwJAB0ieOGXy6pgJdmWCKRUo6UH/BwJoLQGE1gXy3/+vHx1NRBmmgkG7x5OUiLKvHx1tfvn3D/R/IIBWTWQEz+dyz786mwKuBeKkNA3uKQ4dZWXymAoG7T7914eOsgYHnv71B93zr85G/wcCaJUiQNnRyHOvzc7lHSF4kPXH/td+/eioRBYD2jtSaT/9DzhkhOC5vPPca7PtKPo/EEArCEDHY9FFH616573FjLEgryDnpDUdNNwcN9DMOBqbwkD7jDWcMo4eN9A8aLipg6X/SinG2DvvLV700ap4LIr+DwTQKmit9VPPvxnwuzBqPBvuG8dE8wVcFAzaaaxhlC/obxwTbTz9LfA3fOr5N7XWwY/kggDA/hYB8dhzr87etqNGCB50Paimo8bbfbuIfAHrQUF7gzHKF6hvF3HUeFvroKs/heDbdtQ89+rseDyG9B8CaLX837bNbdt2PvXcm0QUaC0QI6WpMsEuOCqazmE9KGhvCE7pnLrgqGhlgikdKMXxB9pTz725bdtO28YNMBBAqxYBlm39/enXPCkDLkXgjLSm86ZH+3ZFEQDaY/rfVZw3Pap10LNvheCelH9/+jXLtpD+QwCtKwAVj0fnLVo+Z+5HQTcENBUBX0cRANpj+v/1kNJ/xticuR/NW7Q8Ho9i/Q8E0NpPkDO34D3y5MshfCtGWtNXUQSA9pj+fzWM9N/nkSdfdgse7n+HAFofz5PJssQ/Xnh77catQvAgNekeRQBOhgDtJv3Ph5P+K6WF4Gs3bv3HC28nyxIets5DAKWAaYra2vrH/vk6EWkdqCZtLgJ6d0YRANpJ+t+7czjpvz+4Hvvn67W19aYp8HghgJJAShWNRf/86AvZXJ7zQOtBm4uAq0+LZx0tIADQptN/RllHX31aPHj6r7XmnGdz+T8/+kI0FsXtjxBAqaC1jkbs6nWbn3n53YC7gv0iQGk667DI6P5GKo+NwaDNBhdOqbwe3d8467CICpz++7t/n3n53ep1m6MRG6s/IYDScoBhGnfc+1jeKXAe6GggxkhrMgRdfVpMhbFhEoBWgREpRVefFjME6aDpP3HO807hjnsfM0wD0R8CKC2UUslEbN7CZc+/NocxJmWg6SnBSSo6dpI9faxVl9EC3U7Q1hCC6jJ6+ljr2Em2VBRwRYOUkjH2/Gtz5i1clkzEsPoTAijJIsAQDz/xEhEFX6DGiLSm68+MRy2SWOwA2hpSUtSi68+Max1CFesPqIefeMkwBNJ/CKA033iVTCZenvn+O+8v5pwHnKTy7wkY1c+4cEasLqMNFAGg7WAIqsvoC2fERvUzAp77748szvk77y9+eeb7yWQC078QQInCGCml/u+ev/n/O+hnw0hpuvKU2IBuHEtCQRsaBfkCDejGrzwlpsLY+eW/+f93z9+UUhgFEEBJFwFlZaEVAf5scHmMXX9mIoXDIUAbQXBK5dT1ZybKYyzg3O9e6X9ZGdJ/CKDtFAGhjCWp6JSD7eMn27VpbeDjAqWNwak2rY+fbJ9ycAhzv80g/YcA2lgRMHP2fCF48ISFMeKMfnZ+oirBChKNIFDS2U9BUlWC/ez8BGchvKtSKiH4zNnzkf5DAG1rJLDrfvoHp+D6bZxAnxAjqahvF3Hj2Yn6DBpBoHQRnOoz6sazE327CKmCH/xAjJFTcK/76R8YEh8IoK2glEomYx8uXPb0C29zzpUKuorTd8CZh0WmjbHqMxoOAKUa/fW0MdaZh0WCR38iUkpyzp9+4e0PFy5LJrH2HwJoSw7Qtm3fce9jBddjjAVcucwYMUaGoN9ckqyIM9dDIwiUWslLrkcVcfabS5KGaHxjg6X/mjFWcL077n3Mtm1c/AIBtLEiIJGIzluw7O7/9xTnPHjy4hcBfbqIG89JNGTRCAIll/43ZNWN5yT6hNH88UcQ5/zu//fUvAXLEglc/AIBtDWkVPFE7M77nti+s5YxHjyF8VcEnTstcvyBdk0KW8NAqWAIqknp4w+0z50WCWXlj1KaMb59Z+2d9z0RT8Qw9wsBtD201pGItXHT9p/e8WfOWcB7ApoLba3p9ouTfbvwnEM4KBS0fgThlHOobxd++8XJ4Kv+m8aO4pz99I4/b9y0PRKxcPYDBNAm8TxZXpH865MvL1q6WggRSiNIaepcxn93WZmnMC5AKSQ65Cn9u8vKOpfxUPb9KqWEEIuWrv7rky+XVyRx7RcE0IYRgqez+etvvVepcAK24ORJmjLCvO6MeE1KoREEWhFDUE1KXXdGfMoI05PhbPvSWiulrr/13nQ2LzDZBQG0aaRUlRXJl9/49+PPzhRCyDAO9jQESUXfOSl2gr89GA4ArRT9a9P6hMn2d06KSUWhvIdSSiHE48/OfPmNf1dWJNH9LzasfMgxeArFfcSMua7brUvVnBf+1KWqnIgFPyzav12vpkEd++PanfXatgirJECLZo6cnAJ1Lmcv/6yyqozrcJo/mkjvqKmfcsI3t++oMU0TXU5UAG0erXUkYq9Zs+mW2x8MZUko+ZMBijqV8fuvKteklcLOANCSOQ0pRZr0/VeVdyrjKox1n9S09POW2x9cs2ZTBJc+QgDtBs+TlZ0qHvz7C2//e6FhiFAKW39V6LiBxq8vStZncE4WaFEB1GfUry9KjhtohHXim5TKMMTb/1744N9fqOxUgblfCKAdcsUPf5vO5Bij0CaEFZ15WOSy42N1mAwALYIhqC6tLzs+duZhEU+FNvHLGKUzuSt++Fs84ZZERDoNxlNoAfxtAevWb3alOmbagf4p5yHkYkRK0/Rx1pJ13qI1XlmUYc88KB6moB316rhJ9p2XlUlFgoXTe5RSCSFuvO3+f73wVgXmfiGAdolSOhaLvvPvhYcdPG5gv55SqhCuDmbEiDTR9LH2vFXe2u0yasMBoFi5f0NWf2mY9acryy2T8fCiv9/8ufz6O8rLEf1bFLSAWvyJC+43gvyyIIQigBFpqoizey4vq0ryTB7HhYIipIqcMnldleT3XF5WEWcU2qZfTdTY/OF4cVEBtG/2aAQdEV4jiJFUVBFnh46yXphbyDlkGIQ1FCC0rIVTwaWyGP/7teWDe4oQr/ry0380fyCAjoLfCJo1Z8G4A4aMGNpfShmKA/zjQrtX8ikjrEffyjHGBCcoAIT1akmpn/hhxZgBRqjRXxqGePaVd7//k7vKK8oQ/SGADoF/vdE7/1547mlHJ+JRrSmUC484I09Sz068V2fx7HuOaTDO4AAQ9KVSmjJ5/ZtLk9PHWZ6ksBabKaU5Zzt21X3lohvzBdcwBBb+QwAdAq21bZvbPqlZuWbj2acepVU4jSC/VPckje5v9Okqnn0fDgAhRP90Xv/usuSZh0ZCjP5EpJRinJ/37Z9+sGBZWTKO9B8C6EAopePx6IKFy6OxyKEHjfU8Gb4DusABIIzo/83wo7/nScMQv/793+65/8lO2PYFAXRMB0RjkVff+uDQg8aGtSoUDgClH/39id+35iz4xnd/mSxL4LYvCKCjDjPOldZvvDPvnNOOTsQiYU0GwAGgZKN/Y+u/pv6kr12XzTumYaD1DwF0UPzJgK3bd62s3njOqUdJqRjjYZ3qAweAUov+Wvv3vfCvfvuW9+d/jNY/BNDRaZwMWLTC9eTRh0+W0uPh3fS4lwMMwYTA/gDweS+MVJRxihL9iUhKzzCMG3953wOPPNupUzla/xAA8HcGRGa+O2/IoD5jRw72pCySA16Z5yjFTOwRA58ZCzgVXJJK33FJUaK/J6VhGI/+6/Wrb74be75KBFwIUyKZF3NdaZnijafuGnfAEClVuJfh+YN5/mr3wt801GVUPMKQe4HdMQRl8roizv/8/bIJg80i5P5KCL7wo1VHnn5lwZWmKRSOrCqFyINHUCJFgGWZ6Uz+vG//tK4hLQQPd3gYgjxJEwabf7u2vCLOG7LaxNnRoAlTUENWV8T5364tL0b0V0oLwesa0ud9+6fpTN6yTER/CADslSLJZDK2onrDKedfn807FNJRcbs7QCo6oL/x3M0VEwebn9TjQnnQ+GJ8Uq8mDjafu7nigP5GWLf7NuO/xtm8c8r516+o3pBMxkK5GRuEAuYASqsOiMejy5avqalPnTRjariLgqjpUJfKBD/hQHv1ZvnRehm1cZFYx4Ux4pxq0/q4ifb93y3vWsFDPOenKfo3Nn+uvPF3/3h2ZhX2fEEA4HMdoJJl8VmzPiwodfThkz3pCR7miPQvE45Y7NQpkfqMnvOxa5uMc0wLd7zan5PWVJ/RlxwbvetbZbbJVNjRn4g86ZmG8cNf3ve73/+tc7dOruvhyUMA4AscEEvE3po9r2/vHhNGDw3xlIjmvE9rUpqOHGf16iRem+9ISbaJa2Q6EIagfEErRf/3jeSVp8SkIkbEQ4/+njQN46EnXr7h538oryxH5wcCAPuKaRpPPTuzf98e44vjAL8dNGaAcdgB1puLCtvrdDwCB3SU6F+b1j2q+CM/KJ8xwfbbPizsXqB/2s/DT778je/+Ih6P4bGXJlgGWrIVOpNSua4365l7Jo0d7g+n0P8Wf/DvqFfXPJB6ca5TleT+JcOgfb5UjDRRTUodP9m+/eJkl/Lwm/67R/8PFy0/7JTLTdMIfVUbQAXQztGahBBK6adfeHv6oZN6de8ceh1ATVMCiSg7dUokYrJZSwpKox3UbhP/fEEXPLrhzMRtFybjkaI0/Zuj/4KPVp103rUF1zNNA9EfAgD74QBtmkYqnf3HC28fWTQH+FMCmuig4ebkoea7H7vba3U8wjBk20+Zz8gQVJvSPTrx+64sO+PQiB+QeTGj//HnXlNXn4pGbRz2CQGA/XeAbVupdKbYDmCMPEn9u4kvHxyp3iaXrJWmYAYODmoHI5yTJ6kuo4+ZZD10dfmIPoYni9L0/8/oH4tFcd4DBADagAOo6SAwvx3UtYJ/sMKtz+iYjVKgbSf+DVmdjLKbz0vcfF4iHmGh7/P63OiPZT8QAGhDDmhqB40fZB4/2V620Vu2wbNMlAJtM/FXVJtSh4yy/nJ1+bQxVvHaPoj+bThLwCqgNjOkhchmcxXlyRf/fvv4A4YUaV2Qj784RCr668zc/z2V2ZXSlQkmFTTQNhJ/wak2rTsl2Q9Oj583Pep/lKJox74g+qMCAO2nDqCmW0EEp3EDzeMn2xs+kYvXeQZnlolFoiWNIajgUl1GHTPBvv+q8mljrOaPEtEfoAJAHbA/pQARPflO/o6nM2u3qfI48zNKUFovBiepqD6jB3TnV58WP+PQyO4fH6I/QAXQruqAp198+5Avje3bq1vx6oDmUoCIRvUzTpsaUZoWrvHSOR21mb9+FLQ6nBNnVJ/RQrBvHBu967KySUNMfy6nBaL/+/M/Pvn86xD9IQDQkg4ws1nnL4+92Ltntwmjh7peyGfG7VEkMmKMpKJ4hE0bYx0+2tpSo1Zulp4k28Rhoq1avzMSnLJ57bg0bYx152VlZx8eidpMKuLFWejZjOt5/jk/Z1z8o4LrRaM2VnxCAKDlHGAYBmPsqefe7Ne7+4Qxw6RUjBEr2qDnTUfI9aziX5kaGdLLqN4qN+xQnDHLxF3zrRD6/XZ/Oq9H9jV+dn7ihrPi3St5kY512+vdU0obhnjo8Ze+8b1fRiK2aRrY7QUBgJZ2gBDcNM2nX3g7k8vPOHwyY0wpXTwH+EfIKU2aaHhv49xp0W4VfNFab3utEhwaaOnQX59RXSv4j86O/+qi5Ig+hmrq+RQ18VdKc844Z9ffeu8Pb/1TPB7FOT8QAGgtBxBjzLat12e+t21n7bQpE2zblFJxXsQY0NwRMgSNG2iefkikLM5XbfG21ioDGmiR0F+XUd0r+eUnxW+/OHnwCIs3rfJkRW7I+Ve7pDO5K3/0uzv/+Fh5RRmFfW8daOmXCquA2gGmaez8pOawqeOfeehXFeWJYi8N+jQiNC0y2dmg/joz/9eZuQ07ZCLCIxYphQWjocEZcU75AqXzqm8Xcd706HnTI53LOBV/nU8z/ktVV58+5YLrZs1e0LlrFW53gQBAyTjAMOrqU8MG93349z/2r5FpGQf4EwN7aWDjDhWxyD9GAs3hQKGfEyPKOjpfoD5d+F6hn7OiZ/27R//5S1ae/52frVi9oaI86XqI/hAAKCUMQ6RS2UQ8eufPrzrv9GOkVIyHeqfwPmvgtfmFh17PLVrrcUbxCBOclMaa0f9lWDbd2JPJa6Vp7ADjgqOiR0+wWj70K6210kLwvz71ylU/ujOdySWTMdzrCwGAUkQIUSgU8k7h6m+dc9uNl1FT37Zl/vbdNeB69NbiwiMzc3M+dtN5HbOZbRIKgn1M+R2Xso5ORNiUkebXpkenjbFMg1o49O/+8lx/6713/PHRiG1ZloXF/hAAKOUIwjRRXW3Dycceet/t13brUtVi7aD/1AARLdvoPfx67vUFhQ07lSkoZjNDYIbgPz41RpyTJynraFdS3878qPHW+UdFR/QxGmNxy4Z+amr7bN9Rc+k1v3725XcqKssYERb8QACgDWAYYldtw4A+Pe7/zXVHHjJRKUWshdpBjRogUrvFLL8v9NS7+SXrvLqMilosYrHGRaUdOKQ0r6zNF3SuoCvifHR/4/RDIs3dHt+mflnQYiitSWvO+Rvvzrvk+79au3Frp8oytH0gANDGHJDN5aVUN11z0Q+v/BoRSSmFEC38Y/ghvrkgWL7Re3q289aSwspNXt6lqMUskwTvWDWBn+9LRQWXcgUdMWlob2PaaOu0qfbw3VJ+Xw8tTPNL8ou7Hrnl9geF4LFoBNEfAgBtMNBwrrWuq6k/+fjDGttBUgrOGWvpuNKYyTYVBFLSwrXuP9513l5S2FIjs3mKWOTXBM3OaJfJvv+vyxd0vkCxCPWsEoePtr5yiD1ugOmrea8H1bKfkZZKGaKp7fPirIqqcsYYdvlCAKBtlwI1tQ39enf/zS1XnHr8YdSyM8OfXxDkHP3xRu/VeYU3FxdWbvZyDglBEZNZZuOF9f6hZm11dPlxn5PSVHAp72opKWrT0F7GEWOsGROtkX2MqM1aN+VvSvwbX4l/vjjr+zfdvX7Ttiq0fSAA0G4ckM3lPVdecfHpN19zUTIRa61SoDHZJNJqj+MqpaKP1nkfrHTfXlJYscnbWqMKHtkG2RYzBDFGpNtGZdAYxBlpTZ4kp6AdjyyDelTxYb2Nw0dbBw41D+hv7P4PZ0SsZbv8n5n4p9LZm29/8O4HnjJMgbYPBADaFf6R0bU19aNHDf7tT6848tBJrVsKNEUf+s+Di+vS+sNV7pxl7oJqd/UWWZNS/j3mlkmWwfwzD5q/sHWVwFhjmu//SFJRwdMFt/GojKokH9xTjB9kThlhThpiViQ+DfKNcb81Wj2fmfi/8c6H3/vJ3UuWrq6sKicitH0gANA+S4F0JkdaX37RVxpLAU8K0WqlwH/WBHu1QbbsUtVbvYVrvHmr3DXb5LZalcppqbTBmWGQKZgpPo2/jUZpUku4gb65peP/v76BXEmu1J5HntKCs2SUda/kA7uLiUPMcQONQT2Mnp0+NZtfwbRuvr9H4i+VYTQm/vc8+A9iLBGPIvGHAEC7LwV0bU3D7qWAJ6XR4guEPqcs8FcE7VWc5Ap6005VvcVbukEuXe9u2aW21aqatPIkuZ4WnAlOgpNpMCIyBflRdned7IsVdldh48IkTa4kInI9LRVJRVJp02CGoKoE717Je3bio/qZo/qKQT2N3p151NojvPvn5PPWTvZ3p/nj3i3xLyPCfC8EADpMKZBK5wSj8886/uZrLuretUopTaQ55yX1czbKQBPje0+QKkW1abVmm6xJ6eUbva21asMnsi6jttYozmlHXeOmpYJHWmv/S4X4glJHa5JS+xUJY8wyGmN3lwquFPWo4hVx3rer6FHJh/cxqpJsYHdRmdj7mSlNWhGx0gr6TQ9NETHO2bZPam6+/cGHH39RakomkPhDAKCDlgL1gwb0uemai752xjFEJKXkvPU7Qv8tOuumaQN/Kf1nBThqyCnSbNUWz5OkNC1a43qyMRCv2OTVprXBP2NlESPyFFUm2LDehv93GYLGDjQ5I0PQkJ4GMV0W5f/tL1W6sRPFiEry4fkXuSh/jf8jT75yy+0PVq/dWFlVjsQfAgAduhTI5Z1sJn/skQf99NqLJ48b3qgBIUr8vkc/UW+2gh9/i72M0u/jN/51fqxnVPoPSjVt75q7cPlPfv3Ay2+8F4tHohEbiT8EAAF0+JeAMSF4bV0qGY+eferRN119YY9undqKBj7TCkSf7iveve/vL9D8/K/ffUNycy7f6JWSj/WfE/q3bt91yx1/fuyfr6UyucqKpJQKd7kACAA0IoSQUtbXp/r07n75had9/5tnmaZBRFIpUWITA2BfaP7gXNf7zZ8ev+fPT2/ctK28POl/0Hg+gHAlJPg0W9SaiBKJWCqdfen1OS/NfD8SsUYO7W8YQimttOaM4Sm1ldBPmgTnruv97elXv3nNrx9+4iUpVVlZXCmNxB+gAgD//Z1gTAieTuecQmHi2OG33nDpMdMOJCKltCaNaqDEQz8j5l8K/cpbH9z4y/vmLVpuW1YiEUXPB0AAYF/hnHPOUums1jRj2uTvXXrWkYdOpMYrohTnAvVAKVVvpJRknPtV2hvvzPvtfY+/+tZcxiiZiCmlsc4HQABgfzRARPWptOB8xrQDmzVARFIqzhmDB1o59GuldPN5Hk2h/wOpVHkyQTjUAUAAICBCcK0bNXDMtAPPOfXo0086wr+lUErJGOccGmhplNJaN67rL7jeU8+9+eg/X3ulKfQzRlIi9AMIAISqgYZURko5aeywKy8+88vHHZpMxPxqgBjBAy2R8vtJvSY/60+ls/966Z27Hnjiw0UrhBBlyThCP4AAQBE1wIilMlnXk4P69fz6WcedcdL0oYP6+P+1dU+Zbuehv+ncZv//XVm98cnnZv7l8Zeq128xDZGMxzRphH4AAYCi40835vKFXDZXVVn+5eMOO/e0o6ZNmeCnpX4YapqSBAHjfmMfv/nZvjVn/t+ffv1fL82qqa2PxqLRiNX8ZwCAAEBLvT2MCc5dz2tIZSzTHD966NmnHnnGiUf06tGluSDgjLXsdfTtJ+5rrZTWzSn/5q07nnz+zcf++caCJSsLrluWjJuGIRUWdwIIALTiO0QkhFBaZbL5glPo3q3TiUdPPeXYQ48+fLJtmTBBwLjvFNzX3p77zMvvPP/a7G3bd1m2FY9FOOPSP7YUAAgAlAL+1gGn4KbTOc7Z2FGDj5t+0Ekzpk4eN1w0hbMmE2D96F5xX2utd4/7Usq5C5c/9+rsl2a+t2jpaqV0IhG1LROL+gEEAEr4lWLMP2g6k807eScajYwc2n/GEV86eU8T+PMEjHfookBprZWmpv5+c9x/9tXZr775/scr1+VyeTtix2MR/9BmdHsABADaSkHAOOdSqlze2csEY0YOikUjzclv8wEGHaEs8Ldu+YdqNP97s7n84o+r94r70YgtBFdKKYW4DyAA0HZNwLhUTSaIRfr27DZt6oTjph80fvSQvr26fZoRK6W0ZsTaWWXgZ/qaNG8sjxrZsHn7giWrXpr53luz52/Ysj2XbYr7nCuNuA8gANAeawKnUMjlHM55p8rysaMGTT9k4pRJo8eMHFRZkdz9z0upNGl/7rhtFQd+wNdaM2Jiz6uNa+tSiz+unvPhkpnvzlu0tHpXbb1SKhq1bctCvg8gANABXjvGBOeatOt6uXzBK7imbfbs3nn0iEFTJ4+eNG74sEF9+/TsukdIJVJKaaV9F5TUNLJuhLTWjH/GyRgbt3yyonrDhwuXz567ZMmy6i3bdrqOa1hmNGKZpsGIYTUngABARzQBY4xzppQuFNxc3lFSCsPoXFU+bFCf4UP6jx05aOyoIcMG9+lcVfEfYZea2unky6C5bVQMN/gBWhP507Zaa2L039a27qypW7F646KlqxZ9XL181boV1Rt31tRLz+NCRCO2ZZn+P7nRGwBAAAAy4JwxYlpr1/PyTsF1PdLasq0unSoG9evVp2fXcQcMGXfAkE6VZf379qgsT/63b6WU9m+G3KOdwvbpSkdNevc745uyefY5Bx3V1qfWbdi6q7Zh4UerFn60auOWT6rXb96xq67gFIgx0zQitmUaBmNMk8aVLAACAGAfKgPGiEhp5bqeU3A9T2qpuCGiUbuyPDlkQO9oxB4zcnAiER0zYlAyERs8oHciFuWclSXjof9IDamMUjqdza1euymVzi5eVp1O5xZ/vDqXd1at3VRbn8rlHOVJJrhhCNsyTdPgjJM/A4xMH0AAAATxgd/610orrVxPOo6rlFJSktLCMomoS6cKyzSEECOG9OOcGYYxcewwf2uVlKqqMjlq2EAi/d8vhtdEbOmKNTW1KX/m1pNy3qIVnucppZetWi+lLLjejl11RCQLLnHGheCc27ZpGoIzzjhrngxAxAcQAABFVgIxYo19edfz/DkBxyn44Vd53qexnXPDNL7wO3uuR0o1O4IbBhExRrZt+ZsVTP93OPO7RQj3oO1i4BGAtshnxlwhGjcaR2yr2RO75/dqH8I0Z3vMFDT/Lc3TCdI/iQHHMQAIAICSskJjjA5wThoCO+g4cDwCAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAACAAAAAAEAAAAAAIAAAAAAQAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAIAAAAAAQAAAAAAgAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAgAAAAABAAAAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAAAgAAAAABAAAAAACAAAAAAEAAACAAAAAAEAAAAAAIAAAAAAQAAAAdHD+P56tz6+6F0/aAAAAAElFTkSuQmCC",
}

SERVICE_WORKER_JS = """
// Minimal service worker: lets the app install, and shows a clear message when offline.
// It never stores attendance data or pages.
const CACHE = 'attendance-offline-v1';
self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.add('/offline')).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});
self.addEventListener('fetch', (event) => {
  if (event.request.mode === 'navigate') {
    event.respondWith(fetch(event.request).catch(() => caches.match('/offline')));
  }
});
"""

OFFLINE_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>No connection</title>
<style>body{font-family:system-ui,Segoe UI,Arial,sans-serif;background:#f1f5f9;color:#0f172a;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:24px}
.box{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:32px;max-width:420px;text-align:center}
h1{font-size:1.3rem;margin-bottom:10px}p{color:#475569;line-height:1.5;margin-bottom:18px}
a{display:inline-block;background:#2563eb;color:#fff;text-decoration:none;padding:10px 20px;border-radius:8px;font-weight:600}</style></head>
<body><div class="box"><h1>No internet connection</h1>
<p>Your clock-in or clock-out was <strong>not</strong> recorded. Please reconnect and try again.</p>
<a href="/">Try again</a></div></body></html>"""


@app.route('/manifest.webmanifest')
def manifest():
    name = COMPANY_NAME
    data = {
        'name': name,
        'short_name': name[:12],
        'description': 'Employee attendance - clock in and out',
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'orientation': 'portrait',
        'background_color': '#f1f5f9',
        'theme_color': '#0f2547',
        'icons': [
            {'src': '/icon-192.png', 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': '/icon-512.png', 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
    }
    return Response(json.dumps(data), mimetype='application/manifest+json')


@app.route('/icon-<int:size>.png')
def app_icon(size):
    if size not in ICONS:
        abort(404)
    return Response(base64.b64decode(ICONS[size]), mimetype='image/png',
                    headers={'Cache-Control': 'public, max-age=86400'})


@app.route('/sw.js')
def service_worker():
    return Response(SERVICE_WORKER_JS, mimetype='application/javascript',
                    headers={'Cache-Control': 'no-cache', 'Service-Worker-Allowed': '/'})


@app.route('/offline')
def offline():
    return Response(OFFLINE_HTML, mimetype='text/html')


@app.route('/health')
def health():
    info = {'redis_configured': bool(REDIS_URL and REDIS_TOKEN)}
    if REDIS_URL and REDIS_TOKEN:
        try:
            info['redis_ping'] = _cmd('PING')
        except Exception as e:
            info['redis_error'] = str(e)
    return jsonify(info)


if __name__ == '__main__':
    app.run(debug=True, port=5000)
