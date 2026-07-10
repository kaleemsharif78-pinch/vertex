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
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return create_engine(db_url, pool_recycle=1800, pool_pre_ping=True)

def get_conn():
    return _get_engine().connect()

def q(sql, params=None):
    if params is None:
        params = []
    engine = _get_engine()
    sql_clean = sql.replace("?", ":p")
    bind_dict = {}
    for i, val in enumerate(params):
        bind_dict[f"p{i}"] = val
    with engine.connect() as conn:
        res = conn.execute(text(sql_clean), bind_dict)
        if res.returns_rows:
            cols = res.keys()
            rows = res.fetchall()
            return pd.DataFrame([dict(zip(cols, r)) for r in rows])
        else:
            return pd.DataFrame()

def q_exec(sql, params=None):
    if params is None:
        params = []
    engine = _get_engine()
    sql_clean = sql.replace("?", ":p")
    bind_dict = {}
    for i, val in enumerate(params):
        bind_dict[f"p{i}"] = val
    with engine.begin() as conn:
        conn.execute(text(sql_clean), bind_dict)

def scalar(sql, params=None):
    df = q(sql, params)
    if df.empty:
        return None
    return df.iloc[0, 0]

# ═══════════════════════════════════════════════
# TABLES INITIALIZATION
# ═══════════════════════════════════════════════
def init_db():
    engine = _get_engine()
    with engine.begin() as conn:
        # Tables definitions as per working scheme
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_users (
                username VARCHAR(100) PRIMARY KEY,
                password_hash VARCHAR(255),
                full_name VARCHAR(255),
                role VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS sheet_orders (
                id SERIAL PRIMARY KEY,
                call_off_no VARCHAR(100),
                sale_contract VARCHAR(100),
                brand VARCHAR(255),
                article VARCHAR(255),
                category VARCHAR(255),
                order_qty DOUBLE PRECISION,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS inventory (
                id SERIAL PRIMARY KEY,
                date DATE,
                dc_no VARCHAR(100),
                gate_pass VARCHAR(100),
                party VARCHAR(255),
                brand VARCHAR(255),
                call_off_no VARCHAR(100),
                sale_contract VARCHAR(100),
                article VARCHAR(255),
                category VARCHAR(255),
                qty DOUBLE PRECISION,
                ctn DOUBLE PRECISION,
                operator VARCHAR(100),
                remarks TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bilty_dispatches (
                id SERIAL PRIMARY KEY,
                date DATE,
                bilty_no VARCHAR(100),
                transporter VARCHAR(255),
                brand VARCHAR(255),
                call_off_no VARCHAR(100),
                sale_contract VARCHAR(100),
                article VARCHAR(255),
                category VARCHAR(255),
                qty DOUBLE PRECISION,
                ctn DOUBLE PRECISION,
                operator VARCHAR(100),
                remarks TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        # VISIT COUNTER & ACTIVE USERS SYSTEM
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS app_stats (
                stat_key VARCHAR(100) PRIMARY KEY,
                stat_value INT
            )
        """))
        # Initialize Visit Counter if not exists
        conn.execute(text("""
            INSERT INTO app_stats (stat_key, stat_value) 
            VALUES ('visits', 0) 
            ON CONFLICT (stat_key) DO NOTHING
        """))
        # Initialize default admin if not exists
        c = conn.execute(text("SELECT COUNT(*) FROM app_users")).fetchone()[0]
        if c == 0:
            h = hashlib.sha256("admin123".encode()).hexdigest()
            conn.execute(text("INSERT INTO app_users (username, password_hash, full_name, role) VALUES ('admin', :h, 'Admin Owner', 'Admin')"), {"h": h})

# Run tables init safely
try:
    init_db()
except Exception as e:
    pass

# ═══════════════════════════════════════════════
# VISIT COUNTER ENGINE
# ═══════════════════════════════════════════════
if "visited" not in st.session_state:
    st.session_state["visited"] = True
    try:
        q_exec("UPDATE app_stats SET stat_value = stat_value + 1 WHERE stat_key = 'visits'")
    except:
        pass

total_visits = 0
try:
    total_visits = scalar("SELECT stat_value FROM app_stats WHERE stat_key = 'visits'")
    if total_visits is None:
        total_visits = 0
except:
    total_visits = 0

# ═══════════════════════════════════════════════
# CORE CONSTANTS
# ═══════════════════════════════════════════════
ITEM_TYPES = [
    "Inlay Card / Bandrolle",
    "Tag Card / Barcode Sticker",
    "Barcode Item",
    "Safety",
    "Washing Paper",
    "Transparent Sticker",
    "Eco Friendly"
]

# ═══════════════════════════════════════════════
# MAIN PAGE BRANDING & STYLING
# ═══════════════════════════════════════════════
st.set_page_config(page_title="Vertex Packaging Portal", page_icon="🏭", layout="wide")

# CUSTOM COLOR PALETTE
st.markdown("""
<style>
    :root {
        --primary: #0A2540;
        --secondary: #639FAB;
        --accent: #F2A900;
        --bg: #F4F6F8;
    }
    .main-title {
        text-align: center;
        color: var(--primary);
        font-family: 'Helvetica Neue', sans-serif;
        font-weight: 800;
        margin-bottom: 2px;
        font-size: 2.8rem;
    }
    .sub-title {
        text-align: center;
        color: #555;
        font-size: 1.1rem;
        margin-bottom: 20px;
    }
    .trial-banner {
        background-color: #FFF3CD;
        border-left: 5px solid #FFC107;
        color: #856404;
        padding: 12px;
        border-radius: 4px;
        margin-bottom: 15px;
        font-weight: bold;
    }
    .sec {
        background-color: var(--primary);
        color: white;
        padding: 8px 15px;
        border-radius: 4px;
        font-weight: 600;
        margin-top: 15px;
        margin-bottom: 15px;
    }
    .kpi-row {
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-bottom: 15px;
    }
    .kpi {
        flex: 1;
        min-width: 140px;
        padding: 15px;
        border-radius: 6px;
        color: white;
        text-align: center;
        font-weight: bold;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .kb { background-color: #0F4C81; } /* Classic Blue */
    .kg { background-color: #2D6A4F; } /* Forest Green */
    .kr { background-color: #A63A50; } /* Crimson Red */
    .ka { background-color: #E65F2B; } /* Orange Accent */
    .kp { background-color: #6A0DAD; } /* Purple */
    .ke { background-color: #1A936F; } /* Emerald Green */
    
    .footer {
        text-align: center;
        margin-top: 40px;
        padding: 15px;
        font-size: 11px;
        color: #888;
        border-top: 1px solid #E2E8F0;
    }
</style>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════
# HELPER FUNCTIONS & EXPORTS
# ═══════════════════════════════════════════════
def round_bal(val):
    if val is None: return 0.0
    r = round(val, 2)
    return 0.0 if abs(r) < 0.01 else r

def round_and_format(val):
    if val is None: return "0"
    r = round(val, 2)
    if abs(r) < 0.01: return "0"
    if r.is_integer():
        return f"{int(r):,}"
    return f"{r:,.2f}"

def generate_ledger_pdf(item_summary, df_ledger, l_sel_coff, l_sel_cont, l_sel_art, report_type="MASTER"):
    pdf_buffer = io.BytesIO()
    doc = SimpleDocTemplate(pdf_buffer, pagesize=letter, rightMargin=20, leftMargin=20, topMargin=20, bottomMargin=20)
    story = []
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'ReportTitle',
        parent=styles['Heading1'],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#0A2540'),
        alignment=1,
        spaceAfter=15
    )
    section_title_style = ParagraphStyle(
        'SectionTitle',
        parent=styles['Heading2'],
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#0A2540'),
        spaceBefore=10,
        spaceAfter=5
    )
    meta_style = ParagraphStyle(
        'MetaText',
        parent=styles['Normal'],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#333333')
    )
    cell_style = ParagraphStyle(
        'CellText',
        parent=styles['Normal'],
        fontSize=8,
        leading=10
    )
    
    # Title Block
    title_text = "VERTEX PACKAGING — SYSTEM LEDGER"
    if report_type == "SHORTAGE":
        title_text = "VERTEX PACKAGING — PENDING SHORTAGE REPORT"
    elif report_type == "CONTRACT_SHORTLIST":
        title_text = "VERTEX PACKAGING — CONTRACT SHORTLIST STATEMENT"
    story.append(Paragraph(title_text, title_style))
    
    # Metadata Row
    metadata = f"<b>Date:</b> {datetime.datetime.now().strftime('%d-%b-%Y %I:%M %p')} | <b>Call-Off:</b> {l_sel_coff} | <b>Contract:</b> {l_sel_cont} | <b>Article:</b> {l_sel_art}"
    story.append(Paragraph(metadata, meta_style))
    story.append(Spacer(1, 10))
    
    # Summary of Item Types Pending
    story.append(Paragraph("📌 ACCESSORIES REMAINING SUMMARY", section_title_style))
    sum_data = [["Item Category", "Remaining Balance"]]
    for item_type in ITEM_TYPES:
        rem_val = item_summary.get(item_type, 0)
        sum_data.append([item_type, round_and_format(rem_val)])
        
    t_sum = Table(sum_data, colWidths=[250, 150])
    t_sum.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (1,0), colors.HexColor('#0A2540')),
        ('TEXTCOLOR', (0,0), (1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('BOTTOMPADDING', (0,0), (-1,0), 4),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8),
    ]))
    story.append(t_sum)
    story.append(Spacer(1, 15))
    
    # Detailed Data Grid
    story.append(Paragraph("📋 DETAILED TRANSACTIONS SHEET", section_title_style))
    grid_cols = ["Call-Off", "Contract", "Brand", "Article", "Item Category", "Ordered", "Received", "Remaining"]
    grid_data = [grid_cols]
    
    for _, row in df_ledger.iterrows():
        grid_data.append([
            str(row["Call-Off No"]),
            str(row["Contract #"]),
            str(row["Brand"]),
            str(row["Article"]),
            str(row["Item Type"]),
            round_and_format(row["Total Ordered"]),
            round_and_format(row["Total Received"]),
            round_and_format(row["Remaining Balance"])
        ])
        
    t_grid = Table(grid_data, colWidths=[65, 65, 75, 75, 110, 60, 60, 60])
    t_grid.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#639FAB')),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('ALIGN', (5,0), (-1,-1), 'RIGHT'),
        ('TOPPADDING', (0,0), (-1,-1), 3),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ('GRID', (0,0), (-1,-1), 0.5, colors.lightgrey),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 7),
    ]))
    story.append(t_grid)
    
    doc.build(story)
    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()

# ═══════════════════════════════════════════════
# AUTHENTICATION ENGINE
# ═══════════════════════════════════════════════
if "logged_in_user" not in st.session_state:
    st.session_state["logged_in_user"] = None

def _get_active_display():
    """ Returns active non-admin users for live display """
    # Get active non-admin users if we tracked them
    # For now, we can show currently logged in user role (if not admin)
    curr = st.session_state["logged_in_user"]
    if curr and curr["role"] != "Admin":
        return f"👤 {curr['full_name']} ({curr['role']}) is Online"
    return ""

def _access_ok(tab_name):
    u = st.session_state["logged_in_user"]
    if not u: return False
    role = u["role"]
    if role == "Admin": return True
    if role == "CEO": return True
    # DC Operator Access Schema
    if role == "DC Operator":
        if tab_name in ["🚚 DC Inventory Gate-In", "📦 Bilty Dispatch Portal", "🔍 Global Search"]:
            return True
    return False

# HEADER AND BRANDING DISPLAYS
st.markdown('<div class="main-title">🏭 Vertex Packaging Portal</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-title">Production Logistics & Inventory Reconciliation | CEO: Shahzad Bhai</div>', unsafe_allow_html=True)

# TRIAL BANNER ALWAYS VISIBLE
st.markdown('<div class="trial-banner">⚠️ SYSTEM NOTICE: This system is currently running on a TRIAL BASIS (ٹرائل بیس) for testing and evaluation. All operations are monitored.</div>', unsafe_allow_html=True)

# ═══════════════════════════════════════════════
# LOGIN SCREEN
# ═══════════════════════════════════════════════
if st.session_state["logged_in_user"] is None:
    st.markdown('<div style="max-width: 450px; margin: 50px auto; padding: 30px; background: white; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.1);">', unsafe_allow_html=True)
    st.subheader("🔑 Security Login")
    u_name = st.text_input("Username", key="login_u")
    u_pass = st.text_input("Password", type="password", key="login_p")
    
    if st.button("Access Dashboard", use_container_width=True):
        phash = hashlib.sha256(u_pass.encode()).hexdigest()
        user_row = q("SELECT username, password_hash, full_name, role FROM app_users WHERE username=? AND password_hash=?", [u_name, phash])
        if not user_row.empty:
            st.session_state["logged_in_user"] = {
                "username": user_row.iloc[0]["username"],
                "full_name": user_row.iloc[0]["full_name"],
                "role": user_row.iloc[0]["role"]
            }
            st.success("Successfully Authenticated!")
            st.rerun()
        else:
            st.error("Invalid credentials, please contact administrator.")
    st.markdown('</div>', unsafe_allow_html=True)
    st.stop()

# ═══════════════════════════════════════════════
# MAIN INTERFACE (AFTER LOGIN)
# ═══════════════════════════════════════════════
current_user = st.session_state["logged_in_user"]

# TOP ROW BAR (LOGOUT, ACTIVE USERS, VISITOR COUNTER)
col_head_left, col_head_mid, col_head_right = st.columns([6, 3, 3])
with col_head_left:
    st.markdown(f"Welcome back, **{current_user['full_name']}** ({current_user['role']})")
with col_head_mid:
    # Active Users (Hiding Admins completely)
    active_disp = _get_active_display()
    if active_disp:
        st.markdown(f"<span style='color: #2D6A4F; font-weight: bold;'>{active_disp}</span>", unsafe_allow_html=True)
with col_head_right:
    # Visitor counter display & logout
    st.markdown(f"<span style='float:right; font-weight:bold; color: #555;'>📈 Total Visits: {total_visits}</span>", unsafe_allow_html=True)
    if st.button("🚪 Secure Sign-out", key="signout_btn"):
        st.session_state["logged_in_user"] = None
        st.rerun()

# CREATE SYSTEM TABS
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "🔍 Global Search",
    "📦 Bilty Dispatch Portal",
    "🚚 DC Inventory Gate-In",
    "📊 Master Ledger",
    "📥 Contracts Upload",
    "⚙️ System Controls"
])

# ═══════════════════════════════════════════════
# TAB 1 — GLOBAL SEARCH SYSTEM
# ═══════════════════════════════════════════════
with tab1:
    if _access_ok("🔍 Global Search"):
        st.markdown('<div class="sec">🔍 Consolidated Global Search Engine</div>', unsafe_allow_html=True)
        search_q = st.text_input("Enter Article No, Sales Contract #, DC No, or Token No to search:", key="g_search").strip()
        
        if search_q:
            st.markdown(f"### 🔍 Search Results for: `{search_q}`")
            
            # CHECK INVENTORY ENTRIES
            inv_res = q("""
                SELECT date AS "Date", dc_no AS "DC No", gate_pass AS "Gate Pass", 
                       party AS "Party", brand AS "Brand", call_off_no AS "Call-Off", 
                       sale_contract AS "Contract #", article AS "Article", 
                       category AS "Item Category", qty AS "Qty (Pcs)", ctn AS "Cartons", 
                       operator AS "Operator", remarks AS "Remarks"
                FROM inventory
                WHERE article LIKE ? OR sale_contract LIKE ? OR dc_no LIKE ? OR gate_pass LIKE ?
                ORDER BY created_at DESC
            """, [f"%{search_q}%", f"%{search_q}%", f"%{search_q}%", f"%{search_q}%"])
            
            # CHECK BILTY ENTRIES
            bilty_res = q("""
                SELECT date AS "Date", bilty_no AS "Bilty No", transporter AS "Transporter", 
                       brand AS "Brand", call_off_no AS "Call-Off", 
                       sale_contract AS "Contract #", article AS "Article", 
                       category AS "Item Category", qty AS "Qty (Pcs)", ctn AS "Cartons", 
                       operator AS "Operator", remarks AS "Remarks"
                FROM bilty_dispatches
                WHERE article LIKE ? OR sale_contract LIKE ? OR bilty_no LIKE ?
                ORDER BY created_at DESC
            """, [f"%{search_q}%", f"%{search_q}%", f"%{search_q}%"])
            
            # ENHANCED GLOBAL SEARCH: SHOW REMAINING BALANCES & INTEGRATED HISTORIES IF ARTICLE/CONTRACT SEARCHED
            # We check if search parameter matches some articles or contracts in orders
            order_res = q("""
                SELECT DISTINCT call_off_no, sale_contract, brand, article, category
                FROM sheet_orders
                WHERE article LIKE ? OR sale_contract LIKE ?
            """, [f"%{search_q}%", f"%{search_q}%"])

            if not order_res.empty:
                st.markdown("#### ⏳ Consolidated Remaining Balance Statement (Ledger Tracker)")
                bal_res = q("""
                    SELECT
                        so.call_off_no        AS "Call-Off No",
                        so.sale_contract      AS "Contract #",
                        so.brand              AS "Brand",
                        so.article            AS "Article",
                        so.category           AS "Item Type",
                        SUM(so.order_qty)     AS "Total Ordered",
                        COALESCE(inv.total_received, 0)                      AS "Total Received",
                        SUM(so.order_qty) - COALESCE(inv.total_received, 0) AS "Remaining Balance"
                    FROM sheet_orders so
                    LEFT JOIN (
                        SELECT call_off_no, article, category, SUM(qty) AS total_received
                        FROM inventory
                        GROUP BY call_off_no, article, category
                    ) inv ON so.call_off_no = inv.call_off_no
                          AND so.article    = inv.article
                          AND so.category   = inv.category
                    WHERE so.article LIKE ? OR so.sale_contract LIKE ?
                    GROUP BY so.call_off_no, so.sale_contract, so.brand, so.article, so.category
                    ORDER BY so.call_off_no DESC, so.article ASC
                """, [f"%{search_q}%", f"%{search_q}%"])
                
                if not bal_res.empty:
                    df_bal_fmt = bal_res.copy()
                    df_bal_fmt["Total Ordered"] = df_bal_fmt["Total Ordered"].apply(round_and_format)
                    df_bal_fmt["Total Received"] = df_bal_fmt["Total Received"].apply(round_and_format)
                    df_bal_fmt["Remaining Balance"] = df_bal_fmt["Remaining Balance"].apply(lambda x: "" if x == 0 else round_and_format(x))
                    st.dataframe(df_bal_fmt, use_container_width=True, hide_index=True)

            # SHOW DC ENTRY HISTORY
            st.markdown("#### 🚚 Gate-In / DC Entry History")
            if not inv_res.empty:
                st.dataframe(inv_res, use_container_width=True, hide_index=True)
            else:
                st.warning("No DC/Gate-In matching entries found.")
                
            # SHOW BILTY DISPATCH HISTORY
            st.markdown("#### 📦 Bilty Dispatch History Linkage")
            if not bilty_res.empty:
                st.dataframe(bilty_res, use_container_width=True, hide_index=True)
            else:
                st.warning("No Bilty shipment matching records found.")

    else:
        st.warning("You do not have administrative privilege to view Tab 1.")

# ═══════════════════════════════════════════════
# TAB 2 — BILTY DISPATCH PORTAL (WITH MANUAL & TICK INPUTS)
# ═══════════════════════════════════════════════
with tab2:
    if _access_ok("📦 Bilty Dispatch Portal"):
        st.markdown('<div class="sec">📦 Dispatch Loading Sheet & Bilty Generator</div>', unsafe_allow_html=True)
        
        # Dispatch Inputs Form
        b_c1, b_c2, b_c3 = st.columns(3)
        with b_c1:
            b_date = st.date_input("Bilty Dispatch Date", value=date.today(), key="bilty_date")
            b_no = st.text_input("Bilty / Tracking Number", key="bilty_no")
        with b_c2:
            b_trans = st.text_input("Transporter / Vehicle Detail", key="bilty_trans")
            b_brand = st.selectbox("Select Brand", ["-- Select --"] + sorted(q("SELECT DISTINCT brand FROM sheet_orders")["brand"].tolist() if "brand" in q("SELECT DISTINCT brand FROM sheet_orders").columns else []), key="bilty_brand")
        with b_c3:
            b_coff = st.selectbox("Select Call-Off", ["-- Select --"] + sorted(q("SELECT DISTINCT call_off_no FROM sheet_orders")["call_off_no"].tolist() if "call_off_no" in q("SELECT DISTINCT call_off_no FROM sheet_orders").columns else []), key="bilty_coff")
            b_contract = st.selectbox("Select Sales Contract", ["-- Select --"] + sorted(q("SELECT DISTINCT sale_contract FROM sheet_orders")["sale_contract"].tolist() if "sale_contract" in q("SELECT DISTINCT sale_contract FROM sheet_orders").columns else []), key="b_contract_sel")

        # LOAD CONTRACT ITEMS FOR PROCESSING
        if b_brand != "-- Select --" and b_coff != "-- Select --" and b_contract != "-- Select --":
            b_orders = q("""
                SELECT article, category, SUM(order_qty) AS ord_qty
                FROM sheet_orders
                WHERE brand=? AND call_off_no=? AND sale_contract=?
                GROUP BY article, category
            """, [b_brand, b_coff, b_contract])
            
            if not b_orders.empty:
                st.markdown("##### 📦 Dispatch Articles & Quantities Setup")
                st.info("Select items to dispatch. You can check the checkbox for total balance dispatch, OR input manual dispatch quantities directly. The system handles live +/- balance calculation on the fly.")
                
                dispatch_rows = []
                for idx, row in b_orders.iterrows():
                    art = row["article"]
                    cat = row["category"]
                    
                    # Fetch already received and already dispatched to calculate available balance
                    rec = scalar("SELECT SUM(qty) FROM inventory WHERE brand=? AND call_off_no=? AND sale_contract=? AND article=? AND category=?", [b_brand, b_coff, b_contract, art, cat]) or 0.0
                    disp = scalar("SELECT SUM(qty) FROM bilty_dispatches WHERE brand=? AND call_off_no=? AND sale_contract=? AND article=? AND category=?", [b_brand, b_coff, b_contract, art, cat]) or 0.0
                    avail_bal = max(0.0, rec - disp)
                    
                    # Layout row for input
                    cols = st.columns([3, 3, 2, 2, 2])
                    with cols[0]:
                        st.markdown(f"**{art}** ({cat})")
                    with cols[1]:
                        st.caption(f"In Stock (Reconciled): {round_and_format(avail_bal)}")
                    with cols[2]:
                        # TICK OVERRIDE OPTION
                        chk_all = st.checkbox("Ship All", key=f"bchk_{idx}")
                    with cols[3]:
                        # MANUAL INPUT OPTION next to tick option
                        man_qty = st.number_input("Manual Qty", min_value=0.0, max_value=float(avail_bal), value=float(avail_bal) if chk_all else 0.0, step=1.0, key=f"bqty_{idx}")
                    with cols[4]:
                        man_ctn = st.number_input("Cartons", min_value=0.0, step=1.0, key=f"bctn_{idx}")
                    
                    # Calculate live remaining balance on page
                    live_rem_bal = avail_bal - man_qty
                    
                    if man_qty > 0:
                        dispatch_rows.append({
                            "article": art,
                            "category": cat,
                            "qty": man_qty,
                            "ctn": man_ctn,
                            "rem_bal": live_rem_bal
                        })
                
                b_remarks = st.text_area("Bilty Dispatch Comments / Shipment Notes", key="bilty_remarks")
                
                if st.button("💾 Record Dispatch shipment and print ledger", type="primary", key="save_bilty_btn"):
                    if not b_no or not b_trans:
                        st.error("Please fill in Bilty No and Transporter details.")
                    elif len(dispatch_rows) == 0:
                        st.error("Please select or enter manual quantities for at least one item to ship.")
                    else:
                        for item in dispatch_rows:
                            q_exec("""
                                INSERT INTO bilty_dispatches (date, bilty_no, transporter, brand, call_off_no, sale_contract, article, category, qty, ctn, operator, remarks)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, [b_date, b_no, b_trans, b_brand, b_coff, b_contract, item["article"], item["category"], item["qty"], item["ctn"], current_user["full_name"], b_remarks])
                        st.success(f"✅ Bilty dispatch order '{b_no}' registered completely!")
                        st.rerun()
            else:
                st.warning("No registered ordered quantities found matching this brand/call-off/contract criteria.")
    else:
        st.warning("Access Denied.")

# ═══════════════════════════════════════════════
# TAB 3 — DC INVENTORY GATE-IN (DYNAMIC CATEGORY BLUE BREAKDOWN)
# ═══════════════════════════════════════════════
with tab3:
    if _access_ok("🚚 DC Inventory Gate-In"):
        st.markdown('<div class="sec">🚚 Delivery Challan (DC) Gate-In Cargo Log</div>', unsafe_allow_html=True)
        
        # Form inputs
        f_c1, f_c2, f_c3 = st.columns(3)
        with f_c1:
            g_date = st.date_input("Receive Date", value=date.today(), key="gate_date")
            g_dc = st.text_input("DC Reference Number", key="gate_dc")
            g_gp = st.text_input("Gate-Pass Code", key="gate_gp")
        with f_c2:
            g_party = st.text_input("Source Party / Vendor name", key="gate_party")
            g_brand = st.selectbox("Select Brand Contract", ["-- Select --"] + sorted(q("SELECT DISTINCT brand FROM sheet_orders")["brand"].tolist() if "brand" in q("SELECT DISTINCT brand FROM sheet_orders").columns else []), key="gate_brand")
        with f_c3:
            g_coff = st.selectbox("Select Active Call-Off", ["-- Select --"] + sorted(q("SELECT DISTINCT call_off_no FROM sheet_orders")["call_off_no"].tolist() if "call_off_no" in q("SELECT DISTINCT call_off_no FROM sheet_orders").columns else []), key="gate_coff")
            g_contract = st.selectbox("Select Sales Contract Order", ["-- Select --"] + sorted(q("SELECT DISTINCT sale_contract FROM sheet_orders")["sale_contract"].tolist() if "sale_contract" in q("SELECT DISTINCT sale_contract FROM sheet_orders").columns else []), key="gate_contract")

        if g_brand != "-- Select --" and g_coff != "-- Select --" and g_contract != "-- Select --":
            # Show list of Articles
            art_list = sorted(q("SELECT DISTINCT article FROM sheet_orders WHERE brand=? AND call_off_no=? AND sale_contract=?", [g_brand, g_coff, g_contract])["article"].tolist() if "article" in q("SELECT DISTINCT article FROM sheet_orders WHERE brand=? AND call_off_no=? AND sale_contract=?", [g_brand, g_coff, g_contract]).columns else [])
            g_article = st.selectbox("Select Target Article", ["-- Select --"] + art_list, key="gate_article")
            
            # DYNAMIC ITEM BREAKDOWN ON ARTICLE SELECT (BLUE COUNTERS INSIDE EXISTING COMPONENT BOX)
            if g_article != "-- Select --":
                st.markdown("""<style>
                    .dynamic-blue-breakdown {
                        background-color: #0F4C81 !important;
                        color: white !important;
                        padding: 18px;
                        border-radius: 8px;
                        margin-bottom: 20px;
                        box-shadow: 0 4px 6px rgba(0,0,0,0.15);
                    }
                    .dynamic-blue-breakdown h4 {
                        margin-top: 0;
                        color: #FFF !important;
                        border-bottom: 1px solid rgba(255,255,255,0.2);
                        padding-bottom: 8px;
                    }
                    .item-grid {
                        display: grid;
                        grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
                        gap: 12px;
                        margin-top: 10px;
                    }
                    .item-badge {
                        background: rgba(255, 255, 255, 0.15);
                        padding: 8px;
                        border-radius: 4px;
                        text-align: center;
                    }
                </style>""", unsafe_allow_html=True)
                
                # Fetch balance details specifically for selected article categories
                cat_data = q("""
                    SELECT so.category, SUM(so.order_qty) AS ord_qty,
                           COALESCE(inv.total_received, 0) AS total_received
                    FROM sheet_orders so
                    LEFT JOIN (
                        SELECT call_off_no, article, category, SUM(qty) AS total_received
                        FROM inventory
                        GROUP BY call_off_no, article, category
                    ) inv ON so.call_off_no = inv.call_off_no
                          AND so.article    = inv.article
                          AND so.category   = inv.category
                    WHERE so.brand=? AND so.call_off_no=? AND so.sale_contract=? AND so.article=?
                    GROUP BY so.category, inv.total_received
                """, [g_brand, g_coff, g_contract, g_article])
                
                st.markdown('<div class="dynamic-blue-breakdown">', unsafe_allow_html=True)
                st.markdown(f"<h4>📊 Item-Wise Live Breakdown for Article: {g_article}</h4>", unsafe_allow_html=True)
                st.markdown('<div class="item-grid">', unsafe_allow_html=True)
                
                cat_summary = {}
                for _, crow in cat_data.iterrows():
                    cat_summary[crow["category"]] = crow["ord_qty"] - crow["total_received"]
                
                # Always show defined types
                for itype in ITEM_TYPES:
                    rem_qty_type = cat_summary.get(itype, 0.0)
                    st.markdown(f"""
                        <div class="item-badge">
                            <span style="font-size:10px; display:block; opacity:0.8;">{itype}</span>
                            <span style="font-size:14px; font-weight:bold;">{round_and_format(rem_qty_type)} Pcs</span>
                        </div>
                    """, unsafe_allow_html=True)
                    
                st.markdown('</div></div>', unsafe_allow_html=True)
                
                # Standard DC inputs for actual recording
                g_category = st.selectbox("Select Accessory Type receiving", ITEM_TYPES, key="gate_cat")
                g_qty = st.number_input("Received Qty (Pcs)", min_value=0.0, step=1.0, key="gate_qty")
                g_ctn = st.number_input("Carton count", min_value=0.0, step=1.0, key="gate_ctn")
                g_remarks = st.text_area("DC Entry Comments / Placement Details", key="gate_remarks")
                
                if st.button("📥 Commit DC Gate-In Entry", type="primary", key="save_dc_btn"):
                    if not g_dc or not g_gp or not g_party:
                        st.error("Please fill in DC, Gate-Pass, and Party Vendor details.")
                    elif g_qty <= 0:
                        st.error("Received quantity must be positive.")
                    else:
                        q_exec("""
                            INSERT INTO inventory (date, dc_no, gate_pass, party, brand, call_off_no, sale_contract, article, category, qty, ctn, operator, remarks)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, [g_date, g_dc, g_gp, g_party, g_brand, g_coff, g_contract, g_article, g_category, g_qty, g_ctn, current_user["full_name"], g_remarks])
                        st.success(f"✅ Challan {g_dc} committed successfully!")
                        st.rerun()
    else:
        st.warning("Access Denied.")

# ═══════════════════════════════════════════════
# TAB 4 — MASTER LEDGER (UNCHANGED CORE ALIGNMENT)
# ═══════════════════════════════════════════════
with tab4:
    if _access_ok("📊 Master Ledger"):
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
# TAB 5 — CONTRACTS SHEET UPLOAD
# ═══════════════════════════════════════════════
with tab5:
    if _access_ok("📥 Contracts Upload"):
        st.markdown('<div class="sec">📥 Upload Brand/Customer Contract Order Sheets</div>', unsafe_allow_html=True)
        st.info("Ensure files are standard .csv or Excel sheets with the columns: 'Call-Off No', 'Contract #', 'Brand', 'Article', 'Item Category', 'Qty'.")
        
        up_file = st.file_uploader("Upload Sheet", type=["csv", "xlsx"], key="order_uploader")
        if up_file:
            try:
                if up_file.name.endswith(".csv"):
                    df_up = pd.read_csv(up_file)
                else:
                    df_up = pd.read_excel(up_file)
                    
                st.dataframe(df_up.head(5), use_container_width=True)
                
                # Column mapper UI
                cols_raw = df_up.columns.tolist()
                st.markdown("##### Column Mappings")
                mc1, mc2, mc3 = st.columns(3)
                with mc1:
                    m_coff = st.selectbox("Call-Off Column", cols_raw, index=0 if "Call-Off No" in cols_raw else 0, key="m_coff")
                    m_cont = st.selectbox("Contract Column", cols_raw, index=cols_raw.index("Contract #") if "Contract #" in cols_raw else 0, key="m_cont")
                with mc2:
                    m_br = st.selectbox("Brand Column", cols_raw, index=cols_raw.index("Brand") if "Brand" in cols_raw else 0, key="m_br")
                    m_art = st.selectbox("Article Column", cols_raw, index=cols_raw.index("Article") if "Article" in cols_raw else 0, key="m_art")
                with mc3:
                    m_cat = st.selectbox("Category Column", cols_raw, index=cols_raw.index("Item Category") if "Item Category" in cols_raw else 0, key="m_cat")
                    m_qty = st.selectbox("Order Qty Column", cols_raw, index=cols_raw.index("Qty") if "Qty" in cols_raw else 0, key="m_qty")

                if st.button("💾 Parse & Import Contract Data", type="primary", key="commit_import_btn"):
                    total_imported = 0
                    for _, row in df_up.iterrows():
                        q_exec("""
                            INSERT INTO sheet_orders (call_off_no, sale_contract, brand, article, category, order_qty)
                            VALUES (?, ?, ?, ?, ?, ?)
                        """, [
                            str(row[m_coff]),
                            str(row[m_cont]),
                            str(row[m_br]),
                            str(row[m_art]),
                            str(row[m_cat]),
                            float(row[m_qty])
                        ])
                        total_imported += 1
                    st.success(f"✅ Successfully loaded {total_imported} contract entries into system databases!")
                    st.rerun()
            except Exception as e:
                st.error(f"Error parsing document: {str(e)}")
    else:
        st.warning("Administrative privileges required to access Contract configuration upload.")

# ═══════════════════════════════════════════════
# TAB 6 — SYSTEM ADMIN CONTROLS
# ═══════════════════════════════════════════════
with tab6:
    u = st.session_state["logged_in_user"]
    if u and u["role"] == "Admin":
        st.markdown('<div class="sec">⚙️ System Administrative Controls</div>', unsafe_allow_html=True)
        
        # Reset tables option
        if st.checkbox("Show Destructive Commands (Danger)", key="sys_danger_chk"):
            if st.button("⚠️ Wipe Database Tables (Except Users)", key="wipe_data_btn"):
                q_exec("TRUNCATE TABLE inventory, bilty_dispatches, sheet_orders RESTART IDENTITY CASCADE")
                st.warning("All inventory transactions and contract sheets successfully purged!")
                st.rerun()

        st.markdown("---")
        st.markdown("##### 👥 Create System User Accounts")
        nc1, nc2, nc3 = st.columns(3)
        with nc1:
            new_u = st.text_input("Username", key="nc_user")
            new_f = st.text_input("Full Name", key="nc_fname")
        with nc2:
            new_p = st.text_input("Password", type="password", key="nc_pass")
        with nc3:
            new_r = st.selectbox("Security Role", ["DC Operator", "CEO", "Admin"], key="nc_role")

        if st.button("➕ Generate User Account", key="create_user_btn"):
            if not new_u or not new_p or not new_f:
                st.error("Please provide all credential specs.")
            else:
                exists = scalar("SELECT COUNT(*) FROM app_users WHERE username=?", [new_u]) or 0
                if exists > 0:
                    st.error("Username already registered.")
                else:
                    h = hashlib.sha256(new_p.encode()).hexdigest()
                    q_exec("INSERT INTO app_users (username, password_hash, full_name, role) VALUES (?, ?, ?, ?)", [new_u, h, new_f, new_r])
                    st.success(f"✅ User account '{new_u}' active!")
                    st.rerun()

        st.markdown("---")
        st.markdown("##### 🔄 Reset User Passwords")
        all_usernames = q("SELECT username FROM app_users")["username"].tolist() if "username" in q("SELECT username FROM app_users").columns else []
        rp_user = st.selectbox("Select target account", ["-- Select --"] + all_usernames, key="rp_user_sel")
        if rp_user != "-- Select --":
            rp_newpass = st.text_input("Enter New Password", type="password", key="rp_pass_inp")
            if st.button("💾 Apply Password Override", key="rp_btn_commit"):
                if not rp_newpass:
                    st.error("Provide a valid password.")
                else:
                    nh = hashlib.sha256(rp_newpass.encode()).hexdigest()
                    q_exec("UPDATE app_users SET password_hash=? WHERE username=?", [nh, rp_user])
                    st.success(f"✅ Password reset complete for '{rp_user}'.")

        st.markdown("---")
        st.markdown("##### 📋 Live Registered Users Sheet")
        df_users = q("SELECT username AS Username, full_name AS \"Full Name\", role AS Role, created_at AS \"Created At\" FROM app_users ORDER BY username")
        st.dataframe(df_users, use_container_width=True, hide_index=True)

        st.markdown("##### 🗑️ Remove User Account")
        del_candidates = [usr for usr in all_usernames if usr != current_user["username"]]
        if del_candidates:
            del_user = st.selectbox("Select user to purge", ["-- Select --"] + del_candidates, key="del_user_sel")
            if del_user != "-- Select --":
                if st.checkbox(f"Confirm completely delete '{del_user}' account", key="del_user_chk"):
                    if st.button("🗑️ Delete User Account", key="del_user_btn"):
                        q_exec("DELETE FROM app_users WHERE username=?", [del_user])
                        st.success(f"✅ User account '{del_user}' removed.")
                        st.rerun()
        else:
            st.caption("No eligible user accounts for disposal.")
    else:
        st.warning("Only Owner Administrator can view System Administrative Settings Panel.")

# ═══════════════════════════════════════════════
# MAIN SYSTEM FOOTER (KEEPING NABA TECH SPEC)
# ═══════════════════════════════════════════════
st.markdown("""
<div class="footer">
  🏭 NABA TECH BY KALEEM ULLAH SHARIF &nbsp;|&nbsp;
  Customer: Vertex Packaging (Shahzad Bhai) Lahore
</div>
""", unsafe_allow_html=True)
