import sqlite3

conn = sqlite3.connect("finance.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    type TEXT,
    amount REAL,
    category TEXT,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# -------------------------
# GROUP / TRIP EXPENSE SPLITTING
# -------------------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    owner_id INTEGER,
    join_code TEXT UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS group_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    user_id INTEGER,
    display_name TEXT,
    username TEXT,
    role TEXT DEFAULT 'member',      -- 'owner' | 'member'
    status TEXT DEFAULT 'pending',   -- 'pending' | 'active' | 'rejected'
    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(group_id, user_id)
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS group_expenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    payer_id INTEGER,
    amount REAL,
    description TEXT,
    category TEXT,
    split_type TEXT,                 -- 'equal' | 'selected' | 'percentage'
    created_by INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS expense_shares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    expense_id INTEGER,
    user_id INTEGER,
    share REAL
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS expense_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER,
    expense_id INTEGER,
    actor_id INTEGER,
    action TEXT,                     -- 'create' | 'edit' | 'delete'
    reason TEXT,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

# -------------------------
# LOANS: lent / borrow lifecycle (who, due date, repayments)
# -------------------------
cursor.execute("""
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    direction TEXT,                  -- 'lent' | 'borrow'
    person TEXT,                     -- who the money is with
    amount REAL,                     -- original principal
    category TEXT,
    note TEXT,
    due_date TEXT,                   -- 'YYYY-MM-DD'
    status TEXT DEFAULT 'open',      -- 'open' | 'settled'
    reminded_on TEXT,                -- last 'YYYY-MM-DD' a reminder was sent
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS loan_payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loan_id INTEGER,
    amount REAL,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

conn.commit()