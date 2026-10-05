"""SQLite persistence for The Last Chapter."""
import os
import secrets
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo
from werkzeug.security import generate_password_hash

IST = ZoneInfo("Asia/Kolkata")
DB_PATH = None
SCHEMA = """
CREATE TABLE IF NOT EXISTS organizer (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS booking (id INTEGER PRIMARY KEY AUTOINCREMENT, pass_code TEXT UNIQUE NOT NULL, pass_type TEXT NOT NULL CHECK (pass_type IN ('single','couple')), person_1_name TEXT NOT NULL, person_1_dob TEXT NOT NULL, person_2_name TEXT, person_2_dob TEXT, contact TEXT NOT NULL, payment_method TEXT NOT NULL CHECK (payment_method IN ('cash','upi')), payment_status TEXT NOT NULL DEFAULT 'pending' CHECK (payment_status IN ('pending','submitted','paid')), amount INTEGER NOT NULL, created_at TEXT NOT NULL, entry_status TEXT NOT NULL DEFAULT 'unused' CHECK (entry_status IN ('unused','used')), entry_time TEXT, verified_by TEXT);
CREATE INDEX IF NOT EXISTS idx_booking_code ON booking(pass_code);
CREATE INDEX IF NOT EXISTS idx_booking_contact ON booking(contact);
CREATE TABLE IF NOT EXISTS pass_code_inventory (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE NOT NULL CHECK(length(code)=4 AND code GLOB '[0-9][0-9][0-9][0-9]'), status TEXT NOT NULL DEFAULT 'available' CHECK(status IN ('available','assigned','used')), booking_id INTEGER UNIQUE REFERENCES booking(id), created_at TEXT NOT NULL, assigned_at TEXT, used_at TEXT);
CREATE INDEX IF NOT EXISTS idx_pass_inventory_status ON pass_code_inventory(status);
CREATE TABLE IF NOT EXISTS activity_log (id INTEGER PRIMARY KEY AUTOINCREMENT, booking_id INTEGER REFERENCES booking(id) ON DELETE CASCADE, action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_activity_created ON activity_log(created_at);
CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
DEFAULT_SETTINGS = {"price_single":"499", "price_couple":"899", "upi_id_single":"yourupi@okaxis", "upi_id_couple":"yourupi2@okaxis", "payee_name":"THE LAST CHAPTER"}
ORGANIZER_USERNAMES = ("neeraj chouhan", "aryan chouhan", "mohit yadav")
ORGANIZER_PASSWORD_ENV = {"neeraj chouhan":"ORGANIZER_NEERAJ_PASSWORD", "aryan chouhan":"ORGANIZER_ARYAN_PASSWORD", "mohit yadav":"ORGANIZER_MOHIT_PASSWORD"}

def now_iso(): return datetime.now(IST).isoformat(timespec="seconds")
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None); conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON"); conn.execute("PRAGMA journal_mode=WAL"); conn.execute("PRAGMA busy_timeout=15000")
    return conn
def init(instance_dir):
    global DB_PATH
    os.makedirs(instance_dir, exist_ok=True); DB_PATH = os.path.join(instance_dir, "last_chapter.db")
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items(): conn.execute("INSERT OR IGNORE INTO setting(key,value) VALUES (?,?)", (key,value))
        conn.execute("""INSERT OR IGNORE INTO pass_code_inventory(code,status,booking_id,created_at,assigned_at,used_at) SELECT pass_code,CASE WHEN entry_status='used' THEN 'used' ELSE 'assigned' END,id,created_at,created_at,CASE WHEN entry_status='used' THEN entry_time END FROM booking""")
        seed_organizers(conn)
    finally: conn.close()
def seed_organizers(conn):
    for username in ORGANIZER_USERNAMES:
        if conn.execute("SELECT 1 FROM organizer WHERE username=?", (username,)).fetchone(): continue
        password = os.environ.get(ORGANIZER_PASSWORD_ENV[username])
        if password: conn.execute("INSERT INTO organizer(username,password_hash,created_at) VALUES (?,?,?)", (username,generate_password_hash(password),now_iso()))
def load_or_create_secret(instance_dir):
    if os.environ.get("SECRET_KEY"): return os.environ["SECRET_KEY"]
    os.makedirs(instance_dir, exist_ok=True); path = os.path.join(instance_dir,"secret_key")
    if os.path.exists(path):
        with open(path,encoding="utf-8") as f:
            value = f.read().strip()
            if value: return value
    value=secrets.token_hex(32)
    with open(path,"w",encoding="utf-8") as f: f.write(value)
    return value
def get_setting(conn,key,default=None):
    row=conn.execute("SELECT value FROM setting WHERE key=?",(key,)).fetchone(); return row["value"] if row else default
def get_settings(conn):
    values=dict(DEFAULT_SETTINGS); values.update({r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM setting")}); return values
def set_settings(conn,updates):
    for key,value in updates.items(): conn.execute("INSERT INTO setting(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
def add_inventory_codes(conn,codes):
    inserted,rejected,now=0,[],now_iso(); conn.execute("BEGIN IMMEDIATE")
    try:
        for code in codes:
            try: conn.execute("INSERT INTO pass_code_inventory(code,status,created_at) VALUES (?,'available',?)",(code,now)); inserted+=1
            except sqlite3.IntegrityError: rejected.append(code)
        conn.commit()
    except Exception: conn.rollback(); raise
    return inserted,rejected
def inventory_stats(conn): return dict(conn.execute("SELECT COUNT(*) total,COALESCE(SUM(status='available'),0) available,COALESCE(SUM(status='assigned'),0) assigned,COALESCE(SUM(status='used'),0) used FROM pass_code_inventory").fetchone())
def list_inventory(conn,limit=500): return conn.execute("SELECT * FROM pass_code_inventory ORDER BY code LIMIT ?",(limit,)).fetchall()
def log(conn,booking_id,action,detail): conn.execute("INSERT INTO activity_log(booking_id,action,detail,created_at) VALUES (?,?,?,?)",(booking_id,action,detail,now_iso()))
def create_booking_with_code(conn,v):
    conn.execute("BEGIN IMMEDIATE")
    try:
        card=conn.execute("SELECT id,code FROM pass_code_inventory WHERE status='available' ORDER BY code LIMIT 1").fetchone()
        if card is None: conn.rollback(); return None
        now=now_iso(); cur=conn.execute("""INSERT INTO booking(pass_code,pass_type,person_1_name,person_1_dob,person_2_name,person_2_dob,contact,payment_method,payment_status,amount,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",(card['code'],v['pass_type'],v['p1_name'],v['p1_dob'],v['p2_name'],v['p2_dob'],v['contact'],v['payment'],'pending',v['amount'],now)); bid=cur.lastrowid
        if conn.execute("UPDATE pass_code_inventory SET status='assigned',booking_id=?,assigned_at=? WHERE id=? AND status='available'",(bid,now,card['id'])).rowcount != 1: raise RuntimeError("pass-code reservation conflict")
        log(conn,bid,'booking',f"new {v['pass_type']} booking — {v['p1_name']}"); conn.commit(); return bid
    except Exception: conn.rollback(); raise
def mark_code_used(conn,booking_id,used_at): return conn.execute("UPDATE pass_code_inventory SET status='used',used_at=? WHERE booking_id=? AND status='assigned'",(used_at,booking_id)).rowcount
def stats(conn):
    d=dict(conn.execute("""SELECT COUNT(*) total,COALESCE(SUM(pass_type='single'),0) single,COALESCE(SUM(pass_type='couple'),0) couple,COALESCE(SUM(payment_method='upi'),0) upi,COALESCE(SUM(payment_method='cash'),0) cash,COALESCE(SUM(payment_status='paid'),0) paid,COALESCE(SUM(payment_status!='paid'),0) pending,COALESCE(SUM(entry_status='used'),0) used,COALESCE(SUM(entry_status='unused'),0) unused FROM booking""").fetchone()); d['guests']=d['single']+d['couple']*2; d['entered']=d['used']; return d
FILTER_SQL={'single':"pass_type='single'",'couple':"pass_type='couple'",'cash':"payment_method='cash'",'upi':"payment_method='upi'",'paid':"payment_status='paid'",'pending':"payment_status IN ('pending','submitted')",'used':"entry_status='used'",'unused':"entry_status='unused'"}
def list_bookings(conn,q='',f=''):
    sql,args,where='SELECT * FROM booking',[],[]
    if q.strip():
        like='%'+q.strip()+'%'; where.append("(person_1_name LIKE ? OR person_2_name LIKE ? OR contact LIKE ? OR pass_code LIKE ?)"); args += [like]*4
    if f in FILTER_SQL: where.append(FILTER_SQL[f])
    if where: sql+=' WHERE '+' AND '.join(where)
    return conn.execute(sql+' ORDER BY id DESC LIMIT 500',args).fetchall()
def recent_activity(conn,limit=120): return conn.execute('SELECT * FROM activity_log ORDER BY id DESC LIMIT ?',(limit,)).fetchall()
