import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import datetime
import io
import math
import hashlib
import secrets as pysecrets
from datetime import date
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from sqlalchemy import create_engine, text, inspect

# ═══════════════════════════════════════════════
# DATABASE SYSTEM — CLOUD (PostgreSQL / MySQL via SQLAlchemy)
# ═══════════════════════════════════════════════
# Set your connection string in .streamlit/secrets.toml as:
#   DB_URL = "postgresql+psycopg2://user:password@host:5432/dbname"
#   -- or --
#   DB_URL = "mysql+pymysql://user:password@host:3306/dbname"
# (For local dry-testing only, "sqlite:///textile_inventory.db" also works.)
#
# IMPORTANT: every existing call-site in this file was written against
# sqlite3's "?" positional placeholders (q(sql, [params]), conn.execute(sql,
# [params]), etc). Rather than rewrite every one of those call-sites for
# Postgres/MySQL's different placeholder styles, get_conn()/q()/scalar()
# below transparently convert "?" -> SQLAlchemy named binds, so ALL of the
# app's business logic below this section is completely unchanged.
@st.cache_resource(show_spinner=False)
def _get_engine():
    db_url = st.secrets.get("DB_URL")
    if not db_url:
        st.error("⚠️ DB_URL is not configured in `.streamlit/secrets.toml`. "
                 "Add e.g. DB_URL = \"postgresql+psycopg2://user:pass@host:5432/dbname\" and restart.")
        st.stop()
    return create_engine(db_url, pool_pre_ping=True)

def _qmark_to_named(sql, params):
    """Converts sqlite-style '?' placeholders + a positional params list into
    SQLAlchemy named-bind SQL + a params dict, so old call-sites work as-is."""
    out, pdict, i = [], {}, 0
    for ch in sql:
        if ch == "?":
            pname = f"p{i}"
            out.append(f":{pname}")
            pdict[pname] = params[i]
            i += 1
        else:
            out.append(ch)
    return "".join(out), pdict

class _CompatConn:
    """Thin wrapper so `conn = get_conn(); conn.execute(sql, [params]); 
    conn.commit(); conn.close()` (written for sqlite3) keeps working unchanged
    against a SQLAlchemy engine/connection for Postgres or MySQL."""
    def __init__(self, sa_conn):
        self._conn = sa_conn
    def execute(self, sql, params=None):
        named_sql, pdict = _qmark_to_named(sql, params or [])
        return self._conn.execute(text(named_sql), pdict)
    def commit(self):
        self._conn.commit()
    def close(self):
        self._conn.close()

@st.cache_resource(show_spinner=False)
def _init_schema():
    engine = _get_engine()
    dialect = engine.dialect.name  # 'postgresql', 'mysql', or 'sqlite' (local dry-test only)
    if dialect == "postgresql":
        pk = "SERIAL PRIMARY KEY"
    elif dialect == "mysql":
        pk = "INT AUTO_INCREMENT PRIMARY KEY"
    else:
        pk = "INTEGER PRIMARY KEY AUTOINCREMENT"

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS sheet_orders (
                id {pk},
                call_off_no TEXT, po_no TEXT, sale_contract TEXT,
                brand TEXT, article TEXT, category TEXT, order_qty REAL)"""))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS inventory (
                id {pk},
                call_off_no TEXT, contract_no TEXT, dc_no TEXT, po_no TEXT,
                article TEXT, category TEXT, qty REAL,
                entry_date TEXT, remark TEXT, company_token TEXT DEFAULT '',
                style_type TEXT DEFAULT '—')"""))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS bilty (
                id {pk},
                call_off_no TEXT, contract_no TEXT, article TEXT, category TEXT,
                qty REAL, cartons INTEGER, transport_mode TEXT,
                bilty_date TEXT, created_at TEXT)"""))
        # NEW: multi-user login + role-based access control
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS app_users (
                id {pk},
                username TEXT UNIQUE, password_hash TEXT, role TEXT,
                full_name TEXT, created_at TEXT)"""))

        insp = inspect(engine)
        inv_cols = [c["name"] for c in insp.get_columns("inventory")]
        for col in ["entry_date", "remark", "company_token", "contract_no", "style_type"]:
            if col not in inv_cols:
                conn.execute(text(f"ALTER TABLE inventory ADD COLUMN {col} TEXT DEFAULT ''"))

        # Indexes. MySQL has no "CREATE INDEX IF NOT EXISTS", so each is
        # wrapped individually — a "duplicate index" error is caught and
        # ignored on any dialect.
        index_stmts = [
            "CREATE INDEX idx_inv_article_category ON inventory(article, category)",
            "CREATE INDEX idx_inv_calloff           ON inventory(call_off_no)",
            "CREATE INDEX idx_inv_contract          ON inventory(contract_no)",
            "CREATE INDEX idx_inv_po                ON inventory(po_no)",
            "CREATE INDEX idx_inv_dc                ON inventory(dc_no)",
            "CREATE INDEX idx_inv_token             ON inventory(company_token)",
            "CREATE INDEX idx_inv_entrydate         ON inventory(entry_date)",
            "CREATE INDEX idx_so_calloff            ON sheet_orders(call_off_no)",
            "CREATE INDEX idx_so_article_category   ON sheet_orders(article, category)",
            "CREATE INDEX idx_so_contract           ON sheet_orders(sale_contract)",
            "CREATE INDEX idx_so_po                 ON sheet_orders(po_no)",
            "CREATE INDEX idx_bilty_calloff_art     ON bilty(call_off_no, article, category)",
            "CREATE INDEX idx_bilty_contract        ON bilty(contract_no)",
        ]
        existing_idx = {ix["name"] for t in ["inventory", "sheet_orders", "bilty"] for ix in insp.get_indexes(t)}
        for stmt in index_stmts:
            idx_name = stmt.split()[2]
            if idx_name in existing_idx:
                continue
            try:
                conn.execute(text(stmt))
            except Exception:
                pass  # already exists on this dialect, or created concurrently

        # Seed one default admin account if app_users is empty, so the very
        # first login always works: username "admin" / password "admin123".
        # ⚠️ Change this password immediately after first login (see the
        # "👤 User Management" tab, Admin-only).
        user_count = conn.execute(text("SELECT COUNT(*) FROM app_users")).scalar()
        if not user_count:
            conn.execute(
                text("INSERT INTO app_users (username,password_hash,role,full_name,created_at) "
                     "VALUES (:u,:p,:r,:f,:c)"),
                {"u": "admin", "p": _hash_password("admin123"), "r": "Admin",
                 "f": "Default Admin", "c": str(datetime.datetime.now())})
    return True

def get_conn():
    _init_schema()  # cached no-op after the first call this session
    engine = _get_engine()
    return _CompatConn(engine.connect())

def _hash_password(password, salt=None):
    salt = salt or pysecrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}${h}"

def _verify_password(password, stored_hash):
    try:
        salt, h = stored_hash.split("$", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False


# ─────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────
ITEM_TYPES = [
    "Inlay Card / Bandrolle", 
    "Tag Card / Barcode Sticker", 
    "Barcode Item", 
    "Safety", 
    "Washing Paper", 
    "Transparent Sticker",
    "Eco Friendly"
]

STYLES_INLAY = ["Normal", "Topper", "Split"]

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def q(sql, params=None):
    named_sql, pdict = _qmark_to_named(sql, params or [])
    engine = _get_engine()
    with engine.connect() as conn:
        df = pd.read_sql(text(named_sql), conn, params=pdict)
    return df

def scalar(sql, params=None):
    conn = get_conn()
    r = conn.execute(sql, params or []).fetchone()
    conn.close()
    return (r[0] or 0) if r else 0

def get_calloff_list():
    return q("SELECT DISTINCT call_off_no FROM sheet_orders WHERE TRIM(call_off_no)!='' ORDER BY call_off_no")["call_off_no"].tolist()

def get_ordered_qty(article, category, coff=None, po=None):
    params, extra = [article, category], ""
    if coff: extra += " AND call_off_no=?"; params.append(coff)
    if po:   extra += " AND po_no=?";       params.append(po)
    return scalar(f"SELECT SUM(order_qty) FROM sheet_orders WHERE article=? AND category=?{extra}", params)

def get_received_qty(article, category, coff=None, po=None, exclude_id=None):
    params, extra = [article, category], ""
    if coff:       extra += " AND call_off_no=?"; params.append(coff)
    if po:         extra += " AND po_no=?";       params.append(po)
    if exclude_id: extra += " AND id!=?";         params.append(exclude_id)
    return scalar(f"SELECT SUM(qty) FROM inventory WHERE article=? AND category=?{extra}", params)

def get_bilty_qty(call_off_no, article, category=None):
    """Total quantity already dispatched (Bilty) from the Lahore factory for
    this Call-Off + Article (optionally scoped to one category)."""
    params, extra = [call_off_no, article], ""
    if category:
        extra = " AND category=?"; params.append(category)
    return scalar(f"SELECT SUM(qty) FROM bilty WHERE call_off_no=? AND article=?{extra}", params)

def round_and_format(val):
    try:
        if pd.isna(val) or val == "" or val == "—":
            return "0"
        if isinstance(val, str):
            val = float(val.replace(",", "").strip())
        rounded_val = int(math.floor(val + 0.5))
        return f"{rounded_val:,}"
    except:
        return "0"

def round_bal(val):
    """Strict zero-balancing helper: rounds away Python float micro-garbage
    (e.g. 0.00001 / -0.00001) so true-zero rows are correctly detected."""
    try:
        if pd.isna(val) or val == "" or val == "—":
            return 0
        if isinstance(val, str):
            val = float(val.replace(",", "").strip())
        return int(math.floor(float(val) + 0.5))
    except:
        return 0

# ─────────────────────────────────────────────
# PDF GENERATION
# ─────────────────────────────────────────────
def generate_ledger_pdf(df_summary, df_articles, sel_coff, sel_cont, sel_art, report_type="MASTER"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    story = []
    
    styles = getSampleStyleSheet()
    if report_type == "MASTER":
        title_color = '#1e40af'
        title_text = "MASTER LEDGER STATUS REPORT"
    elif report_type == "CONTRACT_SHORTLIST":
        title_color = '#7c3aed'
        title_text = "CONTRACT-WISE SHORTLIST REPORT"
    else:
        title_color = '#b91c1c'
        title_text = "PENDING SHORTAGE REPORT"
    
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=15, leading=19, textColor=colors.HexColor(title_color), alignment=1)
    cust_style = ParagraphStyle('Cust', parent=styles['Normal'], fontSize=11, leading=15, textColor=colors.HexColor('#0f172a'), alignment=1)
    sub_style = ParagraphStyle('Sub', parent=styles['Normal'], fontSize=9, leading=13, alignment=1)
    h2_style = ParagraphStyle('H2', parent=styles['Heading2'], fontSize=11, leading=15, textColor=colors.HexColor('#0f172a'), spaceBefore=8, spaceAfter=4)
    
    story.append(Paragraph(f"<b>NABA PACKAGING — {title_text}</b>", title_style))
    story.append(Spacer(1, 2))
    story.append(Paragraph("<b>Customer Name: Vertex Shahzad Bhai Lahore</b>", cust_style))
    story.append(Spacer(1, 4))
    
    filter_info = f"Call-Off: <b>{sel_coff}</b> | Contract: <b>{sel_cont}</b> | Article: <b>{sel_art}</b>"
    story.append(Paragraph(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M %p')} | {filter_info}", sub_style))
    story.append(Spacer(1, 12))
    
    story.append(Paragraph("<b>📊 SECTION 1: ITEM-WISE REMAINING SUMMARY</b>", h2_style))
    sum_data = [["Item Type / Description", "Remaining Balance (Pcs)"]]
    for k, v in df_summary.items():
        sum_data.append([k, round_and_format(v)])
        
    t_sum = Table(sum_data, colWidths=[280, 220])
    t_sum.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (1,0), colors.HexColor(title_color)),
        ('TEXTCOLOR', (0,0), (1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('ALIGN', (1,1), (1,-1), 'RIGHT'),
    ]))
    story.append(t_sum)
    story.append(Spacer(1, 12))
    
    sec2_title = "<b>🔍 SECTION 2: PENDING ARTICLES BREAKDOWN (SHORTAGE LIST)</b>" if report_type in ("SHORTAGE", "CONTRACT_SHORTLIST") else "<b>🔍 SECTION 2: ARTICLES COMPLETE BREAKDOWN</b>"
    story.append(Paragraph(sec2_title, h2_style))

    # Remarks column: only for Shortage / Contract Shortlist reports, showing
    # the manual remarks typed by the operator during DC entry for that
    # exact Call-Off + Article + Item Type combination.
    show_remarks = report_type in ("SHORTAGE", "CONTRACT_SHORTLIST")
    remarks_map = {}
    if show_remarks:
        df_remarks_raw = q("SELECT call_off_no, article, category, remark FROM inventory WHERE TRIM(remark)!=''")
        if not df_remarks_raw.empty:
            for (co, art, cat), grp in df_remarks_raw.groupby(["call_off_no", "article", "category"]):
                uniq = list(dict.fromkeys([str(x).strip() for x in grp["remark"] if str(x).strip()]))
                remarks_map[(co, art, cat)] = "; ".join(uniq)
    remark_cell_style = ParagraphStyle('RemarkCell', parent=styles['Normal'], fontSize=7, leading=9, textColor=colors.HexColor('#334155'))

    if show_remarks:
        art_data = [["Call-Off", "Contract #", "Art. No", "Item Type", "Ordered", "Received", "Remaining", "Remarks"]]
    else:
        art_data = [["Call-Off", "Contract #", "Art. No", "Item Type", "Ordered", "Received", "Remaining"]]

    for _, r in df_articles.iterrows():
        row = [
            str(r["Call-Off No"]), str(r["Contract #"]), str(r["Article"]), str(r["Item Type"]),
            round_and_format(r['Total Ordered']), round_and_format(r['Total Received']), round_and_format(r['Remaining Balance'])
        ]
        if show_remarks:
            rmk_text = remarks_map.get((r["Call-Off No"], r["Article"], r["Item Type"]), "-")
            row.append(Paragraph(rmk_text, remark_cell_style))
        art_data.append(row)

    if len(art_data) == 1:
        no_data_msg = "No pending / active items found for this Brand & Contract selection." if report_type == "CONTRACT_SHORTLIST" else "No pending / shortage items found for current filter selection."
        ncols = 8 if show_remarks else 7
        art_data.append([no_data_msg] + ["-"] * (ncols - 1))

    col_widths = [50, 60, 60, 90, 50, 50, 50, 102] if show_remarks else [55, 65, 65, 125, 65, 65, 65]
    t_art = Table(art_data, colWidths=col_widths)
    t_art.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f1f5f9')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.black),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('FONTSIZE', (0,0), (-1,-1), 8),
        ('ALIGN', (4,1), (6,-1), 'RIGHT'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
    ]))
    story.append(t_art)
    story.append(Spacer(1, 25))
    
    sig_data = [["-------------------------\nReport Checked By\n(NABA Packaging Team)", "-------------------------\nAuthorized Signature\n(Kaleem Ullah Sharif)"]]
    t_sig = Table(sig_data, colWidths=[250, 250])
    t_sig.setStyle(TableStyle([('ALIGN', (0,0), (-1,-1), 'CENTER'), ('FONTSIZE', (0,0), (-1,-1), 9)]))
    story.append(t_sig)
    
    doc.build(story)
    buffer.seek(0)
    return buffer

# ─────────────────────────────────────────────
# PAGE CONFIG & CSS
# ─────────────────────────────────────────────
st.set_page_config(page_title="NABA Inventory | Vertex", layout="wide", page_icon="📦")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html,body,[class*="css"]{font-family:'Inter',sans-serif;}
.stApp{background-color:#1a202c !important;color:#e2e8f0 !important;}
.hdr{background:linear-gradient(135deg,#0f172a,#1e293b);padding:20px 30px;border-radius:12px;
     margin-bottom:20px;border-left:6px solid #38bdf8;box-shadow:0 4px 6px rgba(0,0,0,.2);}
.hdr h1{color:#f8fafc;font-size:1.6rem;font-weight:700;margin:0;}
.hdr .cl{color:#f59e0b;font-size:.88rem;font-weight:600;margin:4px 0 0;}
.hdr .sb{color:#38bdf8;font-size:.78rem;margin:2px 0 0;letter-spacing:.05em;font-weight:500;}
.sec{color:#38bdf8;font-weight:700;font-size:1rem;border-bottom:2px solid #1e293b;
     padding-bottom:6px;margin-bottom:16px;margin-top:10px;}
.kpi-row{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0;}
.kpi{padding:10px 16px;border-radius:8px;font-size:.85rem;font-weight:600;
     box-shadow:0 1px 3px rgba(0,0,0,.1);color:#ffffff !important;}
.kb{background:#1e3a8a;border-left:4px solid #3b82f6;}
.kg{background:#064e3b;border-left:4px solid #10b981;}
.kr{background:#7f1d1d;border-left:4px solid #ef4444;}
.ka{background:#78350f;border-left:4px solid #f59e0b;}
.kp{background:#581c87;border-left:4px solid #8b5cf6;}
.ke {background:#0f766e;border-left:4px solid #14b8a6;}
.auto-box{background:#0f172a;border:1px solid #334155;border-radius:8px;
          padding:12px;font-size:.85rem;color:#cbd5e1;margin:6px 0;}
.footer{text-align:center;padding:15px;color:#94a3b8;font-size:.75rem;
        border-top:1px solid #334155;margin-top:30px;font-weight:500;}
p,span,label,th,td{color:#cbd5e1 !important;}
.stMarkdown div p strong{color:#ffffff !important;}
div[data-testid="stExpander"]{background-color:#0f172a !important;
     border:1px solid #334155 !important;border-radius:8px;}
</style>
""", unsafe_allow_html=True)

components.html("""
<script>
    function initCursor() {
        const targetDoc = window.parent.document;
        if (targetDoc.getElementById('naba-cursor-txt')) return;
        const style = targetDoc.createElement('style');
        style.innerHTML = `
            #naba-cursor-txt {
                position: fixed;
                pointer-events: none;
                z-index: 999999;
                font-size: 11px;
                font-weight: 700;
                color: #38bdf8 !important;
                background-color: rgba(15, 23, 42, 0.9);
                padding: 3px 7px;
                border-radius: 4px;
                border: 1px solid #334155;
                font-family: 'Inter', sans-serif;
                transform: translate(15px, 15px);
                display: none;
                box-shadow: 0 2px 5px rgba(0,0,0,0.5);
            }
        `;
        targetDoc.head.appendChild(style);
        const div = targetDoc.createElement('div');
        div.id = 'naba-cursor-txt';
        div.innerText = 'NABA Tech';
        targetDoc.body.appendChild(div);
        targetDoc.addEventListener('mousemove', function(e) {
            div.style.left = e.clientX + 'px';
            div.style.top = e.clientY + 'px';
            if (div.style.display !== 'block') { div.style.display = 'block'; }
        });
        targetDoc.addEventListener('mouseleave', function() {
            div.style.display = 'none';
        });
    }
    setTimeout(initCursor, 500);
</script>
""", height=0, width=0)

st.markdown("""
<div class="hdr">
  <h1>📦 NABA Packaging Inventory — Smart Tracker</h1>
  <p class="cl">🏢 Customer: Vertex (Shahzad Bhai) — Lahore</p>
  <p class="sb">NABA TECH BY KALEEM ULLAH SHARIF</p>
</div>
""", unsafe_allow_html=True)

# BUGFIX: _init_schema() (creates tables + seeds the default admin account)
# used to only run lazily inside get_conn(). The login form below only calls
# q(), which never triggered it — so after a fresh reboot, a login attempt
# could run before the admin account was (re)seeded, wrongly showing
# "Invalid username or password" even on a correct first login. Calling it
# explicitly here guarantees schema + seeding always exist before anything
# else runs. st.cache_resource makes this a cheap no-op after the first call.
_init_schema()

# ═══════════════════════════════════════════════
# AUTHENTICATION & ROLE-BASED ACCESS CONTROL (NEW — Cloud version only)
# ═══════════════════════════════════════════════
if "auth_user" not in st.session_state:
    st.session_state["auth_user"] = None

if st.session_state["auth_user"] is None:
    st.markdown("### 🔐 Login")
    with st.form("login_form"):
        li_user = st.text_input("Username")
        li_pass = st.text_input("Password", type="password")
        li_submit = st.form_submit_button("Login", type="primary")
    if li_submit:
        urow = q("SELECT username, password_hash, role, full_name FROM app_users WHERE username=?", [li_user.strip()])
        if not urow.empty and _verify_password(li_pass, urow.iloc[0]["password_hash"]):
            st.session_state["auth_user"] = {
                "username": urow.iloc[0]["username"],
                "role": urow.iloc[0]["role"],
                "full_name": urow.iloc[0]["full_name"],
            }
            st.rerun()
        else:
            st.error("❌ Invalid username or password.")
    st.info("First time setup? Default login is **admin / admin123** — please change it immediately "
            "from the 👤 User Management tab after logging in.")
    st.stop()

current_user = st.session_state["auth_user"]
current_role = current_user["role"]

top_c1, top_c2 = st.columns([5, 1])
with top_c1:
    st.caption(f"👋 Logged in as **{current_user['full_name']}** ({current_user['username']}) — Role: **{current_role}**")
with top_c2:
    if st.button("🚪 Logout", key="logout_btn"):
        st.session_state["auth_user"] = None
        st.rerun()

# Role → allowed tab labels. Tabs the current role can't access still render
# in the tab bar (Streamlit limitation: tabs can't be created conditionally
# per-user without breaking layout) but show a 🔒 access-denied message
# instead of their real content.
TAB_ACCESS = {
    "🔍 Global Search":    ["Admin", "Data Entry"],
    "➕ DC Entry":         ["Admin", "Data Entry"],
    "📋 All Entries":      ["Admin", "Data Entry"],
    "📊 Master Ledger":    ["Admin", "Data Entry", "Viewer"],
    "📤 Sheet Upload":     ["Admin"],
    "🚚 Bilty Management": ["Admin", "Data Entry"],
    "👤 User Management":  ["Admin"],
}

def _access_ok(tab_label):
    if current_role not in TAB_ACCESS[tab_label]:
        st.warning(f"🔒 Your role (**{current_role}**) does not have access to this tab. Contact an Admin if you need it.")
        return False
    return True

if "inline_edit_id" not in st.session_state:
    st.session_state["inline_edit_id"] = None

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "🔍 Global Search","➕ DC Entry","📋 All Entries",
    "📊 Master Ledger","📤 Sheet Upload","🚚 Bilty Management","👤 User Management"
])

# ═══════════════════════════════════════════════
# TAB 1 — GLOBAL SEARCH (UNCHANGED)
# ═══════════════════════════════════════════════
with tab1:
    if _access_ok("🔍 Global Search"):
        st.markdown('<div class="sec">🔍 Global Search — Contract / PO / Article / DC / Token</div>', unsafe_allow_html=True)
        gs = st.text_input("🔎 Type to search:", placeholder="Contract #, PO, Article, DC, Token...", key="global_search_main")

        if gs and len(gs.strip()) >= 2:
            gq = f"%{gs.strip()}%"
            sug_frames = []
            for sql, label in [
                ("SELECT DISTINCT po_no as val FROM sheet_orders WHERE po_no LIKE ? AND TRIM(po_no)!='' LIMIT 4", "PO No."),
                ("SELECT DISTINCT article as val FROM sheet_orders WHERE article LIKE ? LIMIT 4", "Article"),
                ("SELECT DISTINCT dc_no as val FROM inventory WHERE dc_no LIKE ? AND TRIM(dc_no)!='' LIMIT 4", "DC No."),
                ("SELECT DISTINCT company_token as val FROM inventory WHERE company_token LIKE ? AND TRIM(company_token)!='' LIMIT 4", "Token"),
                ("SELECT DISTINCT call_off_no as val FROM sheet_orders WHERE call_off_no LIKE ? LIMIT 4", "Call-Off"),
                ("SELECT DISTINCT contract_no as val FROM inventory WHERE contract_no LIKE ? AND TRIM(contract_no)!='' LIMIT 4", "Contract #"),
            ]:
                df_s = q(sql, [gq])
                if not df_s.empty:
                    df_s["t"] = label
                    sug_frames.append(df_s)

            if sug_frames:
                sug = pd.concat(sug_frames).dropna()
                sug = sug[sug["val"].astype(str).str.strip() != ""]
                if not sug.empty:
                    st.markdown("**Quick select:**")
                    cols_s = st.columns(min(5, len(sug)))
                    for i, (_, sg) in enumerate(sug.iterrows()):
                        with cols_s[i % 5]:
                            if st.button(f"[{sg['t']}] {sg['val']}", key=f"sug_{i}_{sg['val']}"):
                                st.session_state["gs_sel"] = str(sg["val"])
                                st.rerun()

            active = st.session_state.get("gs_sel","") or gs.strip()
            aq = f"%{active}%"

            df_ord = q("""
                SELECT call_off_no,po_no,brand,article,category,SUM(order_qty) as ordered
                FROM sheet_orders
                WHERE po_no LIKE ? OR article LIKE ? OR call_off_no LIKE ? OR sale_contract LIKE ?
                GROUP BY call_off_no,po_no,brand,article,category
            """, [aq,aq,aq,aq])

            df_inv = q("""
                SELECT id,dc_no,company_token,contract_no,call_off_no,po_no,
                       article,category,qty,entry_date,remark
                FROM inventory
                WHERE po_no LIKE ? OR article LIKE ? OR dc_no LIKE ?
                   OR call_off_no LIKE ? OR company_token LIKE ? OR contract_no LIKE ?
                ORDER BY entry_date DESC
            """, [aq,aq,aq,aq,aq,aq])

            tot_o = df_ord["ordered"].sum() if not df_ord.empty else 0
            tot_r = df_inv["qty"].sum()      if not df_inv.empty else 0
            tot_b = tot_o - tot_r

            st.markdown(f"""<div class="kpi-row">
              <div class="kpi kb">📦 Ordered: {tot_o:,.0f}</div>
              <div class="kpi kg">✅ Received: {tot_r:,.0f}</div>
              <div class="kpi {'kr' if tot_b<0 else 'ka'}">⚖️ Balance: {tot_b:,.0f}</div>
              <div class="kpi kp">🔢 DC Entries: {len(df_inv)}</div>
            </div>""", unsafe_allow_html=True)

            if not df_ord.empty:
                df_it = df_ord.groupby("category")["ordered"].sum().reset_index()
                df_ir = df_inv.groupby("category")["qty"].sum().reset_index() if not df_inv.empty \
                        else pd.DataFrame(columns=["category","qty"])
                df_it = df_it.merge(df_ir, on="category", how="left")
                df_it["qty"]     = df_it["qty"].fillna(0)
                df_it["Balance"] = df_it["ordered"] - df_it["qty"]
                df_it.columns    = ["Item Type","Ordered","Received","Balance"]
                st.subheader("📊 Balance by Item Type")
                st.dataframe(df_it, use_container_width=True, hide_index=True)

            if not df_inv.empty:
                st.subheader(f"📋 DC Entry History ({len(df_inv)} records)")
                disp = df_inv.rename(columns={
                    "dc_no":"DC No.","company_token":"Company Token",
                    "contract_no":"Contract #","call_off_no":"Call-Off",
                    "po_no":"PO No.","article":"Article No.",
                    "category":"Item Type","qty":"Qty",
                    "entry_date":"Date","remark":"Remark"
                }).drop(columns=["id"])
                st.dataframe(disp, use_container_width=True, hide_index=True)
            else:
                st.info("No DC entries found.")

            if st.button("🔄 Clear Search", key="clear_gs"):
                st.session_state.pop("gs_sel", None)
                st.rerun()

# ═══════════════════════════════════════════════
# TAB 2 — DC ENTRY (UNCHANGED)
# ═══════════════════════════════════════════════
with tab2:
    if _access_ok("➕ DC Entry"):
        st.markdown('<div class="sec">➕ New DC Entry — Call-Off Triggered Auto-Load</div>', unsafe_allow_html=True)
    
        dc_main_cols = st.columns([3, 2])
    
        with dc_main_cols[0]:
            st.markdown("**Step 1 — Type Call-Off No. to auto-load Contract, PO & Articles**")
            c_sc1, c_sc2, info_col = st.columns([1.5, 1.5, 2])

            with c_sc1:
                f_coff = st.text_input("Call-Off No. *", placeholder="e.g. 288", key="dc_coff_input").strip()

            f_po, f_contract, brand = "", "", ""
            contracts_for_coff, po_for_sc, art_list = [], [], []

            if f_coff:
                conn_tmp = get_conn()
                rows_cont = conn_tmp.execute(
                    "SELECT DISTINCT sale_contract FROM sheet_orders WHERE call_off_no=? AND TRIM(sale_contract)!='' ORDER BY sale_contract",
                    [f_coff]).fetchall()
                contracts_for_coff = [r[0] for r in rows_cont]
            
                rows_art = conn_tmp.execute(
                    "SELECT DISTINCT article FROM sheet_orders WHERE call_off_no=? ORDER BY article",
                    [f_coff]).fetchall()
                art_list = [r[0] for r in rows_art]

                if contracts_for_coff:
                    with c_sc2:
                        if len(contracts_for_coff) == 1:
                            f_contract = contracts_for_coff[0]
                            st.text_input("Contract # (Auto-loaded)", value=f_contract, disabled=True, key="dc_cont_ro")
                        else:
                            f_contract = st.selectbox("Select Contract # *", contracts_for_coff, key="dc_cont_sel")
                
                    brand_r = conn_tmp.execute(
                        "SELECT DISTINCT brand FROM sheet_orders WHERE call_off_no=? AND sale_contract=? AND TRIM(brand)!='' LIMIT 1",
                        [f_coff, f_contract]).fetchone()
                    brand = brand_r[0] if brand_r else ""

                    rows_po = conn_tmp.execute(
                        "SELECT DISTINCT po_no FROM sheet_orders WHERE call_off_no=? AND sale_contract=? AND TRIM(po_no)!='' ORDER BY po_no",
                        [f_coff, f_contract]).fetchall()
                    po_for_sc = [r[0] for r in rows_po]
                else:
                    brand_r = conn_tmp.execute(
                        "SELECT DISTINCT brand FROM sheet_orders WHERE call_off_no=? AND TRIM(brand)!='' LIMIT 1",
                        [f_coff]).fetchone()
                    brand = brand_r[0] if brand_r else ""
                
                    rows_po = conn_tmp.execute(
                        "SELECT DISTINCT po_no FROM sheet_orders WHERE call_off_no=? AND TRIM(po_no)!='' ORDER BY po_no",
                        [f_coff]).fetchall()
                    po_for_sc = [r[0] for r in rows_po]
                
                conn_tmp.close()

            with info_col:
                if f_coff and (contracts_for_coff or po_for_sc):
                    po_display = " | ".join(po_for_sc) if po_for_sc else "—"
                    brand_display = brand if brand else "—"
                    st.markdown(f"""
                    <div class="auto-box" style="background:#064e3b;border:1px solid #10b981;color:#fff; padding:8px; font-size:11px;">
                      ✅ <b>Call-Off Verified!</b><br>
                      Brand: <b>{brand_display}</b><br>
                      Contract #: <b>{f_contract or '—'}</b> | PO: <b>{po_display}</b>
                    </div>""", unsafe_allow_html=True)
                    if len(po_for_sc) == 1:
                        f_po = po_for_sc[0]
                    elif len(po_for_sc) > 1:
                        f_po = st.selectbox("Select PO No. *", po_for_sc, key="dc_po_sel")
                elif f_coff:
                    st.markdown("""
                    <div class="auto-box" style="background:#7f1d1d;border:1px solid #ef4444;color:#fff; padding:6px;">
                      ⚠️ <b>Call-Off Not Found!</b>
                    </div>""", unsafe_allow_html=True)

            st.markdown(f"**Step 2 — Select Article & Enter DC Details**")
            art_opts = ["-- Select Article --"] + art_list
            c1, c2, c3 = st.columns(3)

            with c1:
                f_art_sel = st.selectbox("Article No. *", art_opts, key="dc_art", disabled=(not bool(art_list)))
                f_art  = "" if f_art_sel == "-- Select Article --" else f_art_sel

                # NEW: Live Bilty (Lahore factory dispatch) indicator — shows as
                # soon as Call-Off + Article are both selected. This is informational
                # only and reads from the separate `bilty` ledger; it does not
                # touch the existing Ordered/Received calculations below.
                if f_coff and f_art:
                    bilty_done_art = get_bilty_qty(f_coff, f_art)
                    ordered_for_art = scalar(
                        "SELECT SUM(order_qty) FROM sheet_orders WHERE call_off_no=? AND article=?",
                        [f_coff, f_art])
                    rb = int(math.floor(bilty_done_art + 0.5))
                    ro = int(math.floor(ordered_for_art + 0.5))
                    pct_txt = f" ({(rb/ro*100):.0f}%)" if ro > 0 else ""
                    st.markdown(f"""
                    <div class="auto-box" style="background:#0c4a6e;border:1px solid #38bdf8;color:#fff;padding:7px;font-size:11.5px;margin-bottom:6px;">
                      🚚 <b>Total Bilty Done:</b> {rb:,} Pcs out of {ro:,} Pcs{pct_txt} <span style="opacity:.8;">(Lahore ➜ Karachi, Article {f_art})</span>
                    </div>""", unsafe_allow_html=True)

                f_type = st.selectbox("Item Type *", ITEM_TYPES, key="dc_type")
            
                f_style = "—"
                if f_type == "Inlay Card / Bandrolle":
                    f_style = st.selectbox("Style Type *", STYLES_INLAY, key="dc_style_inlay")
                
                f_token = st.text_input("Company Token", placeholder="e.g. TOK-771", key="dc_token")
            with c2:
                f_dc   = st.text_input("DC No. *", key="dc_dcno")
                f_date = st.date_input("Entry Date *", value=date.today(), key="dc_date")
            with c3:
                f_qty = st.number_input("Quantity (Pcs) *", min_value=0.0, step=1.0, format="%g", key="dc_qty")
                f_remark = st.text_area("Remark / Notes", height=90, key="dc_remark")

        with dc_main_cols[1]:
            st.markdown("<h5>🎯 Live Contract Status Counter</h5>", unsafe_allow_html=True)
            max_allowed = 0
        
            counter_cols = st.columns(2)
        
            if f_coff and f_contract:
                for idx, item in enumerate(ITEM_TYPES):
                    o_q = scalar("SELECT SUM(order_qty) FROM sheet_orders WHERE call_off_no=? AND sale_contract=? AND category=?", [f_coff, f_contract, item])
                    r_q = scalar("SELECT SUM(qty) FROM inventory WHERE call_off_no=? AND contract_no=? AND category=?", [f_coff, f_contract, item])
                
                    rounded_oq = int(math.floor(o_q + 0.5))
                    rounded_rq = int(math.floor(r_q + 0.5))
                    r_q_rem = rounded_oq - rounded_rq
                
                    if item == f_type:
                        max_allowed = scalar("SELECT SUM(order_qty) FROM sheet_orders WHERE call_off_no=? AND sale_contract=? AND category=? AND article=?", [f_coff, f_contract, item, f_art]) - scalar("SELECT SUM(qty) FROM inventory WHERE call_off_no=? AND contract_no=? AND category=? AND article=?", [f_coff, f_contract, item, f_art])
                
                    b_cls = "kb" if r_q_rem > 0 else "kr"
                
                    with counter_cols[idx % 2]:
                        st.markdown(f"""
                        <div class="kpi {b_cls}" style="margin-bottom: 8px; padding: 10px; border-radius: 6px; text-align: left; box-shadow: 0 2px 4px rgba(0,0,0,0.3);">
                            <span style="font-size: 12px; font-weight: 700; display: block; color: #ffffff !important; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 3px;">📦 {item}</span>
                            <div style="font-size: 11px; color: #cbd5e1 !important; line-height: 1.3;">
                              Ord: <b style="color: #ffffff !important;">{rounded_oq:,}</b> | Rec: <b style="color: #ffffff !important;">{rounded_rq:,}</b><br>
                              <span style="font-size: 12px; font-weight: 700; color: #ffffff !important;">⏳ Rem: {r_q_rem:,} Pcs</span>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

        if f_art and f_type:
            o_qty = get_ordered_qty(f_art, f_type, coff=f_coff or None, po=f_po or None)
            r_qty = get_received_qty(f_art, f_type, coff=f_coff or None, po=f_po or None)
        
            rounded_o_qty = int(math.floor(o_qty + 0.5))
            rounded_r_qty = int(math.floor(r_qty + 0.5))
            pend  = rounded_o_qty - rounded_r_qty
            after = pend - (int(f_qty) if f_qty > 0 else 0)
            clr   = "#ef4444" if after < 0 else ("#10b981" if after == 0 else "#f59e0b")
            sign  = "⚠️ Over" if after < 0 else ("✅ Done" if after == 0 else "⏳ Remaining")
            bp1,bp2,bp3,bp4 = st.columns(4)
            bp1.markdown(f"<div class='kpi kb'>📦 Ordered<br><b>{rounded_o_qty:,}</b></div>", unsafe_allow_html=True)
            bp2.markdown(f"<div class='kpi kg'>✅ Received<br><b>{rounded_r_qty:,}</b></div>", unsafe_allow_html=True)
            bp3.markdown(f"<div class='kpi ka'>⏳ Pending<br><b>{pend:,}</b></div>", unsafe_allow_html=True)
            bp4.markdown(f"<div class='kpi' style='background:#0f172a;color:{clr}'>{sign}<br><b>{after:+,}</b></div>", unsafe_allow_html=True)

        st.markdown("---")
        if st.button("💾 Save Entry", type="primary", key="dc_save"):
            s_dc   = str(f_dc).strip()
            s_po   = str(f_po).strip()
            s_coff = str(f_coff).strip()
            s_cont = str(f_contract).strip()
            s_art  = str(f_art).strip()
        
            rounded_max_allowed = int(math.floor(max_allowed + 0.5))
        
            if not s_dc or not s_po or not s_coff or f_qty <= 0 or not s_art:
                st.error("⚠️ DC No., Call-Off, PO, Article and Quantity are required.")
            elif int(f_qty) > rounded_max_allowed and max_allowed >= 0:
                st.error(f"🚨 ALERT! Over-delivery blocked. Max remaining allowed for {f_type} is exactly {rounded_max_allowed:,} Pcs. You cannot enter {int(f_qty):,} Pcs.")
            else:
                conn = get_conn()
                conn.execute("""
                    INSERT INTO inventory
                    (call_off_no,contract_no,dc_no,po_no,article,category,qty,entry_date,remark,company_token,style_type)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (s_coff, s_cont, s_dc, s_po, s_art, f_type, float(f_qty),
                      str(f_date), str(f_remark).strip(), str(f_token).strip(), f_style))
                conn.commit()
                conn.close()
                st.success(f"✅ Entry saved — DC {s_dc} | {f_qty:,.0f} pcs of {f_type}")
                st.rerun()

# ═══════════════════════════════════════════════
# TAB 3 — ALL ENTRIES (UNCHANGED)
# ═══════════════════════════════════════════════
with tab3:
    if _access_ok("📋 All Entries"):
        st.markdown('<div class="sec">📋 Registered DC Entries Registry</div>', unsafe_allow_html=True)

        # PERFORMANCE FIX: previously this loaded the ENTIRE inventory table into
        # a DataFrame on every rerun and did the filtering in pandas — slow and
        # growing slower with every new DC entry. Now a cheap COUNT checks if any
        # data exists, filter dropdown options come from lightweight indexed
        # DISTINCT queries, and the actual filtering happens in SQL (using the
        # indexes created in get_conn) so only the matching rows are ever loaded.
        total_inv_count = scalar("SELECT COUNT(*) FROM inventory")

        if total_inv_count == 0:
            st.info("No DC entries available yet.")
        else:
            st.markdown("##### 🛠️ Advanced Multi-Filters")
            f_cols = st.columns(6)

            def _distinct(col):
                return q(f"SELECT DISTINCT {col} FROM inventory WHERE TRIM({col})!='' ORDER BY {col}")[col].tolist()

            with f_cols[0]:
                opt_coff = ["All"] + _distinct("call_off_no")
                sel_coff = st.selectbox("Filter Call-Off", opt_coff, key="f3_coff")
            with f_cols[1]:
                opt_po = ["All"] + _distinct("po_no")
                sel_po = st.selectbox("Filter PO No.", opt_po, key="f3_po")
            with f_cols[2]:
                opt_art = ["All"] + _distinct("article")
                sel_art = st.selectbox("Filter Article", opt_art, key="f3_art")
            with f_cols[3]:
                opt_dc = ["All"] + _distinct("dc_no")
                sel_dc = st.selectbox("Filter DC No.", opt_dc, key="f3_dc")
            with f_cols[4]:
                opt_tok = ["All"] + _distinct("company_token")
                sel_tok = st.selectbox("Filter Token", opt_tok, key="f3_tok")
            with f_cols[5]:
                opt_cat = ["All"] + _distinct("category")
                sel_cat = st.selectbox("Filter Item Type", opt_cat, key="f3_cat")

            where_sql, params = "WHERE 1=1", []
            if sel_coff != "All": where_sql += " AND call_off_no=?";    params.append(sel_coff)
            if sel_po != "All":   where_sql += " AND po_no=?";          params.append(sel_po)
            if sel_art != "All":  where_sql += " AND article=?";        params.append(sel_art)
            if sel_dc != "All":   where_sql += " AND dc_no=?";          params.append(sel_dc)
            if sel_tok != "All":  where_sql += " AND company_token=?";  params.append(sel_tok)
            if sel_cat != "All":  where_sql += " AND category=?";       params.append(sel_cat)

            df_inv = q(f"""
                SELECT id,call_off_no,contract_no,dc_no,company_token,po_no,
                       article,category,style_type,qty,entry_date,remark
                FROM inventory {where_sql}
                ORDER BY entry_date DESC, id DESC
            """, params)

            tot_r3 = df_inv["qty"].sum()
            tot_o3 = scalar("SELECT SUM(order_qty) FROM sheet_orders")
            net3   = tot_o3 - tot_r3
        
            st.markdown(f"""<div class="kpi-row">
              <div class="kpi kb">📦 Total Ordered: {tot_o3:,.0f}</div>
              <div class="kpi kg">✅ Current Filtered Received: {tot_r3:,.0f}</div>
              <div class="kpi {'kr' if net3<0 else 'ka'}">⚖️ Net Balance: {net3:,.0f}</div>
              <div class="kpi kp">🔢 Displayed Entries: {len(df_inv)}</div>
            </div>""", unsafe_allow_html=True)

            df_show = df_inv.copy()
            df_show.columns = ["ID","Call-Off","Contract #","DC No.","Token","PO No.",
                               "Article","Item Type","Style","Qty","Date","Remark"]
            st.dataframe(df_show, use_container_width=True, hide_index=True)

            st.markdown("---")
            st.markdown("### 🛠️ Edit / Delete Entries")

            for _, row in df_inv.iterrows():
                with st.expander(f"📦 ID:{row['id']} | DC:{row['dc_no']} | Token:{row['company_token'] or '—'} | Article:{row['article']} | {row['qty']:.0f} Pcs"):
                    if st.session_state["inline_edit_id"] == row["id"]:
                        st.markdown(f"#### ✏️ Editing Record ID: {row['id']}")
                        ec1,ec2,ec3 = st.columns(3)
                        with ec1:
                            e_coff  = st.text_input("Call-Off No.", value=str(row["call_off_no"] or ""), key=f"e_coff_{row['id']}")
                            e_cont  = st.text_input("Contract #",   value=str(row["contract_no"]  or ""), key=f"e_cont_{row['id']}")
                            e_po    = st.text_input("PO No.",        value=str(row["po_no"]        or ""), key=f"e_po_{row['id']}")
                        with ec2:
                            e_art   = st.text_input("Article No.",  value=str(row["article"]       or ""), key=f"e_art_{row['id']}")
                            cat_cur = str(row["category"] or "")
                            e_type  = st.selectbox("Item Type", ITEM_TYPES, index=ITEM_TYPES.index(cat_cur) if cat_cur in ITEM_TYPES else 0, key=f"e_type_{row['id']}")
                            e_dc    = st.text_input("DC No.",       value=str(row["dc_no"]         or ""), key=f"e_dc_{row['id']}")
                        with ec3:
                            e_tok   = st.text_input("Company Token",value=str(row["company_token"] or ""), key=f"e_tok_{row['id']}")
                            try:    dv = datetime.strptime(str(row["entry_date"]),"%Y-%m-%d").date()
                            except: dv = date.today()
                            e_date  = st.date_input("Date", value=dv, key=f"e_date_{row['id']}")
                            e_qty   = st.number_input("Quantity", min_value=0.0, step=1.0, value=float(row["qty"] or 0), format="%g", key=f"e_qty_{row['id']}")
                            e_rem   = st.text_area("Remark", value=str(row["remark"] or ""), height=70, key=f"e_rem_{row['id']}")

                        eb1,eb2,_ = st.columns([1.5,1.5,5])
                        with eb1:
                            if st.button("💾 Update & Save", key=f"save_inline_{row['id']}", type="primary"):
                                conn = get_conn()
                                conn.execute("""
                                    UPDATE inventory SET call_off_no=?,contract_no=?,po_no=?,
                                    article=?,category=?,dc_no=?,entry_date=?,qty=?,
                                    remark=?,company_token=? WHERE id=?
                                """, (e_coff.strip(), e_cont.strip(), e_po.strip(), e_art.strip(), e_type, e_dc.strip(), str(e_date), float(e_qty), e_rem.strip(), e_tok.strip(), row["id"]))
                                conn.commit(); conn.close()

                                # Clear inline-edit UI state so the widgets reload fresh
                                # values from the database on rerun (fixes stale/ghost
                                # quantity showing after Update & Save).
                                st.session_state["inline_edit_id"] = None
                                for _k in (f"e_coff_{row['id']}", f"e_cont_{row['id']}", f"e_po_{row['id']}",
                                           f"e_art_{row['id']}", f"e_type_{row['id']}", f"e_dc_{row['id']}",
                                           f"e_tok_{row['id']}", f"e_date_{row['id']}", f"e_qty_{row['id']}",
                                           f"e_rem_{row['id']}"):
                                    if _k in st.session_state:
                                        del st.session_state[_k]

                                st.success("✅ Updated successfully!")
                                st.rerun()
                        with eb2:
                            if st.button("❌ Cancel", key=f"cancel_inline_{row['id']}"):
                                st.session_state["inline_edit_id"] = None
                                st.rerun()
                    else:
                        ca,cb,_ = st.columns([1.5,1.5,5])
                        with ca:
                            if st.button("✏️ Edit", key=f"edit_{row['id']}", type="secondary"):
                                st.session_state["inline_edit_id"] = row["id"]
                                st.rerun()
                        with cb:
                            if st.button("🗑️ Delete", key=f"del_{row['id']}", type="primary"):
                                st.session_state[f"cdel_{row['id']}"] = True
                        if st.session_state.get(f"cdel_{row['id']}"):
                            st.warning(f"Delete DC **{row['dc_no']}** — {row['qty']:.0f} pcs? Balance will revert.")
                            cy,cn,_ = st.columns([1,1,6])
                            if cy.button("✅ Confirm", key=f"cy_{row['id']}"):
                                conn = get_conn()
                                conn.execute("DELETE FROM inventory WHERE id=?", (row["id"],))
                                conn.commit(); conn.close()
                                st.session_state.pop(f"cdel_{row['id']}", None)
                                st.success("Deleted."); st.rerun()
                            if cn.button("❌ No", key=f"cn_{row['id']}"):
                                st.session_state.pop(f"cdel_{row['id']}", None)
                                st.rerun()

            st.markdown("---")
            st.markdown("### ⚠️ Total System Reset")
            if st.checkbox("Yes, I want to delete EVERYTHING", key="confirm_reset"):
                if st.button("🚨 FULL SOFTWARE RESET", type="primary", key="btn_reset"):
                    conn = get_conn()
                    conn.execute("DELETE FROM inventory")
                    conn.execute("DELETE FROM sheet_orders")
                    conn.commit(); conn.close()
                    st.session_state["inline_edit_id"] = None
                    st.success("System reset complete!")
                    st.rerun()

# ═══════════════════════════════════════════════
# TAB 4 — MASTER LEDGER
# ═══════════════════════════════════════════════
with tab4:
    st.markdown('<div class="sec">📊 Master Ledger (Order vs Delivery Status)</div>', unsafe_allow_html=True)

    # FIX: Using direct sqlite3 connection execution with proper matching indentation
    import sqlite3
    db_connection = sqlite3.connect(DB_PATH)
    
    query_ledger = """
        SELECT
            so.call_off_no        AS "Call-Off No",
            so.sale_contract      AS "Contract #",
            so.brand              AS "Brand",
            so.article            AS "Article",
            so.category           AS "Item Type",
            SUM(so.order_qty)     AS "Total Ordered",
            COALESCE(inv.total_received, 0)                        AS "Total Received",
            SUM(so.order_qty) - COALESCE(inv.total_received, 0)   AS "Remaining Balance"
        FROM sheet_orders so
        LEFT JOIN (
            SELECT call_off_no, article, category, SUM(qty) AS total_received
            FROM inventory
            GROUP BY call_off_no, article, category
        ) inv ON so.call_off_no = inv.call_off_no
              AND so.article    = inv.article
              AND so.category   = inv.category
        GROUP BY so.call_off_no, so.sale_contract, so.brand, so.article, so.category
        ORDER BY so.call_off_no DESC, so.article ASC
    """
    
    df_ledger_raw = pd.read_sql_query(query_ledger, db_connection)
    db_connection.close()

    if df_ledger_raw.empty:
        st.info("Master Ledger is empty. Upload a contract sheet in Tab 5 first.")
    else:
        st.markdown("##### 🛠️ Dynamic Ledger Filters")
        fl_cols = st.columns(5)
        
        with fl_cols[0]:
            l_opt_coff = ["All"] + sorted(df_ledger_raw["Call-Off No"].unique().tolist())
            l_sel_coff = st.selectbox("Ledger Call-Off", l_opt_coff, key="l_coff")
        with fl_cols[1]:
            l_opt_cont = ["All"] + sorted(df_ledger_raw["Contract #"].unique().tolist())
            l_sel_cont = st.selectbox("Ledger Contract", l_opt_cont, key="l_cont")
        with fl_cols[2]:
            l_opt_br = ["All"] + sorted(df_ledger_raw["Brand"].unique().tolist())
            l_sel_br = st.selectbox("Ledger Brand", l_opt_br, key="l_brand")
        with fl_cols[3]:
            l_opt_art = ["All"] + sorted(df_ledger_raw["Article"].unique().tolist())
            l_sel_art = st.selectbox("Ledger Article", l_opt_art, key="l_art")
        with fl_cols[4]:
            l_opt_cat = ["All"] + sorted(df_ledger_raw["Item Type"].unique().tolist())
            l_sel_cat = st.selectbox("Ledger Item Type", l_opt_cat, key="l_cat")

        df_ledger = df_ledger_raw.copy()
        if l_sel_coff != "All": df_ledger = df_ledger[df_ledger["Call-Off No"] == l_sel_coff]
        if l_sel_cont != "All": df_ledger = df_ledger[df_ledger["Contract #"] == l_sel_cont]
        if l_sel_br != "All":   df_ledger = df_ledger[df_ledger["Brand"] == l_sel_br]
        if l_sel_art != "All":  df_ledger = df_ledger[df_ledger["Article"] == l_sel_art]
        if l_sel_cat != "All":  df_ledger = df_ledger[df_ledger["Item Type"] == l_sel_cat]

        tot_o = df_ledger["Total Ordered"].sum()
        tot_r = df_ledger["Total Received"].sum()
        tot_b = df_ledger["Remaining Balance"].sum()

        st.markdown(f"""<div class="kpi-row">
          <div class="kpi kb">Filtered Ordered: {round_and_format(tot_o)}</div>
          <div class="kpi kg">Filtered Received: {round_and_format(tot_r)}</div>
          <div class="kpi {'kr' if tot_b<0 else 'ka'}">Filtered Balance: {round_and_format(tot_b)}</div>
        </div>""", unsafe_allow_html=True)

            # ═══════════════════════════════════════════════
            # 🆕 NEW ADDITION: CONTRACT-WISE SHORTFALL SUMMARY
            # ═══════════════════════════════════════════════
            
        st.markdown("---")
            st.markdown("### 📌 CONTRACT-WISE SHORTFALL SUMMARY")
            st.info("💡 نیچے ہر سیلز کنٹریکٹ کے حساب سے الگ الگ بقایا (Shortfall) بریک ڈاؤن دکھایا گیا ہے:")
        
            # گوبل لیجر سے اس کال آف کے تمام یونیک کنٹریکٹس نکالیں
            unique_contracts_in_ledger = sorted(df_ledger["Contract #"].unique().tolist())
        
            for contract_no in unique_contracts_in_ledger:
                # اس مخصوص کنٹریکٹ کا ڈیٹا فلٹر کریں
                df_contract_sub = df_ledger[df_ledger["Contract #"] == contract_no]
                contract_item_summary = df_contract_sub.groupby("Item Type")["Remaining Balance"].sum().to_dict()
            
                # صرف وہ آئٹمز چیک کریں جن کا شارٹ فال 0 سے زیادہ ہے
                has_shortage = any(v > 0 for v in contract_item_summary.values())
            
                with st.expander(f"📄 Contract: {contract_no} | {'🚨 Shortage Pending' if has_shortage else '✅ Fully Cleared'}", expanded=True):
                    sub_cols = st.columns(3)
                    col_counter = 0
                    for it_name in ITEM_TYPES:
                        rem_val_sub = contract_item_summary.get(it_name, 0)
                    
                        # اگر بقایا 0 سے زیادہ ہے تو ڈبے کا رنگ لال (kr) ہوگا ورنہ ہرا (kg)
                        sub_cls = "kr" if rem_val_sub > 0 else "kg"
                    
                        with sub_cols[col_counter % 3]:
                            st.markdown(f'''
                            <div class="kpi {sub_cls}" style="margin-bottom: 6px; padding: 8px; border-radius: 6px;">
                                <span style="font-size: 11px; font-weight: 600; display:block;">📦 {it_name}</span>
                                <span style="font-size: 13px; font-weight: 700;">Rem: {round_and_format(rem_val_sub)} Pcs</span>
                            </div>
                            ''', unsafe_allow_html=True)
                        col_counter += 1
            # ═══════════════════════════════════════════════

            st.markdown("---")
            hide_zero = st.checkbox("Hide rows with zero Remaining Balance", key="hide_zero_bal")
            if hide_zero:
                df_ledger = df_ledger[df_ledger["Remaining Balance"].apply(round_bal) != 0]

            df_fmt = df_ledger.copy()
            df_fmt["Total Ordered"]     = df_fmt["Total Ordered"].apply(round_and_format)
            df_fmt["Total Received"]    = df_fmt["Total Received"].apply(round_and_format)
            df_fmt["Remaining Balance"] = df_fmt["Remaining Balance"].apply(lambda x: "" if x == 0 else round_and_format(x))

            st.dataframe(df_fmt, use_container_width=True, hide_index=True)

            st.markdown("##### 🖨️ Export options:")
            btn_c1, btn_c2, btn_c3 = st.columns([2.5, 3, 3.2])
        
            with btn_c1:
                pdf_buf_master = generate_ledger_pdf(item_summary, df_ledger, l_sel_coff, l_sel_cont, l_sel_art, report_type="MASTER")
                st.download_button(
                    label="🖨️ Print Full Master Ledger (PDF)",
                    data=pdf_buf_master,
                    file_name=f"Master_Ledger_CO_{l_sel_coff}_CN_{l_sel_cont}.pdf",
                    mime="application/pdf",
                    type="primary",
                    key="btn_print_master"
                )
            
            with btn_c2:
                df_shortage_only = df_ledger[df_ledger["Remaining Balance"].apply(round_bal) > 0]
                pdf_buf_shortage = generate_ledger_pdf(item_summary, df_shortage_only, l_sel_coff, l_sel_cont, l_sel_art, report_type="SHORTAGE")
                st.download_button(
                    label="🚨 Print Shortage / Remaining Only (PDF)",
                    data=pdf_buf_shortage,
                    file_name=f"Shortage_Report_CO_{l_sel_coff}_CN_{l_sel_cont}.pdf",
                    mime="application/pdf",
                    type="secondary",
                    key="btn_print_shortage"
                )

            with btn_c3:
                # Contract-wise Shortlist: only ACTIVE/PENDING items for the currently
                # selected Brand + Contract filters. Strict rounding removes any
                # plus/minus float micro-garbage so true-zero articles never appear.
                df_contract_shortlist = df_ledger[df_ledger["Remaining Balance"].apply(round_bal) > 0]
                pdf_buf_contract = generate_ledger_pdf(item_summary, df_contract_shortlist, l_sel_coff, l_sel_cont, l_sel_art, report_type="CONTRACT_SHORTLIST")
                safe_brand = (l_sel_br if l_sel_br != "All" else "AllBrands")
                safe_cont  = (l_sel_cont if l_sel_cont != "All" else "AllContracts")
                st.download_button(
                    label="📋 Print Contract Shortlist (PDF)",
                    data=pdf_buf_contract,
                    file_name=f"Contract_Shortlist_{safe_brand}_{safe_cont}.pdf",
                    mime="application/pdf",
                    type="secondary",
                    key="btn_print_contract_shortlist"
                )

# ═══════════════════════════════════════════════
# TAB 5 — SHEET UPLOAD (UNCHANGED)
# ═══════════════════════════════════════════════
with tab5:
    if _access_ok("📤 Sheet Upload"):
        st.markdown('<div class="sec">📤 Upload Sale Contract Sheet</div>', unsafe_allow_html=True)
        u1,_ = st.columns([1,2])
        with u1:
            u_coff = st.text_input("Call-Off No. * (e.g. 288):", key="u_coff")
            u_file = st.file_uploader("Select Excel or CSV File", type=["csv","xlsx"], key="u_file")

        if u_file and u_coff:
            if st.button("💾 Save Sheet Data", type="primary", key="u_save"):
                try:
                    raw = pd.read_csv(u_file, header=None) if u_file.name.endswith("csv") else pd.read_excel(u_file, header=None)
                    conn = get_conn()
                    conn.execute("DELETE FROM sheet_orders WHERE call_off_no=?", (u_coff.strip(),))
                    saved = 0
                    current_headers = {}

                    for idx, row in raw.iterrows():
                        row_str = [str(x).strip().lower().replace(" ", "") for x in row.values]
                    
                        if any("article" in s or "cont#" in s or "contract" in s for s in row_str):
                            current_headers = {str(row.iloc[i]).strip().lower().replace(" ", ""): i for i, s in enumerate(row_str) if str(row.iloc[i]).strip() != "nan"}
                            continue
                    
                        if not current_headers:
                            continue
                        
                        def get_val_by_keys(*keys):
                            for k in keys:
                                k_lbl = k.lower().replace(" ", "")
                                if k_lbl in current_headers:
                                    v = str(row.iloc[current_headers[k_lbl]]).strip()
                                    if v and v not in ["nan", "None", "-", "0", "0.0"]: return v
                            return ""
                    
                        art  = get_val_by_keys("article no.", "article no", "article", "art no", "item code")
                        cont = get_val_by_keys("cont #", "contract #", "sale contract", "contract")
                        po   = get_val_by_keys("po #", "po#", "po no", "purchase order")
                        br   = get_val_by_keys("brand", "customer/brand", "customer", "client")
                    
                        if not art or "total" in art.lower() or "total" in cont.lower():
                            continue
                        
                        for ct in ITEM_TYPES:
                            search_names = [ct.lower().replace(" ", "")]
                            if ct == "Inlay Card / Bandrolle":
                                search_names = ["inlaycard/bandrolle", "inlaycard", "bandrolle", "inlay", "bandroll"]
                            elif ct == "Tag Card / Barcode Sticker":
                                search_names = ["tagcard/barcodesticker", "tagcard", "barcodesticker", "tag", "tagcardsticker"]
                            elif ct == "Barcode Item":
                                search_names = ["barcodeitem", "barcode item", "barcode", "barcodepure", "barcodeonly"]
                            elif ct == "Transparent Sticker":
                                search_names = ["transparentsticker", "pricesticker", "roundsticker", "transparent", "sticker", "price", "round"]
                            elif ct == "Eco Friendly":
                                search_names = ["ecofriendly", "eco-friendly", "eco"]
                        
                            raw_qty = ""
                            for name in search_names:
                                cleaned_name = name.lower().replace(" ", "")
                                if cleaned_name in current_headers:
                                    raw_qty = str(row.iloc[current_headers[cleaned_name]]).strip()
                                    break
                                
                            try:
                                oq = float(raw_qty.replace(",","")) if raw_qty not in ["", "nan", "-", "None"] else 0
                            except:
                                oq = 0
                            
                            if oq > 0:
                                conn.execute(
                                    "INSERT INTO sheet_orders (call_off_no,po_no,sale_contract,brand,article,category,order_qty) VALUES (?,?,?,?,?,?,?)",
                                    (u_coff.strip(), po, cont, br, art, ct, oq))
                                saved += 1
                            
                    conn.commit(); conn.close()
                    if saved > 0:
                        st.success(f"🎉 Call-Off {u_coff.strip()} — All multi-row items ({saved} configs) auto-scanned and saved!")
                        st.rerun()
                    else:
                        st.warning("⚠️ No valid rows found. Please check column titles.")
                except Exception as e:
                    st.error(f"Error parsing sheet: {e}")

        st.markdown("---")
        st.markdown('<div class="sec">🗂️ Active Sheet Repository</div>', unsafe_allow_html=True)

        value_df = q("""
            SELECT call_off_no AS "Call-Off Sheet",
                   COUNT(*) AS "Config Rows",
                   SUM(order_qty) AS "Total Qty"
            FROM sheet_orders GROUP BY call_off_no ORDER BY call_off_no
        """)

        if value_df.empty:
            st.info("No sheets loaded yet.")
        else:
            value_df["Total Qty"] = value_df["Total Qty"].apply(lambda x: round_and_format(float(str(x).replace(",",""))))
            st.dataframe(value_df, use_container_width=True, hide_index=True)

            st.markdown("### 🗑️ Delete a Sheet")
            sheet_opts = q("SELECT DISTINCT call_off_no FROM sheet_orders ORDER BY call_off_no")["call_off_no"].tolist()
            to_drop = st.selectbox("Select sheet to delete:", ["-- Select --"] + sheet_opts, key="drop_sel")
            if to_drop != "-- Select --":
                st.warning(f"⚠️ This will permanently delete all order targets for **{to_drop}**.")
                if st.checkbox(f"Confirm delete '{to_drop}'", key="drop_chk"):
                    if st.button("🗑️ Erase Sheet", type="primary", key="drop_btn"):
                        conn = get_conn()
                        conn.execute("DELETE FROM sheet_orders WHERE call_off_no=?", (to_drop,))
                        conn.commit(); conn.close()
                        st.success(f"✅ Sheet '{to_drop}' deleted!"); st.rerun()

# ═══════════════════════════════════════════════
# TAB 6 — 🆕 BILTY MANAGEMENT (Lahore ➜ Karachi Dispatch)
# ═══════════════════════════════════════════════
with tab6:
    if _access_ok("🚚 Bilty Management"):
        st.markdown('<div class="sec">🚚 Bilty Management — Lahore Factory Dispatch</div>', unsafe_allow_html=True)
        st.caption("یہ ایک نیا علیحدہ بلٹی لیجر ہے۔ اس کا پرانے Master Ledger (Ordered vs Received) کے حساب کتاب پر کوئی اثر نہیں پڑتا — صرف بلٹی/ڈسپیچ کا ریکارڈ رکھتا ہے۔")

        b_c1, b_c2 = st.columns([2, 2])

        with b_c1:
            b_coff_opts = get_calloff_list()
            b_sel_coff = st.selectbox("Select Call-Off Sheet *", ["-- Select --"] + b_coff_opts, key="bilty_coff")

        b_sel_cont = "-- Select --"
        if b_sel_coff != "-- Select --":
            cont_opts = q(
                "SELECT DISTINCT sale_contract FROM sheet_orders WHERE call_off_no=? AND TRIM(sale_contract)!='' ORDER BY sale_contract",
                [b_sel_coff]
            )["sale_contract"].tolist()
            with b_c2:
                b_sel_cont = st.selectbox("Select Sales Contract *", ["-- Select --"] + cont_opts, key="bilty_cont")

        if b_sel_coff == "-- Select --" or b_sel_cont == "-- Select --":
            st.info("👆 Please select a Call-Off Sheet and Sales Contract to load its articles.")
        else:
            # FIX: quantity now comes straight from the Call-Off Sheet (sum of
            # order_qty per Article + Category) instead of a manual fixed number.
            # No typing needed — the checkbox label shows the exact sheet quantity,
            # and ticking it adds that exact quantity to the grand total.
            df_arts = q("""
                SELECT article, category, SUM(order_qty) AS sheet_qty
                FROM sheet_orders
                WHERE call_off_no=? AND sale_contract=?
                GROUP BY article, category
                ORDER BY article, category
            """, [b_sel_coff, b_sel_cont])

            if df_arts.empty:
                st.info("No articles found for this Call-Off / Contract combination.")
            else:
                st.markdown("##### ✅ Tick items packed into this Bilty")
                st.caption("مقدار خودکار طور پر کال-آف شیٹ سے لی جا رہی ہے — کچھ ٹائپ کرنے کی ضرورت نہیں۔")
                grand_total = 0.0
                ticked_items = []

                for art, grp in df_arts.groupby("article"):
                    with st.expander(f"📦 Article: {art}", expanded=True):
                        tcols = st.columns(3)
                        for i, (_, r_row) in enumerate(grp.iterrows()):
                            cat = r_row["category"]
                            cat_qty = float(r_row["sheet_qty"] or 0)
                            cb_key = f"bilty_cb_{b_sel_coff}_{b_sel_cont}_{art}_{cat}"
                            with tcols[i % 3]:
                                is_checked = st.checkbox(f"{cat} ({cat_qty:,.0f} Pcs)", key=cb_key)
                            if is_checked:
                                grand_total += cat_qty
                                ticked_items.append((art, cat, cat_qty))

                st.markdown(f"""<div class="kpi-row">
                  <div class="kpi kp" style="font-size:1rem;">🧮 Live Grand Total: {grand_total:,.0f} Pcs ({len(ticked_items)} item(s) ticked)</div>
                </div>""", unsafe_allow_html=True)

                st.markdown("---")
                bc1, bc2, bc3 = st.columns(3)
                with bc1:
                    cartons_n = st.number_input("Number of Cartons *", min_value=0, step=1, key="bilty_cartons")
                with bc2:
                    transport_mode = st.selectbox("Transport Mode *", ["By Air", "By Train"], key="bilty_transport")
                with bc3:
                    bilty_date_val = st.date_input("Bilty Date *", value=date.today(), key="bilty_date")

                if st.button("🚚 Save Bilty Record", type="primary", key="bilty_save"):
                    if not ticked_items:
                        st.error("⚠️ Please tick at least one item before saving.")
                    elif cartons_n <= 0:
                        st.error("⚠️ Please enter the number of cartons.")
                    else:
                        conn = get_conn()
                        for art, cat, cat_qty in ticked_items:
                            conn.execute("""
                                INSERT INTO bilty (call_off_no, contract_no, article, category, qty, cartons, transport_mode, bilty_date, created_at)
                                VALUES (?,?,?,?,?,?,?,?,?)
                            """, (b_sel_coff, b_sel_cont, art, cat, float(cat_qty), int(cartons_n),
                                  transport_mode, str(bilty_date_val), str(datetime.datetime.now())))
                        conn.commit()
                        conn.close()
                        st.success(f"✅ Bilty saved — {len(ticked_items)} item(s), {grand_total:,.0f} Pcs total, {cartons_n} Carton(s) via {transport_mode}.")
                        st.rerun()

                st.markdown("---")
                st.markdown("##### 📜 Bilty History for this Contract")
                df_bilty_hist = q("""
                    SELECT bilty_date AS "Date", article AS "Article", category AS "Item Type",
                           qty AS "Qty", cartons AS "Cartons", transport_mode AS "Transport"
                    FROM bilty WHERE call_off_no=? AND contract_no=?
                    ORDER BY id DESC
                """, [b_sel_coff, b_sel_cont])
                if df_bilty_hist.empty:
                    st.info("No Bilty records saved yet for this contract.")
                else:
                    st.dataframe(df_bilty_hist, use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════
# TAB 7 — 🆕 USER MANAGEMENT (Admin only)
# ═══════════════════════════════════════════════
with tab7:
    if _access_ok("👤 User Management"):
        st.markdown('<div class="sec">👤 User Management — Admin Only</div>', unsafe_allow_html=True)

        st.markdown("##### ➕ Create New User")
        with st.form("create_user_form", clear_on_submit=True):
            nu_c1, nu_c2 = st.columns(2)
            with nu_c1:
                nu_username = st.text_input("Username *")
                nu_fullname = st.text_input("Full Name *")
            with nu_c2:
                nu_password = st.text_input("Password *", type="password")
                nu_role = st.selectbox("Role *", ["Admin", "Data Entry", "Viewer"])
            nu_submit = st.form_submit_button("➕ Create User", type="primary")

        if nu_submit:
            if not nu_username.strip() or not nu_password or not nu_fullname.strip():
                st.error("⚠️ Username, Full Name and Password are all required.")
            else:
                existing = q("SELECT id FROM app_users WHERE username=?", [nu_username.strip()])
                if not existing.empty:
                    st.error(f"⚠️ Username '{nu_username.strip()}' already exists.")
                else:
                    conn = get_conn()
                    conn.execute("""
                        INSERT INTO app_users (username,password_hash,role,full_name,created_at)
                        VALUES (?,?,?,?,?)
                    """, (nu_username.strip(), _hash_password(nu_password), nu_role,
                          nu_fullname.strip(), str(datetime.datetime.now())))
                    conn.commit()
                    conn.close()
                    st.success(f"✅ User '{nu_username.strip()}' created with role '{nu_role}'.")
                    st.rerun()

        st.markdown("---")
        st.markdown("##### 🔑 Reset a User's Password")
        all_usernames = q("SELECT username FROM app_users ORDER BY username")["username"].tolist()
        rp_c1, rp_c2, rp_c3 = st.columns([2, 2, 1])
        with rp_c1:
            rp_user = st.selectbox("Select User", all_usernames, key="rp_user")
        with rp_c2:
            rp_newpass = st.text_input("New Password", type="password", key="rp_newpass")
        with rp_c3:
            st.markdown("<br>", unsafe_allow_html=True)
            if st.button("🔑 Reset Password", key="rp_btn"):
                if not rp_newpass:
                    st.error("⚠️ Enter a new password first.")
                else:
                    conn = get_conn()
                    conn.execute("UPDATE app_users SET password_hash=? WHERE username=?",
                                 (_hash_password(rp_newpass), rp_user))
                    conn.commit()
                    conn.close()
                    st.success(f"✅ Password reset for '{rp_user}'.")

        st.markdown("---")
        st.markdown("##### 📋 All Users")
        df_users = q("SELECT username AS Username, full_name AS \"Full Name\", role AS Role, created_at AS \"Created At\" FROM app_users ORDER BY username")
        st.dataframe(df_users, use_container_width=True, hide_index=True)

        st.markdown("##### 🗑️ Delete a User")
        del_candidates = [u for u in all_usernames if u != current_user["username"]]
        if del_candidates:
            del_user = st.selectbox("Select user to delete", ["-- Select --"] + del_candidates, key="del_user_sel")
            if del_user != "-- Select --":
                if st.checkbox(f"Confirm delete '{del_user}'", key="del_user_chk"):
                    if st.button("🗑️ Delete User", key="del_user_btn"):
                        conn = get_conn()
                        conn.execute("DELETE FROM app_users WHERE username=?", (del_user,))
                        conn.commit()
                        conn.close()
                        st.success(f"✅ User '{del_user}' deleted.")
                        st.rerun()
        else:
            st.caption("No other users to delete.")

st.markdown("""
<div class="footer">
  🏭 NABA TECH BY KALEEM ULLAH SHARIF &nbsp;|&nbsp;
  Customer: Vertex (Shahzad Bhai) Lahore &nbsp;|&nbsp; v6.4 Cloud
</div>
""", unsafe_allow_html=True)
