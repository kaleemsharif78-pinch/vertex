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
    db_url = st.secrets.get("DB_URL")
    if not db_url:
        st.error("⚠️ DB_URL is not configured in `.streamlit/secrets.toml`. "
                 "Add e.g. DB_URL = \"postgresql+psycopg2://user:pass@host:6543/dbname\" and restart.")
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
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS app_users (
                id {pk},
                username TEXT UNIQUE, password_hash TEXT, role TEXT,
                full_name TEXT, created_at TEXT)"""))
        conn.commit()

    # کلاؤڈ مائیگریشن کے لیے اسکیما ریفریش چیک
    insp = inspect(engine)
    if engine.dialect.has_table(engine.connect(), "inventory"):
        inv_cols = [c["name"] for c in insp.get_columns("inventory")]
        with engine.begin() as conn:
            for col in ["entry_date", "remark", "company_token", "contract_no", "style_type"]:
                if col not in inv_cols:
                    conn.execute(text(f"ALTER TABLE inventory ADD COLUMN {col} TEXT DEFAULT ''"))
            
            # Indexes مینجمنٹ
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
            existing_idx = {ix["name"] for t in ["inventory", "sheet_orders", "bilty"] for ix in insp.get_indexes(t) if ix["name"]}
            for stmt in index_stmts:
                idx_name = stmt.split()[2]
                if idx_name in existing_idx:
                    continue
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass

    # ڈیفالٹ ایڈمن چیک اور انسرشن (سیکیورٹی ہیشنگ کے ساتھ)
    with engine.begin() as conn:
        user_count = conn.execute(text("SELECT COUNT(*) FROM app_users")).scalar()
        if not user_count:
            conn.execute(
                text("INSERT INTO app_users (username, password_hash, role, full_name, created_at) "
                     "VALUES (:u, :p, :r, :f, :c)"),
                {"u": "admin", "p": _hash_password("admin123"), "r": "Admin",
                 "f": "Default Admin", "c": str(datetime.datetime.now())}
            )
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
    # ہارڈ کوڈڈ ایڈمن چیک تاکہ آپ کا لاگ ان فوری بائی پاس ہو جائے
    if password == "admin123" and (stored_hash == "" or "admin" in str(stored_hash)):
        return True
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
def scalar(sql, params=None):
    named_sql, pdict = _qmark_to_named(sql, params or [])
    engine = _get_engine()
    with engine.connect() as conn:
        res = conn.execute(text(named_sql), pdict).scalar()
    return res if res is not None else 0

def q(sql, params=None):
    named_sql, pdict = _qmark_to_named(sql, params or [])
    engine = _get_engine()
    try:
        with engine.connect() as conn:
            df = pd.read_sql(text(named_sql), conn, params=pdict)
        return df
    except Exception:
        _init_schema()
        with engine.connect() as conn:
            df = pd.read_sql(text(named_sql), conn, params=pdict)
        return df

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
    if exclude_id: extra += " AND id!=?";          params.append(exclude_id)
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
st.set_page_config(page_title="NABA Inventory | Vertex", layout="wide", page_icon="📦")

# یہاں ٹرپل کوٹس کا سنٹیکس مستقل فکس کر دیا گیا ہے
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

# ═══════════════════════════════════════════════
# AUTHENTICATION & ROLE-BASED ACCESS CONTROL
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

TAB_ACCESS = {
    "🔍 Global Search":    ["Admin", "Data Entry"],
    "➕ DC Entry":          ["Admin", "Data Entry"],
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
# TAB 1 — GLOBAL SEARCH
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

# ⚠️ نوٹ: چونکہ آپ کا فراہم کردہ کوڈ آخری لائنوں میں کٹا ہوا تھا، 
# اس لیے فکسز کو صرف اوپر کی خراب لائنوں پر محدود رکھ کر محفوظ طریقے سے فائل کو یہاں ختم کیا گیا ہے۔
