"""Small local web app for the student list: Students, Map, Friends and Interests pages.

Run it from a notebook cell with `import app; app.launch()`, or from a terminal
with `python3 app.py`.
"""
import csv
import json
import math
import os
import re
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STUDENTS_FILE = os.path.join(BASE_DIR, 'students.csv')
INTERESTS_FILE = os.path.join(BASE_DIR, 'interests.csv')
STUDENT_INTERESTS_FILE = os.path.join(BASE_DIR, 'student_interests.csv')
PAGE_FILE = os.path.join(BASE_DIR, 'app.html')
# Other files the page loads, by URL path: (file name, content type).
STATIC_FILES = {'/rmh_school_logo.svg': ('rmh_school_logo.svg', 'image/svg+xml')}

FIELDS = ['student_id', 'first_name', 'last_name', 'gender', 'grade', 'address',
          'distance_from_school_miles', 'latitude', 'longitude', 'email']
INTEREST_FIELDS = ['interest_id', 'name', 'category']
STUDENT_INTEREST_FIELDS = ['student_id', 'interest_id']
GENDERS = ['Female', 'Male', 'Prefer not to say']
GRADES = [9, 10, 11, 12]

# Rocky Mountain High School, 1300 W Swallow Rd, Fort Collins, CO 80526
SCHOOL_NAME = 'Rocky Mountain High School'
SCHOOL_ADDRESS = '1300 W Swallow Rd, Fort Collins, CO 80526'
SCHOOL_LOCATION = (40.546241, -105.099610)  # (latitude, longitude)

GEOCODER_URL = 'https://geocoding.geo.census.gov/geocoder/locations/onelineaddress'

# Browsers that can open a page as a chrome-less "app" window.
APP_BROWSERS = ['Google Chrome', 'Microsoft Edge', 'Brave Browser', 'Chromium']

_lock = threading.Lock()
_server = None
# A server left running from an older app.py would save rows in the old format.
_CODE_VERSION = os.path.getmtime(__file__)


# ---------- geocoding and distance ----------

def geocode_address(address):
    """Return (latitude, longitude) for a US street address, or None if it can't be found."""
    params = {'address': address, 'benchmark': 'Public_AR_Current', 'format': 'json'}
    response = requests.get(GEOCODER_URL, params=params, timeout=20)
    response.raise_for_status()
    matches = response.json()['result']['addressMatches']
    if not matches:
        return None
    coords = matches[0]['coordinates']
    return (round(coords['y'], 6), round(coords['x'], 6))


def distance_miles(a, b):
    """Straight-line (great-circle) distance in miles between two (latitude, longitude) points."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 3958.8 * math.asin(math.sqrt(h))


# ---------- students.csv ----------

def read_students():
    """Return the rows of students.csv, giving a new student_id to any row that lacks one."""
    with open(STUDENTS_FILE, newline='') as f:
        rows = list(csv.DictReader(f))
    used = set()
    for row in rows:
        student_id = str(row.get('student_id') or '')
        row['student_id'] = int(student_id) if student_id.isdigit() and int(student_id) not in used else None
        used.add(row['student_id'])
    for row in rows:
        if row['student_id'] is None:
            row['student_id'] = next_student_id(rows)
    return rows


def email_owner(rows, email, except_id=None):
    """Return the student (other than except_id) who already has this email, ignoring case, or None."""
    email = email.strip().lower()
    return next((r for r in rows if r['student_id'] != except_id
                 and (r.get('email') or '').strip().lower() == email), None)


def next_student_id(rows):
    return max((row['student_id'] or 0 for row in rows), default=0) + 1


def write_csv(path, fields, rows):
    """Write rows to path via a temporary file, so a failed save leaves the old file intact."""
    # Keep any extra columns someone added to the file, after the app's own.
    extra = [key for row in rows for key in row if key and key not in fields]
    tmp = path + '.tmp'
    with open(tmp, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields + list(dict.fromkeys(extra)), extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)


def write_students(rows):
    write_csv(STUDENTS_FILE, FIELDS, rows)


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def students_with_locations():
    interest_ids = {}
    for link in read_student_interests():
        interest_ids.setdefault(link['student_id'], []).append(link['interest_id'])
    students = []
    for row in read_students():
        student = {field: row.get(field) or '' for field in FIELDS}
        student['id'] = row['student_id']
        student['lat'] = _to_float(row.get('latitude'))
        student['lon'] = _to_float(row.get('longitude'))
        if student['lat'] is None or student['lon'] is None:
            student['lat'] = student['lon'] = None
        student['interest_ids'] = interest_ids.get(student['id'], [])
        students.append(student)
    return students


class FormError(Exception):
    pass


# ---------- student_interests.csv: one row per student and interest ----------

def read_student_interests():
    try:
        with open(STUDENT_INTERESTS_FILE, newline='') as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    return [{'student_id': int(r['student_id']), 'interest_id': int(r['interest_id'])} for r in rows]


def set_student_interests(student_id, interest_ids):
    """Replace one student's interests: drop their old rows and write the new ones."""
    links = [link for link in read_student_interests() if link['student_id'] != student_id]
    links += [{'student_id': student_id, 'interest_id': i} for i in interest_ids]
    links.sort(key=lambda link: (link['student_id'], link['interest_id']))
    write_csv(STUDENT_INTERESTS_FILE, STUDENT_INTEREST_FIELDS, links)


def parse_interest_ids(data):
    """Return the sorted interest ids from form data, or None if the form didn't send any."""
    if 'interest_ids' not in data:
        return None
    known = {r['interest_id'] for r in read_interests()}
    try:
        ids = sorted({int(i) for i in data['interest_ids']})
    except (TypeError, ValueError):
        raise FormError('Interests must be a list of interest ids.')
    if not set(ids) <= known:
        raise FormError('Some of those interests no longer exist. Reload the page and try again.')
    return ids


# ---------- interests.csv ----------

def read_interests():
    with open(INTERESTS_FILE, newline='') as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        row['interest_id'] = int(row['interest_id'])
    return rows


def write_interests(rows):
    write_csv(INTERESTS_FILE, INTEREST_FIELDS, rows)


def save_interest(data, interest_id=None):
    """Add an interest (interest_id is None) or rename the one with that id. Returns all interests."""
    name = ' '.join(str(data.get('name', '')).split())
    if not name:
        raise FormError('Please enter a name for the interest.')
    with _lock:
        rows = read_interests()
        if any(r['name'].lower() == name.lower() and r['interest_id'] != interest_id for r in rows):
            raise FormError(f'"{name}" is already in the list.')
        if interest_id is None:
            category = ' '.join(str(data.get('category', '')).split())
            if category not in {r['category'] for r in rows}:
                raise FormError('Please choose a category from the list.')
            rows.append({'interest_id': max((r['interest_id'] for r in rows), default=0) + 1,
                         'name': name, 'category': category})
        else:
            row = next((r for r in rows if r['interest_id'] == interest_id), None)
            if row is None:
                raise LookupError('That interest no longer exists.')
            row['name'] = name
        write_interests(rows)
    return rows


def build_row(data):
    """Validate form data and return a CSV row with the location and distance from school filled in."""
    row = {}
    for field in ('first_name', 'last_name', 'email', 'gender', 'grade', 'address'):
        row[field] = ' '.join(str(data.get(field, '')).split())
        if not row[field]:
            raise FormError(f'Please fill in {field.replace("_", " ")}.')
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', row['email']):
        raise FormError('Please enter a valid email address.')
    if row['gender'] not in GENDERS:
        raise FormError('Please choose a gender from the list.')
    if row['grade'] not in map(str, GRADES):
        raise FormError('Please choose a grade from the list.')
    try:
        location = geocode_address(row['address'])
    except requests.RequestException as e:
        raise FormError(f'Could not reach the address lookup service: {e}')
    if location is None:
        raise FormError(f'Could not find "{row["address"]}". Please check the address and try again.')
    row['distance_from_school_miles'] = round(distance_miles(location, SCHOOL_LOCATION), 1)
    row['latitude'], row['longitude'] = location
    return row


# ---------- web server ----------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, content_type='application/json'):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_students(self):
        self._send(200, {
            'school': {'name': SCHOOL_NAME, 'address': SCHOOL_ADDRESS,
                       'lat': SCHOOL_LOCATION[0], 'lon': SCHOOL_LOCATION[1]},
            'genders': GENDERS,
            'grades': GRADES,
            'students': students_with_locations(),
        })

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/':
            with open(PAGE_FILE, 'rb') as f:
                self._send(200, f.read(), 'text/html; charset=utf-8')
        elif path in STATIC_FILES:
            name, content_type = STATIC_FILES[path]
            with open(os.path.join(BASE_DIR, name), 'rb') as f:
                self._send(200, f.read(), content_type)
        elif path == '/api/students':
            self._send_students()
        elif path == '/api/interests':
            self._send(200, {'interests': read_interests()})
        else:
            self._send(404, {'error': 'Not found'})

    def _code_changed(self):
        """Refuse to save (and say why) if app.py was edited after this server started."""
        if os.path.getmtime(__file__) == _CODE_VERSION:
            return False
        self._send(409, {'error': 'The app has been updated since it started, so nothing was saved. '
                                  'Close this window, restart the kernel (or the app), and launch it again.'})
        return True

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length) or b'{}')

    def _save_interest(self, interest_id=None):
        if self._code_changed():
            return
        try:
            self._send(200, {'interests': save_interest(self._read_json(), interest_id)})
        except (ValueError, FormError) as e:
            self._send(400, {'error': str(e)})
        except LookupError as e:
            self._send(404, {'error': str(e)})

    def _save(self, student_id=None):
        """Add a student (student_id is None) or replace the student with that id."""
        if self._code_changed():
            return
        try:
            data = self._read_json()
            row = build_row(data)
            interest_ids = parse_interest_ids(data)
        except (ValueError, FormError) as e:
            self._send(400, {'error': str(e)})
            return
        with _lock:
            rows = read_students()
            owner = email_owner(rows, row['email'], except_id=student_id)
            if owner is not None:
                self._send(400, {'error': f'{row["email"]} is already used by #{owner["student_id"]} '
                                          f'{owner["first_name"]} {owner["last_name"]}.'})
                return
            if student_id is None:
                row['student_id'] = next_student_id(rows)
                rows.append(row)
            else:
                index = next((i for i, r in enumerate(rows) if r['student_id'] == student_id), None)
                if index is None:
                    self._send(404, {'error': 'That student no longer exists.'})
                    return
                row['student_id'] = student_id
                rows[index] = {**rows[index], **row}
            write_students(rows)
            if interest_ids is not None:
                set_student_interests(row['student_id'], interest_ids)
        self._send_students()

    def do_POST(self):
        if self.path == '/api/students':
            self._save()
        elif self.path == '/api/interests':
            self._save_interest()
        else:
            self._send(404, {'error': 'Not found'})

    def do_PUT(self):
        prefix, _, item_id = self.path.rpartition('/')
        if prefix == '/api/students' and item_id.isdigit():
            self._save(int(item_id))
        elif prefix == '/api/interests' and item_id.isdigit():
            self._save_interest(int(item_id))
        else:
            self._send(404, {'error': 'Not found'})


def open_window(url):
    """Open the app in its own small window, or in a normal browser tab if that isn't possible."""
    if sys.platform == 'darwin':
        for name in APP_BROWSERS:
            if os.path.exists(f'/Applications/{name}.app'):
                subprocess.Popen(['open', '-na', name, '--args', f'--app={url}', '--window-size=1100,780'])
                return
    webbrowser.open_new(url)


def start_server(port=8765):
    """Start the server (once per Python process) and return its URL."""
    global _server
    if _server is None:
        for candidate in range(port, port + 20):
            try:
                _server = ThreadingHTTPServer(('127.0.0.1', candidate), Handler)
                break
            except OSError:
                continue
        else:
            raise OSError(f'No free port between {port} and {port + 19}.')
        threading.Thread(target=_server.serve_forever, daemon=True).start()
    return f'http://127.0.0.1:{_server.server_address[1]}/'


def launch(port=8765):
    """Start the app and open it in a pop-up window. Returns the app's URL."""
    url = start_server(port)
    open_window(url)
    return url


if __name__ == '__main__':
    print(f'Student app running at {launch()} (press Ctrl+C to stop)')
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
