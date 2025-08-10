 # py code beginning

# py code beginning

# ty_dashboard_llm_app.py
import os
import time
import base64
import json
import io
import re
import contextlib
import importlib
import subprocess
import sys

import pandas as pd
import requests
import streamlit as st
from msal import ConfidentialClientApplication
from openai import OpenAI

# ─────────────────────────────────────────────────────────────
# Static workspace (TYCGEDB FORMAL). Change if needed.
WORKSPACE_ID = "41bfeb46-6ff1-4baa-824a-9681be3a586d"
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

# Unicode font (Traditional Chinese) for PDFs and charts

# === Local font path (no download) ===
FONT_DIR  = "./fonts"
FONT_PATH = os.path.join(FONT_DIR, "NotoSansTC-Regular.otf")

def ensure_font():
    """Ensure Traditional Chinese font file exists locally."""
    if not os.path.exists(FONT_PATH):
        raise FileNotFoundError(f"Chinese font not found at: {FONT_PATH}")

# Call immediately so app stops early if font missing
ensure_font()



def enable_matplotlib_chinese(font_path: str) -> bool:
    """Enable the given Chinese font for matplotlib globally."""
    try:
        import matplotlib.pyplot as plt
        from matplotlib import font_manager as fm
        fm.fontManager.addfont(font_path)
        fam = fm.FontProperties(fname=font_path).get_name()
        plt.rcParams["font.sans-serif"] = [fam, "Arial", "DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        return True
    except Exception as e:
        st.error(f"Matplotlib Chinese font init failed: {e}")
        return False

def get_tc_fontprops():
    """Return a FontProperties object for the Traditional Chinese font."""
    from matplotlib.font_manager import FontProperties
    return FontProperties(fname=FONT_PATH)

# Enable matplotlib Chinese font globally
FONT_OK = enable_matplotlib_chinese(FONT_PATH)
fp = get_tc_fontprops()

# ─────────────────────────────────────────────────────────────
# Sidebar credentials (UI only)
st.sidebar.header("🔐 Credentials")
OPENAI_KEY = st.sidebar.text_input("OpenAI API Key", type="password")
TENANT_ID  = st.sidebar.text_input("Azure Tenant ID",  value="ba129fe2-5c7b-4f4b-9670-ed7494972f23")
CLIENT_ID  = st.sidebar.text_input("Azure Client ID",  value="770a4905-e32f-493e-817f-9731db47761b")
CLIENT_SECRET = st.sidebar.text_input("Azure Client Secret (VALUE)", type="password")

# Optional: allow runtime pip installs (off by default for cloud)
#ALLOW_AUTOINSTALL = st.sidebar.checkbox(
#    "Allow auto-install missing libs (matplotlib/plotly/seaborn/fpdf2)",
#    value=False,
#)

ALLOW_AUTOINSTALL = False

if not all([OPENAI_KEY, TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
    st.sidebar.warning("Please fill in OpenAI key and Azure credentials to continue.")
    st.stop()

# OpenAI 1.x client
client = OpenAI(api_key=OPENAI_KEY)

# ─────────────────────────────────────────────────────────────
# Token helpers
@st.cache_data(ttl=3300, show_spinner=False)
def get_powerbi_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    app = ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=["https://analysis.windows.net/powerbi/api/.default"])
    return result.get("access_token", "")

@st.cache_data(ttl=3300, show_spinner=False)
def get_fabric_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://api.fabric.microsoft.com/.default",
    }
    r = requests.post(token_url, data=data, timeout=60)
    if not r.ok:
        return ""
    return r.json().get("access_token", "")

# ─────────────────────────────────────────────────────────────
# REST helpers
@st.cache_data(ttl=300, show_spinner=False)
def list_datasets(pbi_token: str, workspace_id: str) -> list[dict]:
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets"
    r = requests.get(url, headers={"Authorization": f"Bearer {pbi_token}"}, timeout=60)
    if not r.ok:
        return []
    return r.json().get("value", [])

# Fabric: getDefinition (TMSL) → schema
@st.cache_data(ttl=900, show_spinner=False)
def get_model_schema_via_fabric(workspace_id: str, dataset_id: str, fabric_token: str) -> dict:
    """Return {"tables": {name:[cols...]}, "raw": model_json} using Fabric semantic model export."""
    if not fabric_token:
        return {"tables": {}, "raw": {}}

    fh = {"Authorization": f"Bearer {fabric_token}"}
    start = requests.post(
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/semanticModels/{dataset_id}/getDefinition?format=TMSL",
        headers=fh, timeout=60
    )
    resp = start
    while resp.status_code == 202:
        op_url = resp.headers.get("Location")
        if not op_url:
            break
        retry = int(resp.headers.get("Retry-After", "3"))
        time.sleep(retry)
        resp = requests.get(op_url, headers=fh, timeout=60)

    op_id = resp.headers.get("x-ms-operation-id") or (
        resp.request.url.rstrip("/").split("/")[-1] if getattr(resp, "request", None) else None
    )
    if not op_id:
        return {"tables": {}, "raw": {}}

    result = requests.get(f"https://api.fabric.microsoft.com/v1/operations/{op_id}/result", headers=fh, timeout=60)
    if not result.ok:
        return {"tables": {}, "raw": {}}

    j = result.json()
    parts = j.get("definition", {}).get("parts", [])
    model_json = None
    for p in parts:
        if p.get("path", "").endswith(("model.tmsl", "model.bim", "model.json")):
            payload = p.get("payload")
            if payload:
                try:
                    model_json = json.loads(base64.b64decode(payload).decode("utf-8", "ignore"))
                except Exception:
                    model_json = None
            break

    if not model_json:
        return {"tables": {}, "raw": {}}

    tables = model_json.get("model", {}).get("tables", [])
    schema = {t["name"]: [c["name"] for c in t.get("columns", [])] for t in tables}
    return {"tables": schema, "raw": model_json}

# DAX executeQueries
def execute_dax(workspace_id: str, dataset_id: str, dax: str, pbi_token: str) -> pd.DataFrame:
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
    headers = {"Authorization": f"Bearer {pbi_token}", "Content-Type": "application/json"}
    payload = {"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if not r.ok:
        return pd.DataFrame()
    res = r.json().get("results", [])
    if not res:
        return pd.DataFrame()
    t = res[0].get("tables", [{}])[0]
    cols = [c["name"] for c in t.get("columns", [])]
    rows = t.get("rows", [])
    if rows:
        df = pd.DataFrame(rows)
        try:
            df.columns = [c.split("[")[-1].rstrip("]") for c in df.columns]
        except Exception:
            pass
        return df
    return pd.DataFrame(columns=cols)

@st.cache_data(ttl=900, show_spinner="Fetching table via DAX…")
def fetch_table_topn(workspace_id: str, dataset_id: str, table_name: str, pbi_token: str, n: int = 200, order_by: str | None = None, ascending=True) -> pd.DataFrame:
    if order_by:
        direction = "ASC" if ascending else "DESC"
        dax = f"EVALUATE TOPN({n}, '{table_name}', '{table_name}'[{order_by}], {direction})"
    else:
        dax = f"EVALUATE TOPN({n}, '{table_name}')"
    return execute_dax(workspace_id, dataset_id, dax, pbi_token)

# ─────────────────────────────────────────────────────────────

def make_unicode_pdf(font_path: str = FONT_PATH, size: int = 12):
    """Create an fpdf2 PDF preloaded with a Unicode TC font."""
    from fpdf import FPDF
    pdf = FPDF()
    pdf.add_page()
    pdf.add_font("NotoSansTC", "", font_path, uni=True)
    pdf.set_font("NotoSansTC", "", size)
    return pdf

# ─────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────
# Safe code execution + (optional) auto-install plotting libs
def run_safe_python(code_str: str, local_vars: dict, allow_autoinstall: bool = False):
    # Block dangerous stuff
    forbidden = [
        "import os", "subprocess", "shutil", "sys.exit", "import sys",
        "open(", "exec(", "eval(", "__import__", "os.system", "os.remove", "os.rmdir",
        "Path(", "pickle", "dotenv", "requests.delete", "requests.put", "requests.post("
    ]
    if any(s in code_str for s in forbidden):
        st.error("⚠️ Unsafe code detected. Execution blocked.")
        return

    # Detect required packages from the code
    pkgs_needed = []
    if "matplotlib" in code_str: pkgs_needed.append("matplotlib")
    if "seaborn"   in code_str: pkgs_needed.append("seaborn")
    if "plotly"    in code_str: pkgs_needed.append("plotly")
    if "fpdf"      in code_str: pkgs_needed.append("fpdf2")

    missing = []
    for pkg in pkgs_needed:
        try:
            importlib.import_module(pkg)
        except ImportError:
            missing.append(pkg)

    if missing:
        if not allow_autoinstall:
            st.warning(
                "Missing packages: " + ", ".join(missing) +
                "\n\nEnable 'Allow auto-install missing libs' in the sidebar OR add them to requirements.txt."
            )
            return
        # Try install, but do not crash app on failure
        for pkg in missing:
            try:
                res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", pkg],
                    capture_output=True, text=True, check=False
                )
                if res.returncode != 0:
                    st.error(
                        f"Could not install `{pkg}` (exit {res.returncode}). "
                        f"Please add it to requirements.txt.\n\n{res.stderr[:500]}"
                    )
                    return
            except Exception as e:
                st.error(f"Auto-install failed for `{pkg}`: {e}. Please add it to requirements.txt.")
                return
        # Re-import after install
        for pkg in missing:
            try:
                importlib.import_module(pkg)
            except ImportError:
                st.error(f"`{pkg}` still not importable after install. Please add it to requirements.txt.")
                return

    # Execute and capture stdout
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            exec(code_str, {}, local_vars)
        except KeyError as e:
            bad = str(e).strip("'")
            tv = local_vars.get("table_vars", {})
            valid = ", ".join(sorted(tv.keys())) if tv else "(none)"
            details = "; ".join([f"{k}:{len(v['columns'])} cols" for k, v in tv.items()])
            st.error(f"Unknown name/column: '{bad}'. Valid table variables: {valid}. {details}")
            return
        except AttributeError as e:
            st.error(f"Attribute error in GPT code: {e}")
            return
        except Exception as e:
            st.error(f"Error running GPT code: {e}")
            return
    out = buf.getvalue()
    if out:
        st.text(out)

    # Try to display charts
    try:
        if "fig" in local_vars:
            fig = local_vars["fig"]
            # Plotly?
            try:
                import plotly.graph_objects as go  # noqa
                if hasattr(fig, "to_plotly_json") or hasattr(fig, "to_dict"):
                    st.plotly_chart(fig, use_container_width=True)
                    return
            except Exception:
                pass
            # Matplotlib?
            try:
                import matplotlib.pyplot as plt  # noqa
                st.pyplot(fig)
                return
            except Exception:
                pass

        # If no 'fig', attempt matplotlib current figure
        if "matplotlib" in code_str:
            try:
                import matplotlib.pyplot as plt  # noqa
                st.pyplot(plt.gcf())
                return
            except Exception:
                pass
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# Helpers to make GPT robust to names
def slugify(name: str) -> str:
    s = re.sub(r"\W+", "_", name, flags=re.U).strip("_").lower()
    return s or "table"

def make_long_df(dfs: dict[str, pd.DataFrame]) -> pd.DataFrame:
    parts = []
    for name, d in dfs.items():
        tmp = d.copy()
        tmp.insert(0, "_table", name)
        parts.append(tmp)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

# ─────────────────────────────────────────────────────────────
# UI
st.title("📊 Power BI → Fabric Tables → GPT-4o (multi-turn, safe charts, Unicode PDFs)")

# Acquire tokens
pbi_token = get_powerbi_token(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
if not pbi_token:
    st.error("Failed to get Power BI token. Check Azure credentials.")
    st.stop()
fabric_token = get_fabric_token(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
if not fabric_token:
    st.warning("Fabric token not available. Table discovery via Fabric may fail.")

# Pick dataset (dynamic from REST)
datasets = list_datasets(pbi_token, WORKSPACE_ID)
if not datasets:
    st.error("No datasets found in the workspace (or missing permissions).")
    st.stop()

ds_labels = [f"{d['name']} ({d['id'][:8]}…)" for d in datasets]
ds_idx = st.selectbox("Select a dataset", options=range(len(datasets)), format_func=lambda i: ds_labels[i])
dataset_id = datasets[ds_idx]["id"]

# Discover tables via Fabric model
schema = get_model_schema_via_fabric(WORKSPACE_ID, dataset_id, fabric_token)
tables_dict = schema.get("tables", {})
table_names = sorted(tables_dict.keys())

if not table_names:
    st.error("Couldn’t discover tables from Fabric model. Check Fabric permissions.")
    st.stop()

# Multi-select tables + TOPN picker
picked_tables = st.multiselect("Select tables", options=table_names, default=table_names[:1])
top_n = st.slider("Rows per table (TOPN)", 50, 5000, 200, 50)

if not picked_tables:
    st.info("Pick at least one table to continue.")
    st.stop()


# Fetch selected tables, preview, CSV
dfs: dict[str, pd.DataFrame] = {}
for tbl in picked_tables:
    df = fetch_table_topn(WORKSPACE_ID, dataset_id, tbl, pbi_token, n=top_n)
    if df.empty:
        st.warning(f"⚠️ No data returned for `{tbl}` (empty table or DAX blocked).")
        continue
    dfs[tbl] = df
    with st.expander(f"📄 Preview — {tbl}", expanded=False):
        st.write("**Columns:**", df.columns.tolist())
        st.dataframe(df.head(20))
        st.download_button(
            f"⬇️ Download `{tbl}` (CSV)",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"{tbl}.csv",
            mime="text/csv",
            key=f"dl_{tbl}"
        )

if not dfs:
    st.stop()

# Suggest a common X-axis
common_cols = set.intersection(*(set(df.columns) for df in dfs.values()))
x_axis = None
for cand in ["Date", "Month"]:
    if cand in common_cols:
        x_axis = cand
        break
if not x_axis and common_cols:
    x_axis = sorted(common_cols)[0]  # fallback

if x_axis:
    st.info(f"Suggested common X-axis: `{x_axis}`")
else:
    st.info("No common X-axis column found.")

# Build safe locals for GPT code + variable hints
safe_locals = {
    "pd": pd,
    "st": st,
    "data_dict": dfs,
    "x_axis": x_axis,
    "long_df": make_long_df(dfs),
    "table_vars": {},
    "make_unicode_pdf": make_unicode_pdf,
    "FONT_PATH": FONT_PATH,
    "enable_matplotlib_chinese": enable_matplotlib_chinese,
    "get_tc_fontprops": get_tc_fontprops,  # <-- must exist now
    "fp": fp,                               # <-- inject ready FontProperties
    "FONT_OK": FONT_OK,                     # optional for debugging
}

# add per-table variables with slugified names
for orig_name, df in dfs.items():
    var = slugify(orig_name)
    safe_locals[var] = df
    safe_locals["table_vars"][var] = {"orig": orig_name, "columns": df.columns.tolist()}

# ─────────────────────────────────────────────────────────────
# Multi-turn chat (strict plotting rules, escaped code fence)
if "chat" not in st.session_state:
    st.session_state.chat = [
        {
            "role": "system",
            "content": """You are a helpful data analyst.

HARD RULES (MUST FOLLOW):
- Assume chart titles/labels/legends may contain Chinese at any time.
- ALWAYS call: ok = enable_matplotlib_chinese(FONT_PATH)  (already available).
- ALWAYS set: fp = get_tc_fontprops()  (already available).
- ALWAYS pass fontproperties=fp (and legend(prop=fp)) on ALL titles/labels/legends/text/annotate calls,
  regardless of language (safe for English; required for Chinese).
- Prefer matplotlib. Do NOT use seaborn/plotly/fpdf unless explicitly requested.
- Avoid file I/O, network calls, or reading external images/files.
- Use the combined dataframe `long_df` for cross-table comparisons with a `_table` column.
- Only use per-table variables listed in `table_vars` (keys only). Do NOT invent names.
- When comparing tables, prefer the common x-axis variable `x_axis` if available.
- Output Python code in triple backticks ```python ...``` and create `fig` for the plot when possible.
- Validate columns; if missing, print available columns and exit cleanly.

TEMPLATE (copy/adapt for any matplotlib chart):
\`\`\`python
import matplotlib.pyplot as plt

# Font setup (already injected in the runtime as well)
ok = enable_matplotlib_chinese(FONT_PATH)
fp = get_tc_fontprops()

# ... data prep ...

plt.figure(figsize=(8,4))
# e.g. plt.plot(df['Month'], df['ElectricSalary'], label='電費薪資')
title_txt = '電費薪資'  # replace with your computed title
xlabel_txt = '月份'
ylabel_txt = '薪資'

plt.title(title_txt, fontproperties=fp)
plt.xlabel(xlabel_txt, fontproperties=fp)
plt.ylabel(ylabel_txt, fontproperties=fp)
plt.legend(prop=fp)
plt.xticks(rotation=45)
fig = plt.gcf()
\`\`\`
"""  # <<< END OF PROMPT STRING
        }
    ]

# Helper: compact data + allowed names (sample up to 50 rows per table)
def build_user_payload(question: str, locals_env: dict, per_table_rows=50) -> str:
    samples = []
    for name, df in locals_env["data_dict"].items():
        samples.append(f"""### Table: {name}
{df.head(per_table_rows).to_csv(index=False)}""")
    samples_blob = "\n\n".join(samples)
    allowed = ", ".join(sorted(locals_env["table_vars"].keys())) or "(none)"
    cols_hint = "\n".join([f"{k}: {locals_env['table_vars'][k]['columns']}" for k in sorted(locals_env["table_vars"].keys())])
    return (
        f"{question}\n\n"
        f"Allowed per-table variables (use keys exactly, no new names): {allowed}\n"
        f"Columns per variable:\n{cols_hint}\n"
        f"Common X-axis (if any): {locals_env.get('x_axis')}\n"
        f"Prefer using the combined dataframe `long_df` with `_table` as the series label when comparing tables.\n\n"
        f"Samples:\n{samples_blob}\n"
    )

# === Ask OpenAI (right after table selection) ===
st.subheader("🤖 Ask OpenAI about the selected tables")
with st.form("ask_openai_form", clear_on_submit=False):
    user_q = st.text_area(
        "Your question / request",
        placeholder="e.g., 用 long_df 依 `_table` 分組，畫每月趨勢線；標題與標籤需支援中文字型。",
        height=120,
    )
    c1, c2, _ = st.columns([1.3, 1, 4])
    ask_clicked = c1.form_submit_button("🤖 Ask OpenAI")
    reset_clicked = c2.form_submit_button("🔄 Reset")

if reset_clicked:
    sys_msg = st.session_state.chat[0]
    st.session_state.chat = [sys_msg]
    st.rerun()

if ask_clicked:
    if not user_q.strip():
        st.warning("Please enter a question.")
    else:
        payload = build_user_payload(user_q, safe_locals, per_table_rows=50)
        st.session_state.chat.append({"role": "user", "content": payload})
        with st.spinner("Calling GPT-4o…"):
            resp = client.chat.completions.create(
                model="gpt-4o",
                messages=st.session_state.chat,
                temperature=0.3,
            )
        reply = resp.choices[0].message.content
        st.session_state.chat.append({"role": "assistant", "content": reply})
        st.rerun()

# Optional: show conversation
with st.expander("🧵 Conversation", expanded=False):
    for m in st.session_state.chat:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

# Execute last assistant code block if present
if st.session_state.chat and st.session_state.chat[-1]["role"] == "assistant":
    last = st.session_state.chat[-1]["content"]
    code_match = re.search(r"```python(.*?)```", last, re.S | re.I)
    if code_match:
        st.markdown("**🛠 Executing GPT-generated code…**")
        code_block = code_match.group(1)
        run_safe_python(code_block, safe_locals, allow_autoinstall=ALLOW_AUTOINSTALL)


