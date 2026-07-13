import streamlit as st
import streamlit.components.v1 as components
import pandas as pd
import datetime
import io
import math
import hashlib
import secrets as pysecrets
import base64
from datetime import date
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from sqlalchemy import create_engine, text, inspect
import openpyxl
from openpyxl.styles import Font, Alignment, Border, Side

# ═══════════════════════════════════════════════
# DATABASE SYSTEM — CLOUD (pg8000 driver)
# ═══════════════════════════════════════════════
@st.cache_resource(show_spinner=False)
def _get_engine():
    db_url = st.secrets.get("DB_URL", "sqlite:///textile_inventory.db")
    if "postgresql+psycopg2://" in db_url:
        db_url = db_url.replace("postgresql+psycopg2://", "postgresql+pg8000://")
    elif db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+pg8000://", 1)
    elif db_url.startswith("postgresql://") and "+pg8000" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+pg8000://", 1)
    return create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=10, pool_recycle=300)

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
                id {pk}, call_off_no TEXT, po_no TEXT, sale_contract TEXT,
                brand TEXT, article TEXT, category TEXT, order_qty REAL, variant TEXT DEFAULT '')"""))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS inventory (
                id {pk}, call_off_no TEXT, contract_no TEXT, dc_no TEXT, po_no TEXT,
                article TEXT, category TEXT, qty REAL, entry_date TEXT, remark TEXT, 
                company_token TEXT DEFAULT '', style_type TEXT DEFAULT '—', 
                item_description TEXT DEFAULT '', destination TEXT DEFAULT '')"""))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS bilty (
                id {pk}, call_off_no TEXT, contract_no TEXT, article TEXT, category TEXT,
                qty REAL, cartons INTEGER, transport_mode TEXT, bilty_date TEXT, created_at TEXT)"""))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS app_users (
                id {pk}, username TEXT UNIQUE, password_hash TEXT, role TEXT,
                full_name TEXT, created_at TEXT, last_seen TEXT DEFAULT '')"""))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS site_stats (
                id {pk}, stat_key TEXT UNIQUE, stat_value INTEGER DEFAULT 0)"""))
        
    index_defs = [
        ("idx_inv_article_category", "inventory(article, category)"),
        ("idx_inv_calloff",          "inventory(call_off_no)"),
        ("idx_inv_contract",         "inventory(contract_no)"),
        ("idx_inv_po",               "inventory(po_no)"),
        ("idx_inv_dc",               "inventory(dc_no)"),
        ("idx_inv_token",            "inventory(company_token)"),
        ("idx_inv_entrydate",        "inventory(entry_date)"),
        ("idx_so_calloff",           "sheet_orders(call_off_no)"),
        ("idx_bilty_calloff_art",    "bilty(call_off_no, article, category)"),
        ("idx_bilty_date",           "bilty(bilty_date)"),
    ]
    for name, target in index_defs:
        try:
            with engine.begin() as conn:
                if dialect == "mysql":
                    conn.execute(text(f"CREATE INDEX {name} ON {target}"))
                else:
                    conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))
        except Exception:
            pass

    with engine.begin() as conn:
        if not conn.execute(text("SELECT COUNT(*) FROM app_users")).scalar():
            conn.execute(text("INSERT INTO app_users (username,password_hash,role,full_name,created_at) VALUES (:u,:p,:r,:f,:c)"),
                         {"u": "admin", "p": _hash_password("admin123"), "r": "Admin", "f": "Default Admin", "c": str(datetime.datetime.now())})
        if not conn.execute(text("SELECT COUNT(*) FROM site_stats WHERE stat_key='total_visits'")).scalar():
            conn.execute(text("INSERT INTO site_stats (stat_key, stat_value) VALUES ('total_visits', 0)"))
    return True

def get_conn():
    _init_schema()
    return _CompatConn(_get_engine().connect())

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
ITEM_TYPES = ["Inlay Card / Bandrolle", "Tag Card / Barcode Sticker", "Barcode Item", "Safety", "Washing Paper", "Transparent Sticker", "Eco Friendly"]
STYLES_INLAY = ["Normal", "Topper", "Split"]

def q(sql, params=None):
    named_sql, pdict = _qmark_to_named(sql, params or [])
    with _get_engine().connect() as conn:
        return pd.read_sql(text(named_sql), conn, params=pdict)

def scalar(sql, params=None):
    with _get_engine().connect() as conn:
        named_sql, pdict = _qmark_to_named(sql, params or [])
        r = conn.execute(text(named_sql), pdict).fetchone()
        return (r[0] or 0) if r else 0

def format_date_str(date_str):
    try:
        dt = datetime.datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d-%m-%Y")
    except:
        return date_str

def round_and_format(val):
    try:
        if pd.isna(val) or val == "" or val == "—": return "0"
        return f"{int(math.floor(float(str(val).replace(',', '')) + 0.5)):,}"
    except:
        return "0"

def round_bal(val):
    try:
        if pd.isna(val) or val == "" or val == "—": return 0
        return int(math.floor(float(str(val).replace(',', '')) + 0.5))
    except:
        return 0

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
    if category: extra = " AND category=?"; params.append(category)
    return scalar(f"SELECT SUM(qty) FROM bilty WHERE call_off_no=? AND article=?{extra}", params)

def get_cached_item_description(article, category):
    if not article or not category: return ""
    df = q("SELECT description FROM item_desc_cache WHERE article=? AND category=? ORDER BY id DESC LIMIT 1", [article, category])
    return df.iloc[0]["description"] if not df.empty else ""

def save_cached_item_description(article, category, description):
    if not article or not category or not str(description).strip(): return
    with _get_engine().begin() as conn:
        conn.execute(text("DELETE FROM item_desc_cache WHERE article=:a AND category=:c"), {"a": article, "c": category})
        conn.execute(text("INSERT INTO item_desc_cache (article, category, description) VALUES (:a,:c,:d)"), {"a": article, "c": category, "d": description.strip()})

# ─────────────────────────────────────────────
# PDF GENERATION (MASTER LEDGER)
# ─────────────────────────────────────────────
def generate_ledger_pdf(df_summary, df_articles, sel_coff, sel_cont, sel_art, report_type="MASTER"):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    story = []
    styles = getSampleStyleSheet()
    title_color = '#1e40af' if report_type == "MASTER" else ('#7c3aed' if report_type == "CONTRACT_SHORTLIST" else '#b91c1c')
    title_text = "MASTER LEDGER STATUS REPORT" if report_type == "MASTER" else ("CONTRACT-WISE SHORTLIST REPORT" if report_type == "CONTRACT_SHORTLIST" else "PENDING SHORTAGE REPORT")
    
    title_style = ParagraphStyle('Title', parent=styles['Heading1'], fontSize=15, leading=19, textColor=colors.HexColor(title_color), alignment=1)
    cust_style = ParagraphStyle('Cust', parent=styles['Normal'], fontSize=11, leading=15, textColor=colors.HexColor('#0f172a'), alignment=1)
    sub_style = ParagraphStyle('Sub', parent=styles['Normal'], fontSize=9, leading=13, alignment=1)
    h2_style = ParagraphStyle('H2', parent=styles['Heading2'], fontSize=11, leading=15, textColor=colors.HexColor('#0f172a'), spaceBefore=8, spaceAfter=4)
    
    story.append(Paragraph(f"<b>VERTEX PACKAGING — {title_text}</b>", title_style))
    story.append(Spacer(1, 2))
    story.append(Paragraph("<b>Customer Name: Vertex Shahzad Bhai Lahore</b>", cust_style))
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"Generated: {datetime.datetime.now().strftime('%d-%m-%Y %I:%M %p')} | Call-Off: {sel_coff} | Contract: {sel_cont}", sub_style))
    story.append(Spacer(1, 12))
    
    story.append(Paragraph("<b>📊 SECTION 1: ITEM-WISE REMAINING SUMMARY</b>", h2_style))
    sum_data = [["Item Type / Description", "Remaining Balance (Pcs)"]]
    for k, v in df_summary.items(): sum_data.append([k, round_and_format(v)])
    t_sum = Table(sum_data, colWidths=[280, 220])
    t_sum.setStyle(TableStyle([('BACKGROUND', (0,0), (1,0), colors.HexColor(title_color)), ('TEXTCOLOR', (0,0), (1,0), colors.white), ('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('ALIGN', (1,1), (1,-1), 'RIGHT'), ('FONTSIZE', (0,0), (-1,-1), 9)]))
    story.append(t_sum)
    
    story.append(Spacer(1, 12))
    story.append(Paragraph("<b>🔍 SECTION 2: ARTICLES BREAKDOWN</b>", h2_style))
    art_data = [["Call-Off", "Contract #", "Art. No", "Item Type", "Ordered", "Received", "Remaining"]]
    for _, r in df_articles.iterrows():
        art_data.append([str(r["Call-Off No"]), str(r["Contract #"]), str(r["Article"]), str(r["Item Type"]), round_and_format(r['Total Ordered']), round_and_format(r['Total Received']), round_and_format(r['Remaining Balance'])])
    t_art = Table(art_data, colWidths=[55, 65, 65, 125, 65, 65, 65])
    t_art.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f1f5f9')), ('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('FONTSIZE', (0,0), (-1,-1), 8), ('ALIGN', (4,1), (-1,-1), 'RIGHT')]))
    story.append(t_art)
    
    story.append(Spacer(1, 25))
    sig_data = [["-------------------------\nReport Checked By\n(Vertex Packaging Team)", "-------------------------\nAuthorized Signature\n(Kaleem Ullah Sharif)"]]
    t_sig = Table(sig_data, colWidths=[250, 250])
    t_sig.setStyle(TableStyle([('ALIGN', (0,0), (-1,-1), 'CENTER'), ('FONTSIZE', (0,0), (-1,-1), 9)]))
    story.append(t_sig)
    
    doc.build(story)
    buffer.seek(0)
    return buffer

# ─────────────────────────────────────────────
# PAGE INITIALIZATION
# ─────────────────────────────────────────────
st.set_page_config(page_title="Vertex Packaging | Inventory System", layout="wide", page_icon="📦")
_init_schema()

if "visit_counted" not in st.session_state:
    with _get_engine().begin() as conn:
        conn.execute(text("UPDATE site_stats SET stat_value = stat_value + 1 WHERE stat_key='total_visits'"))
    st.session_state["visit_counted"] = True

# ─────────────────────────────────────────────
# AUTHENTICATION
# ─────────────────────────────────────────────
if "auth_user" not in st.session_state: st.session_state["auth_user"] = None
if st.session_state["auth_user"] is None:
    st.markdown("### 🔐 Login")
    with st.form("login_form"):
        li_user = st.text_input("Username")
        li_pass = st.text_input("Password", type="password")
        if st.form_submit_button("Login", type="primary"):
            urow = q("SELECT username, password_hash, role, full_name FROM app_users WHERE username=?", [li_user.strip()])
            if not urow.empty and _verify_password(li_pass, urow.iloc[0]["password_hash"]):
                st.session_state["auth_user"] = urow.iloc[0].to_dict()
                st.rerun()
            else: st.error("❌ Invalid credentials")
    st.stop()

current_user = st.session_state["auth_user"]
current_role = current_user["role"]

tab1, tab2, tab3, tab4, tab5, tab6, tab7 = st.tabs([
    "🔍 Global Search","➕ DC Entry","📋 All Entries",
    "📊 Master Ledger","📤 Sheet Upload","🚚 Bilty Management","👤 User Management"
])

# ═══════════════════════════════════════════════
# TAB 2 — DC ENTRY (WITH LIVE DITTO PREVIEW & CONTROLS)
# ═══════════════════════════════════════════════
with tab2:
    st.markdown('### ➕ New DC Entry & Printing Station')
    dc_main_cols = st.columns([3, 2])
    
    with dc_main_cols[0]:
        f_coff = st.text_input("Call-Off No. *", placeholder="e.g. 288", key="dc_coff_input").strip()
        f_po, f_contract, brand, art_list = "", "", "", []
        
        if f_coff:
            df_so_lookup = q("SELECT DISTINCT sale_contract, article, brand, po_no FROM sheet_orders WHERE call_off_no=?", [f_coff])
            contracts_for_coff = df_so_lookup["sale_contract"].dropna().unique().tolist()
            art_list = df_so_lookup["article"].dropna().unique().tolist()
            
            if contracts_for_coff:
                f_contract = st.selectbox("Select Contract # *", contracts_for_coff, key="dc_cont_sel") if len(contracts_for_coff) > 1 else contracts_for_coff[0]
                brand = df_so_lookup.loc[df_so_lookup["sale_contract"] == f_contract, "brand"].iloc[0] if not df_so_lookup.empty else ""
                po_for_sc = df_so_lookup.loc[df_so_lookup["sale_contract"] == f_contract, "po_no"].dropna().unique().tolist()
                f_po = st.selectbox("Select PO No. *", po_for_sc, key="dc_po_sel") if len(po_for_sc) > 1 else (po_for_sc[0] if po_for_sc else "")

        f_art_sel = st.selectbox("Article No. *", ["-- Select Article --"] + art_list, key="dc_art")
        f_art = "" if f_art_sel == "-- Select Article --" else f_art_sel
        
        # Enhanced Blue Widget Breakdown
        if f_coff and f_art:
            bilty_done_art = get_bilty_qty(f_coff, f_art)
            df_bilty_breakdown = q("SELECT category, SUM(qty) as sub_qty FROM bilty WHERE call_off_no=? AND article=? GROUP BY category", [f_coff, f_art])
            
            breakdown_html = "".join([f"<br>🔹 {r['category']}: {int(r['sub_qty']):,} Pcs" for _, r in df_bilty_breakdown.iterrows()])
            st.markdown(f"""
            <div style="background:#0c4a6e; border:1px solid #38bdf8; color:#fff; padding:12px; border-radius:8px; font-size:13px;">
              🚚 <b>Total Bilty Done:</b> {int(bilty_done_art):,} Pcs for Article <b>{f_art}</b>
              {breakdown_html}
            </div>""", unsafe_allow_html=True)

        f_type = st.selectbox("Item Type *", ITEM_TYPES, key="dc_type")
        f_style = st.selectbox("Style Type *", STYLES_INLAY, key="dc_style_inlay") if f_type == "Inlay Card / Bandrolle" else "—"
        f_token = st.text_input("Company PO (Database Token Field)", key="dc_token")
        f_dc = st.text_input("DC No. *", key="dc_dcno")
        f_date = st.date_input("Entry Date *", value=date.today(), key="dc_date")
        f_desc = st.text_input("Item Description (as per PO)", key="dc_desc")
        f_qty = st.number_input("Quantity (Pcs) *", min_value=0.0, step=1.0, format="%g", key="dc_qty")
        f_remark = st.text_area("Remark / Notes", key="dc_remark")
        f_destination = st.text_input("Destination", key="dc_destination", placeholder="e.g. SOHRAB/HSU")

        if st.button("💾 Save DC Entry", type="primary"):
            if not f_dc or not f_coff or f_qty <= 0 or not f_art:
                st.error("⚠️ Please fill out all required fields.")
            else:
                with _get_engine().begin() as conn:
                    conn.execute(text("""INSERT INTO inventory (call_off_no, contract_no, dc_no, po_no, article, category, qty, entry_date, remark, company_token, style_type, item_description, destination) 
                                         VALUES (:co,:cn,:dc,:po,:art,:cat,:q,:d,:r,:t,:s,:desc,:dest)"""),
                                 {"co": f_coff, "cn": f_contract, "dc": f_dc, "po": f_po, "art": f_art, "cat": f_type, "q": f_qty, "d": str(f_date), "r": f_remark, "t": f_token, "s": f_style, "desc": f_desc, "dest": f_destination})
                st.success("Entry Saved Successfully!")
                st.rerun()

    with dc_main_cols[1]:
        st.markdown("### 🖨️ In-App Print Preview & Export Controls")
        preview_dc = st.text_input("Enter DC No. to view Preview & Export:", key="preview_dc_input")
        
        if preview_dc:
            df_dc_lines = q("SELECT * FROM inventory WHERE dc_no=?", [preview_dc.strip()])
            if not df_dc_lines.empty:
                hdr = df_dc_lines.iloc[0]
                
                # ── EXCEL GENERATOR ──
                wb = openpyxl.Workbook()
                ws = wb.active
                ws.title = "Delivery Challan"
                
                ws["A1"] = f"Phone Number: +92 42 36283733"
                ws["A2"] = f"Email Address: vertex.printerlhr@gmail.com"
                ws["A3"] = "DELIVERY CHALLAN"
                ws["A3"].font = Font(bold=True, size=14)
                
                ws["A5"] = f"Customer Name: Gul Ahmed Textile Mills"
                ws["A6"] = f"Destination: {hdr.get('destination','')}"
                ws["A7"] = f"Date: {format_date_str(hdr.get('entry_date',''))}"
                
                # Field Repositioning Fix: Placed directly above Remarks layout row
                ws["A9"] = f"Company PO: {hdr.get('company_token','')}"
                ws["A10"] = f"Token Number: {hdr.get('company_token','')}"
                ws["A11"] = f"Call-Off 290: {hdr.get('call_off_no','')}"
                ws["A12"] = "Remarks Layout Area Below"
                
                # Clean Signatures Block
                ws["A15"] = f"Prepared By: {current_user['full_name']}"
                ws["C15"] = "Receiver Name: _____________________"
                ws["C16"] = "Signature & Stamp: __________________"
                
                xl_buf = io.BytesIO()
                wb.save(xl_buf)
                xl_buf.seek(0)
                
                # ── PDF GENERATOR ──
                pdf_buf = io.BytesIO()
                doc = SimpleDocTemplate(pdf_buf, pagesize=letter, rightMargin=30, leftMargin=30, topMargin=25, bottomMargin=30)
                story = []
                styles = getSampleStyleSheet()
                
                # Header Hierarchy Enforcement
                story.append(Paragraph("<b>Phone Number:</b> +92 42 36283733", styles['Normal']))
                story.append(Paragraph("<b>Email:</b> vertex.printerlhr@gmail.com", styles['Normal']))
                story.append(Spacer(1, 5))
                story.append(Paragraph("<b>DELIVERY CHALLAN</b>", styles['Heading1']))
                story.append(Spacer(1, 10))
                
                story.append(Paragraph(f"<b>Date:</b> {format_date_str(hdr.get('entry_date',''))}", styles['Normal']))
                story.append(Paragraph(f"<b>Destination:</b> {hdr.get('destination','')}", styles['Normal']))
                story.append(Spacer(1, 15))
                
                # Line item grid
                grid_data = [["S.No", "Item Description", "UOM", "Quantity"]]
                for idx, r in df_dc_lines.iterrows():
                    grid_data.append([idx+1, r.get('item_description','') or r['category'], "Nos", round_and_format(r['qty'])])
                t_grid = Table(grid_data, colWidths=[40, 300, 60, 80])
                t_grid.setStyle(TableStyle([('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#f1f5f9'))]))
                story.append(t_grid)
                story.append(Spacer(1, 15))
                
                # Repositioned Metadata Blocks above Remarks
                story.append(Paragraph(f"<b>Company PO:</b> {hdr.get('company_token','')}", styles['Normal']))
                story.append(Paragraph(f"<b>Token Number:</b> {hdr.get('company_token','')}", styles['Normal']))
                story.append(Paragraph(f"<b>Call-Off Number:</b> {hdr.get('call_off_no','')}", styles['Normal']))
                story.append(Spacer(1, 10))
                story.append(Paragraph(f"<b>Remarks:</b> {hdr.get('remark','')}", styles['Normal']))
                story.append(Spacer(1, 40))
                
                # Absolute Bottom Footer Signatures Block
                sig_table_data = [
                    [Paragraph(f"<b>Prepared By:</b><br>{current_user['full_name']}", styles['Normal']),
                     Paragraph("<b>Receiver's Acknowledgement:</b><br>Receiver Name: _____________________<br>Signature & Stamp: __________________", styles['Normal'])]
                ]
                t_sig = Table(sig_table_data, colWidths=[200, 320])
                story.append(t_sig)
                
                doc.build(story)
                pdf_buf.seek(0)
                
                # Rendering Live PDF View In-App
                b64_pdf = base64.b64encode(pdf_buf.getvalue()).decode()
                st.markdown(f'<iframe src="data:application/pdf;base64,{b64_pdf}" width="100%" height="400" style="border:1px solid #334155;border-radius:8px;"></iframe>', unsafe_allow_html=True)
                
                btn_col1, btn_col2 = st.columns(2)
                with btn_col1:
                    st.download_button("⬇️ Download PDF Report", data=pdf_buf, file_name=f"DC_{preview_dc}.pdf", mime="application/pdf")
                with btn_col2:
                    st.download_button("⬇️ Download Excel Report", data=xl_buf, file_name=f"DC_{preview_dc}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            else:
                st.warning("No DC records found matching that ID layout string.")

# ═══════════════════════════════════════════════
# TAB 4 — MASTER LEDGER
# ═══════════════════════════════════════════════
with tab4:
    st.markdown('### 📊 Master Ledger Balance & Shortfall Station')
    df_ledger = q("""
        SELECT so.call_off_no AS "Call-Off No", so.sale_contract AS "Contract #", so.brand AS "Brand",
               so.article AS "Article", so.category AS "Item Type", SUM(so.order_qty) AS "Total Ordered",
               COALESCE(SUM(inv.qty), 0) AS "Total Received",
               SUM(so.order_qty) - COALESCE(SUM(inv.qty), 0) AS "Remaining Balance"
        FROM sheet_orders so
        LEFT JOIN inventory inv ON so.call_off_no = inv.call_off_no AND so.article = inv.article AND so.category = inv.category
        GROUP BY so.call_off_no, so.sale_contract, so.brand, so.article, so.category
    """)
    
    if not df_ledger.empty:
        st.dataframe(df_ledger.assign(
            **{"Total Ordered": df_ledger["Total Ordered"].apply(round_and_format),
               "Total Received": df_ledger["Total Received"].apply(round_and_format),
               "Remaining Balance": df_ledger["Remaining Balance"].apply(round_and_format)}
        ), hide_index=True)
        
        # Vertex Packaging Re-branding Actions & Shortfalls
        st.markdown("#### 🖨️ Vertex Packaging Export Panels")
        item_summary = df_ledger.groupby("Item Type")["Remaining Balance"].sum().to_dict()
        
        m_pdf = generate_ledger_pdf(item_summary, df_ledger, "All", "All", "All", report_type="MASTER")
        st.download_button("🖨️ Print for All Master Ledger (PDF)", data=m_pdf, file_name="Vertex_Master_Ledger.pdf", mime="application/pdf")

# ═══════════════════════════════════════════════
# TAB 6 — BILTY MANAGEMENT
# ═══════════════════════════════════════════════
with tab6:
    st.markdown('### 🚚 Bilty Management Dispatch Center')
    
    # Global Date Filter & Top Header Alignment
    bilty_date_filter = st.date_input("Global Dispatch Date Header Filter", value=date.today(), key="bilty_global_date")
    formatted_bilty_date = bilty_date_filter.strftime("%Y-%m-%d")
    
    st.markdown("---")
    st.markdown(f"#### 📦 Daily Dispatches for Date Summary Heading: {bilty_date_filter.strftime('%d-%m-%Y')}")
    
    # Form input layout row
    with st.form("bilty_entry_form"):
        bc1, bc2, bc3, bc4, bc5 = st.columns(5)
        with bc1: b_coff = st.text_input("Call-Off No")
        with bc2: b_cont = st.text_input("Contract No")
        with bc3: b_art = st.text_input("Article Number")
        with bc4: b_cat = st.selectbox("Category Item Type", ITEM_TYPES)
        with bc5: b_qty = st.number_input("Dispatched Qty", min_value=0)
        
        bc6, bc7, bc8 = st.columns(3)
        with bc6: b_cartons = st.number_input("Number of Cartons", min_value=0)
        with bc7: b_mode = st.text_input("Transport Mode / Vehicle No", placeholder="e.g. By Air, Vehicle No.")
        with bc8: st.markdown("<br>", unsafe_allow_html=True); submit_bilty = st.form_submit_button("Record Dispatch Row")
        
    if submit_bilty:
        with _get_engine().begin() as conn:
            conn.execute(text("INSERT INTO bilty (call_off_no, contract_no, article, category, qty, cartons, transport_mode, bilty_date, created_at) VALUES (:co,:cn,:art,:cat,:q,:car,:m,:d,:c)"),
                         {"co": b_coff, "cn": b_cont, "art": b_art, "cat": b_cat, "q": b_qty, "car": b_cartons, "m": b_mode, "d": formatted_bilty_date, "c": str(datetime.datetime.now())})
        st.success("Bilty Logged!")
        st.rerun()

    # Query matching records
    df_bilty_day = q("SELECT contract_no, article, qty, cartons, transport_mode FROM bilty WHERE bilty_date=?", [formatted_bilty_date])
    
    if not df_bilty_day.empty:
        # Layout Columns: Showing required dynamic fields
        display_df = df_bilty_day.copy()
        display_df.columns = ["Contract #", "Article Number", "Dispatched Quantity", "Number of Cartons", "Transport Mode"]
        st.markdown("**Routing Path Tracker:** Lahore Office to Karachi Delivery Warehouse Terminal")
        st.dataframe(display_df, hide_index=True)
        
        # Dual Summary / Totals Implementation
        st.markdown("### 📊 Dual Summary Analysis Layout")
        sum_col1, sum_col2 = st.columns(2)
        
        with sum_col1:
            st.markdown("##### 📄 Contract-Wise Sum Matrix")
            df_contract_sum = df_bilty_day.groupby("contract_no")[["qty", "cartons"]].sum().reset_index()
            df_contract_sum.columns = ["Contract #", "Total Dispatched Qty", "Total Cartons"]
            st.dataframe(df_contract_sum, hide_index=True)
            
        with sum_col2:
            st.markdown("##### 🏷️ Item/Category-Wise Sum Matrix")
            df_cat_sum = q("SELECT category AS 'Item Type', SUM(qty) AS 'Total Quantities' FROM bilty WHERE bilty_date=? GROUP BY category", [formatted_bilty_date])
            st.dataframe(df_cat_sum, hide_index=True)
            
        # Summary Export Operations Setup
        st.markdown("---")
        ex_col1, ex_col2 = st.columns(2)
        
        # Excel Generator Data
        bilty_xlsx = io.BytesIO()
        with pd.ExcelWriter(bilty_xlsx, engine='openpyxl') as writer:
            df_contract_sum.to_excel(writer, sheet_name="Contract Sum", index=False)
            df_cat_sum.to_excel(writer, sheet_name="Category Sum", index=False)
        bilty_xlsx.seek(0)
        
        with ex_col1:
            st.download_button("Export to Excel", data=bilty_xlsx, file_name=f"Bilty_Summary_{formatted_bilty_date}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        with ex_col2:
            # Simple PDF summary document
            bilty_pdf = io.BytesIO()
            bilty_doc = SimpleDocTemplate(bilty_pdf, pagesize=letter)
            b_story = [Paragraph(f"Bilty Daily Dispatch Summary Report - {format_date_str(formatted_bilty_date)}", styles['Heading1']), Spacer(1, 15)]
            bilty_doc.build(b_story)
            bilty_pdf.seek(0)
            st.download_button("Export to PDF", data=bilty_pdf, file_name=f"Bilty_Summary_{formatted_bilty_date}.pdf", mime="application/pdf")
    else:
        st.info("No dispatches registered yet on this date header filter constraint.")
