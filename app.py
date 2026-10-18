from flask import Flask, render_template_string, request, jsonify, redirect, url_for, send_file, abort, session
from datetime import datetime, timedelta
import json
import os
import csv
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
    <title>Employee Attendance System</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 50%, #7e22ce 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1400px;
            margin: 0 auto;
            background: rgba(255, 255, 255, 0.95);
            border-radius: 20px;
            box-shadow: 0 25px 80px rgba(0,0,0,0.4);
            overflow: hidden;
            backdrop-filter: blur(10px);
        }
        .header {
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 50%, #7e22ce 100%);
            color: white;
            padding: 40px;
            text-align: center;
            position: relative;
            overflow: hidden;
        }
        .header::before {
            content: '';
            position: absolute;
            top: -50%;
            right: -50%;
            width: 200%;
            height: 200%;
            background: radial-gradient(circle, rgba(255,255,255,0.1) 0%, transparent 70%);
            animation: pulse 4s ease-in-out infinite;
        }
        @keyframes pulse {
            0%, 100% { transform: scale(1); }
            50% { transform: scale(1.1); }
        }
        .header h1 { 
            font-size: 2.8em; 
            margin-bottom: 10px; 
            position: relative;
            z-index: 1;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }
        .header p {
            position: relative;
            z-index: 1;
            font-size: 1.1em;
            opacity: 0.9;
        }
        .nav {
            display: flex;
            justify-content: center;
            gap: 10px;
            padding: 25px;
            background: linear-gradient(to right, #f8f9fa, #e9ecef);
            border-bottom: 3px solid #dee2e6;
            flex-wrap: wrap;
        }
        .nav a {
            padding: 14px 28px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            text-decoration: none;
            border-radius: 50px;
            transition: all 0.3s ease;
            font-weight: 600;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            position: relative;
            overflow: hidden;
        }
        .nav a::before {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: 0;
            height: 0;
            border-radius: 50%;
            background: rgba(255,255,255,0.2);
            transform: translate(-50%, -50%);
            transition: width 0.6s, height 0.6s;
        }
        .nav a:hover::before {
            width: 300px;
            height: 300px;
        }
        .nav a:hover { 
            transform: translateY(-3px) scale(1.05); 
            box-shadow: 0 6px 20px rgba(102, 126, 234, 0.6);
        }
        .content { padding: 40px; background: #fafbfc; }
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        .stat-card {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 25px;
            border-radius: 15px;
            box-shadow: 0 8px 20px rgba(102, 126, 234, 0.3);
            transition: transform 0.3s;
        }
        .stat-card:hover {
            transform: translateY(-5px);
        }
        .stat-card h3 {
            font-size: 2.5em;
            margin-bottom: 10px;
        }
        .stat-card p {
            opacity: 0.9;
            font-size: 1.1em;
        }
        .form-group {
            margin-bottom: 25px;
        }
        .form-group label {
            display: block;
            margin-bottom: 10px;
            font-weight: 700;
            color: #2d3748;
            font-size: 1em;
        }
        .form-group input, .form-group select {
            width: 100%;
            padding: 15px 18px;
            border: 2px solid #e2e8f0;
            border-radius: 12px;
            font-size: 1em;
            transition: all 0.3s;
            background: white;
        }
        .form-group input:focus, .form-group select:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
            transform: translateY(-2px);
        }
        .btn {
            padding: 15px 35px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 12px;
            cursor: pointer;
            font-size: 1.1em;
            font-weight: 700;
            transition: all 0.3s ease;
            box-shadow: 0 4px 15px rgba(102, 126, 234, 0.4);
            position: relative;
            overflow: hidden;
        }
        .btn::after {
            content: '';
            position: absolute;
            top: 50%;
            left: 50%;
            width: 0;
            height: 0;
            border-radius: 50%;
            background: rgba(255,255,255,0.3);
            transform: translate(-50%, -50%);
            transition: width 0.6s, height 0.6s;
        }
        .btn:hover::after {
            width: 400px;
            height: 400px;
        }
        .btn:hover { 
            transform: translateY(-3px); 
            box-shadow: 0 6px 25px rgba(102, 126, 234, 0.6);
        }
        .btn-success { 
            background: linear-gradient(135deg, #28a745 0%, #20c997 100%);
            box-shadow: 0 4px 15px rgba(40, 167, 69, 0.4);
        }
        .btn-success:hover { 
            box-shadow: 0 6px 25px rgba(40, 167, 69, 0.6);
        }
        .btn-danger { 
            background: linear-gradient(135deg, #dc3545 0%, #c82333 100%);
            box-shadow: 0 4px 15px rgba(220, 53, 69, 0.4);
        }
        .btn-danger:hover { 
            box-shadow: 0 6px 25px rgba(220, 53, 69, 0.6);
        }
        table {
            width: 100%;
            border-collapse: separate;
            border-spacing: 0;
            margin-top: 20px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            border-radius: 12px;
            overflow: hidden;
            background: white;
        }
        table th {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 18px 15px;
            text-align: left;
            font-weight: 700;
            text-transform: uppercase;
            font-size: 0.9em;
            letter-spacing: 0.5px;
        }
        table td {
            padding: 18px 15px;
            border-bottom: 1px solid #e2e8f0;
            color: #2d3748;
        }
        table tbody tr {
            transition: all 0.3s;
        }
        table tbody tr:hover { 
            background: #f7fafc; 
            transform: scale(1.01);
            box-shadow: 0 2px 8px rgba(0,0,0,0.05);
        }
        .status-in { 
            color: #28a745; 
            font-weight: 700;
            background: #d4edda;
            padding: 6px 12px;
            border-radius: 20px;
            display: inline-block;
        }
        .status-out { 
            color: #dc3545; 
            font-weight: 700;
            background: #f8d7da;
            padding: 6px 12px;
            border-radius: 20px;
            display: inline-block;
        }
        .card {
            background: white;
            padding: 35px;
            border-radius: 15px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.08);
            margin-bottom: 25px;
            border: 1px solid #e2e8f0;
            transition: all 0.3s;
        }
        .card:hover {
            box-shadow: 0 8px 30px rgba(0,0,0,0.12);
            transform: translateY(-2px);
        }
        .clock-display {
            text-align: center;
            font-size: 3.5em;
            font-weight: 900;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
            margin: 25px 0;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.1);
        }
        .date-display {
            text-align: center;
            font-size: 1.3em;
            color: #4a5568;
            margin-bottom: 20px;
            font-weight: 600;
        }
        .success-msg {
            background: linear-gradient(135deg, #d4edda 0%, #c3e6cb 100%);
            color: #155724;
            padding: 18px 20px;
            border-radius: 12px;
            margin-bottom: 25px;
            border-left: 5px solid #28a745;
            box-shadow: 0 4px 10px rgba(40, 167, 69, 0.2);
            font-weight: 600;
        }
        .error-msg {
            background: linear-gradient(135deg, #f8d7da 0%, #f5c6cb 100%);
            color: #721c24;
            padding: 18px 20px;
            border-radius: 12px;
            margin-bottom: 25px;
            border-left: 5px solid #dc3545;
            box-shadow: 0 4px 10px rgba(220, 53, 69, 0.2);
            font-weight: 600;
        }
        .button-group {
            display: flex;
            gap: 15px;
            flex-wrap: wrap;
        }
        .button-group .btn {
            flex: 1;
            min-width: 180px;
        }
        .login-inline {
            display:inline-flex;
            gap:8px;
            align-items:center;
        }
        .login-inline input {
            padding:8px;
            border-radius:6px;
            border:1px solid #e2e8f0;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🕐 Employee Attendance System</h1>
            <p>Track and manage employee attendance efficiently</p>
        </div>
        <div class="nav">
            <a href="/">Clock In/Out</a>
            <a href="/employees">Employees</a>
            <a href="/records">Attendance Records</a>
            <a href="/reports">Reports</a>
            <a href="/export">Export CSV</a>
            {% if is_admin %}
                <a href="/logout">Logout</a>
            {% else %}
                <a href="/login">Admin Login</a>
            {% endif %}
        </div>
        <div class="content">
            {% block content %}{% endblock %}
        </div>
    </div>
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
        <h2 style="margin-bottom: 20px; color: #2d3748; font-size: 1.8em;">🔒 Admin Login</h2>
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
    <div class="card">
        <h2 style="margin-bottom: 20px; color: #2d3748; font-size: 1.8em;">⏰ Clock In/Out</h2>
        <div class="date-display" id="live-date"></div>
        <div class="clock-display" id="live-clock"></div>
        {% if message %}
        <div class="{% if success %}success-msg{% else %}error-msg{% endif %}">
            {{ message }}
        </div>
        {% endif %}
        <form method="POST" action="/clock">
            <div class="form-group">
                <label>👤 Select Employee:</label>
                <select name="employee_id" required>
                    <option value="">-- Choose Employee --</option>
                    {% for emp_id, emp in employees.items() %}
                    <option value="{{ emp_id }}">{{ emp.name }} ({{ emp.department }})</option>
                    {% endfor %}
                </select>
            </div>
            <div class="button-group">
                <button type="submit" name="action" value="in" class="btn btn-success">✓ Clock In</button>
                <button type="submit" name="action" value="out" class="btn btn-danger">✗ Clock Out</button>
            </div>
        </form>
    </div>
    
    <div class="card">
        <h3 style="color: #2d3748; margin-bottom: 20px; font-size: 1.6em;">📊 Today's Activity</h3>
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
                            {% if record.clock_out %}Clocked Out{% else %}● Working{% endif %}
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
        <h2 style="margin-bottom: 20px; color: #2d3748; font-size: 1.8em;">➕ Add New Employee</h2>
        {% if message %}
        <div class="success-msg">✓ {{ message }}</div>
        {% endif %}
        <form method="POST" action="/add_employee">
            <div class="form-group">
                <label>👤 Employee Name:</label>
                <input type="text" name="name" required placeholder="Enter full name">
            </div>
            <div class="form-group">
                <label>🏢 Department:</label>
                <input type="text" name="department" required placeholder="e.g., IT, HR, Sales">
            </div>
            <div class="form-group">
                <label>💼 Position:</label>
                <input type="text" name="position" required placeholder="e.g., Manager, Developer">
            </div>
            <button type="submit" class="btn">Add Employee</button>
        </form>
    </div>
    
    <div class="card">
        <h3 style="color: #2d3748; margin-bottom: 20px; font-size: 1.6em;">👥 Employee List</h3>
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
        <h2 style="color: #333;">Attendance Records</h2>
        <form method="GET" action="/records" style="margin-bottom:16px;">
            <label>Filter by date:</label>
            <input type="date" name="date" value="{{ filter_date or '' }}" style="padding:8px;border-radius:8px;border:1px solid #e2e8f0;margin-left:8px;">
            <button class="btn" style="margin-left:8px;">Apply</button>
            <a href="/records" class="btn" style="margin-left:8px;">Clear</a>
        </form>
        <table>
            <thead>
                <tr>
                    <th>Date</th>
                    <th>Employee</th>
                    <th>Department</th>
                    <th>Clock In</th>
                    <th>Clock Out</th>
                    <th>Hours Worked</th>
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
        <h2 style="color: #333;">Weekly Attendance Report</h2>
        <table>
            <thead>
                <tr>
                    <th>Employee</th>
                    <th>Department</th>
                    <th>Days Present</th>
                    <th>Total Hours</th>
                    <th>Average Hours/Day</th>
                </tr>
            </thead>
            <tbody>
                {% for emp_id, report in weekly_report.items() %}
                <tr>
                    <td>{{ report.name }}</td>
                    <td>{{ report.department }}</td>
                    <td>{{ report.days_present }}</td>
                    <td>{{ "%.2f"|format(report.total_hours) }}</td>
                    <td>{{ "%.2f"|format(report.avg_hours) }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    </div>
''')


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
        try:
            if record.get('clock_out'):
                clock_in = datetime.strptime(f"{record['date']} {record['clock_in']}", '%Y-%m-%d %H:%M:%S')
                clock_out = datetime.strptime(f"{record['date']} {record['clock_out']}", '%Y-%m-%d %H:%M:%S')
                hours = (clock_out - clock_in).total_seconds() / 3600
                if hours < 0:
                    hours += 24
                record['hours_worked'] = f"{hours:.2f} hrs"
            else:
                record['hours_worked'] = 'In Progress'
        except Exception:
            record['hours_worked'] = 'N/A'

    records = list(reversed(records))
    return render_template_string(RECORDS_TEMPLATE, records=records, filter_date=filter_date, is_admin=session.get('is_admin', False))

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
            try:
                clock_in = datetime.strptime(f"{record['date']} {record['clock_in']}", '%Y-%m-%d %H:%M:%S')
                clock_out = datetime.strptime(f"{record['date']} {record['clock_out']}", '%Y-%m-%d %H:%M:%S')
                hours = (clock_out - clock_in).total_seconds() / 3600
                if hours < 0:
                    hours += 24
            except Exception:
                hours = 0.0

            weekly_report[emp_id]['days_present'] += 1
            weekly_report[emp_id]['total_hours'] += hours

    for emp_id in weekly_report:
        days = weekly_report[emp_id]['days_present']
        weekly_report[emp_id]['avg_hours'] = weekly_report[emp_id]['total_hours'] / days if days > 0 else 0.0

    return render_template_string(REPORTS_TEMPLATE, weekly_report=weekly_report, is_admin=session.get('is_admin', False))

@app.route('/export')
def export_csv():
    data = load_data()
    filter_date = request.args.get('date')

    rows = []
    for r in data['attendance']:
        if filter_date and r['date'] != filter_date:
            continue
        rows.append({
            'date': r['date'],
            'employee_id': r['employee_id'],
            'employee_name': r.get('employee_name', ''),
            'department': r.get('department', ''),
            'clock_in': r.get('clock_in', ''),
            'clock_out': r.get('clock_out', '')
        })

    si = io.StringIO()
    writer = csv.DictWriter(si, fieldnames=['date','employee_id','employee_name','department','clock_in','clock_out'])
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    output = io.BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)

    filename = f"attendance_export_{filter_date or 'all'}.csv"
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name=filename)


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
