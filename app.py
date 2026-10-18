from flask import Flask, render_template_string, request, jsonify, redirect, url_for, send_file, abort, session
from datetime import datetime, timedelta
import json
import os
import csv
import re
import io
import urllib.request
from zoneinfo import ZoneInfo

app = Flask(__name__)

app.secret_key = os.environ.get('SECRET_KEY', 'dev_secret_change_me')

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin')
TIMEZONE = ZoneInfo(os.environ.get('TIMEZONE', 'Asia/Karachi'))

# Storage: Upstash Redis (persistent, used on Vercel) if configured,
# otherwise a local JSON file (fine for local development).
REDIS_URL = os.environ.get('KV_REST_API_URL') or os.environ.get('UPSTASH_REDIS_REST_URL')
REDIS_TOKEN = os.environ.get('KV_REST_API_TOKEN') or os.environ.get('UPSTASH_REDIS_REST_TOKEN')
REDIS_KEY = 'attendance_data'
DATA_FILE = os.path.join('/tmp' if os.environ.get('VERCEL') else '.', 'attendance_data.json')


def EMPTY():
    return {'employees': {}, 'attendance': []}


def now_local():
    return datetime.now(TIMEZONE)


def _redis(path, body=None):
    req = urllib.request.Request(
        f"{REDIS_URL.rstrip('/')}/{path}",
        data=body,
        headers={'Authorization': f'Bearer {REDIS_TOKEN}'},
        method='POST' if body is not None else 'GET',
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode('utf-8'))


def load_data():
    if REDIS_URL and REDIS_TOKEN:
        # No try/except on purpose: if Redis fails we want an error,
        # not an empty list that would overwrite real data on the next save.
        raw = _redis(f'get/{REDIS_KEY}').get('result')
        return json.loads(raw) if raw else EMPTY()
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            try:
                return json.load(f)
            except Exception:
                return EMPTY()
    return EMPTY()


def save_data(data):
    payload = json.dumps(data, indent=2, sort_keys=True)
    if REDIS_URL and REDIS_TOKEN:
        _redis(f'set/{REDIS_KEY}', payload.encode('utf-8'))
        return
    with open(DATA_FILE, 'w') as f:
        f.write(payload)


BASE_TEMPLATE = r'''
<!DOCTYPE html>
<html>
<head>
    <title>{{ company_name }} - Attendance</title>
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
        @media(max-width:640px){.container{padding:18px 14px 30px}.card{padding:16px}.clock-display{font-size:2.3rem}.topbar-inner{padding:0 14px}}
    </style>
</head>
<body>
    {% set p = request.path %}
    <div class="topbar"><div class="topbar-inner">
        <div class="brand"><span class="brand-mark">{{ company_name[:1] }}</span>{{ company_name }}</div>
        <div class="nav">
            <a href="/" class="{{ 'active' if p == '/' }}">Clock In/Out</a>
            <a href="/employees" class="{{ 'active' if p == '/employees' }}">Employees</a>
            <a href="/records" class="{{ 'active' if p == '/records' }}">Attendance Records</a>
            <a href="/reports" class="{{ 'active' if p == '/reports' }}">Reports</a>
            <a href="/export">Export Excel</a>
            {% if is_admin %}<a href="/logout">Logout</a>{% else %}<a href="/login" class="{{ 'active' if p == '/login' }}">Admin Login</a>{% endif %}
        </div>
    </div></div>
    <div class="container">
        {% block content %}{% endblock %}
    </div>
    <div class="footer">&copy; {{ company_name }} &middot; Attendance Management System</div>
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
                            <!-- Admin logged in: show delete button -->
                            <form method="POST" action="/delete_employee" style="display:inline;">
                                <input type="hidden" name="emp_id" value="{{ emp_id }}">
                                <button type="submit" class="btn btn-danger" style="margin-left:8px; padding:8px 12px;">Delete</button>
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
                </tr>
                {% endfor %}
            </tbody>
        </table>
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


def next_employee_id(employees):
    max_n = 0
    for eid in employees.keys():
        if eid.startswith('EMP'):
            try:
                n = int(eid[3:])
                if n > max_n:
                    max_n = n
            except ValueError:
                continue
    return f"EMP{max_n + 1:03d}"

def parse_bool_param(val):
    if val is None:
        return None
    return str(val) in ('1', 'true', 'True', 'yes', 'Yes')


@app.context_processor
def inject_company():
    return {'company_name': COMPANY_NAME}


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        if pwd == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('employees'))
        else:
            return render_template_string(LOGIN_TEMPLATE, message='Invalid password', is_admin=False)
    return render_template_string(LOGIN_TEMPLATE, is_admin=session.get('is_admin', False))

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
    data = load_data()
    employee_id = request.form.get('employee_id')
    action = request.form.get('action')
    now = now_local()
    today = now.strftime('%Y-%m-%d')

    if not employee_id or employee_id not in data['employees']:
        return redirect(url_for('index', message='Employee not found', success=0))

    employee = data['employees'][employee_id]

    today_record = None
    for record in data['attendance']:
        if record['employee_id'] == employee_id and record['date'] == today:
            today_record = record
            break

    if action == 'in':
        if today_record and not today_record.get('clock_out') and today_record.get('clock_in'):
            return redirect(url_for('index', message='Already clocked in!', success=0))

        new_record = {
            'employee_id': employee_id,
            'employee_name': employee['name'],
            'department': employee['department'],
            'date': today,
            'clock_in': now.strftime('%H:%M:%S'),
            'clock_out': None
        }
        data['attendance'].append(new_record)
        save_data(data)
        return redirect(url_for('index', message=f'{employee["name"]} clocked in successfully!', success=1))

    elif action == 'out':
        if not today_record:
            return redirect(url_for('index', message='No clock-in record found!', success=0))

        if today_record.get('clock_out'):
            return redirect(url_for('index', message='Already clocked out!', success=0))

        today_record['clock_out'] = now.strftime('%H:%M:%S')
        save_data(data)
        return redirect(url_for('index', message=f'{employee["name"]} clocked out successfully!', success=1))

    return redirect(url_for('index', message='Invalid action', success=0))

@app.route('/employees')
def employees():
    data = load_data()
    message = request.args.get('message')
    return render_template_string(EMPLOYEES_TEMPLATE, employees=data['employees'], message=message, is_admin=session.get('is_admin', False))

@app.route('/add_employee', methods=['POST'])
def add_employee():
    data = load_data()
    name = request.form.get('name', '').strip()
    department = request.form.get('department', '').strip()
    position = request.form.get('position', '').strip()

    if not name or not department or not position:
        return redirect(url_for('employees', message='All fields are required'))

    emp_id = next_employee_id(data['employees'])
    data['employees'][emp_id] = {
        'name': name,
        'department': department,
        'position': position,
        'join_date': now_local().strftime('%Y-%m-%d')
    }
    save_data(data)
    return redirect(url_for('employees', message=f'Employee {name} added successfully!'))

@app.route('/delete_employee', methods=['POST'])
def delete_employee():
    data = load_data()
    emp_id = request.form.get('emp_id')

    
    if not session.get('is_admin', False):
        return redirect(url_for('employees', message='Admin login required to delete employees'))

    if emp_id not in data['employees']:
        return redirect(url_for('employees', message='Employee not found'))

    del data['employees'][emp_id]
    data['attendance'] = [r for r in data['attendance'] if r['employee_id'] != emp_id]
    save_data(data)
    return redirect(url_for('employees', message=f'Employee {emp_id} deleted'))

@app.route('/records')
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
    return render_template_string(RECORDS_TEMPLATE, records=records, filter_date=filter_date, current_month=now_local().strftime('%Y-%m'), is_admin=session.get('is_admin', False))

@app.route('/reports')
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


@app.route('/health')
def health():
    info = {'redis_configured': bool(REDIS_URL and REDIS_TOKEN)}
    if REDIS_URL and REDIS_TOKEN:
        try:
            info['redis_ping'] = _redis('ping').get('result')
        except Exception as e:
            info['redis_error'] = str(e)
    return jsonify(info)


if __name__ == '__main__':
    app.run(debug=True, port=5000)
