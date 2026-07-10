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
@st.cache_resource(show_spinner=False)
def _get_engine():
    db_url = st.secrets.get("DB_URL", "sqlite:///textile_inventory.db")
    return create_engine(db_url, pool_pre_ping=True, pool_recycle=3600)

def get_conn():
    engine = _get_engine()
    return engine.connect()

def q(sql, params=None):
    if params is None:
        params = []
    engine = _get_engine()
    compiled_sql = sql
    bind_params = {}
    for i, p in enumerate(params):
        placeholder = f"p{i}"
        compiled_sql = compiled_sql.replace("?", f":{placeholder}", 1)
        bind_params[placeholder] = p
    with engine.connect() as conn:
        res = conn.execute(text(compiled_sql), bind_params)
        if res.returns_rows:
            return pd.DataFrame(res.fetchall(), columns=res.keys())
        return pd.DataFrame()

def scalar(sql, params=None):
    df = q(sql, params)
    if not df.empty:
        return df.iloc[0, 0]
    return None

# ═══════════════════════════════════════════════
# SYSTEM INITIALIZATION & DB SCHEMA
# ═══════════════════════════════════════════════
def init_db():
    engine = _get_engine()
    with engine.connect() as conn:
        # Create core application tables if not exist
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_users (
                username TEXT PRIMARY KEY,
                password_hash TEXT,
                full_name TEXT,
                role TEXT,
                created_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sheet_orders (
                id SERIAL PRIMARY KEY,
                call_off_no TEXT,
                sale_contract TEXT,
                brand TEXT,
                article TEXT,
                category TEXT,
                order_qty NUMERIC,
                uploaded_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS inventory (
                id SERIAL PRIMARY KEY,
                dc_no TEXT,
                dc_date TEXT,
                gate_pass TEXT,
                vehicle_no TEXT,
                driver_name TEXT,
                call_off_no TEXT,
                article TEXT,
                category TEXT,
                qty NUMERIC,
                remarks TEXT,
                operator TEXT,
                created_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bilty_records (
                id SERIAL PRIMARY KEY,
                bilty_no TEXT,
                bilty_date TEXT,
                transporter TEXT,
                cartons NUMERIC,
                weight NUMERIC,
                destination TEXT,
                dc_nos_linked TEXT,
                remarks TEXT,
                operator TEXT,
                created_at TEXT
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS system_visits (
                id SERIAL PRIMARY KEY,
                visit_time TEXT
            )
        """))
        
        # Verify default superuser account
        chk = conn.execute(text("SELECT COUNT(*) FROM app_users WHERE username = :u"), {"u": "admin"}).scalar()
        if chk == 0:
            hp = hashlib.sha256("admin123".encode()).hexdigest()
            conn.execute(text("""
                INSERT INTO app_users (username, password_hash, full_name, role, created_at)
                VALUES (:u, :hp, :fn, :r, :ca)
            """), {"u": "admin", "hp": hp, "fn": "System Administrator", "r": "Admin", "ca": str(datetime.datetime.now())})
        conn.commit()

init_db()

# ═══════════════════════════════════════════════
# VISIT COUNTER LOGIC
# ═══════════════════════════════════════════════
if "visited" not in st.session_state:
    st.session_state["visited"] = True
    try:
        conn_v = get_conn()
        conn_v.execute(text("INSERT INTO system_visits (visit_time) VALUES (:t)"), {"t": str(datetime.datetime.now())})
        conn_v.commit()
        conn_v.close()
    except Exception:
        pass

total_visits = scalar("SELECT COUNT(*) FROM system_visits") or 1

# ═══════════════════════════════════════════════
# APP CONFIGURATION & STYLING
# ═══════════════════════════════════════════════
st.set_page_config(page_title="Vertex Packaging — Factory Panel", layout="wide", initial_sidebar_state="expanded")

# Inject Custom Professional Layout CSS Styles
st.markdown("""
<style>
    .reportview-container .main .block-container { max-width: 95%; padding-top: 1rem; }
    .main-header { font-size:28px; font-weight:800; color:#1E3A8A; margin-bottom: 2px; text-transform: uppercase; letter-spacing: 1px; }
    .sub-header { font-size:14px; color:#556B2F; margin-bottom: 18px; font-weight: 500; }
    .sec { background-color: #F0F4F8; padding: 10px 14px; border-left: 5px solid #1E3A8A; font-weight: 700; font-size: 16px; color: #1E3A8A; margin-bottom: 15px; border-radius: 0 4px 4px 0; }
    .kpi-row { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 15px; }
    .kpi { flex: 1; min-width: 180px; padding: 12px 16px; border-radius: 8px; color: white; font-size: 14px; font-weight: 500; box-shadow: 0 2px 4px rgba(0,0,0,0.08); }
    .kb { background: linear-gradient(135deg, #1E3A8A, #3B82F6); }
    .kg { background: linear-gradient(135deg, #115E59, #14B8A6); }
    .kr { background: linear-gradient(135deg, #9F1239, #F43F5E); }
    .ka { background: linear-gradient(135deg, #B45309, #F59E0B); }
    .kp { background: linear-gradient(135deg, #6B21A8, #A855F7); }
    .ke { background: linear-gradient(135deg, #374151, #4B5563); }
    .footer { position: fixed; left: 0; bottom: 0; width: 100%; background-color: #F8FAFC; color: #64748B; text-align: center; padding: 6px; font-size: 12px; font-weight: 600; border-top: 1px solid #E2E8F0; z-index: 100; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] { background-color: #F1F5F9; border: 1px solid #E2E8F0; padding: 8px 16px; border-radius: 6px 6px 0 0; font-weight: 600; color: #475569; }
    .stTabs [data-baseweb="tab"]:hover { background-color: #E2E8F0; }
    .stTabs [data-baseweb="tab"][aria-selected="true"] { background-color: #1E3A8A !important; color: white !important; border-color: #1E3A8A; }
</style>
""", unsafe_allow_html=True)

# Global Configuration Parameters
ITEM_TYPES = [
    "Inlay Card / Bandrolle",
    "Tag Card / Barcode Sticker",
    "Barcode Item",
    "Safety",
    "Washing Paper",
    "Transparent Sticker",
    "Eco Friendly"
]

def round_bal(val):
    if val is None: return 0
    return int(math.floor(val)) if val >= 0 else int(math.ceil(val))

def round_and_format(val):
    if val is None: return "0"
    v = int(math.floor(val)) if val >= 0 else int(math.ceil(val))
    return f"{v:,}"

# ═══════════════════════════════════════════════
# AUTHENTICATION ENGINE
# ═══════════════════════════════════════════════
if "auth_user" not in st.session_state:
    st.session_state["auth_user"] = None

def do_login(u, p):
    hp = hashlib.sha256(p.encode()).hexdigest()
    res = q("SELECT username, full_name, role FROM app_users WHERE username=? AND password_hash=?", [u, hp])
    if not res.empty:
        st.session_state["auth_user"] = {
            "username": res.iloc[0]["username"],
            "full_name": res.iloc[0]["full_name"],
            "role": res.iloc[0]["role"]
        }
        return True
    return False

def do_logout():
    st.session_state["auth_user"] = None
    st.rerun()

current_user = st.session_state["auth_user"]

# ═══════════════════════════════════════════════
# ROLE-BASED ACCESS CONTROL
# ═══════════════════════════════════════════════
ROLE_PERMISSIONS = {
    "Admin": ["🔍 Global Search", "📦 Inventory Ledger", "📝 DC Entry", "📊 Master Ledger", "📤 Upload Contracts", "🚛 Bilty Entry", "⚙️ User Management"],
    "CEO": ["🔍 Global Search", "📦 Inventory Ledger", "📊 Master Ledger", "🚛 Bilty Entry"],
    "DC Operator": ["🔍 Global Search", "📦 Inventory Ledger", "📝 DC Entry", "🚛 Bilty Entry"]
}

def _access_ok(module_name):
    if not current_user: return False
    perms = ROLE_PERMISSIONS.get(current_user["role"], [])
    return module_name in perms

# ═══════════════════════════════════════════════
# PDF REPORT GENERATOR ENGINE
# ═══════════════════════════════════════════════
def generate_ledger_pdf(global_summary, df_data, filter_coff, filter_cont, filter_art, report_type="MASTER"):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=30)
    story = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontSize=18, textColor=colors.HexColor('#1E3A8A'), spaceAfter=4, alignment=1)
    meta_style = ParagraphStyle('MetaStyle', parent=styles['Normal'], fontSize=9, textColor=colors.HexColor('#475569'), alignment=1, spaceAfter=15)
    section_title = ParagraphStyle('SecTitle', parent=styles['Heading2'], fontSize=11, textColor=colors.HexColor('#0F172A'), spaceBefore=8, spaceAfter=6)
    cell_text = ParagraphStyle('CellText', parent=styles['Normal'], fontSize=8, textColor=colors.HexColor('#334155'))
    cell_header = ParagraphStyle('CellHeader', parent=styles['Normal'], fontSize=8, fontName='Helvetica-Bold', textColor=colors.white)

    # Header Titles
    r_title = "MASTER INVENTORY LEDGER REPORT"
    if report_type == "SHORTAGE":
        r_title = "PENDING SHORTFALL & REMAINING MATERIAL REPORT"
    elif report_type == "CONTRACT_SHORTLIST":
        r_title = "CONTRACT-WISE RUNNING SHORTLIST"

    story.append(Paragraph(f"<b>{r_title}</b>", title_style))
    story.append(Paragraph(f"Generated on: {datetime.datetime.now().strftime('%Y-%m-%d %I:%M %p')} | Scope Filters: CO={filter_coff}, Contract={filter_cont}, Article={filter_art}", meta_style))
    
    # Global Consolidated Box
    story.append(Paragraph("<b>Consolidated Remaining Material Balances</b>", section_title))
    sum_data = [[Paragraph("<b>Item Category</b>", cell_header), Paragraph("<b>Net Remaining Balance (Pcs)</b>", cell_header)]]
    for k, v in global_summary.items():
        sum_data.append([Paragraph(k, cell_text), Paragraph(round_and_format(v), cell_text)])
    
    sum_table = Table(sum_data, colWidths=[250, 150])
    sum_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#1E3A8A')),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#CBD5E1')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#F8FAFC'), colors.white])
    ]))
    story.append(sum_table)
    story.append(Spacer(1, 15))
    
    # Main Line Item Table Breakdowns
    story.append(Paragraph("<b>Itemized Ledger Records Account Details</b>", section_title))
    main_headers = ["Call-Off", "Contract", "Brand", "Article", "Item Category", "Ordered", "Received", "Remaining"]
    main_data = [[Paragraph(f"<b>{h}</b>", cell_header) for h in main_headers]]
    
    for _, r in df_data.iterrows():
        rem_v = r["Remaining Balance"]
        rem_str = "" if (rem_v == 0 and report_type == "MASTER") else round_and_format(rem_v)
        
        main_data.append([
            Paragraph(str(r["Call-Off No"]), cell_text),
            Paragraph(str(r["Contract #"]), cell_text),
            Paragraph(str(r["Brand"]), cell_text),
            Paragraph(str(r["Article"]), cell_text),
            Paragraph(str(r["Item Type"]), cell_text),
            Paragraph(round_and_format(r["Total Ordered"]), cell_text),
            Paragraph(round_and_format(r["Total Received"]), cell_text),
            Paragraph(rem_str, cell_text)
        ])
        
    main_table = Table(main_data, colWidths=[55, 60, 65, 75, 120, 60, 60, 65])
    main_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#334155')),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#E2E8F0')),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.HexColor('#F1F5F9'), colors.white])
    ]))
    story.append(main_table)
    
    def add_footer(canvas, doc):
        canvas.saveState()
        canvas.setFont('Helvetica-Bold', 8)
        canvas.setFillColor(colors.HexColor('#64748B'))
        canvas.drawString(20, 15, "🏭 NABA TECH BY KALEEM ULLAH SHARIF  |  Customer: Vertex Packaging (CEO: Shahzad Bhai)")
        canvas.drawRightString(letter[0]-20, 15, f"Page {doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=add_footer, onLaterPages=add_footer)
    buf.seek(0)
    return buf.getvalue()

# ═══════════════════════════════════════════════
# SCREEN INTERFACE ROUTER
# ═══════════════════════════════════════════════
if not current_user:
    # Render Login Terminal Interface Window
    st.markdown('<div class="main-header" style="text-align:center; margin-top:8%;">VERTEX PACKAGING</div>', unsafe_allow_html=True)
    st.markdown('<div class="sub-header" style="text-align:center;">FACTORY INVENTORY CONTROL TERMINAL — TRIAL VERSION</div>', unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1.5, 2, 1.5])
    with col2:
        with st.form("login_form", clear_on_submit=False):
            st.markdown("##### 🔐 Secure Operational Sign-In")
            u_input = st.text_input("Username", key="login_u")
            p_input = st.text_input("Password", type="password", key="login_p")
            sub_btn = st.form_submit_button("Authenticate Secure Session", use_container_width=True)
            if sub_btn:
                if do_login(u_input, p_input):
                    st.success("Authorized. Initializing application data...")
                    st.rerun()
                else:
                    st.error("Invalid credentials or unauthorized access protocol.")
    st.stop()

# ═══════════════════════════════════════════════
# APPLICATION HEADER & TOP RUNTIME ALERTS
# ═══════════════════════════════════════════════
st.warning("⚠️ **NOTICE:** This software platform is currently running on a **Trial Basis** for assessment and dry-testing operations.")

# Dynamic User Activity Greeting Box (Hiding Admin, Showing Operators/CEO)
user_display_string = ""
if current_user["role"] != "Admin":
    user_display_string = f"👤 **Active Session:** {current_user['full_name']} ({current_user['role']}) &nbsp;|&nbsp; "

st.markdown(f"""
<div style="display:flex; justify-content:space-between; align-items:center; background:#F8FAFC; padding:10px 20px; border-radius:8px; border:1px solid #E2E8F0; margin-bottom:15px;">
    <div>
        <span style="font-size:24px; font-weight:800; color:#1E3A8A;">VERTEX PACKAGING</span><br>
        <span style="font-size:12px; color:#556B2F; font-weight:600;">CEO: Shahzad Bhai &nbsp;|&nbsp; Total Platform Hits: {total_visits}</span>
    </div>
    <div style="font-size:13px; font-weight:600; color:#334155;">
        {user_display_string}
        <span style="color:#9F1239; cursor:pointer;">🔒 Secure Interface Active</span>
    </div>
</div>
""", unsafe_allow_html=True)

if st.sidebar.button("🚪 Terminate Session (Logout)", key="global_logout_btn"):
    do_logout()

# Build Application Modular Navigation Tabs
all_tabs = ["🔍 Global Search", "📦 Inventory Ledger", "📝 DC Entry", "📊 Master Ledger", "📤 Upload Contracts", "🚛 Bilty Entry", "⚙️ User Management"]
active_tabs_names = [t for t in all_tabs if _access_ok(t)]
ui_tabs = st.tabs(active_tabs_names)

tab_map = {name: ui_tabs[i] for i, name in enumerate(active_tabs_names)}

# ═══════════════════════════════════════════════
# FEATURE MODULE 1: GLOBAL SEARCH SYSTEM
# ═══════════════════════════════════════════════
if "🔍 Global Search" in tab_map:
    with tab_map["🔍 Global Search"]:
        st.markdown('<div class="sec">🔍 Consolidated Engine Global Query Search</div>', unsafe_allow_html=True)
        g_search = st.text_input("Enter Contract Number, Article Code, Bilty ID or DC Key:", key="g_search_input").strip()
        
        if g_search:
            st.markdown(f"#### 📡 Query Evaluation Matrix Analysis for: `{g_search}`")
            
            # Sub-Requirement A: Balance Type & Remaining Material Status
            st.markdown("##### 📊 A. Material Allocation & Remaining Ledger Balances")
            query_b = """
                SELECT 
                    so.sale_contract AS "Contract #", so.article AS "Article", so.category AS "Item Type",
                    SUM(so.order_qty) AS "Ordered",
                    COALESCE(inv.total_received, 0) AS "Received",
                    SUM(so.order_qty) - COALESCE(inv.total_received, 0) AS "Remaining Balance"
                FROM sheet_orders so
                LEFT JOIN (
                    SELECT call_off_no, article, category, SUM(qty) AS total_received FROM inventory GROUP BY call_off_no, article, category
                ) inv ON so.call_off_no = inv.call_off_no AND so.article = inv.article AND so.category = inv.category
                WHERE so.sale_contract LIKECASE ? OR so.article LIKECASE ?
                GROUP BY so.sale_contract, so.article, so.category
            """.replace("LIKECASE", "ILIKE" if "postgresql" in st.secrets.get("DB_URL", "") else "LIKE")
            
            df_b = q(query_b, [f"%{g_search}%", f"%{g_search}%"])
            if not df_b.empty:
                st.dataframe(df_b, use_container_width=True, hide_index=True)
            else:
                st.caption("No dynamic order balances matched for Contract or Article codes.")
                
            # Sub-Requirement B: Delivery Challan History Logs
            st.markdown("##### 📝 B. Linked Delivery Challan (DC) Logs & Inward Receipts")
            query_dc = """
                SELECT dc_no AS "DC Number", dc_date AS "DC Date", call_off_no AS "Call-Off No", 
                       article AS "Article", category AS "Item Category", qty AS "Quantity Received", 
                       operator AS "Received By"
                FROM inventory 
                WHERE dc_no LIKECASE ? OR article LIKECASE ? OR call_off_no LIKECASE ?
                ORDER BY dc_date DESC
            """.replace("LIKECASE", "ILIKE" if "postgresql" in st.secrets.get("DB_URL", "") else "LIKE")
            
            df_dc = q(query_dc, [f"%{g_search}%", f"%{g_search}%", f"%{g_search}%"])
            if not df_dc.empty:
                st.dataframe(df_dc, use_container_width=True, hide_index=True)
            else:
                st.caption("No Delivery Challan entries tracked for this query.")

            # Sub-Requirement C: Linked Bilty Dispatches
            st.markdown("##### 🚛 C. Outward Bilty Fleet Dispatches & Cargo Linking")
            query_bl = """
                SELECT bilty_no AS "Bilty No", bilty_date AS "Bilty Date", transporter AS "Transporter", 
                       cartons AS "Cartons", destination AS "Destination", dc_nos_linked AS "Linked DCs"
                FROM bilty_records
                WHERE bilty_no LIKECASE ? OR dc_nos_linked LIKECASE ?
                ORDER BY bilty_date DESC
            """.replace("LIKECASE", "ILIKE" if "postgresql" in st.secrets.get("DB_URL", "") else "LIKE")
            
            df_bl = q(query_bl, [f"%{g_search}%", f"%{g_search}%"])
            if not df_bl.empty:
                st.dataframe(df_bl, use_container_width=True, hide_index=True)
            else:
                st.caption("No logistics outbound bilty dispatches bound to this specific query parameters.")

# ═══════════════════════════════════════════════
# FEATURE MODULE 2: INVENTORY LEDGER REGISTER
# ═══════════════════════════════════════════════
if "📦 Inventory Ledger" in tab_map:
    with tab_map["📦 Inventory Ledger"]:
        st.markdown('<div class="sec">📦 Raw Ledger Log Register View</div>', unsafe_allow_html=True)
        df_inv = q("SELECT dc_no, dc_date, gate_pass, vehicle_no, driver_name, call_off_no, article, category, qty, remarks, operator, created_at FROM inventory ORDER BY id DESC")
        if df_inv.empty:
            st.info("No recorded material inward entries exist in database logs.")
        else:
            st.dataframe(df_inv, use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════
# FEATURE MODULE 3: DC ENTRY WIDGET ENGINE
# ═══════════════════════════════════════════════
if "📝 DC Entry" in tab_map:
    with tab_map["📝 DC Entry"]:
        st.markdown('<div class="sec">📝 Inward Delivery Challan Receipt Record System</div>', unsafe_allow_html=True)
        
        with st.form("dc_form", clear_on_submit=True):
            c1, c2, c3, c4 = st.columns(4)
            with c1: f_dc = st.text_input("DC Number / Key", key="fdc")
            with c2: f_dt = st.date_input("Challan Date", date.today(), key="fdt")
            with c3: f_gp = st.text_input("Gate Pass Refer", key="fgp")
            with c4: f_vh = st.text_input("Vehicle Plate Number", key="fvh")
            
            c5, c6 = st.columns(2)
            with c5: f_dr = st.text_input("Driver Name Reference", key="fdr")
            with c6: f_rm = st.text_input("Operational Remarks / Annotations", key="frm")
            
            st.markdown("---")
            st.markdown("##### 📦 Line Item Processing Stream")
            
            df_ops = q("SELECT DISTINCT call_off_no FROM sheet_orders ORDER BY call_off_no DESC")
            l_coff = df_ops["call_off_no"].tolist() if not df_ops.empty else []
            
            sel_coff = st.selectbox("Select Target Call-Off Contract", ["-- Select --"] + l_coff, key="dc_co_sel")
            
            l_art = []
            if sel_coff != "-- Select --":
                df_art = q("SELECT DISTINCT article FROM sheet_orders WHERE call_off_no=? ORDER BY article ASC", [sel_coff])
                l_art = df_art["article"].tolist() if not df_art.empty else []
                
            sel_art = st.selectbox("Select Article Key Code", ["-- Select --"] + l_art, key="dc_art_sel")
            
            # Live Multi-Category Component Item Breakdown inside the Blue Box Container Style
            if sel_coff != "-- Select --" and sel_art != "-- Select --":
                st.markdown("""<div style='background-color:#EBF8FF; padding:12px; border-radius:8px; border-left:4px solid #3182CE; margin-bottom:15px;'>
                    <h6 style='color:#2B6CB0; margin:0 0 8px 0; font-weight:700;'>💎 Current Accessories Status Summary (Selected Article)</h6>""", unsafe_allow_html=True)
                
                # Fetch contract vs current received values for this specific article
                query_art_breakdown = """
                    SELECT so.category, SUM(so.order_qty) AS ordered, COALESCE(inv.received, 0) AS received
                    FROM sheet_orders so
                    LEFT JOIN (
                        SELECT call_off_no, article, category, SUM(qty) AS received FROM inventory GROUP BY call_off_no, article, category
                    ) inv ON so.call_off_no = inv.call_off_no AND so.article = inv.article AND so.category = inv.category
                    WHERE so.call_off_no = ? AND so.article = ?
                    GROUP BY so.category
                """
                df_ab = q(query_art_breakdown, [sel_coff, sel_art])
                if not df_ab.empty:
                    cols_ab = st.columns(3)
                    for idx, r_ab in df_ab.iterrows():
                        rem_p = round_bal(r_ab["ordered"] - r_ab["received"])
                        with cols_ab[idx % 3]:
                            st.write(f"🔹 **{r_ab['category']}:** Balance Pcs: `{round_and_format(rem_p)}` (Rec: {round_and_format(r_ab['received'])})")
                else:
                    st.caption("No matching specification limits loaded.")
                st.markdown("</div>", unsafe_allow_html=True)
            
            sel_cat = st.selectbox("Assign Inventory Material Group", ["-- Select --"] + ITEM_TYPES, key="dc_cat_sel")
            f_qty = st.number_input("Inward Receipt Quantity Count (Pcs)", min_value=0, step=1, key="dc_qty_val")
            
            submit_dc = st.form_submit_button("Log Inward Receipt Transaction", type="primary")
            if submit_dc:
                if f_dc and sel_coff != "-- Select --" and sel_art != "-- Select --" and sel_cat != "-- Select --" and f_qty > 0:
                    conn = get_conn()
                    conn.execute(text("""
                        INSERT INTO inventory (dc_no, dc_date, gate_pass, vehicle_no, driver_name, call_off_no, article, category, qty, remarks, operator, created_at)
                        VALUES (:dc, :dt, :gp, :vh, :dr, :co, :ar, :ca, :qt, :rm, :op, :cr)
                    """), {"dc": f_dc, "dt": str(f_dt), "gp": f_gp, "vh": f_vh, "dr": f_dr, "co": sel_coff, "ar": sel_art, "ca": sel_cat, "qt": f_qty, "rm": f_rm, "op": current_user["full_name"], "cr": str(datetime.datetime.now())})
                    conn.commit()
                    conn.close()
                    st.success(f"✅ Inward Receipt Transaction Logged for DC {f_dc} successfully.")
                    st.rerun()
                else:
                    st.error("Validation breakdown. Check parameters constraint limits fields.")

# ═══════════════════════════════════════════════
# FEATURE MODULE 4: DYNAMIC MASTER LEDGER REGISTER
# ═══════════════════════════════════════════════
if "📊 Master Ledger" in tab_map:
    with tab_map["📊 Master Ledger"]:
        st.markdown('<div class="sec">📊 Master Ledger (Order vs Delivery Status)</div>', unsafe_allow_html=True)

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
        
        df_ledger_raw = q(query_ledger)

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

            st.markdown("##### 🏷️ Item-Wise Remaining Status (Filtered Global Summary)")
            item_summary = df_ledger.groupby("Item Type")["Remaining Balance"].sum().to_dict()
            
            color_classes = {
                "Inlay Card / Bandrolle": "kb",
                "Tag Card / Barcode Sticker": "kg",
                "Barcode Item": "kr",
                "Safety": "ka",
                "Washing Paper": "kp",
                "Transparent Sticker": "ke",
                "Eco Friendly": "kb"
            }
            
            item_cols = st.columns(3)
            for idx, it_name in enumerate(ITEM_TYPES):
                rem_val = item_summary.get(it_name, 0)
                cls = color_classes.get(it_name, "kb")
                with item_cols[idx % 3]:
                    st.markdown(f'<div class="kpi {cls}" style="margin-bottom: 6px;">⏳ Rem. {it_name}<br><b>{round_and_format(rem_val)}</b> Pcs</div>', unsafe_allow_html=True)

            st.markdown("---")
            st.markdown("### 📌 CONTRACT-WISE SHORTFALL SUMMARY")
            st.info(" نیچے ہر سیلز کنٹریکٹ کے حساب سے الگ الگ بقایا (Shortfall) بریک ڈاؤن دکھایا گیا ہے:")
            
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

            st.markdown("##### Export options:")
            btn_c1, btn_c2, btn_c3 = st.columns([2.5, 3, 3.2])
            
            with btn_c1:
                pdf_buf_master = generate_ledger_pdf(item_summary, df_ledger, l_sel_coff, l_sel_cont, l_sel_art, report_type="MASTER")
                st.download_button(
                    label="Print Full Master Ledger (PDF)",
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
                    label="Print Shortage / Remaining Only (PDF)",
                    data=pdf_buf_shortage,
                    file_name=f"Shortage_Report_CO_{l_sel_coff}_CN_{l_sel_cont}.pdf",
                    mime="application/pdf",
                    type="secondary",
                    key="btn_print_shortage"
                )

            with btn_c3:
                df_contract_shortlist = df_ledger[df_ledger["Remaining Balance"].apply(round_bal) > 0]
                pdf_buf_contract = generate_ledger_pdf(item_summary, df_contract_shortlist, l_sel_coff, l_sel_cont, l_sel_art, report_type="CONTRACT_SHORTLIST")
                safe_brand = (l_sel_br if l_sel_br != "All" else "AllBrands")
                safe_cont  = (l_sel_cont if l_sel_cont != "All" else "AllContracts")
                st.download_button(
                    label="Print Contract Shortlist (PDF)",
                    data=pdf_buf_contract,
                    file_name=f"Contract_Shortlist_{safe_brand}_{safe_cont}.pdf",
                    mime="application/pdf",
                    type="secondary",
                    key="btn_print_contract_shortlist"
                )

# ═══════════════════════════════════════════════
# FEATURE MODULE 5: CONTRACT SHEET UPLOADER ENGINE
# ═══════════════════════════════════════════════
if "📤 Upload Contracts" in tab_map:
    with tab_map["📤 Upload Contracts"]:
        st.markdown('<div class="sec">📤 Upload Production Sales Order Contract Specification Sheet</div>', unsafe_allow_html=True)
        st.info("💡 **Requirement Specification Format:** Upload an Excel workbook file containing `Call-Off No`, `Sale Contract`, `Brand`, `Article Code` and mapping category column headers.")
        
        up_file = st.file_uploader("Choose Sales Contract Specification Sheet (Excel format .xlsx)", type=["xlsx"])
        if up_file:
            try:
                xls = pd.ExcelFile(up_file)
                sh_names = xls.sheet_names
                sel_sheet = st.selectbox("Select Target Specification Sheet Data Workspace Tab", sh_names)
                
                df_raw = pd.read_excel(up_file, sheet_name=sel_sheet)
                st.markdown("##### 📁 Ingested Specification Stream Preview Raw Structure")
                st.dataframe(df_raw.head(5), use_container_width=True)
                
                col_mapping = {}
                headers = df_raw.columns.tolist()
                
                st.markdown("##### 🗺️ Map Workbook Headers into Core Platform Structure Keys")
                mc1, mc2, mc3 = st.columns(3)
                with mc1: col_mapping["co"] = st.selectbox("Call-Off No mapping", headers, index=headers.index("Call-Off No") if "Call-Off No" in headers else 0)
                with mc2: col_mapping["sc"] = st.selectbox("Sale Contract mapping", headers, index=headers.index("Sale Contract") if "Sale Contract" in headers else 0)
                with mc3: col_mapping["br"] = st.selectbox("Brand mapping", headers, index=headers.index("Brand") if "Brand" in headers else 0)
                
                mc4, mc5 = st.columns(2)
                with mc4: col_mapping["ar"] = st.selectbox("Article Code mapping", headers, index=headers.index("Article Code") if "Article Code" in headers else 0)
                with mc5: col_mapping["qty"] = st.selectbox("Order Quantity mapping", headers, index=headers.index("Order Qty") if "Order Qty" in headers else 0)
                
                st.markdown("---")
                st.markdown("##### 🏷️ Specific Sub-Category Allocation Limits")
                st.caption("Each row contains multiple accessory categories across different columns. Map each category to the matching column below:")
                
                for item_cat in ITEM_TYPES:
                    default_idx = headers.index(item_cat) if item_cat in headers else 0
                    col_mapping[f"cat_{item_cat}"] = st.selectbox(f"Column for '{item_cat}'", ["-- Skip Category --"] + headers, index=default_idx + 1 if item_cat in headers else 0)
                
                if st.button("🚀 Process & Ingest Specification Matrix into Database Registers", type="primary"):
                    conn = get_conn()
                    records_inserted = 0
                    
                    for _, row in df_raw.iterrows():
                        v_co = str(row[col_mapping["co"]]).strip()
                        v_sc = str(row[col_mapping["sc"]]).strip()
                        v_br = str(row[col_mapping["br"]]).strip()
                        v_ar = str(row[col_mapping["ar"]]).strip()
                        v_base_qty = pd.to_numeric(row[col_mapping["qty"]], errors='coerce')
                        if pd.isna(v_base_qty): v_base_qty = 0
                        
                        if not v_co or v_co == "nan" or not v_ar or v_ar == "nan":
                            continue
                            
                        for item_cat in ITEM_TYPES:
                            map_col = col_mapping[f"cat_{item_cat}"]
                            if map_col != "-- Skip Category --":
                                raw_item_val = row[map_col]
                                if pd.isna(raw_item_val) or str(raw_item_val).strip().lower() in ["nan", "", "-", "0"]:
                                    continue
                                
                                final_qty = v_base_qty
                                
                                conn.execute(text("""
                                    INSERT INTO sheet_orders (call_off_no, sale_contract, brand, article, category, order_qty, uploaded_at)
                                    VALUES (:co, :sc, :br, :ar, :ca, :oq, :ua)
                                """), {"co": v_co, "sc": v_sc, "br": v_br, "ar": v_ar, "ca": item_cat, "oq": final_qty, "ua": str(datetime.datetime.now())})
                                records_inserted += 1
                                
                    conn.commit()
                    conn.close()
                    st.success(f"✅ Specification Data Stream Matrix Ingested. Successfully registered {records_inserted} discrete line nodes.")
                    st.rerun()
            except Exception as e:
                st.error(f"Failed parsing target Excel specification matrix workbook sheet file: {e}")

# ═══════════════════════════════════════════════
# FEATURE MODULE 6: BILTY LOGISTICS SYSTEM
# ═══════════════════════════════════════════════
if "🚛 Bilty Entry" in tab_map:
    with tab_map["🚛 Bilty Entry"]:
        st.markdown('<div class="sec">🚛 Outward Fleet Logistics & Bilty Freight Ingest</div>', unsafe_allow_html=True)
        
        # Dual Input Strategy: Option to select automated dispatch or manual overriding entries
        processing_mode = st.radio("Processing Quantities Allocation Strategy Mode:", ["Standard Tick Selection Mode", "Manual Override Input Commentary Adjustment Mode"], horizontal=True)
        
        with st.form("bilty_form", clear_on_submit=True):
            bc1, bc2, bc3 = st.columns(3)
            with bc1: f_bilty = st.text_input("Bilty ID / Consignment Code", key="fbilty")
            with bc2: f_bdate = st.date_input("Dispatch Departure Date", date.today(), key="fbdate")
            with bc3: f_trans = st.text_input("Transporter / Logistics Carrier Name", key="ftrans")
            
            bc4, bc5, bc6 = st.columns(3)
            with bc4: f_cartons = st.number_input("Total Dispatched Cartons Count", min_value=0, step=1, key="fcartons")
            with bc5: f_weight = st.number_input("Gross Cargo Consignment Weight (Kgs)", min_value=0.0, step=0.1, key="fweight")
            with bc6: f_dest = st.text_input("Destination Terminal Location City", key="fdest")
            
            st.markdown("---")
            st.markdown("##### 🔗 Associate Unlinked Delivery Challans (DC) to Outbound Logistics Fleet Voyage")
            
            df_avail_dcs = q("SELECT DISTINCT dc_no, dc_date FROM inventory ORDER BY dc_no ASC")
            
            selected_dc_list = []
            manual_commentary_adjustments = {}
            
            if df_avail_dcs.empty:
                st.caption("No pending Delivery Challan receipts tracked to bind freight documentation.")
            else:
                for idx, r_dc in df_avail_dcs.iterrows():
                    d_no = r_dc["dc_no"]
                    d_dt = r_dc["dc_date"]
                    
                    cc1, cc2 = st.columns([1, 2])
                    with cc1:
                        is_ticked = st.checkbox(f"Challan No: {d_no} ({d_dt})", key=f"chk_dc_{d_no}")
                        if is_ticked:
                            selected_dc_list.append(d_no)
                    with cc2:
                        if processing_mode == "Manual Override Input Commentary Adjustment Mode":
                            manual_val = st.text_input("Manual Commentary Quantity Override / Balance Weight Offset Adjust:", placeholder="Enter manual calculation offsets here...", key=f"txt_manual_{d_no}")
                            if manual_val:
                                manual_commentary_adjustments[d_no] = manual_val

            st.markdown("---")
            f_bremarks = st.text_input("Logistics Freight Manifest Special Remarks", key="fbremarks")
            
            submit_bilty = st.form_submit_button("Generate Outbound Bilty Dispatch Invoice Manifest", type="primary")
            if submit_bilty:
                if f_bilty and f_trans and selected_dc_list:
                    dc_links_str = ", ".join(selected_dc_list)
                    
                    # If manual adjustment commentaries are passed, inject notes directly into the main remarks section securely
                    final_remarks = f_bremarks
                    if manual_commentary_adjustments:
                        adjustment_summary = " | Manual Adjusts: " + "; ".join([f"DC {k}: {v}" for k, v in manual_commentary_adjustments.items()])
                        final_remarks += adjustment_summary
                        
                    conn = get_conn()
                    conn.execute(text("""
                        INSERT INTO bilty_records (bilty_no, bilty_date, transporter, cartons, weight, destination, dc_nos_linked, remarks, operator, created_at)
                        VALUES (:bn, :bd, :tr, :ct, :wt, :ds, :dl, :rm, :op, :cr)
                    """), {"bn": f_bilty, "bd": str(f_bdate), "tr": f_trans, "ct": f_cartons, "wt": f_weight, "ds": f_dest, "dl": dc_links_str, "rm": final_remarks, "op": current_user["full_name"], "cr": str(datetime.datetime.now())})
                    conn.commit()
                    conn.close()
                    st.success(f"✅ Logistics Freight Bilty Manifest {f_bilty} successfully registered.")
                    st.rerun()
                else:
                    st.error("Missing fields or missing bound delivery challans references.")
                    
        st.markdown("---")
        st.markdown("##### 📜 Active Logistics Outbound Fleet manifest Log View")
        df_b_logs = q("SELECT bilty_no, bilty_date, transporter, cartons, weight, destination, dc_nos_linked, remarks, operator FROM bilty_records ORDER BY id DESC")
        if not df_b_logs.empty:
            st.dataframe(df_b_logs, use_container_width=True, hide_index=True)

# ═══════════════════════════════════════════════
# FEATURE MODULE 7: SECURE USER TERMINAL CONTROL
# ═══════════════════════════════════════════════
if "⚙️ User Management" in tab_map:
    with tab_map["⚙️ User Management"]:
        st.markdown('<div class="sec">⚙️ Secure User Access Credentials Profile Configuration</div>', unsafe_allow_html=True)
        
        nu_col, rp_col = st.columns(2)
        
        with nu_col:
            st.markdown("##### ➕ Provision New User Terminal Account Profile")
            with st.form("new_user_form"):
                new_user = st.text_input("Target Username Index ID", key="nu_user")
                new_pass = st.text_input("Secure Profile Password Access Key", type="password", key="nu_pass")
                new_name = st.text_input("Full Identity Human Name String", key="nu_name")
                new_role = st.selectbox("Assign Access Privilege Authorization Protocol Level Role", ["DC Operator", "CEO", "Admin"], key="nu_role")
                
                submit_nu = st.form_submit_button("Provision Terminal Profile Account Nodes", type="primary")
                if submit_nu:
                    if new_user and new_pass and new_name:
                        chk = scalar("SELECT COUNT(*) FROM app_users WHERE username=?", [new_user])
                        if chk > 0:
                            st.error("Account Profile username matches existing key constraint node entry.")
                        else:
                            hp = hashlib.sha256(new_pass.encode()).hexdigest()
                            conn = get_conn()
                            conn.execute(text("""
                                INSERT INTO app_users (username, password_hash, full_name, role, created_at)
                                VALUES (:u, :hp, :fn, :r, :ca)
                            """), {"u": new_user, "hp": hp, "fn": new_name, "r": new_role, "ca": str(datetime.datetime.now())})
                            conn.commit()
                            conn.close()
                            st.success(f"✅ Secure Account Entry registered successfully for context profile identifier '{new_user}'.")
                            st.rerun()
                    else:
                        st.error("Parameters constraints parsing fields required check limits inputs parameters values.")

        with rp_col:
            st.markdown("##### 🔑 Override / Reset Terminal Authorization Access Passwords Keys")
            df_all_u = q("SELECT username FROM app_users")
            all_usernames = df_all_u["username"].tolist() if not df_all_u.empty else []
            
            with st.form("reset_pass_form"):
                rp_user = st.selectbox("Select Target Profile Key Entity Node ID", all_usernames, key="rp_user_sel")
                rp_newpass = st.text_input("Enter New Authorized Secret Credentials Key Pass", type="password", key="rp_newpass_val")
                
                submit_rp = st.form_submit_button("Override Credentials Profile Entry", type="secondary")
                if submit_rp:
                    if rp_user and rp_newpass:
                        hp = hashlib.sha256(rp_newpass.encode()).hexdigest()
                        conn = get_conn()
                        conn.execute(text("UPDATE app_users SET password_hash=? WHERE username=?"), (hp, rp_user))
                        conn.commit()
                        conn.close()
                        st.success(f"✅ Password reset for '{rp_user}'.")

        st.markdown("---")
        st.markdown("##### 📋 All Users")
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
                        conn.execute(text("DELETE FROM app_users WHERE username=:u"), {"u": del_user})
                        conn.commit()
                        conn.close()
                        st.success(f"✅ User '{del_user}' deleted.")
                        st.rerun()
        else:
            st.caption("No other users to delete.")

# ═══════════════════════════════════════════════
# FOOTER TECHNICAL SIGNATURE TRADEMARK METRIC
# ═══════════════════════════════════════════════
st.markdown("""
<div class="footer">
  🏭 NABA TECH BY KALEEM ULLAH SHARIF &nbsp;|&nbsp;
  Customer: Vertex Packaging (CEO: Shahzad Bhai)
</div>
""", unsafe_allow_html=True)
