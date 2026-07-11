import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import datetime
import io
import math
import hashlib
import re
import secrets as pysecrets
from datetime import date
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from sqlalchemy import create_engine, text, inspect

# =====================================================================
# CONFIGURATIONS & UNIVERSAL MAPPING
# =====================================================================
UNIVERSAL_CATEGORIES = [
    "Inlay Card",
    "Tag Card",
    "Barcode Sticker",
    "Washing Paper",
    "Safety Sticker"
]

ITEM_MAPPING_DICTIONARY = {
    r"INLAY CARD.*": "Inlay Card / Bandrolle",
    r"TAG CARD.*": "Tag Card / Barcode Sticker",
    r"BARCODE STICKER.*": "Barcode Item",
    r"POLY BAG BARCODE.*": "Barcode Item",
    r"WASHING PAPER.*": "Washing Paper",
    r"LEAFLET.*": "Washing Paper",
    r"SAFETY STICKER.*": "Safety",
    r"TRANSPARENT STICKER.*": "Transparent Sticker"
}

def map_item_to_universal(item_name):
    """طویل ناموں کو یونیورسل کیٹیگری میں تبدیل کرنے کا فنکشن"""
    for pattern, category in ITEM_MAPPING_DICTIONARY.items():
        if re.search(pattern, str(item_name), re.IGNORECASE):
            return category
    return "Other"

# ═══════════════════════════════════════════════
# DATABASE SYSTEM — CLOUD (Using pg8000 for PostgreSQL safely)
# ═══════════════════════════════════════════════
@st.cache_resource(show_spinner=False)
def _get_engine():
    db_url = st.secrets.get("DB_URL", "sqlite:///textile_inventory.db")
    
    if "postgresql+psycopg2://" in db_url:
        db_url = db_url.replace("postgresql+psycopg2://", "postgresql+pg8000://")
    elif db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+pg8000://", 1)
    elif db_url.startswith("postgresql://") and not "+pg8000" in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+pg8000://", 1)
        
    return create_engine(db_url, pool_recycle=1800, pool_pre_ping=True)

def _qmark_to_named(sql, params):
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
    dialect = engine.dialect.name
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
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS app_users (
                id {pk},
                username TEXT UNIQUE, password_hash TEXT, role TEXT,
                full_name TEXT, created_at TEXT)"""))

    insp = inspect(engine)
    inv_cols = [c["name"] for c in insp.get_columns("inventory")]
    missing_cols = [c for c in ["entry_date", "remark", "company_token", "contract_no", "style_type"] if c not in inv_cols]
    if missing_cols:
        with engine.begin() as conn:
            for col in missing_cols:
                conn.execute(text(f"ALTER TABLE inventory ADD COLUMN {col} TEXT DEFAULT ''"))

    au_cols = [c["name"] for c in insp.get_columns("app_users")]
    missing_au_cols = [c for c in ["last_seen"] if c not in au_cols]
    if missing_au_cols:
        with engine.begin() as conn:
            for col in missing_au_cols:
                conn.execute(text(f"ALTER TABLE app_users ADD COLUMN {col} TEXT DEFAULT ''"))

    index_defs = [
        ("idx_inv_article_category", "inventory(article, category)"),
        ("idx_inv_calloff",          "inventory(call_off_no)"),
        ("idx_inv_contract",         "inventory(contract_no)"),
        ("idx_inv_po",               "inventory(po_no)"),
        ("idx_inv_dc",               "inventory(dc_no)"),
        ("idx_inv_token",            "inventory(company_token)"),
        ("idx_inv_entrydate",        "inventory(entry_date)"),
        ("idx_so_calloff",           "sheet_orders(call_off_no)"),
        ("idx_so_article_category",  "sheet_orders(article, category)"),
        ("idx_so_contract",          "sheet_orders(sale_contract)"),
        ("idx_so_po",                "sheet_orders(po_no)"),
        ("idx_bilty_calloff_art",    "bilty(call_off_no, article, category)"),
        ("idx_bilty_contract",       "bilty(contract_no)"),
    ]
    if dialect == "mysql":
        for name, target in index_defs:
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"CREATE INDEX {name} ON {target}"))
            except Exception:
                pass
    else:
        with engine.begin() as conn:
            for name, target in index_defs:
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))

    with engine.begin() as conn:
        user_count = conn.execute(text("SELECT COUNT(*) FROM app_users")).scalar()
        if not user_count:
            conn.execute(
                text("INSERT INTO app_users (username,password_hash,role,full_name,created_at) "
                     "VALUES (:u,:p,:r,:f,:c)"),
                {"u": "admin", "p": _hash_password("admin123"), "r": "Admin",
                 "f": "Default Admin", "c": str(datetime.datetime.now())})

    with engine.begin() as conn:
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS site_stats (
                id {pk},
                stat_key TEXT UNIQUE, stat_value INTEGER DEFAULT 0)"""))
        vc = conn.execute(text("SELECT COUNT(*) FROM site_stats WHERE stat_key='total_visits'")).scalar()
        if not vc:
            conn.execute(text("INSERT INTO site_stats (stat_key, stat_value) VALUES ('total_visits', 0)"))
    return True

def get_conn():
    _init_schema()
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
# CONSTANTS & HELPERS
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

@st.cache_data(ttl=20, show_spinner=False)
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
st.set_page_config(page_title="Vertex Packaging | Inventory System", layout="wide", page_icon="📦")

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
html,body,[class*="css"]{font-family:'Inter',sans-serif;}
.stApp{background-color:#1a202c !important;color:#e2e8f0 !important;}
.hdr{background:linear-gradient(135deg,#0f172a,#1e293b);padding:20px 30px;border-radius:12px;
     margin-bottom:20px;border-left:6px solid #38bdf8;box-shadow:0 4px 6px rgba(0,0,0,.2);}
.hdr h1{color:#f8fafc;font-size:1.6rem;font-weight:700;margin:0;}
.hdr .cl{color:#f59e0b;font-size:.88rem;font-weight:600;margin:4px 0 0;}
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
p,span,label,th,td{color:#cbd5e1 !important;}
.stMarkdown div p strong{color:#ffffff !important;}
div[data-testid="stExpander"]{background-color:#0f172a !important;
     border:1px solid #334155 !important;border-radius:8px;}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="hdr">
  <h1>📦 Vertex Packaging — Smart Inventory Tracker</h1>
  <p class="cl">🏢 CEO: Shahzad Bhai — Lahore</p>
</div>
""", unsafe_allow_html=True)

_init_schema()

st.warning("⚠️ **Trial Version Notice:** This system is currently running on a **Trial Basis** for testing purposes. "
           "یہ نظام فی الحال ٹیسٹنگ کے مقاصد کے لیے ٹرائل بیس پر چل رہا ہے۔")

if "visit_counted" not in st.session_state:
    _vc_conn = get_conn()
    _vc_conn.execute("UPDATE site_stats SET stat_value = stat_value + 1 WHERE stat_key='total_visits'")
    _vc_conn.commit()
    _vc_conn.close()
    st.session_state["visit_counted"] = True

_total_visits = scalar("SELECT stat_value FROM site_stats WHERE stat_key='total_visits'")
st.caption(f"👁️ Total Visits: {int(_total_visits):,}")

# Authentication Guard
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
    st.info("First time setup? Default login is **admin / admin123**")
    st.stop()

current_user = st.session_state["auth_user"]
current_role = current_user["role"]

_now_ts = str(datetime.datetime.now())
_conn_ls = get_conn()
_conn_ls.execute("UPDATE app_users SET last_seen=? WHERE username=?", (_now_ts, current_user["username"]))
_conn_ls.commit()
_conn_ls.close()

_ACTIVE_WINDOW_MINUTES = 5
_df_active = q("SELECT username, role, full_name, last_seen FROM app_users "
               "WHERE last_seen IS NOT NULL AND last_seen != '' AND role != 'Admin'")
_active_list = []
_now_dt = datetime.datetime.now()
for _, _r in _df_active.iterrows():
    try:
        _ls = datetime.datetime.fromisoformat(_r["last_seen"])
        if (_now_dt - _ls).total_seconds() <= _ACTIVE_WINDOW_MINUTES * 60:
            _active_list.append(f"{_r['full_name']} ({_r['role']})")
    except Exception:
        pass

top_c1, top_c2 = st.columns([5, 1])
with top_c1:
    st.caption(f"👋 Logged in as **{current_user['full_name']}** ({current_user['username']}) — Role: **{current_role}**")
    if _active_list:
        st.caption("🟢 Active now: " + ", ".join(_active_list))
with top_c2:
    if st.button("🚪 Logout", key="logout_btn"):
        st.session_state["auth_user"] = None
        st.rerun()

TAB_ACCESS = {
    "🔍 Global Search":    ["Admin", "Data Entry", "CEO"],
    "➕ DC Entry":         ["Admin", "Data Entry", "CEO"],
    "📋 All Entries":      ["Admin", "Data Entry"],
    "📊 Master Ledger":    ["Admin", "Data Entry", "Viewer", "CEO"],
    "📤 Sheet Upload":     ["Admin"],
    "🚚 Bilty Management": ["Admin", "Data Entry", "CEO"],
    "👤 User Management":  ["Admin"],
}

DC_ENTRY_WRITE_ROLES = ["Admin", "Data Entry"]

def _access_ok(tab_label):
    if current_role not in TAB_ACCESS[tab_label]:
        st.warning(f"🔒 Your role (**{current_role}**) does not have access to this tab.")
        return False
    return True

if "inline_edit_id" not in st.session_state:
    st.session_state["inline_edit_id"] = None

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "🔍 Global Search","➕ DC Entry","📋 All Entries",
    "📊 Master Ledger","📤 Sheet Upload","🚚 Bilty Management","👤 User Management"
])

# ═══════════════════════════════════════════════
# TAB 1 — GLOBAL SEARCH
# ═══════════════════════════════════════════════
with tab1:
    if _access_ok("🔍 Global Search"):
        st.markdown('<div class="sec">🔍 Global Search — Contract / PO / Article / DC / Token</div>', unsafe_allow_html=True)
        gs = st.text_input("🔎 Type to search:", placeholder="Contract #, PO, Article, DC, Token...", key="global_search_main")

        if gs and len(gs.strip()) >= 2:
            gq = f"%{gs.strip()}%"
            sug_sql = """
                SELECT * FROM (SELECT 'PO No.'    AS t, po_no          AS val FROM sheet_orders WHERE po_no LIKE ? AND TRIM(po_no)!='' LIMIT 4) x1
                UNION ALL
                SELECT * FROM (SELECT 'Article'   AS t, article        AS val FROM sheet_orders WHERE article LIKE ? LIMIT 4) x2
                UNION ALL
                SELECT * FROM (SELECT 'DC No.'    AS t, dc_no          AS val FROM inventory WHERE dc_no LIKE ? AND TRIM(dc_no)!='' LIMIT 4) x3
                UNION ALL
                SELECT * FROM (SELECT 'Token'     AS t, company_token  AS val FROM inventory WHERE company_token LIKE ? AND TRIM(company_token)!='' LIMIT 4) x4
                UNION ALL
                SELECT * FROM (SELECT 'Call-Off'  AS t, call_off_no    AS val FROM sheet_orders WHERE call_off_no LIKE ? LIMIT 4) x5
                UNION ALL
                SELECT * FROM (SELECT 'Contract #' AS t, contract_no  AS val FROM inventory WHERE contract_no LIKE ? AND TRIM(contract_no)!='' LIMIT 4) x6
            """
            sug = q(sug_sql, [gq, gq, gq, gq, gq, gq]).dropna()
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

            df_bilty_search = q("""
                SELECT bilty_date AS "Date", call_off_no AS "Call-Off", contract_no AS "Contract #",
                       article AS "Article", category AS "Item Type", qty AS "Qty",
                       cartons AS "Cartons", transport_mode AS "Transport"
                FROM bilty
                WHERE call_off_no LIKE ? OR contract_no LIKE ? OR article LIKE ?
                ORDER BY id DESC
            """, [aq, aq, aq])

            if not df_bilty_search.empty:
                st.subheader(f"🚚 Bilty Dispatch History ({len(df_bilty_search)} records)")
                st.dataframe(df_bilty_search, use_container_width=True, hide_index=True)

            if st.button("🔄 Clear Search", key="clear_gs"):
                st.session_state.pop("gs_sel", None)
                st.rerun()

# ═══════════════════════════════════════════════
# TAB 2 — DC ENTRY (INTEGRATED WORKFLOW)
# ═══════════════════════════════════════════════
with tab2:
    if _access_ok("➕ DC Entry"):
        st.markdown('<div class="sec">➕ New DC Entry — Dual-Pack Workflow & Universal Layout</div>', unsafe_allow_html=True)
    
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
                    po_display = " | ".join([str(p) for p in po_for_sc]) if po_for_sc else "—"
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
                      🚚 <b>Total Bilty Done:</b> {rb:,} Pcs out of {ro:,} Pcs{pct_txt}
                    </div>""", unsafe_allow_html=True)

                f_type = st.selectbox("Item Type *", ITEM_TYPES, key="dc_type")
            
                f_style = "—"
                if f_type == "Inlay Card / Bandrolle":
                    f_style = st.selectbox("Style Type *", STYLES_INLAY, key="dc_style_inlay")
                
                f_token = st.text_input("Company Token", placeholder="e.g. TOK-771", key="dc_token")
            with c2:
                f_dc = st.text_input("Physical DC Number (Manual Input Only) *", key="dc_dcno", placeholder="e.g., 4689")
                f_date = st.date_input("Entry Date *", value=date.today(), key="dc_date")
            with c3:
                # --- SECTION 2: DUAL-PACK WORKFLOW WORK INJECTION ---
                is_dual_pack = str(f_po) in ["42807", "42642"]
                
                if is_dual_pack:
                    st.info("💡 Dual-Pack PO Detected. Split Input Fields Activated.")
                    col_j, col_m = st.columns(2)
                    with col_j:
                        qty_jersey = st.number_input("Jersey Variant Qty", min_value=0, value=0, key="j_qty")
                    with col_m:
                        qty_molton = st.number_input("Molton Variant Qty", min_value=0, value=0, key="m_qty")
                    f_qty = float(qty_jersey + qty_molton)
                else:
                    f_qty = st.number_input("Standard Item Quantity (Pcs) *", min_value=0.0, step=1.0, format="%g", key="dc_qty")
                
                f_remark = st.text_area("Remark / Notes", height=90, key="dc_remark")

            # Global Common Items Logic
            st.markdown("##### 🌍 Global Common Items (Automated Standard Reference)")
            col_wp, col_sf = st.columns(2)
            with col_wp:
                st.text_input("Washing Paper (Leaflet)", value="Active & Linked to Standard Account", disabled=True, key="wp_lock_lbl")
                qty_washing = st.number_input("Washing Paper Qty Indicator", min_value=0, value=int(f_qty), key="wp_qty_ind")
            with col_sf:
                st.text_input("Safety Sticker Reference", value="Active & Locked to Standard Account", disabled=True, key="sf_lock_lbl")
                qty_safety = st.number_input("Safety Sticker Qty Indicator", min_value=0, value=int(f_qty), key="sf_qty_ind")

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
                            <span style="font-size: 12px; font-weight: 700; display: block; color: #ffffff !important;">📦 {item}</span>
                            <div style="font-size: 11px; color: #cbd5e1 !important; line-height: 1.3;">
                              Ord: <b style="color: #ffffff !important;">{rounded_oq:,}</b> | Rec: <b style="color: #ffffff !important;">{rounded_rq:,}</b><br>
                              <span style="font-size: 12px; font-weight: 700; color: #ffffff !important;">⏳ Rem: {r_q_rem:,} Pcs</span>
                            </div>
                        </div>
                        """, unsafe_allow_html=True)

        if f_art:
            st.markdown(f"<h5>🧩 Item-Wise Breakdown — Article {f_art}</h5>", unsafe_allow_html=True)
            art_bd_cols = st.columns(4)
            for idx, item in enumerate(ITEM_TYPES):
                a_o = get_ordered_qty(f_art, item, coff=f_coff or None)
                a_r = get_received_qty(f_art, item, coff=f_coff or None)
                a_ro = int(math.floor(a_o + 0.5))
                a_rr = int(math.floor(a_r + 0.5))
                a_rem = a_ro - a_rr
                b_cls2 = "kb" if a_rem > 0 else "kr"
                with art_bd_cols[idx % 4]:
                    st.markdown(f"""
                    <div class="kpi {b_cls2}" style="margin-bottom: 8px; padding: 10px; border-radius: 6px; text-align: left;">
                        <span style="font-size: 12px; font-weight: 700; display: block; color: #ffffff !important;">📦 {item}</span>
                        <div style="font-size: 11px; color: #cbd5e1 !important; line-height: 1.3;">
                          Ord: <b style="color: #ffffff !important;">{a_ro:,}</b> | Rec: <b style="color: #ffffff !important;">{a_rr:,}</b><br>
                          <span style="font-size: 12px; font-weight: 700; color: #ffffff !important;">⏳ Rem: {a_rem:,} Pcs</span>
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
        if current_role not in DC_ENTRY_WRITE_ROLES:
            st.info(f"🔒 Your role (**{current_role}**) has view-only access to DC Entry.")
        elif st.button("💾 Save & Post Challan", type="primary", key="dc_save"):
            s_dc   = str(f_dc).strip()
            s_po   = str(f_po).strip()
            s_coff = str(f_coff).strip()
            s_cont = str(f_contract).strip()
            s_art  = str(f_art).strip()
        
            rounded_max_allowed = int(math.floor(max_allowed + 0.5))
        
            if not s_dc or not s_po or not s_coff or f_qty <= 0 or not s_art:
                st.error("❌ چالان نمبر، کال آف، پی او، آرٹیکل اور کوانٹٹی ٹائپ کرنا لازمی ہے!")
            elif int(f_qty) > rounded_max_allowed and max_allowed >= 0:
                st.error(f"🚨 ALERT! Over-delivery blocked. Max remaining allowed for {f_type} is exactly {rounded_max_allowed:,} Pcs.")
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
                st.success(f"🎉 DC #{s_dc} Saved and Posted Successfully by {current_user['username']}!")
                st.rerun()

        # --- DITTO DELIVERY CHALLAN PREVIEW INJECTION ---
        st.markdown("---")
        st.header("📄 Ditto Delivery Challan Preview")
        
        latest_dc_df = q("SELECT * FROM inventory ORDER BY id DESC LIMIT 1")
        if not latest_dc_df.empty:
            ldc = latest_dc_df.iloc[0]
            
            st.markdown("#### Carton Settings")
            c_start = st.number_input("Carton From #", min_value=0, value=1, key="c_start_val")
            c_end = st.number_input("Carton To #", min_value=0, value=10, key="c_end_val")
            
            # Map dynamic items breakdown under this DC
            dc_all_items = q("SELECT category, qty FROM inventory WHERE dc_no=? AND call_off_no=?", [ldc['dc_no'], ldc['call_off_no']])
            
            st.markdown(
                f"""
                <div style="border:2px solid #000; padding:20px; font-family: 'Courier New', Courier, monospace; background-color:#fff; color:#000; border-radius:5px;">
                    <h2 style="text-align:center; margin:0; font-weight:bold; color:#000;">DELIVERY CHALLAN</h2>
                    <hr style="border-top: 1px solid #000;">
                    <table style="width:100%; color:#000; font-size:14px;">
                        <tr>
                            <td><b>DC NO:</b> {ldc['dc_no']}</td>
                            <td style="text-align:right;"><b>DATE:</b> {ldc['entry_date']}</td>
                        </tr>
                        <tr>
                            <td><b>SALE CONTRACT NO:</b> {ldc['contract_no']}</td>
                            <td style="text-align:right;"><b>PO NO:</b> {ldc['po_no']}</td>
                        </tr>
                        <tr>
                            <td><b>ARTICLE NO:</b> {ldc['article']}</td>
                            <td style="text-align:right;"><b>STYLE:</b> {ldc['style_type']}</td>
                        </tr>
                    </table>
                    <br>
                    <table style="width:100%; border-collapse: collapse; color:#000; font-size:14px;">
                        <thead>
                            <tr style="border-bottom: 2px solid #000; border-top: 2px solid #000;">
                                <th style="text-align:left; padding:5px; color:#000 !important;">ITEM DESCRIPTION</th>
                                <th style="text-align:right; padding:5px; color:#000 !important;">DISPATCHED QTY</th>
                            </tr>
                        </thead>
                        <tbody>
                """, 
                unsafe_allow_html=True
            )
            
            for _, ir in dc_all_items.iterrows():
                st.markdown(
                    f"""
                            <tr style="border-bottom: 1px dashed #ccc;">
                                <td style="padding:5px; color:#000 !important;">{str(ir['category']).upper()}</td>
                                <td style="text-align:right; padding:5px; color:#000 !important;">{int(ir['qty']):,}</td>
                            </tr>
                    """, 
                    unsafe_allow_html=True
                )
                
            st.markdown(
                f"""
                        </tbody>
                    </table>
                    <br><br>
                    <p style="text-align:center; font-weight:bold; font-size:15px; margin:20px 0; color:#000;">
                        PACK OF {c_start:02d} TO {c_end:02d} CARTONS
                    </p>
                    <br><br>
                    <table style="width:100%; color:#000; font-size:13px; margin-top:30px;">
                        <tr>
                            <td style="border-top:1px solid #000; width:30%; text-align:center; color:#000 !important;">PREPARED BY</td>
                            <td style="width:40%;"></td>
                            <td style="border-top:1px solid #000; width:30%; text-align:center; color:#000 !important;">RECEIVED BY SIGNATURE</td>
                        </tr>
                    </table>
                    <p style="font-size:10px; color:#555; margin-top:15px;">System Time Logs: {ldc['entry_date']} | Token Ref: {ldc['company_token']}</p>
                </div>
                """, 
                unsafe_allow_html=True
            )
            st.caption("ℹ️ برائوزر کا `Ctrl + P` دبا کر آپ اس لے آؤٹ کو ہو بہو (Dot to Dot) فزیکل چالان کی طرح پرنٹ کر سکتے ہیں۔")
        else:
            st.info("کوئی چالان پوسٹ نہیں ہوا، چالان جنریٹ کرنے کے لیے اوپر دیے گئے فارم کو پُر کر کے 'Save & Post Challan' پر کلک کریں.")

# ═══════════════════════════════════════════════
# TAB 3 — ALL ENTRIES
# ═══════════════════════════════════════════════
with tab3:
    if _access_ok("📋 All Entries"):
        st.markdown('<div class="sec">📋 Registered DC Entries Registry</div>', unsafe_allow_html=True)

        total_inv_count = scalar("SELECT COUNT(*) FROM inventory")

        if total_inv_count == 0:
            st.info("No DC entries available yet.")
        else:
            st.markdown("##### 🛠️ Advanced Multi-Filters")
            f_cols = st.columns(6)

            @st.cache_data(ttl=15, show_spinner=False)
            def _tab3_filter_options():
                cols = ["call_off_no", "po_no", "article", "dc_no", "company_token", "category"]
                union_sql = " UNION ALL ".join(
                    f"SELECT '{c}' AS col_name, {c} AS val FROM inventory WHERE TRIM({c})!=''" for c in cols
                )
                df = q(union_sql)
                return {c: sorted(df.loc[df["col_name"] == c, "val"].dropna().unique().tolist()) for c in cols}

            _opts = _tab3_filter_options()

            with f_cols[0]:
                sel_coff = st.selectbox("Filter Call-Off", ["All"] + _opts["call_off_no"], key="f3_coff")
            with f_cols[1]:
                sel_po = st.selectbox("Filter PO No.", ["All"] + _opts["po_no"], key="f3_po")
            with f_cols[2]:
                sel_art = st.selectbox("Filter Article", ["All"] + _opts["article"], key="f3_art")
            with f_cols[3]:
                sel_dc = st.selectbox("Filter DC No.", ["All"] + _opts["dc_no"], key="f3_dc")
            with f_cols[4]:
                sel_tok = st.selectbox("Filter Token", ["All"] + _opts["company_token"], key="f3_tok")
            with f_cols[5]:
                sel_cat = st.selectbox("Filter Item Type", ["All"] + _opts["category"], key="f3_cat")

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
                with st.expander(f"📦 ID:{row['id']} | DC:{row['dc_no']} | Article:{row['article']} | {row['qty']:.0f} Pcs"):
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
                            try:    dv = datetime.datetime.strptime(str(row["entry_date"]),"%Y-%m-%d").date()
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
                                st.session_state["inline_edit_id"] = None
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
                            st.warning(f"Delete DC **{row['dc_no']}**?")
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

# ═══════════════════════════════════════════════
# TAB 4 — MASTER LEDGER (WITH CONTRACT SHORTFALL)
# ═══════════════════════════════════════════════
with tab4:
    if _access_ok("📊 Master Ledger"):
        st.markdown('<div class="sec">📊 Master Ledger (Order vs Delivery Status)</div>', unsafe_allow_html=True)

        df_ledger_raw = q("""
            SELECT
                so.call_off_no        AS "Call-Off No",
                so.sale_contract      AS "Contract #",
                so.brand              AS "Brand",
                so.article            AS "Article",
                so.category           AS "Item Type",
                SUM(so.order_qty)     AS "Total Ordered",
                MAX(COALESCE(inv.total_received, 0))                        AS "Total Received",
                SUM(so.order_qty) - MAX(COALESCE(inv.total_received, 0))   AS "Remaining Balance"
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
        """)

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
              <div class="kpi kb">📦 Filtered Ordered: {round_and_format(tot_o)}</div>
              <div class="kpi kg">✅ Filtered Received: {round_and_format(tot_r)}</div>
              <div class="kpi {'kr' if tot_b<0 else 'ka'}">⚖️ Filtered Balance: {round_and_format(tot_b)}</div>
            </div>""", unsafe_allow_html=True)

            st.markdown("##### 🏷️ Item-Wise Remaining Status (Filtered Global Summary)")
            item_summary = df_ledger.groupby("Item Type")["Remaining Balance"].sum().to_dict()
        
            color_classes = {
                "Inlay Card / Bandrolle": "kb", "Tag Card / Barcode Sticker": "kg",
                "Barcode Item": "kr", "Safety": "ka", "Washing Paper": "kp",
                "Transparent Sticker": "ke", "Eco Friendly": "kb"
            }
        
            item_cols = st.columns(3)
            for idx, it_name in enumerate(ITEM_TYPES):
                rem_val = item_summary.get(it_name, 0)
                cls = color_classes.get(it_name, "kb")
                with item_cols[idx % 3]:
                    st.markdown(f'<div class="kpi {cls}" style="margin-bottom: 6px;">⏳ Rem. {it_name}<br><b>{round_and_format(rem_val)}</b> Pcs</div>', unsafe_allow_html=True)

            st.markdown("---")
            st.markdown("### 📌 CONTRACT-WISE SHORTFALL SUMMARY")
            
            unique_contracts_in_ledger = sorted(df_ledger["Contract #"].unique().tolist())
            for contract_no in unique_contracts_in_ledger:
                df_contract_sub = df_ledger[df_ledger["Contract #"] == contract_no]
                contract_item_summary = df_contract_sub.groupby("Item Type")["Remaining Balance"].sum().to_dict()
                has_shortage = any(v > 0 for v in contract_item_summary.values())
            
                with st.expander(f"📄 Contract: {contract_no} | {'🚨 Shortage Pending' if has_shortage else '✅ Fully Cleared'}", expanded=True):
                    sub_cols = st.columns(3)
                    col_counter = 0
                    for it_name in ITEM_TYPES:
                        rem_val_sub = contract_item_summary.get(it_name, 0)
                        sub_cls = "kr" if rem_val_sub > 0 else "kg"
                    
                        with sub_cols[col_counter % 3]:
                            st.markdown(f'''
                            <div class="kpi {sub_cls}" style="margin-bottom: 6px; padding: 8px; border-radius: 6px;">
                                <span style="font-size: 11px; font-weight: 600; display:block;">📦 {it_name}</span>
                                <span style="font-size: 13px; font-weight: 700;">Rem: {round_and_format(rem_val_sub)} Pcs</span>
                            </div>
                            ''', unsafe_allow_html=True)
                        col_counter += 1

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
                st.download_button(label="🖨️ Print Full Master Ledger (PDF)", data=pdf_buf_master, file_name="Master_Ledger.pdf", mime="application/pdf", type="primary")
            with btn_c2:
                df_shortage_only = df_ledger[df_ledger["Remaining Balance"].apply(round_bal) > 0]
                pdf_buf_shortage = generate_ledger_pdf(item_summary, df_shortage_only, l_sel_coff, l_sel_cont, l_sel_art, report_type="SHORTAGE")
                st.download_button(label="🚨 Print Shortage Report (PDF)", data=pdf_buf_shortage, file_name="Shortage_Report.pdf", mime="application/pdf", type="secondary")
            with btn_c3:
                df_contract_shortlist = df_ledger[df_ledger["Remaining Balance"].apply(round_bal) > 0]
                pdf_buf_contract = generate_ledger_pdf(item_summary, df_contract_shortlist, l_sel_coff, l_sel_cont, l_sel_art, report_type="CONTRACT_SHORTLIST")
                st.download_button(label="📋 Print Contract Shortlist (PDF)", data=pdf_buf_contract, file_name="Contract_Shortlist.pdf", mime="application/pdf", type="secondary")

# ═══════════════════════════════════════════════
# TAB 5 — SHEET UPLOAD (FUTURE PROOF MAPPING)
# ═══════════════════════════════════════════════
with tab5:
    if _access_ok("📤 Sheet Upload"):
        st.markdown('<div class="sec">📤 Upload Sale Contract Sheet (Auto Match System)</div>', unsafe_allow_html=True)
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
                                search_names = ["transparentsticker", "pricesticker", "roundsticker", "transparent", "sticker"]
                        
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
                    st.success(f"🎉 Call-Off {u_coff.strip()} saved successfully with {saved} records!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error parsing sheet: {e}")

# ═══════════════════════════════════════════════
# TAB 6 — BILTY MANAGEMENT
# ═══════════════════════════════════════════════
with tab6:
    if _access_ok("🚚 Bilty Management"):
        st.markdown('<div class="sec">🚚 Bilty Management — Lahore Factory Dispatch</div>', unsafe_allow_html=True)

        b_c1, b_c2 = st.columns([2, 2])
        with b_c1:
            b_coff_opts = get_calloff_list()
            b_sel_coff = st.selectbox("Select Call-Off Sheet *", ["-- Select --"] + b_coff_opts, key="bilty_coff")

        b_sel_cont = "-- Select --"
        if b_sel_coff != "-- Select --":
            cont_opts = q("SELECT DISTINCT sale_contract FROM sheet_orders WHERE call_off_no=? AND TRIM(sale_contract)!='' ORDER BY sale_contract", [b_sel_coff])["sale_contract"].tolist()
            with b_c2:
                b_sel_cont = st.selectbox("Select Sales Contract *", ["-- Select --"] + cont_opts, key="bilty_cont")

        if b_sel_coff == "-- Select --" or b_sel_cont == "-- Select --":
            st.info("👆 Please select a Call-Off Sheet and Sales Contract.")
        else:
            df_arts = q("SELECT article, category, SUM(order_qty) AS sheet_qty FROM sheet_orders WHERE call_off_no=? AND sale_contract=? GROUP BY article, category ORDER BY article, category", [b_sel_coff, b_sel_cont])

            if df_arts.empty:
                st.info("No articles found.")
            else:
                grand_total = 0.0
                ticked_items = []

                for art, grp in df_arts.groupby("article"):
                    with st.expander(f"📦 Article: {art}", expanded=True):
                        tcols = st.columns(3)
                        for i, (_, r_row) in enumerate(grp.iterrows()):
                            cat = r_row["category"]
                            cat_qty = float(r_row["sheet_qty"] or 0)
                            cb_key = f"bilty_cb_{b_sel_coff}_{b_sel_cont}_{art}_{cat}"
                            mq_key = f"bilty_manualqty_{b_sel_coff}_{b_sel_cont}_{art}_{cat}"
                            with tcols[i % 3]:
                                is_checked = st.checkbox(f"{cat} ({cat_qty:,.0f} Pcs)", key=cb_key)
                                manual_qty = st.number_input(f"Qty for {cat}", min_value=0.0, max_value=cat_qty, value=cat_qty, step=1.0, key=mq_key, disabled=not is_checked, label_visibility="collapsed")
                            if is_checked:
                                grand_total += manual_qty
                                ticked_items.append((art, cat, manual_qty))

                st.markdown(f'<div class="kpi-row"><div class="kpi kp">🧮 Live Grand Total: {grand_total:,.0f} Pcs</div></div>', unsafe_allow_html=True)

                bc1, bc2, bc3 = st.columns(3)
                with bc1: cartons_n = st.number_input("Number of Cartons *", min_value=0, step=1, key="bilty_cartons")
                with bc2: transport_mode = st.selectbox("Transport Mode *", ["By Air", "By Train"], key="bilty_transport")
                with bc3: bilty_date_val = st.date_input("Bilty Date *", value=date.today(), key="bilty_date")

                if st.button("🚚 Save Bilty Record", type="primary", key="bilty_save"):
                    if not ticked_items or cartons_n <= 0:
                        st.error("⚠️ Items tick karein aur zero se zyada cartons batayein.")
                    else:
                        conn = get_conn()
                        for art, cat, cat_qty in ticked_items:
                            conn.execute("INSERT INTO bilty (call_off_no, contract_no, article, category, qty, cartons, transport_mode, bilty_date, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                                         (b_sel_coff, b_sel_cont, art, cat, float(cat_qty), int(cartons_n), transport_mode, str(bilty_date_val), str(datetime.datetime.now())))
                        conn.commit(); conn.close()
                        st.success("✅ Bilty Saved Successfully!")
                        st.rerun()

# ═══════════════════════════════════════════════
# TAB 7 — USER MANAGEMENT
# ═══════════════════════════════════════════════
with tab7:
    if _access_ok("👤 User Management"):
        st.markdown('<div class="sec">👤 User Management — Admin Only</div>', unsafe_allow_html=True)
        with st.form("create_user_form", clear_on_submit=True):
            nu_c1, nu_c2 = st.columns(2)
            with nu_c1:
                nu_username = st.text_input("Username *")
                nu_fullname = st.text_input("Full Name *")
            with nu_c2:
                nu_password = st.text_input("Password *", type="password")
                nu_role = st.selectbox("Role *", ["Admin", "Data Entry", "Viewer", "CEO"])
            if st.form_submit_button("➕ Create User"):
                if nu_username and nu_password:
                    try:
                        conn = get_conn()
                        conn.execute("INSERT INTO app_users (username, password_hash, role, full_name, created_at) VALUES (?,?,?,?,?)",
                                     (nu_username.strip(), _hash_password(nu_password), nu_role, nu_fullname, str(datetime.datetime.now())))
                        conn.commit(); conn.close()
                        st.success("User created successfully!")
                    except: st.error("Username already exists!")
                else: st.error("Fields required!")
