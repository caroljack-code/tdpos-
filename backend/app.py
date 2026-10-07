import sqlite3
import json
import os
import shutil
import datetime
import jwt
import time
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, make_response
import csv
import io
from flask_cors import CORS
from werkzeug.security import generate_password_hash, check_password_hash
import cloudinary
import cloudinary.uploader
import cloudinary.api as cloudinary_api
from werkzeug.utils import secure_filename
import base64
try:
    import requests
except Exception:
    requests = None

IS_SERVERLESS = bool(os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME'))
CLOUDINARY_DB_PUBLIC_ID = "pimut_pos/data/pos_db"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, '..')

_TMPDIR = '/tmp' if (os.name == 'posix' and os.path.exists('/tmp')) else (
    os.environ.get('TEMP') or os.environ.get('TMP') or BASE_DIR
)
_DB_VERSION_FILE = os.path.join(_TMPDIR, "_db_version.json") if IS_SERVERLESS else os.path.join(BASE_DIR, "_db_version.json")

if IS_SERVERLESS:
    DB_NAME = os.path.join(_TMPDIR, "pos.db")
    PRODUCT_UPLOAD_DIR = os.path.join(_TMPDIR, 'uploads', 'products')
    BRAND_UPLOAD_DIR = os.path.join(_TMPDIR, 'uploads', 'branding')
else:
    DB_NAME = os.environ.get('DB_PATH') or os.path.join(BASE_DIR, "pos.db")
    PRODUCT_UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads', 'products')
    BRAND_UPLOAD_DIR = os.path.join(BASE_DIR, 'uploads', 'branding')

os.makedirs(PRODUCT_UPLOAD_DIR, exist_ok=True)
os.makedirs(BRAND_UPLOAD_DIR, exist_ok=True)

app = Flask(__name__, static_url_path='', static_folder=FRONTEND_DIR)
app.config['SECRET_KEY'] = 'your_secret_key_change_this_in_production'
CORS(app)

_CLOUD_NAME = None

def configure_cloudinary():
    global _CLOUD_NAME
    url = os.environ.get('CLOUDINARY_URL')
    if not url:
        url = 'cloudinary://633288168755168:fWtJvHuIIVmRVDnz0PGoR7beeLc@diocrcpdl'
    try:
        clean_url = url.replace('cloudinary://', '').strip()
        auth_part, cloud_name = clean_url.split('@')
        api_key, api_secret = auth_part.split(':')
        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True
        )
        _CLOUD_NAME = cloud_name
        print(f"Cloudinary configured successfully for cloud: {cloud_name}")
    except Exception as e:
        print(f"Warning: Cloudinary explicit config failed: {e}. Trying generic config.")
        try:
            cloudinary.config(cloudinary_url=url, secure=True)
            try:
                _CLOUD_NAME = cloudinary.config().cloud_name
            except Exception:
                pass
        except Exception as e2:
            print(f"Error: Cloudinary total config failure: {e2}")

configure_cloudinary()

def _read_local_version():
    try:
        if os.path.exists(_DB_VERSION_FILE):
            with open(_DB_VERSION_FILE, 'r') as f:
                d = json.load(f) or {}
                return int(d.get('ts') or 0)
    except Exception:
        pass
    return 0

def _write_local_version(ts):
    try:
        with open(_DB_VERSION_FILE, 'w') as f:
            json.dump({'ts': int(ts)}, f)
    except Exception:
        pass

def _is_cloudinary_configured():
    global _CLOUD_NAME
    try:
        if _CLOUD_NAME:
            return True
        cfg = cloudinary.config()
        if getattr(cfg, 'cloud_name', None):
            _CLOUD_NAME = cfg.cloud_name
            return True
    except Exception:
        pass
    return bool(os.environ.get('CLOUDINARY_URL'))

def _upload_image_to_cloudinary(source, public_id_prefix="product"):
    """Upload any image source (FileStorage, data URL, or path-like) to Cloudinary.
    Returns the secure https URL or None on failure."""
    try:
        timestamp = int(datetime.datetime.utcnow().timestamp())
        public_id = f"{public_id_prefix}_{timestamp}"
        upload_kwargs = dict(
            folder="pimut_pos/products",
            public_id=public_id,
            overwrite=True,
            invalidate=True,
            timeout=60
        )
        if hasattr(source, 'seek'):
            try: source.seek(0)
            except Exception: pass
        result = cloudinary.uploader.upload(source, **upload_kwargs)
        url = result.get('secure_url') or result.get('url')
        if url:
            print(f"[Cloudinary] Uploaded -> {public_id}: {url[:80]}")
            return url
    except Exception as e:
        print(f"[Cloudinary] Upload failed: {e}")
    return None

def _cloudinary_resource_info():
    try:
        r = cloudinary_api.resource(CLOUDINARY_DB_PUBLIC_ID, resource_type="raw")
        if r and r.get('bytes', 0) > 0:
            return {
                'version': int(r.get('version') or 0),
                'bytes': int(r.get('bytes') or 0),
                'created_at': r.get('created_at')
            }
    except Exception as e:
        if "404" in str(e) or "not found" in str(e).lower():
            return None
        print(f"[DB] Cloudinary resource_info failed: {e}")
    return None

def _cloudinary_db_url(version=None):
    if not _CLOUD_NAME:
        try:
            _CLOUD_NAME = cloudinary.config().cloud_name
        except Exception:
            return None
    if not _CLOUD_NAME:
        return None
    if version:
        return f"https://res.cloudinary.com/{_CLOUD_NAME}/raw/upload/v{version}/{CLOUDINARY_DB_PUBLIC_ID}?_={int(time.time()*1000)}"
    return f"https://res.cloudinary.com/{_CLOUD_NAME}/raw/upload/{CLOUDINARY_DB_PUBLIC_ID}?_={int(time.time()*1000)}"

def restore_db_from_cloudinary(force=False):
    if not IS_SERVERLESS:
        return False
    try:
        info = _cloudinary_resource_info()
        if not info:
            print("[DB] Cloudinary: no remote DB found (first run? will seed)")
        else:
            remote_ts = info.get('version') or 0
            local_ts = _read_local_version()
            if remote_ts and not force and (remote_ts <= local_ts):
                print(f"[DB] Cloudinary: local up-to-date (local={local_ts} remote={remote_ts})")
                return False
            url = _cloudinary_db_url(version=remote_ts if remote_ts else None)
            if not url:
                return False
            r = requests.get(url, timeout=20)
            if r.status_code == 200 and len(r.content) > 1000:
                tmp_path = DB_NAME + ".tmp"
                with open(tmp_path, 'wb') as f:
                    f.write(r.content)
                shutil.move(tmp_path, DB_NAME)
                _write_local_version(remote_ts or int(time.time()))
                print(f"[DB] Restored from Cloudinary v{remote_ts} ({len(r.content)} bytes)")
                return True
    except Exception as e:
        print(f"[DB] Cloudinary restore failed: {e}")
    local_version_exists = os.path.exists(_DB_VERSION_FILE)
    local_db_exists = os.path.exists(DB_NAME)
    try:
        original_db = os.path.join(BASE_DIR, "pos.db")
        if os.path.exists(original_db):
            if not local_db_exists and not local_version_exists:
                shutil.copy2(original_db, DB_NAME)
                print("[DB] Restored from bundled pos.db (first run only)")
                return True
            elif local_db_exists and local_version_exists:
                print("[DB] Keeping existing local DB (Cloudinary unreachable)")
            elif local_db_exists:
                print("[DB] Keeping existing local DB (no version file but DB present)")
    except Exception as e:
        print(f"[DB] Bundled restore failed: {e}")
    if not os.path.exists(DB_NAME):
        print("[DB] No remote, no bundled. Will init fresh.")
    return False

def save_db_to_cloudinary():
    if not IS_SERVERLESS:
        return True
    if not os.path.exists(DB_NAME):
        return False
    try:
        with open(DB_NAME, 'rb') as f:
            res = cloudinary.uploader.upload(
                f,
                public_id=CLOUDINARY_DB_PUBLIC_ID,
                resource_type="raw",
                overwrite=True,
                invalidate=False,
                unique_filename=False
            )
        new_version = int(res.get('version') or time.time())
        _write_local_version(new_version)
        print(f"[DB] Saved to Cloudinary: v{new_version} ({res.get('bytes', '?')} bytes)")
        return True
    except Exception as e:
        print(f"[DB] Cloudinary save failed: {e}")
        return False

_request_restored = set()

def _ensure_db_for_request():
    if not IS_SERVERLESS:
        return
    rid = request.environ.get('werkzeug.request_id') or id(request)
    if rid in _request_restored:
        return
    _request_restored.add(rid)
    try:
        restore_db_from_cloudinary(force=False)
    except Exception:
        pass

@app.before_request
def _before_req_db_sync():
    _request_restored.clear()
    if IS_SERVERLESS:
        try:
            _ensure_db_for_request()
        except Exception:
            pass

def get_db_connection():
    if IS_SERVERLESS:
        try:
            _ensure_db_for_request()
        except Exception:
            pass
    conn = sqlite3.connect(DB_NAME, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except Exception:
        pass
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Create Users table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL
        )
    ''')

    # Create Products table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER,
            price REAL NOT NULL,
            stock INTEGER NOT NULL,
            category TEXT,
            barcode TEXT,
            low_stock_threshold INTEGER,
            image_url TEXT,
            min_price REAL
        )
    ''')
    cols = [row['name'] for row in cursor.execute("PRAGMA table_info(products)").fetchall()]
    if 'parent_id' not in cols:
        cursor.execute("ALTER TABLE products ADD COLUMN parent_id INTEGER")
    if 'barcode' not in cols:
        cursor.execute("ALTER TABLE products ADD COLUMN barcode TEXT")
    if 'low_stock_threshold' not in cols:
        cursor.execute("ALTER TABLE products ADD COLUMN low_stock_threshold INTEGER")
    if 'image_url' not in cols:
        cursor.execute("ALTER TABLE products ADD COLUMN image_url TEXT")
    if 'min_price' not in cols:
        cursor.execute("ALTER TABLE products ADD COLUMN min_price REAL")
    cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode) WHERE barcode IS NOT NULL")
    seed_barcodes = {
        'Samsung 43\" Smart TV': '890100000001',
        'Blender 500W': '890100000002',
        'Double Bedsheet Set': '890100000003',
        'Non-stick Cookware Set': '890100000004',
        'Bluetooth Speaker': '890100000005',
        'Electric Kettle': '890100000006',
        'King Size Duvet': '890100000007',
        'Iron Box': '890100000008'
    }
    for name, code in seed_barcodes.items():
        cursor.execute("UPDATE products SET barcode = ? WHERE name = ? AND (barcode IS NULL OR barcode = '')", (code, name))
    
    # Create Sales table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total REAL NOT NULL,
            subtotal REAL,
            vat REAL,
            cashier TEXT,
            payment_method TEXT,
            payment_reference TEXT,
            date TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'completed'
        )
    ''')
    sales_cols = [row['name'] for row in cursor.execute("PRAGMA table_info(sales)").fetchall()]
    if 'subtotal' not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN subtotal REAL")
    if 'vat' not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN vat REAL")
    if 'cashier' not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN cashier TEXT")
    if 'payment_method' not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN payment_method TEXT")
    if 'payment_reference' not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN payment_reference TEXT")
    if 'status' not in sales_cols:
        cursor.execute("ALTER TABLE sales ADD COLUMN status TEXT DEFAULT 'completed'")

    # Create Sale Items table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sale_items (
            sale_id INTEGER,
            product_id INTEGER,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL,
            FOREIGN KEY(sale_id) REFERENCES sales(id),
            FOREIGN KEY(product_id) REFERENCES products(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sale_id INTEGER,
            action TEXT,
            reason TEXT,
            actor TEXT,
            date TEXT DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Seed products if empty
    cursor.execute("SELECT count(*) as count FROM products")
    if cursor.fetchone()['count'] == 0:
        products = [
            ('Samsung 43" Smart TV', 35000, 10, 'Electronics', '890100000001'),
            ('Blender 500W', 4500, 20, 'Kitchenware', '890100000002'),
            ('Double Bedsheet Set', 2500, 30, 'Beddings', '890100000003'),
            ('Non-stick Cookware Set', 8000, 15, 'Kitchenware', '890100000004'),
            ('Bluetooth Speaker', 3000, 25, 'Electronics', '890100000005'),
            ('Electric Kettle', 1500, 40, 'Kitchenware', '890100000006'),
            ('King Size Duvet', 5000, 12, 'Beddings', '890100000007'),
            ('Iron Box', 1200, 50, 'Electronics', '890100000008')
        ]
        cursor.executemany("INSERT INTO products (name, price, stock, category, barcode) VALUES (?, ?, ?, ?, ?)", products)
        print("Seeded initial products")
    cursor.execute("UPDATE products SET low_stock_threshold = COALESCE(low_stock_threshold, 5)")
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS banks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')
    bcount = cursor.execute("SELECT COUNT(*) as c FROM banks").fetchone()['c']
    if bcount == 0:
        cursor.executemany("INSERT INTO banks (name) VALUES (?)", [
            ('KCB Bank',),
            ('Co-op Bank',),
            ('Equity Bank',)
        ])
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    ''')
    ccount = cursor.execute("SELECT COUNT(*) as c FROM categories").fetchone()['c']
    if ccount == 0:
        cursor.executemany("INSERT INTO categories (name) VALUES (?)", [
            ('Home Appliances',),
            ('Electronics',),
            ('Beddings',),
            ('Household Items',)
        ])
    
    # Seed default users: superadmin, admin, cashier
    # Super Admin
    existing_super = cursor.execute("SELECT 1 FROM users WHERE username = ?", ('superadmin',)).fetchone()
    if not existing_super:
        super_pw = generate_password_hash('super123')
        cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                       ('superadmin', super_pw, 'super_admin'))
    # Admin
    existing_admin = cursor.execute("SELECT role FROM users WHERE username = ?", ('admin',)).fetchone()
    if not existing_admin:
        admin_pw = generate_password_hash('admin123')
        cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                       ('admin', admin_pw, 'admin'))
    else:
        if existing_admin['role'] == 'super_admin':
            cursor.execute("UPDATE users SET role = 'admin' WHERE username = 'admin'")
    # Cashier
    existing_cashier = cursor.execute("SELECT 1 FROM users WHERE username = ?", ('cashier',)).fetchone()
    if not existing_cashier:
        cashier_pw = generate_password_hash('cashier123')
        cursor.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                       ('cashier', cashier_pw, 'cashier'))
    print("Default users ensured (superadmin/super123, admin/admin123, cashier/cashier123)")
    
    conn.commit()
    conn.close()

# Initialize DB
try:
    init_db()
    if IS_SERVERLESS:
        try:
            info = _cloudinary_resource_info()
            if info is None and os.path.exists(DB_NAME):
                save_db_to_cloudinary()
        except Exception:
            pass
except Exception as e:
    print(f"CRITICAL ERROR during database initialization: {e}")

def sync_db(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        result = f(*args, **kwargs)
        if IS_SERVERLESS:
            try:
                save_db_to_cloudinary()
            except Exception as ex:
                print(f"[DB] sync_db error in {getattr(f, '__name__', '?')}: {ex}")
        return result
    return decorated

# --- Auth Helpers ---

def token_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = None
        if 'Authorization' in request.headers:
            auth_header = request.headers['Authorization']
            if auth_header.startswith('Bearer '):
                token = auth_header.split(" ")[1]
        
        if not token:
            return jsonify({'message': 'Token is missing!'}), 401
        
        try:
            data = jwt.decode(token, app.config['SECRET_KEY'], algorithms=["HS256"])
            conn = get_db_connection()
            user = conn.execute('SELECT * FROM users WHERE id = ?', (data['user_id'],)).fetchone()
            conn.close()
            if not user:
                 return jsonify({'message': 'User not found!'}), 401
            request.current_user = user
        except Exception as e:
            return jsonify({'message': 'Token is invalid!', 'error': str(e)}), 401
            
        return f(*args, **kwargs)
    return decorated

def role_required(allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(request, 'current_user'):
                 return jsonify({'message': 'User not authenticated'}), 401
            
            user_role = request.current_user['role']
            if user_role not in allowed_roles:
                return jsonify({'message': 'Permission denied'}), 403
                
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def role_required_strict(allowed_roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not hasattr(request, 'current_user'):
                return jsonify({'message': 'User not authenticated'}), 401
            user_role = request.current_user['role']
            if user_role not in allowed_roles:
                return jsonify({'message': 'Permission denied'}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# --- Routes ---

@app.route('/api/db/sync', methods=['POST'])
@token_required
@role_required_strict(['admin'])
def manual_db_sync():
    action = (request.get_json(silent=True) or {}).get('action') or 'save'
    data = {
        'is_serverless': IS_SERVERLESS,
        'db_exists': os.path.exists(DB_NAME),
        'db_size': os.path.getsize(DB_NAME) if os.path.exists(DB_NAME) else 0,
        'local_version': _read_local_version(),
    }
    try:
        info = _cloudinary_resource_info()
        data['remote'] = info
    except Exception as e:
        data['remote_error'] = str(e)
    try:
        if action == 'restore':
            data['restore_result'] = restore_db_from_cloudinary(force=True)
        else:
            if not os.path.exists(DB_NAME):
                init_db()
            data['save_result'] = save_db_to_cloudinary()
    except Exception as e:
        data['action_error'] = str(e)
    try:
        data['local_version_after'] = _read_local_version()
    except Exception:
        pass
    return jsonify({"message": "success", "data": data})

@app.route('/api/db/info', methods=['GET'])
@token_required
@role_required_strict(['admin'])
def db_info():
    data = {
        'is_serverless': IS_SERVERLESS,
        'db_path': DB_NAME,
        'db_exists': os.path.exists(DB_NAME),
        'db_size': os.path.getsize(DB_NAME) if os.path.exists(DB_NAME) else 0,
        'local_version': _read_local_version(),
        'cloudinary_url': _cloudinary_db_url(),
        'cloud_public_id': CLOUDINARY_DB_PUBLIC_ID,
        'cloud_name': _CLOUD_NAME,
    }
    try:
        data['remote'] = _cloudinary_resource_info()
    except Exception as e:
        data['remote_error'] = str(e)
    if os.path.exists(DB_NAME):
        try:
            conn = get_db_connection()
            counts = {}
            for t in ['users', 'products', 'sales', 'sale_items', 'categories', 'banks', 'holds']:
                try:
                    c = conn.execute(f'SELECT COUNT(*) AS n FROM {t}').fetchone()
                    counts[t] = int(c['n'])
                except Exception:
                    counts[t] = None
            conn.close()
            data['row_counts'] = counts
        except Exception as ex:
            data['counts_error'] = str(ex)
    return jsonify({"message": "success", "data": data})

# Login Route
@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    # Try /tmp first (for new uploads on Vercel)
    tmp_dir = os.path.join('/tmp', 'uploads')
    if os.path.exists(os.path.join(tmp_dir, filename)):
        return send_from_directory(tmp_dir, filename)
    
    # Try repo path (for pre-existing files)
    repo_dir = os.path.join(BASE_DIR, 'uploads')
    if os.path.exists(os.path.join(repo_dir, filename)):
        return send_from_directory(repo_dir, filename)
        
    return make_response("File not found", 404)

@app.route('/login', methods=['POST'])
@app.route('/api/login', methods=['POST'])
def login():
    auth = request.get_json()
    
    if not auth or not auth.get('username') or not auth.get('password'):
        return jsonify({'message': 'Could not verify', 'WWW-Authenticate': 'Basic realm="Login required!"'}), 401
    
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE username = ?', (auth.get('username'),)).fetchone()
    conn.close()
    
    if not user:
        return jsonify({'message': 'User not found'}), 401
        
    if check_password_hash(user['password_hash'], auth.get('password')):
        token = jwt.encode({
            'user_id': user['id'],
            'role': user['role'],
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=24)
        }, app.config['SECRET_KEY'], algorithm="HS256")
        if isinstance(token, bytes):
            token = token.decode('utf-8')
        
        return jsonify({
            'message': 'success',
            'token': token,
            'role': user['role'],
            'username': user['username']
        })
        
    return jsonify({'message': 'Could not verify', 'WWW-Authenticate': 'Basic realm="Login required!"'}), 401

# Serve Frontend
@app.route('/')
def index():
    return send_from_directory(FRONTEND_DIR, 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    return send_from_directory(FRONTEND_DIR, path)

@app.route('/api/ping', methods=['GET'])
def ping():
    return jsonify({"message": "pong"})

# --- Held Orders (Pause/Resume) ---
def ensure_holds_table():
    conn = get_db_connection()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS holds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT DEFAULT CURRENT_TIMESTAMP,
                cashier TEXT,
                note TEXT,
                items TEXT NOT NULL,
                payment_method TEXT,
                payment_reference TEXT,
                subtotal REAL,
                vat REAL,
                total REAL
            )
        ''')
        conn.commit()
    finally:
        conn.close()
ensure_holds_table()

@app.route('/api/holds', methods=['GET'])
@token_required
@role_required(['cashier', 'admin', 'assistant', 'super_admin'])
def list_holds():
    mine = request.args.get('mine', '1') != '0'
    conn = get_db_connection()
    try:
        if mine and request.current_user:
            rows = conn.execute("SELECT id, date, cashier, note, subtotal, vat, total FROM holds WHERE cashier = ? ORDER BY date DESC", (request.current_user['username'],)).fetchall()
        else:
            rows = conn.execute("SELECT id, date, cashier, note, subtotal, vat, total FROM holds ORDER BY date DESC").fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/holds', methods=['POST'])
@token_required
@role_required(['cashier', 'admin', 'assistant', 'super_admin'])
@sync_db
def create_hold():
    data = request.get_json() or {}
    items = data.get('items') or []
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({"error": "items required"}), 400
    note = (data.get('note') or '').strip()
    payment_method = data.get('payment_method')
    payment_reference = data.get('payment_reference')
    subtotal = float(data.get('subtotal') or 0)
    vat = float(data.get('vat') or 0)
    total = float(data.get('total') or 0)
    cashier = request.current_user['username'] if request.current_user else None
    conn = get_db_connection()
    try:
        cur = conn.execute("""
            INSERT INTO holds (cashier, note, items, payment_method, payment_reference, subtotal, vat, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (cashier, note, json.dumps(items), payment_method, payment_reference, subtotal, vat, total))
        conn.commit()
        return jsonify({"message": "success", "id": cur.lastrowid})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/holds/<int:hold_id>', methods=['GET'])
@token_required
@role_required(['cashier', 'admin', 'assistant', 'super_admin'])
def get_hold(hold_id):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT * FROM holds WHERE id = ?", (hold_id,)).fetchone()
        if not row:
            return jsonify({"error": "not found"}), 404
        d = dict(row)
        try:
            d['items'] = json.loads(d.get('items') or '[]')
        except Exception:
            d['items'] = []
        return jsonify({"message": "success", "data": d})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/holds/<int:hold_id>', methods=['DELETE'])
@token_required
@role_required(['cashier', 'admin', 'assistant', 'super_admin'])
@sync_db
def delete_hold(hold_id):
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM holds WHERE id = ?", (hold_id,))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

def get_mpesa_config():
    cfg = {
        "env": os.environ.get("MPESA_ENV", "sandbox"),
        "consumer_key": os.environ.get("MPESA_CONSUMER_KEY"),
        "consumer_secret": os.environ.get("MPESA_CONSUMER_SECRET"),
        "shortcode": os.environ.get("MPESA_SHORTCODE"),
        "passkey": os.environ.get("MPESA_PASSKEY"),
        "callback": os.environ.get("MPESA_CALLBACK_URL", "https://example.com/callback")
    }
    return cfg

def mpesa_access_token(cfg):
    if not requests:
        raise Exception("requests not installed")
    if not (cfg["consumer_key"] and cfg["consumer_secret"]):
        raise Exception("M-Pesa credentials not configured")
    base = "https://sandbox.safaricom.co.ke" if cfg["env"] == "sandbox" else "https://api.safaricom.co.ke"
    url = base + "/oauth/v1/generate?grant_type=client_credentials"
    r = requests.get(url, auth=(cfg["consumer_key"], cfg["consumer_secret"]), timeout=15)
    if r.status_code != 200:
        raise Exception("Failed to get access token")
    return r.json()["access_token"]

def mpesa_stkpush_request(cfg, token, amount, phone, account_ref="POS", trans_desc="Payment"):
    if not requests:
        raise Exception("requests not installed")
    if not (cfg["shortcode"] and cfg["passkey"]):
        raise Exception("M-Pesa shortcode/passkey not configured")
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode((cfg["shortcode"] + cfg["passkey"] + timestamp).encode()).decode()
    base = "https://sandbox.safaricom.co.ke" if cfg["env"] == "sandbox" else "https://api.safaricom.co.ke"
    url = base + "/mpesa/stkpush/v1/processrequest"
    payload = {
        "BusinessShortCode": cfg["shortcode"],
        "Password": password,
        "Timestamp": timestamp,
        "TransactionType": "CustomerPayBillOnline",
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": cfg["shortcode"],
        "PhoneNumber": phone,
        "CallBackURL": cfg["callback"],
        "AccountReference": account_ref,
        "TransactionDesc": trans_desc
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code != 200:
        raise Exception(r.text)
    return r.json()

def mpesa_query_request(cfg, token, checkout_id):
    if not requests:
        raise Exception("requests not installed")
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode((cfg["shortcode"] + cfg["passkey"] + timestamp).encode()).decode()
    base = "https://sandbox.safaricom.co.ke" if cfg["env"] == "sandbox" else "https://api.safaricom.co.ke"
    url = base + "/mpesa/stkpushquery/v1/query"
    payload = {
        "BusinessShortCode": cfg["shortcode"],
        "Password": password,
        "Timestamp": timestamp,
        "CheckoutRequestID": checkout_id
    }
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if r.status_code != 200:
        raise Exception(r.text)
    return r.json()

@app.route('/api/pay/mpesa/stkpush', methods=['POST'])
@token_required
@role_required(['cashier', 'admin'])
def mpesa_stkpush():
    data = request.get_json() or {}
    amount = float(data.get('amount') or 0)
    phone = str(data.get('phone') or '').strip()
    if amount <= 0 or not phone:
        return jsonify({"error": "amount and phone required"}), 400
    cfg = get_mpesa_config()
    # Dev fallback: if not configured, simulate success
    if not (cfg["consumer_key"] and cfg["consumer_secret"] and cfg["shortcode"] and cfg["passkey"]):
        return jsonify({
            "message": "simulated",
            "MerchantRequestID": "SIMULATED_MERCHANT",
            "CheckoutRequestID": "SIMULATED_CHECKOUT",
            "CustomerMessage": "Simulated prompt sent"
        })
    try:
        token = mpesa_access_token(cfg)
        res = mpesa_stkpush_request(cfg, token, amount, phone, account_ref="PIMUT POS", trans_desc="Sale Payment")
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/pay/mpesa/query', methods=['GET'])
@token_required
@role_required(['cashier', 'admin'])
def mpesa_query():
    checkout_id = request.args.get('CheckoutRequestID') or ''
    if not checkout_id:
        return jsonify({"error": "CheckoutRequestID required"}), 400
    cfg = get_mpesa_config()
    if checkout_id.startswith("SIMULATED"):
        return jsonify({"ResultCode": "0", "ResultDesc": "Success", "MpesaReceiptNumber": "SIM123456"})
    try:
        token = mpesa_access_token(cfg)
        res = mpesa_query_request(cfg, token, checkout_id)
        return jsonify(res)
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/banks', methods=['GET'])
@token_required
def list_banks():
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT id, name FROM banks ORDER BY name").fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/banks', methods=['POST'])
@token_required
@role_required_strict(['admin'])
@sync_db
def add_bank():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO banks (name) VALUES (?)", (name,))
        conn.commit()
        return jsonify({"message": "success"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "bank already exists"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
@app.route('/api/categories', methods=['GET'])
@token_required
def list_categories():
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/categories', methods=['POST'])
@token_required
@role_required(['admin'])
@sync_db
def add_category():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    conn = get_db_connection()
    try:
        conn.execute("INSERT INTO categories (name) VALUES (?)", (name,))
        conn.commit()
        return jsonify({"message": "success"})
    except sqlite3.IntegrityError:
        return jsonify({"error": "category already exists"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

# GET /products
@app.route('/products', methods=['GET'])
@app.route('/api/products', methods=['GET']) # Alias for frontend compatibility
@token_required
def get_products():
    try:
        conn = get_db_connection()
        products = conn.execute('SELECT * FROM products').fetchall()
        conn.close()
        data = []
        for ix in products:
            d = dict(ix)
            thr = d.get('low_stock_threshold')
            d['low_stock'] = thr is not None and d.get('stock', 0) <= int(thr)
            data.append(d)
        return jsonify({"message": "success", "data": data})
    except Exception as e:
        print(f"Error in get_products: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/products/barcode/<barcode>', methods=['GET'])
@app.route('/api/products/barcode/<barcode>', methods=['GET'])
@token_required
def get_product_by_barcode(barcode):
    conn = get_db_connection()
    product = conn.execute('SELECT * FROM products WHERE barcode = ?', (barcode,)).fetchone()
    conn.close()
    if product:
        return jsonify({"message": "success", "data": dict(product)})
    return jsonify({"error": "Product not found"}), 404

@app.route('/products/<int:id>', methods=['GET'])
@app.route('/api/products/<int:id>', methods=['GET'])
@token_required
def get_product_by_id(id):
    conn = get_db_connection()
    product = conn.execute('SELECT * FROM products WHERE id = ?', (id,)).fetchone()
    conn.close()
    if product:
        return jsonify({"message": "success", "data": dict(product)})
    return jsonify({"error": "Product not found"}), 404

# POS-friendly products endpoint (explicitly allows all authenticated roles)
@app.route('/api/pos/products', methods=['GET'])
@token_required
def get_products_for_pos():
    try:
        conn = get_db_connection()
        products = conn.execute('SELECT * FROM products').fetchall()
        conn.close()
        data = []
        for ix in products:
            d = dict(ix)
            thr = d.get('low_stock_threshold')
            d['low_stock'] = thr is not None and d.get('stock', 0) <= int(thr)
            data.append(d)
        return jsonify({"message": "success", "data": data})
    except Exception as e:
        print(f"Error in get_products_for_pos: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/products', methods=['POST'])
@token_required
@role_required(['admin', 'assistant'])
@sync_db
def create_product():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    category = (data.get('category') or '').strip()
    price = data.get('price')
    stock = data.get('stock')
    barcode = (data.get('barcode') or '').strip() or None
    low_stock_threshold = data.get('low_stock_threshold')
    image_url = (data.get('image_url') or '').strip() or None
    min_price = data.get('min_price')
    
    if not name:
        return jsonify({"error": "Name required"}), 400
    try:
        price = float(price)
        stock = int(stock)
        if min_price is not None:
            min_price = float(min_price)
    except Exception:
        return jsonify({"error": "Invalid price or stock"}), 400
    if category == '':
        category = 'General'
    if image_url and image_url.startswith('data:'):
        cloud_url = _upload_image_to_cloudinary(image_url, public_id_prefix=f"product_new")
        if cloud_url:
            image_url = cloud_url
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        if min_price is not None:
            cursor.execute(
                "INSERT INTO products (name, price, stock, category, barcode, low_stock_threshold, image_url, min_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (name, price, stock, category, barcode, low_stock_threshold, image_url, min_price)
            )
        else:
            cursor.execute(
                "INSERT INTO products (name, price, stock, category, barcode, low_stock_threshold, image_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, price, stock, category, barcode, low_stock_threshold, image_url)
            )
        new_id = cursor.lastrowid
        conn.commit()
        if image_url and image_url.startswith('data:'):
            try:
                is_vercel = os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
                if not is_vercel:
                    os.makedirs(PRODUCT_UPLOAD_DIR, exist_ok=True)
                    import re as _re
                    import base64 as _base64
                    m = _re.match(r'data:image/(png|jpeg|jpg|gif|webp);base64,(.*)', image_url, _re.S | _re.I)
                    if m:
                        ext = ('jpg' if m.group(1).lower() == 'jpeg' else m.group(1).lower())
                        fname = f"product_{new_id}_{int(datetime.datetime.utcnow().timestamp())}.{ext}"
                        path = os.path.join(PRODUCT_UPLOAD_DIR, fname)
                        with open(path, 'wb') as fh:
                            fh.write(_base64.b64decode(m.group(2)))
                        local_url = f"/uploads/products/{fname}"
                        conn.execute("UPDATE products SET image_url = ? WHERE id = ?", (local_url, new_id))
                        conn.commit()
                        image_url = local_url
            except Exception as e:
                print(f"[Product Create] Local data URL save failed: {e}")
        return jsonify({"message": "success", "id": new_id, "image_url": image_url})
    except sqlite3.IntegrityError as e:
        err = str(e)
        if 'idx_products_barcode' in err or 'UNIQUE' in err:
            return jsonify({"error": "Barcode already exists"}), 400
        return jsonify({"error": err}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/products/<int:product_id>/variants', methods=['POST', 'OPTIONS'])
@app.route('/api/products/<int:product_id>/variants/', methods=['POST', 'OPTIONS'])
@app.route('/products/<int:product_id>/variants', methods=['POST', 'OPTIONS'])
@app.route('/products/<int:product_id>/variants/', methods=['POST', 'OPTIONS'])
@token_required
@role_required(['admin', 'assistant', 'super_admin'])
@sync_db
def create_variants(product_id):
    # Handle preflight/OPTIONS quickly
    if request.method == 'OPTIONS':
        return jsonify({"message": "ok"}), 200
    data = request.get_json() or {}
    variants = data.get('variants') or []
    if not isinstance(variants, list) or not variants:
        return jsonify({"error": "variants list required"}), 400
    conn = get_db_connection()
    try:
        parent = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
        if not parent:
            return jsonify({"error": "Parent product not found"}), 404
        cursor = conn.cursor()
        created_ids = []
        for v in variants:
            color = (v.get('color') or '').strip()
            size = (v.get('size') or '').strip()
            name_suffix = " ".join([s for s in [size, color] if s]).strip()
            child_name = parent['name'] + (f" ({name_suffix})" if name_suffix else "")
            price = v.get('price', parent['price'])
            stock = v.get('stock', 0)
            barcode = (v.get('barcode') or '').strip() or None
            min_price = v.get('min_price', parent['min_price'])
            cursor.execute(
                "INSERT INTO products (name, parent_id, price, stock, category, barcode, low_stock_threshold, image_url, min_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (child_name, parent['id'], float(price), int(stock), parent['category'], barcode, parent['low_stock_threshold'], parent['image_url'], float(min_price) if min_price is not None else None)
            )
            created_ids.append(cursor.lastrowid)
        conn.commit()
        return jsonify({"message": "success", "ids": created_ids})
    except sqlite3.IntegrityError as e:
        err = str(e)
        if 'idx_products_barcode' in err or 'UNIQUE' in err:
            return jsonify({"error": "Barcode already exists"}), 400
        return jsonify({"error": err}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
@app.route('/api/users', methods=['GET'])
@token_required
@role_required(['admin'])
def list_users():
    role = request.args.get('role')
    conn = get_db_connection()
    try:
        if role:
            rows = conn.execute("SELECT id, username, role FROM users WHERE role = ?", (role,)).fetchall()
        else:
            rows = conn.execute("SELECT id, username, role FROM users").fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
@app.route('/api/users', methods=['POST'])
@token_required
@role_required_strict(['admin'])
@sync_db
def create_user():
    data = request.get_json() or {}
    username = (data.get('username') or '').strip()
    password = (data.get('password') or '').strip()
    role = (data.get('role') or 'cashier').strip()
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    allowed_roles = ['cashier', 'admin', 'assistant']
    if role == 'super_admin':
        # Allow creating super_admin from admin or existing super_admin
        if request.current_user['role'] not in ['admin', 'super_admin']:
            role = 'cashier'
    elif role not in allowed_roles:
        role = 'cashier'
    conn = get_db_connection()
    try:
        hashed = generate_password_hash(password)
        conn.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", (username, hashed, role))
        conn.commit()
        new_id = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()['id']
        return jsonify({"message": "success", "id": new_id})
    except sqlite3.IntegrityError:
        return jsonify({"error": "username already exists"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
@app.route('/api/me/password', methods=['POST'])
@token_required
@sync_db
def change_my_password():
    data = request.get_json() or {}
    old_pw = (data.get('old_password') or '').strip()
    new_pw = (data.get('new_password') or '').strip()
    if not old_pw or not new_pw:
        return jsonify({"error": "old_password and new_password required"}), 400
    conn = get_db_connection()
    try:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (request.current_user['id'],)).fetchone()
        if not user or not check_password_hash(user['password_hash'], old_pw):
            return jsonify({"error": "invalid old password"}), 400
        hashed = generate_password_hash(new_pw)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user['id']))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
@app.route('/api/users/<int:user_id>/password', methods=['PUT'])
@token_required
@role_required(['admin'])
@sync_db
def admin_set_password(user_id):
    data = request.get_json() or {}
    new_pw = (data.get('new_password') or '').strip()
    if not new_pw:
        return jsonify({"error": "new_password required"}), 400
    conn = get_db_connection()
    try:
        user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify({"error": "user not found"}), 404
        hashed = generate_password_hash(new_pw)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hashed, user_id))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/products/<int:id>/image/upload', methods=['POST'])
@token_required
@role_required(['admin', 'assistant'])
@sync_db
def upload_product_image(id):
    file = request.files.get('file')
    if not file:
        data = request.get_json(silent=True) or {}
        if data.get('file'):
            file = data['file']
        elif data.get('data_url'):
            file = data['data_url']
        else:
            return jsonify({"error": "file required"}), 400
    is_vercel = os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
    url = None

    if _is_cloudinary_configured():
        url = _upload_image_to_cloudinary(file, public_id_prefix=f"product_{id}")
        if not url and is_vercel:
            return jsonify({"error": "Cloudinary upload failed; local storage not persistent on Vercel"}), 500
    elif is_vercel:
        return jsonify({"error": "Cloudinary not configured; cannot persist images on Vercel"}), 500

    if not url:
        print("[Upload] Cloudinary unavailable or failed; falling back to local storage")
        has_file_obj = hasattr(file, 'save') and callable(file.save)
        name = ''
        if has_file_obj:
            name = secure_filename(file.filename or '')
        else:
            name = f"product_{id}.jpg"
        ext = os.path.splitext(name)[1].lower()
        if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
            ext = '.jpg'
        os.makedirs(PRODUCT_UPLOAD_DIR, exist_ok=True)
        fname = f"product_{id}_{int(datetime.datetime.utcnow().timestamp())}{ext}"
        path = os.path.join(PRODUCT_UPLOAD_DIR, fname)
        if has_file_obj:
            try: file.seek(0)
            except Exception: pass
            file.save(path)
        else:
            import re as _re
            import base64 as _base64
            if isinstance(file, str) and file.startswith('data:'):
                m = _re.match(r'data:image/(png|jpeg|jpg|gif|webp);base64,(.*)', file, _re.S | _re.I)
                if m:
                    ext = ('jpg' if m.group(1).lower() == 'jpeg' else m.group(1).lower())
                    fname = f"product_{id}_{int(datetime.datetime.utcnow().timestamp())}.{ext}"
                    path = os.path.join(PRODUCT_UPLOAD_DIR, fname)
                    with open(path, 'wb') as fh:
                        fh.write(_base64.b64decode(m.group(2)))
                else:
                    return jsonify({"error": "invalid data URL"}), 400
            elif isinstance(file, str):
                try:
                    import requests as _req
                    r = _req.get(file, timeout=30)
                    r.raise_for_status()
                    with open(path, 'wb') as fh:
                        fh.write(r.content)
                except Exception as e:
                    return jsonify({"error": f"Failed to fetch URL: {e}"}), 400
            else:
                return jsonify({"error": "unsupported file"}), 400
        url = f"/uploads/products/{fname}"

    if not url:
        return jsonify({"error": "Failed to determine image URL"}), 500

    conn = get_db_connection()
    try:
        conn.execute("UPDATE products SET image_url = ? WHERE id = ?", (url, id))
        conn.commit()
        return jsonify({"message": "success", "image_url": url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/branding/logo', methods=['POST'])
@token_required
@role_required(['admin'])
@sync_db
def upload_brand_logo():
    file = request.files.get('file')
    if not file:
        return jsonify({"error": "file required"}), 400
    
    try:
        upload_result = cloudinary.uploader.upload(file, folder="pimut_pos/branding", public_id="logo")
        url = upload_result.get('secure_url')
    except Exception as e:
        print(f"Cloudinary logo upload failed: {e}. Falling back to local storage.")
        name = secure_filename(file.filename or '')
        ext = os.path.splitext(name)[1].lower()
        if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
            return jsonify({"error": "invalid file type"}), 400
        os.makedirs(BRAND_UPLOAD_DIR, exist_ok=True)
        fname = f"logo{ext}"
        path = os.path.join(BRAND_UPLOAD_DIR, fname)
        file.seek(0)
        file.save(path)
        url = f"/uploads/branding/{fname}"
        
    return jsonify({"message": "success", "image_url": url})

@app.route('/api/branding/logo', methods=['GET'])
def get_brand_logo():
    try:
        exts = ['.jpg', '.jpeg', '.png', '.gif', '.webp']
        for ext in exts:
            candidate = os.path.join(BRAND_UPLOAD_DIR, f"logo{ext}")
            if os.path.exists(candidate):
                return jsonify({"message": "success", "image_url": f"/uploads/branding/logo{ext}"})
        if _is_cloudinary_configured():
            try:
                info = cloudinary_api.resource("pimut_pos/branding/logo")
                if info and info.get("secure_url"):
                    return jsonify({"message": "success", "image_url": info["secure_url"]})
            except Exception as ce:
                if "404" not in str(ce) and "not found" not in str(ce).lower():
                    print(f"[Logo] Cloudinary lookup failed: {ce}")
        return jsonify({"error": "not_found"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/sale', methods=['POST'])
@app.route('/api/sales', methods=['POST']) # Alias for frontend compatibility
@token_required
@role_required(['cashier', 'admin', 'super_admin'])
@sync_db
def create_sale():
    data = request.get_json()
    items = data.get('items') # List of {productId, quantity, price}
    payment_method = data.get('payment_method') or data.get('paymentMethod')
    payment_reference = data.get('payment_reference') or data.get('paymentReference')
    
    if not items:
        return jsonify({"error": "No items in sale"}), 400
    allowed_methods = {'cash', 'mpesa', 'bank', 'card', 'cheque', 'credit', 'jumia'}
    if payment_method not in allowed_methods:
        return jsonify({"error": "Invalid payment method"}), 400
    if payment_method in {'mpesa', 'bank', 'card', 'cheque', 'credit', 'jumia'} and payment_reference is not None:
        payment_reference = str(payment_reference).strip() or None

    normalized_items = []
    for i, item in enumerate(items):
        raw_id = item.get('productId') if item.get('productId') is not None else item.get('id')
        try:
            product_id = int(raw_id)
        except Exception:
            return jsonify({"error": f"Invalid item {i+1}: bad product id {repr(raw_id)}"}), 400
        try:
            quantity = int(item.get('quantity', 0))
        except Exception:
            return jsonify({"error": f"Invalid item {i+1} (product {product_id}): bad quantity"}), 400
        try:
            price = float(item.get('price', 0))
        except Exception:
            return jsonify({"error": f"Invalid item {i+1} (product {product_id}): bad price"}), 400
        normalized_items.append({'productId': product_id, 'quantity': quantity, 'price': price})
    items = normalized_items

    conn = get_db_connection()
    try:
        conn.execute("BEGIN TRANSACTION")
        
        subtotal = 0
        for item in items:
            subtotal += float(item['price']) * int(item['quantity'])
        vat = 0
        total = subtotal
        cashier = request.current_user['username']

        cursor = conn.cursor()
        cols = [row['name'] for row in conn.execute("PRAGMA table_info(sales)").fetchall()]
        if 'items' in cols:
            cursor.execute(
                "INSERT INTO sales (total, subtotal, vat, cashier, payment_method, payment_reference, items) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (total, subtotal, vat, cashier, payment_method, payment_reference, json.dumps(items))
            )
        else:
            cursor.execute(
                "INSERT INTO sales (total, subtotal, vat, cashier, payment_method, payment_reference) VALUES (?, ?, ?, ?, ?, ?)",
                (total, subtotal, vat, cashier, payment_method, payment_reference)
            )
        sale_id = cursor.lastrowid
        
        for item in items:
            product_id = item['productId']
            quantity = item['quantity']
            price = item['price']
            
            cur = conn.execute("SELECT id, name, stock, min_price FROM products WHERE id = ?", (product_id,)).fetchone()
            if not cur:
                raise Exception(f"Product not found (id={product_id}). Try refreshing the page and re-adding the item.")
            product_name = cur['name']
            if cur['min_price'] is not None and float(price) < float(cur['min_price']):
                raise Exception(f"Price below minimum allowed for {product_name}")
            if cur['stock'] < quantity:
                raise Exception(f"Insufficient stock for {product_name} (have {cur['stock']}, need {quantity})")
            
            conn.execute("UPDATE products SET stock = stock - ? WHERE id = ?", (quantity, product_id))
            
            conn.execute("INSERT INTO sale_items (sale_id, product_id, quantity, price) VALUES (?, ?, ?, ?)", 
                         (sale_id, product_id, quantity, price))
        
        conn.commit()
        return jsonify({"message": "success", "saleId": sale_id})
        
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/sales/<int:sale_id>', methods=['GET'])
@token_required
@role_required(['cashier', 'admin', 'assistant', 'super_admin'])
def get_sale(sale_id):
    conn = get_db_connection()
    try:
        sale = conn.execute("SELECT id, date, cashier, payment_method, payment_reference, subtotal, vat, total, status FROM sales WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            return jsonify({"error": "Sale not found"}), 404
        items = conn.execute("""
            SELECT si.product_id, si.quantity, si.price, COALESCE(p.name, '') as name
            FROM sale_items si
            LEFT JOIN products p ON si.product_id = p.id
            WHERE si.sale_id = ?
        """, (sale_id,)).fetchall()
        return jsonify({
            "message": "success",
            "sale": dict(sale),
            "items": [dict(ix) for ix in items]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/sales/<int:sale_id>/refund', methods=['POST'])
@token_required
@role_required(['admin'])
@sync_db
def refund_sale(sale_id):
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({"error": "Reason required"}), 400
    conn = get_db_connection()
    try:
        conn.execute("BEGIN TRANSACTION")
        sale = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            raise Exception("Sale not found")
        if sale['status'] != 'completed':
            raise Exception("Sale not refundable")
        items = conn.execute("SELECT product_id, quantity FROM sale_items WHERE sale_id = ?", (sale_id,)).fetchall()
        for it in items:
            conn.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (it['quantity'], it['product_id']))
        conn.execute("UPDATE sales SET status = 'refunded' WHERE id = ?", (sale_id,))
        actor = request.current_user['username']
        conn.execute("INSERT INTO audit_log (sale_id, action, reason, actor) VALUES (?, ?, ?, ?)",
                     (sale_id, 'refund', reason, actor))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/sales/recent', methods=['GET'])
@token_required
@role_required(['cashier', 'admin', 'assistant', 'super_admin'])
def recent_sales():
    start = request.args.get('start')
    end = request.args.get('end')
    q = (request.args.get('q') or '').strip()
    try:
        limit = int(request.args.get('limit', '50'))
    except Exception:
        limit = 50
    limit = max(1, min(limit, 200))
    conn = get_db_connection()
    try:
        base = """
            SELECT id, date, cashier, payment_method, payment_reference, subtotal, vat, total, status
            FROM sales
        """
        params = []
        where = []
        if start and end:
            where.append("DATE(date) BETWEEN ? AND ?")
            params.extend([start, end])
        if q:
            where.append("(CAST(id AS TEXT) LIKE ? OR cashier LIKE ? OR payment_reference LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        if where:
            base += " WHERE " + " AND ".join(where)
        base += " ORDER BY date DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(base, tuple(params)).fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
@app.route('/api/sales/<int:sale_id>/void', methods=['POST'])
@token_required
@role_required(['admin'])
def void_sale(sale_id):
    data = request.get_json() or {}
    reason = (data.get('reason') or '').strip()
    if not reason:
        return jsonify({"error": "Reason required"}), 400
    conn = get_db_connection()
    try:
        conn.execute("BEGIN TRANSACTION")
        sale = conn.execute("SELECT * FROM sales WHERE id = ?", (sale_id,)).fetchone()
        if not sale:
            raise Exception("Sale not found")
        if sale['status'] != 'completed':
            raise Exception("Sale not voidable")
        sale_date = conn.execute("SELECT DATE(date) as d FROM sales WHERE id = ?", (sale_id,)).fetchone()['d']
        today = conn.execute("SELECT DATE('now') as d").fetchone()['d']
        if sale_date != today:
            raise Exception("Void only allowed same day")
        items = conn.execute("SELECT product_id, quantity FROM sale_items WHERE sale_id = ?", (sale_id,)).fetchall()
        for it in items:
            conn.execute("UPDATE products SET stock = stock + ? WHERE id = ?", (it['quantity'], it['product_id']))
        conn.execute("UPDATE sales SET status = 'voided' WHERE id = ?", (sale_id,))
        actor = request.current_user['username']
        conn.execute("INSERT INTO audit_log (sale_id, action, reason, actor) VALUES (?, ?, ?, ?)",
                     (sale_id, 'void', reason, actor))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

# PUT /products/<id>/stock
@app.route('/products/<int:id>/stock', methods=['PUT'])
@app.route('/api/products/<int:id>/stock', methods=['PUT']) # Alias
@token_required
@role_required(['admin'])
@sync_db
def update_stock(id):
    data = request.get_json()
    new_stock = data.get('stock')
    
    if new_stock is None:
        return jsonify({"error": "Stock value required"}), 400
        
    conn = get_db_connection()
    try:
        conn.execute("UPDATE products SET stock = ? WHERE id = ?", (new_stock, id))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

@app.route('/api/products/<int:id>/threshold', methods=['PUT'])
@token_required
@role_required(['admin'])
@sync_db
def update_low_stock_threshold(id):
    data = request.get_json() or {}
    thr = data.get('low_stock_threshold')
    if thr is None:
        return jsonify({"error": "low_stock_threshold required"}), 400
    try:
        thr_i = int(thr)
    except Exception:
        return jsonify({"error": "invalid threshold"}), 400
    conn = get_db_connection()
    try:
        conn.execute("UPDATE products SET low_stock_threshold = ? WHERE id = ?", (thr_i, id))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
@app.route('/api/products/<int:id>/min_price', methods=['PUT'])
@token_required
@role_required(['admin'])
@sync_db
def update_min_price(id):
    data = request.get_json() or {}
    mp = data.get('min_price')
    if mp is None:
        return jsonify({"error": "min_price required"}), 400
    try:
        val = float(mp)
        if val < 0:
            return jsonify({"error": "invalid min_price"}), 400
    except Exception:
        return jsonify({"error": "invalid min_price"}), 400
    conn = get_db_connection()
    try:
        conn.execute("UPDATE products SET min_price = ? WHERE id = ?", (val, id))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()
@app.route('/api/products/low-stock', methods=['GET'])
@token_required
@role_required(['admin'])
def get_low_stock_products():
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM products WHERE low_stock_threshold IS NOT NULL AND stock <= low_stock_threshold"
        ).fetchall()
        data = []
        for ix in rows:
            d = dict(ix)
            d['low_stock'] = True
            data.append(d)
        return jsonify({"message": "success", "data": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/products/<int:id>/image', methods=['POST'])
@token_required
@role_required(['admin', 'assistant'])
@sync_db
def set_product_image(id):
    data = request.get_json() or {}
    image_url = (data.get('image_url') or '').strip()
    if not image_url:
        return jsonify({"error": "image_url required"}), 400
    try:
        secure_url = None
        if _is_cloudinary_configured():
            secure_url = _upload_image_to_cloudinary(image_url, public_id_prefix=f"product_{id}")
            if not secure_url:
                return jsonify({"error": "Cloudinary upload failed"}), 500
        else:
            is_vercel = os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME')
            if is_vercel and image_url.startswith('data:'):
                return jsonify({"error": "Cloudinary required on Vercel for persistent image storage"}), 500
            secure_url = image_url
        conn = get_db_connection()
        conn.execute("UPDATE products SET image_url = ? WHERE id = ?", (secure_url, id))
        conn.commit()
        conn.close()
        return jsonify({"message": "success", "image_url": secure_url})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/api/products/<int:id>/image', methods=['DELETE'])
@token_required
@role_required(['admin', 'assistant'])
def remove_product_image(id):
    try:
        cloudinary.uploader.destroy(f"pimut_pos/products/product_{id}", invalidate=True)
    except Exception:
        pass
    conn = get_db_connection()
    try:
        conn.execute("UPDATE products SET image_url = NULL WHERE id = ?", (id,))
        conn.commit()
        return jsonify({"message": "success"})
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    finally:
        conn.close()

# Daily Report (Extra, for frontend compatibility)
@app.route('/api/sales/daily', methods=['GET'])
@token_required
@role_required(['admin', 'assistant'])
def get_daily_sales():
    conn = get_db_connection()
    try:
        query = """
            SELECT DATE(date) as sale_date,
                   COUNT(id) as total_sales,
                   SUM(subtotal) as subtotal_sum,
                   SUM(vat) as vat_sum,
                   SUM(total) as total_revenue
            FROM sales
            GROUP BY DATE(date)
            ORDER BY sale_date DESC
        """
        report = conn.execute(query).fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in report]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/reports/daily', methods=['GET'])
@token_required
@role_required(['admin', 'assistant', 'super_admin'])
def report_daily():
    start = request.args.get('start')
    end = request.args.get('end')
    conn = get_db_connection()
    try:
        base = """
            SELECT DATE(date) as sale_date,
                   COUNT(id) as total_sales,
                   SUM(subtotal) as subtotal_sum,
                   SUM(vat) as vat_sum,
                   SUM(total) as total_sum
            FROM sales
        """
        if start and end:
            if request.current_user['role'] == 'super_admin':
                try:
                    d1 = datetime.datetime.strptime(start, "%Y-%m-%d").date()
                    d2 = datetime.datetime.strptime(end, "%Y-%m-%d").date()
                    if (d2 - d1).days + 1 > 7:
                        return jsonify({"error": "super_admin limited to ranges up to 7 days"}), 403
                except Exception:
                    pass
            base += " WHERE DATE(date) BETWEEN ? AND ?"
            base += " GROUP BY DATE(date) ORDER BY sale_date DESC"
            rows = conn.execute(base, (start, end)).fetchall()
        else:
            if request.current_user['role'] == 'super_admin':
                base += " WHERE DATE(date) >= DATE('now','-6 days','localtime')"
            base += " GROUP BY DATE(date) ORDER BY sale_date DESC"
            rows = conn.execute(base).fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/reports/cashier', methods=['GET'])
@token_required
@role_required(['admin', 'super_admin'])
def report_by_cashier():
    start = request.args.get('start')
    end = request.args.get('end')
    conn = get_db_connection()
    try:
        base = """
            SELECT cashier,
                   COUNT(id) as total_sales,
                   SUM(subtotal) as subtotal_sum,
                   SUM(vat) as vat_sum,
                   SUM(total) as total_sum
            FROM sales
        """
        if start and end:
            if request.current_user['role'] == 'super_admin':
                try:
                    d1 = datetime.datetime.strptime(start, "%Y-%m-%d").date()
                    d2 = datetime.datetime.strptime(end, "%Y-%m-%d").date()
                    if (d2 - d1).days + 1 > 7:
                        return jsonify({"error": "super_admin limited to ranges up to 7 days"}), 403
                except Exception:
                    pass
            base += " WHERE DATE(date) BETWEEN ? AND ?"
            base += " GROUP BY cashier ORDER BY cashier"
            rows = conn.execute(base, (start, end)).fetchall()
        else:
            if request.current_user['role'] == 'super_admin':
                base += " WHERE DATE(date) >= DATE('now','-6 days','localtime')"
            base += " GROUP BY cashier ORDER BY cashier"
            rows = conn.execute(base).fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/reports/items', methods=['GET'])
@token_required
@role_required(['admin', 'assistant', 'super_admin'])
def report_items_daily():
    period = (request.args.get('period') or 'daily').lower()
    start = request.args.get('start')
    end = request.args.get('end')
    conn = get_db_connection()
    try:
        if period not in ('daily', 'weekly', 'monthly'):
            return jsonify({"error": "Invalid period"}), 400
        if period == 'daily':
            label = "DATE(s.date)"
        elif period == 'weekly':
            label = "strftime('%Y-W%W', s.date)"
        else:
            label = "strftime('%Y-%m', s.date)"
        base = f"""
            SELECT {label} AS period_label,
                   COALESCE(p.name, 'Unknown') AS item_name,
                   SUM(si.quantity) AS units_sold,
                   SUM(si.quantity * si.price) AS revenue
            FROM sale_items si
            JOIN sales s ON si.sale_id = s.id
            LEFT JOIN products p ON si.product_id = p.id
        """
        params = []
        where = []
        if start and end:
            where.append("DATE(s.date) BETWEEN ? AND ?")
            params.extend([start, end])
        else:
            if period == 'daily':
                where.append("DATE(s.date) = DATE('now','localtime')")
            elif period == 'weekly':
                where.append("DATE(s.date) >= DATE('now','-6 days','localtime')")
            else:
                where.append("strftime('%Y-%m', s.date) = strftime('%Y-%m','now','localtime')")
        if where:
            base += " WHERE " + " AND ".join(where)
        base += " GROUP BY period_label, item_name ORDER BY period_label DESC, units_sold DESC"
        rows = conn.execute(base, tuple(params)).fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/reports/payment_methods', methods=['GET'])
@token_required
@role_required_strict(['admin', 'super_admin'])
def report_payment_methods():
    period = (request.args.get('period') or 'monthly').lower()
    start = request.args.get('start')
    end = request.args.get('end')
    # Allow daily now
    if period not in ('daily', 'weekly', 'monthly', 'annual'):
        return jsonify({"error": "Invalid period"}), 400
    # Enforce super_admin restriction: only daily or weekly
    user_role = request.current_user['role']
    if user_role == 'super_admin' and period not in ('daily', 'weekly'):
        return jsonify({"error": "Forbidden: super_admin limited to daily or weekly"}), 403
    if period == 'daily':
        label = "DATE(date)"
    elif period == 'weekly':
        label = "strftime('%Y-W%W', date)"
    elif period == 'monthly':
        label = "strftime('%Y-%m', date)"
    else:
        label = "strftime('%Y', date)"
    conn = get_db_connection()
    try:
        base = f"""
            SELECT {label} as period_label,
                   payment_method,
                   COUNT(id) as total_sales,
                   SUM(subtotal) as subtotal_sum,
                   SUM(vat) as vat_sum,
                   SUM(total) as total_sum
            FROM sales
        """
        params = ()
        if start and end:
            base += " WHERE DATE(date) BETWEEN ? AND ?"
            params = (start, end)
        base += " GROUP BY period_label, payment_method ORDER BY period_label DESC, payment_method"
        rows = conn.execute(base, params).fetchall()
        return jsonify({"message": "success", "data": [dict(ix) for ix in rows]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/export/sales.csv', methods=['GET'])
@token_required
@role_required(['admin'])
def export_sales_csv():
    start = request.args.get('start')
    end = request.args.get('end')
    conn = get_db_connection()
    try:
        base = """
            SELECT id, date, cashier, payment_method, payment_reference, subtotal, vat, total, status
            FROM sales
        """
        params = ()
        if start and end:
            base += " WHERE DATE(date) BETWEEN ? AND ?"
            params = (start, end)
        base += " ORDER BY date DESC"
        rows = conn.execute(base, params).fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['id','date','cashier','payment_method','payment_reference','subtotal','vat','total','status'])
        for r in rows:
            writer.writerow([r['id'], r['date'], r['cashier'], r['payment_method'], r['payment_reference'], r['subtotal'], r['vat'], r['total'], r['status']])
        resp = make_response(output.getvalue())
        resp.headers['Content-Type'] = 'text/csv'
        resp.headers['Content-Disposition'] = 'attachment; filename="sales_export.csv"'
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()

@app.route('/api/export/products.csv', methods=['GET'])
@token_required
@role_required(['admin'])
def export_products_csv():
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT id, name, category, price, stock, barcode, low_stock_threshold, min_price FROM products ORDER BY name").fetchall()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['id','name','category','price','stock','barcode','low_stock_threshold','min_price'])
        for r in rows:
            writer.writerow([r['id'], r['name'], r['category'], r['price'], r['stock'], r['barcode'], r['low_stock_threshold'], r['min_price']])
        resp = make_response(output.getvalue())
        resp.headers['Content-Type'] = 'text/csv'
        resp.headers['Content-Disposition'] = 'attachment; filename="products_export.csv"'
        return resp
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        conn.close()
def _get_lan_ips():
    ips = []
    try:
        import socket
        host = socket.gethostname()
        try:
            local_hosts = socket.gethostbyname_ex(host)
            for ip in local_hosts[2]:
                if not ip.startswith("127.") and not ip.startswith("169.254."):
                    ips.append(ip)
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ext_ip = s.getsockname()[0]
            s.close()
            if ext_ip and not ext_ip.startswith("127.") and ext_ip not in ips:
                ips.insert(0, ext_ip)
        except Exception:
            pass
    except Exception:
        pass
    return ips


if __name__ == '__main__':
    port = int(os.environ.get('POS_PORT', '5000'))
    bind_host = os.environ.get('POS_BIND_HOST', '').strip() or '0.0.0.0'

    hosts_to_try = []
    if bind_host and bind_host.lower() != 'auto':
        hosts_to_try.append(bind_host)
    hosts_to_try.extend(['0.0.0.0', '127.0.0.1', 'localhost'])
    seen = set()
    hosts_to_try = [h for h in hosts_to_try if not (h in seen or seen.add(h))]

    lan_ips = _get_lan_ips()
    print("=" * 58)
    print("  PIMUT TRADERS POS SERVER")
    print("=" * 58)
    print(f"  Local access:    http://127.0.0.1:{port}/")
    for ip in lan_ips:
        print(f"  Phone/Device:    http://{ip}:{port}/")
    if not lan_ips:
        print(f"  Phone/Device:    [Find your PC's LAN IP under Network Settings]")
    print("=" * 58)
    print(" Allow the Firewall popup if shown so phones can connect.")
    print()

    success = False
    last_error = None
    for h in hosts_to_try:
        print(f"--- Binding to {h}:{port} ---")
        try:
            app.run(host=h, port=port, threaded=True, use_reloader=False)
            success = True
            break
        except Exception as e:
            last_error = str(e)
            print(f"FAILED on {h}: {last_error}")
            continue
        except BaseException as e:
            last_error = str(e)
            print(f"CRITICAL ERROR on {h}: {last_error}")
            continue

    if not success:
        print("\n******************************************************")
        print(" ERROR: The POS server could not start.")
        print(f" Last error: {last_error}")
        print(" TIP: Check the TCP port is free OR allow python.exe")
        print("      through Windows Firewall (private networks).")
        print("******************************************************")
        import time
        time.sleep(12)
        sys.exit(1)