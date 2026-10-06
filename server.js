const express = require('express');
const sqlite3 = require('sqlite3').verbose();
const bodyParser = require('body-parser');
const cors = require('cors');
const path = require('path');
const jwt = require('jsonwebtoken');
const bcrypt = require('bcryptjs');

const app = express();
const port = 3000;
const SECRET_KEY = 'your_secret_key_change_this_in_production';

app.use(cors());
app.use(bodyParser.json({ limit: '50mb' }));
app.use(express.static('.'));
app.use('/uploads', express.static('uploads'));
app.use('/uploads/products', express.static('uploads/products'));

const db = new sqlite3.Database('./pos.db', (err) => {
    if (err) {
        console.error('Error opening database', err.message);
    } else {
        console.log('Connected to the SQLite database.');
        initDb();
    }
});

function initDb() {
    db.serialize(() => {
        db.run(`CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL
        )`);

        db.run(`CREATE TABLE IF NOT EXISTS products (
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
        )`);

        db.all("PRAGMA table_info(products)", (err, cols) => {
            if (err) return;
            const colNames = cols.map(c => c.name);
            const adds = [
                ['parent_id', 'INTEGER'],
                ['barcode', 'TEXT'],
                ['low_stock_threshold', 'INTEGER'],
                ['image_url', 'TEXT'],
                ['min_price', 'REAL']
            ];
            adds.forEach(([name, type]) => {
                if (!colNames.includes(name)) {
                    db.run(`ALTER TABLE products ADD COLUMN ${name} ${type}`, (e) => {
                        if (e) console.log(`Alter products add ${name}:`, e.message);
                    });
                }
            });
            db.run("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode) WHERE barcode IS NOT NULL");
        });

        db.run(`CREATE TABLE IF NOT EXISTS sales (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            total REAL NOT NULL,
            subtotal REAL,
            vat REAL,
            cashier TEXT,
            payment_method TEXT,
            payment_reference TEXT,
            date TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'completed',
            items TEXT
        )`);

        db.all("PRAGMA table_info(sales)", (err, cols) => {
            if (err) return;
            const colNames = cols.map(c => c.name);
            const adds = [
                ['subtotal', 'REAL'],
                ['vat', 'REAL'],
                ['cashier', 'TEXT'],
                ['payment_method', 'TEXT'],
                ['payment_reference', 'TEXT'],
                ['status', 'TEXT DEFAULT \'completed\''],
                ['items', 'TEXT']
            ];
            adds.forEach(([name, type]) => {
                if (!colNames.includes(name)) {
                    db.run(`ALTER TABLE sales ADD COLUMN ${name} ${type}`, (e) => {
                        if (e) console.log(`Alter sales add ${name}:`, e.message);
                    });
                }
            });
        });

        db.run(`CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )`);

        db.run(`CREATE TABLE IF NOT EXISTS banks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )`);

        db.run(`CREATE TABLE IF NOT EXISTS sale_items (
            sale_id INTEGER,
            product_id INTEGER,
            quantity INTEGER NOT NULL,
            price REAL NOT NULL
        )`);

        db.run(`CREATE TABLE IF NOT EXISTS holds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            items TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            cashier TEXT
        )`);

        db.get("SELECT count(*) as count FROM users", (err, row) => {
            if (err) return;
            if (row.count === 0) {
                const saltRounds = 10;
                const users = [
                    ['super_admin', 'super123', 'super_admin'],
                    ['admin', 'admin123', 'admin'],
                    ['cashier', 'cashier123', 'cashier']
                ];
                users.forEach(([u, p, r]) => {
                    const hash = bcrypt.hashSync(p, saltRounds);
                    db.run("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", [u, hash, r]);
                });
                console.log('Default users ensured (superadmin/super123, admin/admin123, cashier/cashier123)');
            }
        });

        db.get("SELECT count(*) as count FROM products", (err, row) => {
            if (err) return;
            if (row.count === 0) {
                const stmt = db.prepare("INSERT INTO products (name, price, stock, category, barcode) VALUES (?, ?, ?, ?, ?)");
                const products = [
                    ['Samsung 43" Smart TV', 35000, 10, 'Electronics', '890100000001'],
                    ['Blender 500W', 4500, 20, 'Kitchenware', '890100000002'],
                    ['Double Bedsheet Set', 2500, 30, 'Beddings', '890100000003'],
                    ['Non-stick Cookware Set', 8000, 15, 'Kitchenware', '890100000004'],
                    ['Bluetooth Speaker', 3000, 25, 'Electronics', '890100000005'],
                    ['Electric Kettle', 1500, 40, 'Kitchenware', '890100000006'],
                    ['King Size Duvet', 5000, 12, 'Beddings', '890100000007'],
                    ['Iron Box', 1200, 50, 'Electronics', '890100000008']
                ];
                products.forEach(p => stmt.run(p));
                stmt.finalize();
                console.log('Seeded initial products');
            }
        });

        const defaultCats = ['Home Appliances', 'Electronics', 'Beddings', 'Household Items', 'Kitchenware'];
        defaultCats.forEach(name => {
            db.run("INSERT OR IGNORE INTO categories (name) VALUES (?)", [name]);
        });
    });
}

function authenticateToken(req, res, next) {
    const authHeader = req.headers['authorization'];
    const token = authHeader && authHeader.startsWith('Bearer ') ? authHeader.split(' ')[1] : null;
    if (!token) {
        return res.status(401).json({ message: 'Token is missing!' });
    }
    jwt.verify(token, SECRET_KEY, (err, decoded) => {
        if (err) return res.status(401).json({ message: 'Token is invalid!', error: err.message });
        req.user = decoded;
        next();
    });
}

function requireRole(roles) {
    return (req, res, next) => {
        if (!req.user || !roles.includes(req.user.role)) {
            return res.status(403).json({ error: 'Permission denied' });
        }
        next();
    };
}

app.get('/api/ping', (req, res) => {
    res.json({ message: 'pong' });
});

app.post('/api/login', (req, res) => {
    const { username, password } = req.body || {};
    if (!username || !password) {
        return res.status(400).json({ message: 'Username and password required' });
    }
    db.get("SELECT * FROM users WHERE username = ?", [username], (err, user) => {
        if (err) return res.status(500).json({ message: 'DB error' });
        if (!user) return res.status(401).json({ message: 'Invalid credentials' });
        if (!bcrypt.compareSync(password, user.password_hash)) {
            return res.status(401).json({ message: 'Invalid credentials' });
        }
        const token = jwt.sign(
            { user_id: user.id, username: user.username, role: user.role },
            SECRET_KEY,
            { expiresIn: '7d' }
        );
        res.json({ message: 'success', token, username: user.username, role: user.role });
    });
});

app.post('/login', (req, res) => {
    const { username, password } = req.body || {};
    if (!username || !password) {
        return res.status(400).json({ message: 'Username and password required' });
    }
    db.get("SELECT * FROM users WHERE username = ?", [username], (err, user) => {
        if (err) return res.status(500).json({ message: 'DB error' });
        if (!user) return res.status(401).json({ message: 'Invalid credentials' });
        if (!bcrypt.compareSync(password, user.password_hash)) {
            return res.status(401).json({ message: 'Invalid credentials' });
        }
        const token = jwt.sign(
            { user_id: user.id, username: user.username, role: user.role },
            SECRET_KEY,
            { expiresIn: '7d' }
        );
        res.json({ message: 'success', token, username: user.username, role: user.role });
    });
});

app.get('/api/products', (req, res) => {
    db.all("SELECT * FROM products ORDER BY name", [], (err, rows) => {
        if (err) {
            return res.status(400).json({ error: err.message });
        }
        res.json({ message: 'success', data: rows || [] });
    });
});

app.get('/api/pos/products', (req, res) => {
    db.all("SELECT * FROM products ORDER BY name", [], (err, rows) => {
        if (err) {
            return res.status(400).json({ error: err.message });
        }
        const data = (rows || []).map(p => ({
            ...p,
            low_stock: (p.low_stock_threshold != null && p.stock <= p.low_stock_threshold) ? 1 : 0
        }));
        res.json({ message: 'success', data });
    });
});

app.get('/api/products/barcode/:barcode', (req, res) => {
    const code = req.params.barcode;
    db.get("SELECT * FROM products WHERE barcode = ?", [code], (err, row) => {
        if (err) return res.status(400).json({ error: err.message });
        if (!row) return res.status(404).json({ error: 'Not found' });
        res.json({ message: 'success', data: row });
    });
});

app.get('/api/products/:id', (req, res) => {
    const id = parseInt(req.params.id, 10);
    if (isNaN(id)) return res.status(404).json({ error: 'Not found' });
    db.get("SELECT * FROM products WHERE id = ?", [id], (err, row) => {
        if (err) return res.status(400).json({ error: err.message });
        if (!row) return res.status(404).json({ error: 'Product not found' });
        res.json({ message: 'success', data: row });
    });
});

app.get('/api/products/low-stock', (req, res) => {
    db.all("SELECT * FROM products WHERE low_stock_threshold IS NOT NULL AND stock <= low_stock_threshold", [], (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', data: rows || [] });
    });
});

app.post('/api/products', authenticateToken, requireRole(['admin', 'assistant']), (req, res) => {
    const data = req.body || {};
    const name = (data.name || '').trim();
    let category = (data.category || '').trim();
    const price = data.price;
    const stock = data.stock;
    const barcode = (data.barcode || '').trim() || null;
    const low_stock_threshold = data.low_stock_threshold != null ? data.low_stock_threshold : null;
    const image_url = (data.image_url || '').trim() || null;
    const min_price = data.min_price != null ? data.min_price : null;

    if (!name) return res.status(400).json({ error: 'Name required' });
    let priceNum, stockNum, minNum = null;
    try {
        priceNum = parseFloat(price);
        stockNum = parseInt(stock, 10);
        if (isNaN(priceNum) || isNaN(stockNum)) throw new Error('invalid');
        if (min_price != null) {
            minNum = parseFloat(min_price);
            if (isNaN(minNum)) throw new Error('invalid min');
        }
    } catch (e) {
        return res.status(400).json({ error: 'Invalid price or stock' });
    }
    if (!category) category = 'General';

    const cols = 'name, category, price, stock, barcode, low_stock_threshold, image_url, min_price';
    const vals = [name, category, priceNum, stockNum, barcode, low_stock_threshold, image_url, minNum];
    db.run(
        `INSERT INTO products (${cols}) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
        vals,
        function (err) {
            if (err) {
                if (err.message && (err.message.includes('idx_products_barcode') || err.message.includes('UNIQUE'))) {
                    return res.status(400).json({ error: 'Barcode already exists' });
                }
                return res.status(400).json({ error: err.message });
            }
            const newId = this.lastID;
            let finalImageUrl = image_url;
            const trySaveLocalDataUrl = () => {
                if (finalImageUrl && typeof finalImageUrl === 'string' && finalImageUrl.startsWith('data:')) {
                    try {
                        const fs = require('fs');
                        const path = require('path');
                        const m = finalImageUrl.match(/^data:image\/(png|jpeg|jpg|gif|webp);base64,(.*)$/i);
                        if (m) {
                            const uploadDir = path.join(__dirname, 'uploads', 'products');
                            try { fs.mkdirSync(uploadDir, { recursive: true }); } catch (e) {}
                            const ext = (m[1].toLowerCase() === 'jpeg') ? 'jpg' : m[1].toLowerCase();
                            const fname = `product_${newId}_${Date.now()}.${ext}`;
                            const fpath = path.join(uploadDir, fname);
                            fs.writeFileSync(fpath, Buffer.from(m[2], 'base64'));
                            const local_url = `/uploads/products/${fname}`;
                            db.run("UPDATE products SET image_url = ? WHERE id = ?", [local_url, newId], (e2) => {
                                if (!e2) finalImageUrl = local_url;
                                res.json({ message: 'success', id: newId, image_url: finalImageUrl });
                            });
                            return;
                        }
                    } catch (e) {
                        console.log('Local data URL save failed:', e.message);
                    }
                }
                res.json({ message: 'success', id: newId, image_url: finalImageUrl });
            };
            trySaveLocalDataUrl();
        }
    );
});

app.put('/api/products/:id/stock', authenticateToken, requireRole(['admin', 'assistant']), (req, res) => {
    const id = req.params.id;
    const stock = parseInt(req.body && req.body.stock, 10);
    if (isNaN(stock)) return res.status(400).json({ error: 'Invalid stock' });
    db.run("UPDATE products SET stock = ? WHERE id = ?", [stock, id], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        if (this.changes === 0) return res.status(404).json({ error: 'Not found' });
        res.json({ message: 'success' });
    });
});

app.put('/api/products/:id/threshold', authenticateToken, requireRole(['admin', 'assistant']), (req, res) => {
    const id = req.params.id;
    const thr = req.body && req.body.low_stock_threshold != null ? parseInt(req.body.low_stock_threshold, 10) : null;
    db.run("UPDATE products SET low_stock_threshold = ? WHERE id = ?", [thr, id], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        if (this.changes === 0) return res.status(404).json({ error: 'Not found' });
        res.json({ message: 'success' });
    });
});

app.put('/api/products/:id/min_price', authenticateToken, requireRole(['admin', 'assistant']), (req, res) => {
    const id = req.params.id;
    const mp = req.body && req.body.min_price != null ? parseFloat(req.body.min_price) : null;
    db.run("UPDATE products SET min_price = ? WHERE id = ?", [mp, id], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        if (this.changes === 0) return res.status(404).json({ error: 'Not found' });
        res.json({ message: 'success' });
    });
});

app.post('/api/products/:id/image', authenticateToken, requireRole(['admin', 'assistant']), (req, res) => {
    const id = req.params.id;
    const image_url = (req.body && req.body.image_url) || null;
    db.run("UPDATE products SET image_url = ? WHERE id = ?", [image_url, id], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success' });
    });
});

app.delete('/api/products/:id/image', authenticateToken, requireRole(['admin', 'assistant']), (req, res) => {
    const id = req.params.id;
    db.run("UPDATE products SET image_url = NULL WHERE id = ?", [id], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success' });
    });
});

app.post('/api/products/:id/image/upload', authenticateToken, requireRole(['admin', 'assistant']), (req, res) => {
    const id = req.params.id;
    const file = (req.body && req.body.file) || (req.body && req.body.data_url);
    if (!file) return res.status(400).json({ error: 'No file' });
    let image_url = file;
    if (typeof file === 'string' && file.startsWith('data:')) {
        image_url = file;
    }
    db.run("UPDATE products SET image_url = ? WHERE id = ?", [image_url, id], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', image_url });
    });
});

app.post('/api/products/:product_id/variants', authenticateToken, requireRole(['admin', 'assistant', 'super_admin']), (req, res) => {
    const parent_id = parseInt(req.params.product_id, 10);
    const variants = (req.body && req.body.variants) || [];
    if (!Array.isArray(variants) || variants.length === 0) {
        return res.status(400).json({ error: 'variants list required' });
    }
    db.get("SELECT * FROM products WHERE id = ?", [parent_id], (err, parent) => {
        if (err) return res.status(400).json({ error: err.message });
        if (!parent) return res.status(404).json({ error: 'Parent product not found' });
        const created_ids = [];
        let remaining = variants.length;
        let errored = false;
        variants.forEach(v => {
            const color = (v.color || '').trim();
            const size = (v.size || '').trim();
            const suffix = [size, color].filter(Boolean).join(' ').trim();
            const child_name = parent.name + (suffix ? ` (${suffix})` : '');
            const price = v.price != null ? parseFloat(v.price) : parent.price;
            const stock = v.stock != null ? parseInt(v.stock, 10) : 0;
            const barcode = (v.barcode || '').trim() || null;
            const min_price = v.min_price != null ? parseFloat(v.min_price) : (parent.min_price || null);
            db.run(
                `INSERT INTO products (name, parent_id, price, stock, category, barcode, low_stock_threshold, image_url, min_price) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
                [child_name, parent.id, price, stock, parent.category, barcode, parent.low_stock_threshold, parent.image_url, min_price],
                function (err) {
                    if (err) {
                        errored = true;
                        if (!--remaining) {
                            if (err.message && (err.message.includes('idx_products_barcode') || err.message.includes('UNIQUE'))) {
                                return res.status(400).json({ error: 'Barcode already exists' });
                            }
                            return res.status(400).json({ error: err.message });
                        }
                        return;
                    }
                    created_ids.push(this.lastID);
                    if (!--remaining && !errored) {
                        res.json({ message: 'success', ids: created_ids });
                    }
                }
            );
        });
    });
});

app.get('/api/categories', (req, res) => {
    db.all("SELECT * FROM categories ORDER BY name", [], (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', data: rows || [] });
    });
});

app.post('/api/categories', authenticateToken, requireRole(['admin']), (req, res) => {
    const name = (req.body && req.body.name || '').trim();
    if (!name) return res.status(400).json({ error: 'Name required' });
    db.run("INSERT OR IGNORE INTO categories (name) VALUES (?)", [name], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', id: this.lastID });
    });
});

app.get('/api/sales/daily', (req, res) => {
    const sql = `
        SELECT
            strftime('%Y-%m-%d', date) as sale_date,
            COUNT(*) as total_sales,
            SUM(total) as total_revenue,
            SUM(subtotal) as subtotal_sum,
            SUM(vat) as vat_sum
        FROM sales
        WHERE status IS NULL OR status != 'cancelled'
        GROUP BY sale_date
        ORDER BY sale_date DESC
    `;
    db.all(sql, [], (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', data: rows || [] });
    });
});

app.get('/api/reports/daily', (req, res) => {
    const start = req.query.start || '';
    const end = req.query.end || '';
    let sql = `
        SELECT
            strftime('%Y-%m-%d', date) as sale_date,
            COUNT(*) as total_sales,
            SUM(total) as total_revenue,
            SUM(subtotal) as subtotal_sum,
            SUM(vat) as vat_sum,
            payment_method,
            cashier
        FROM sales
        WHERE (status IS NULL OR status != 'cancelled')
    `;
    const params = [];
    if (start) { sql += " AND date >= ?"; params.push(start + ' 00:00:00'); }
    if (end) { sql += " AND date <= ?"; params.push(end + ' 23:59:59'); }
    sql += " GROUP BY sale_date ORDER BY sale_date DESC";
    db.all(sql, params, (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', data: rows || [] });
    });
});

app.get('/api/sales/recent', (req, res) => {
    const limit = Math.min(parseInt(req.query.limit || '100', 10), 500);
    const q = (req.query.q || '').trim();
    let sql = "SELECT * FROM sales WHERE status IS NULL OR status != 'cancelled'";
    const params = [];
    if (q) {
        sql += " AND (payment_reference LIKE ? OR cashier LIKE ? OR CAST(id AS TEXT) LIKE ?)";
        const like = '%' + q + '%';
        params.push(like, like, like);
    }
    sql += " ORDER BY date DESC LIMIT ?";
    params.push(limit);
    db.all(sql, params, (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', data: rows || [] });
    });
});

app.get('/api/sales/:id', authenticateToken, (req, res) => {
    const id = req.params.id;
    db.get("SELECT * FROM sales WHERE id = ?", [id], (err, sale) => {
        if (err) return res.status(400).json({ error: err.message });
        if (!sale) return res.status(404).json({ error: 'Sale not found' });
        let items = [];
        try { items = sale.items ? JSON.parse(sale.items) : []; } catch {}
        db.all("SELECT * FROM sale_items WHERE sale_id = ?", [id], (err2, saleItems) => {
            if (!err2 && saleItems && saleItems.length) {
                items = saleItems.map(si => ({
                    productId: si.product_id,
                    quantity: si.quantity,
                    price: si.price,
                    name: ''
                }));
            }
            res.json({ message: 'success', sale, items });
        });
    });
});

app.post('/api/sales', authenticateToken, (req, res) => {
    const data = req.body || {};
    const rawItems = data.items || [];
    const payment_method = data.payment_method || 'cash';
    const payment_reference = data.payment_reference || '';
    const cashier = (req.user && req.user.username) || '';

    const allowed_methods = {'cash':1, 'mpesa':1, 'bank':1, 'card':1, 'cheque':1, 'credit':1, 'jumia':1};
    if (!allowed_methods[payment_method]) {
        return res.status(400).json({ error: 'Invalid payment method' });
    }
    if (!Array.isArray(rawItems) || rawItems.length === 0) {
        return res.status(400).json({ error: 'No items in sale' });
    }
    const items = [];
    for (let i = 0; i < rawItems.length; i++) {
        const it = rawItems[i];
        const raw = (it.productId != null) ? it.productId : it.id;
        const product_id = parseInt(raw, 10);
        const quantity = parseInt((it.quantity != null ? it.quantity : 0), 10);
        const price = parseFloat((it.price != null ? it.price : 0));
        if (isNaN(product_id)) {
            return res.status(400).json({ error: `Invalid item ${i+1}: bad product id` });
        }
        if (isNaN(quantity) || quantity <= 0) {
            return res.status(400).json({ error: `Invalid quantity for item ${i+1} (product ${product_id})` });
        }
        if (isNaN(price)) {
            return res.status(400).json({ error: `Invalid price for item ${i+1} (product ${product_id})` });
        }
        items.push({ product_id, quantity, price });
    }

    const subtotal = items.reduce((s, it) => s + (it.price * it.quantity), 0);
    const vat = 0;
    const total = subtotal + vat;

    db.serialize(() => {
        db.run("BEGIN TRANSACTION");
        const checkProductSql = "SELECT id, name, stock, min_price FROM products WHERE id = ?";
        const checksRemaining = [items.length];
        let firstError = null;
        const productsInfo = new Map();
        let checkIdx = 0;
        const doCheck = () => {
            if (checkIdx >= items.length) {
                finishAfterChecks();
                return;
            }
            const it = items[checkIdx++];
            db.get(checkProductSql, [it.product_id], (err, row) => {
                if (err) { if (!firstError) firstError = err.message; }
                else if (!row) {
                    if (!firstError) firstError = `Product not found (id=${it.product_id}). Try refreshing the page and re-adding the item.`;
                } else {
                    productsInfo.set(row.id, row);
                    if (row.min_price != null && it.price < parseFloat(row.min_price)) {
                        if (!firstError) firstError = `Price below minimum allowed for ${row.name}`;
                    } else if (row.stock < it.quantity) {
                        if (!firstError) firstError = `Insufficient stock for ${row.name} (have ${row.stock}, need ${it.quantity})`;
                    }
                }
                doCheck();
            });
        };
        const finishAfterChecks = () => {
            if (firstError) {
                db.run("ROLLBACK", () => res.status(400).json({ error: firstError }));
                return;
            }
            db.run(
                "INSERT INTO sales (total, subtotal, vat, items, cashier, payment_method, payment_reference) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [total, subtotal, vat, JSON.stringify(items.map(i => ({ productId: i.product_id, quantity: i.quantity, price: i.price }))), cashier, payment_method, payment_reference],
                function (err) {
                    if (err) {
                        db.run("ROLLBACK");
                        return res.status(400).json({ error: err.message });
                    }
                    const saleId = this.lastID;
                    const stmt = db.prepare("INSERT INTO sale_items (sale_id, product_id, quantity, price) VALUES (?, ?, ?, ?)");
                    const updateStmt = db.prepare("UPDATE products SET stock = stock - ? WHERE id = ?");
                    let errorOccurred = false;
                    items.forEach(it => {
                        if (errorOccurred) return;
                        try {
                            stmt.run([saleId, it.product_id, it.quantity, it.price]);
                            updateStmt.run([it.quantity, it.product_id]);
                        } catch (e) { errorOccurred = true; firstError = (firstError || 'Failed to update stock/items'); }
                    });
                    stmt.finalize();
                    updateStmt.finalize();
                    if (errorOccurred) {
                        db.run("ROLLBACK");
                        return res.status(400).json({ error: firstError });
                    }
                    db.run("COMMIT", (err2) => {
                        if (err2) {
                            db.run("ROLLBACK");
                            return res.status(400).json({ error: err2.message });
                        }
                        res.json({ message: 'success', saleId });
                    });
                }
            );
        };
        doCheck();
    });
});

app.get('/api/branding/logo', (req, res) => {
    res.json({ image_url: '' });
});

app.get('/api/users', authenticateToken, requireRole(['admin']), (req, res) => {
    const role = req.query.role;
    const sql = role ? "SELECT id, username, role FROM users WHERE role = ? ORDER BY id" : "SELECT id, username, role FROM users ORDER BY id";
    db.all(sql, role ? [role] : [], (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', data: rows || [] });
    });
});

app.post('/api/users', authenticateToken, requireRole(['admin']), (req, res) => {
    const data = req.body || {};
    const username = (data.username || '').trim();
    const password = (data.password || '').trim();
    const role = data.role === 'super_admin' && (req.user.role === 'admin' || req.user.role === 'super_admin')
        ? 'super_admin'
        : (['cashier', 'admin', 'assistant'].includes(data.role) ? data.role : 'cashier');
    if (!username || !password) return res.status(400).json({ error: 'username and password required' });
    const hash = bcrypt.hashSync(password, 10);
    db.run("INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)", [username, hash, role], function (err) {
        if (err) {
            if (err.message && err.message.includes('UNIQUE')) {
                return res.status(400).json({ error: 'username already exists' });
            }
            return res.status(400).json({ error: err.message });
        }
        res.json({ message: 'success', id: this.lastID });
    });
});

app.post('/api/me/password', authenticateToken, (req, res) => {
    const data = req.body || {};
    const old_password = data.old_password || '';
    const new_password = (data.new_password || '').trim();
    if (!old_password || !new_password) return res.status(400).json({ error: 'Both fields required' });
    const uid = req.user.user_id;
    db.get("SELECT * FROM users WHERE id = ?", [uid], (err, user) => {
        if (err || !user) return res.status(400).json({ error: 'User not found' });
        if (!bcrypt.compareSync(old_password, user.password_hash)) {
            return res.status(400).json({ error: 'Old password incorrect' });
        }
        const hash = bcrypt.hashSync(new_password, 10);
        db.run("UPDATE users SET password_hash = ? WHERE id = ?", [hash, uid], (err2) => {
            if (err2) return res.status(400).json({ error: err2.message });
            res.json({ message: 'success' });
        });
    });
});

app.get('/api/banks', authenticateToken, (req, res) => {
    db.all("SELECT * FROM banks ORDER BY name", [], (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', data: rows || [] });
    });
});

app.post('/api/banks', authenticateToken, requireRole(['admin']), (req, res) => {
    const name = (req.body && req.body.name || '').trim();
    if (!name) return res.status(400).json({ error: 'Name required' });
    db.run("INSERT OR IGNORE INTO banks (name) VALUES (?)", [name], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', id: this.lastID });
    });
});

app.get('/api/holds', authenticateToken, (req, res) => {
    db.all("SELECT * FROM holds ORDER BY created_at DESC", [], (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        const data = (rows || []).map(h => {
            let items = [];
            try { items = h.items ? JSON.parse(h.items) : []; } catch {}
            return { ...h, items };
        });
        res.json({ message: 'success', data });
    });
});

app.post('/api/holds', authenticateToken, (req, res) => {
    const data = req.body || {};
    const items = JSON.stringify(data.items || []);
    const cashier = (req.user && req.user.username) || '';
    db.run("INSERT INTO holds (items, cashier) VALUES (?, ?)", [items, cashier], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success', id: this.lastID });
    });
});

app.delete('/api/holds/:id', authenticateToken, (req, res) => {
    const id = req.params.id;
    db.run("DELETE FROM holds WHERE id = ?", [id], function (err) {
        if (err) return res.status(400).json({ error: err.message });
        res.json({ message: 'success' });
    });
});

app.get('/api/export/products.csv', authenticateToken, (req, res) => {
    db.all("SELECT * FROM products ORDER BY id", [], (err, rows) => {
        if (err) return res.status(400).json({ error: err.message });
        const headers = ['id', 'name', 'category', 'price', 'stock', 'barcode', 'min_price', 'low_stock_threshold'];
        const lines = [headers.join(',')];
        (rows || []).forEach(r => {
            const row = headers.map(h => {
                const v = r[h] == null ? '' : String(r[h]).replace(/"/g, '""');
                return `"${v}"`;
            });
            lines.push(row.join(','));
        });
        res.setHeader('Content-Type', 'text/csv');
        res.setHeader('Content-Disposition', 'attachment; filename="products.csv"');
        res.send(lines.join('\n'));
    });
});

app.get('/api/db/sync', authenticateToken, requireRole(['super_admin', 'admin']), (req, res) => {
    res.json({ message: 'success', data: { mode: 'local_node', note: 'Local Node server uses persistent SQLite file directly; no sync needed' } });
});

app.get('/api/db/info', authenticateToken, requireRole(['admin']), (req, res) => {
    res.json({ message: 'success', data: { is_serverless: false, mode: 'local_node', db_path: './pos.db' } });
});

app.listen(port, () => {
    console.log(`Server running on http://localhost:${port}`);
    console.log('Users: super_admin/super123, admin/admin123, cashier/cashier123');
});
