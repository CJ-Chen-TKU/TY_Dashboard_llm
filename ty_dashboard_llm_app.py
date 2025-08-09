 # py code beginning

# ty_dashboard_llm_app.py

import time
import base64
import json
import io
import re
import contextlib
import importlib
import subprocess
import sys
import os

import pandas as pd
import requests
import streamlit as st
from msal import ConfidentialClientApplication
from openai import OpenAI

# ─────────────────────────────────────────────────────────────
# Font config for Chinese output (LOCAL file only; no downloads)
FONT_DIR  = "./fonts"
FONT_NAME = "NotoSansTC"
FONT_PATH = os.path.join(FONT_DIR, "NotoSansTC-Regular.otf")

# Warn at startup if font is missing
if not os.path.exists(FONT_PATH):
    st.warning(
        "⚠️ Chinese font not found at './fonts/NotoSansTC-Regular.otf'.\n"
        "Please add 'NotoSansTC-Regular.otf' to the 'fonts/' folder in your repo "
        "to enable Chinese PDF/chart output."
    )

def make_unicode_pdf(font_path: str = FONT_PATH, size: int = 12):
    """
    Create an fpdf2 PDF preloaded with a Unicode Traditional Chinese font.
    Requires: ./fonts/NotoSansTC-Regular.otf
    """
    from fpdf import FPDF
    if not os.path.exists(font_path):
        raise FileNotFoundError(
            f"Font not found at {font_path}. Ensure 'fonts/NotoSansTC-Regular.otf' exists in the repo."
        )
    pdf = FPDF()
    pdf.add_page()
    pdf.add_font(FONT_NAME, "", font_path, uni=True)
    pdf.set_font(FONT_NAME, "", size)
    return pdf

def enable_matplotlib_chinese(font_path: str = FONT_PATH):
    """
    Register the local TC font for matplotlib so Chinese labels render correctly.
    Robust: detect the actual family name from the OTF and set rcParams accordingly.
    """
    import matplotlib
    from matplotlib import font_manager

    if not os.path.exists(font_path) or os.path.getsize(font_path) < 10_000:
        # Still ensure minus sign renders correctly
        matplotlib.rcParams["axes.unicode_minus"] = False
        return False

    try:
        font_manager.fontManager.addfont(font_path)
        fam = font_manager.FontProperties(fname=font_path).get_name()
        matplotlib.rcParams["font.family"] = [fam]
        matplotlib.rcParams["axes.unicode_minus"] = False
        return True
    except Exception:
        matplotlib.rcParams["axes.unicode_minus"] = False
        return False

# ─────────────────────────────────────────────────────────────
# Static workspace (TYCGEDB FORMAL)
WORKSPACE_ID = "41bfeb46-6ff1-4baa-824a-9681be3a586d"
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

# ─────────────────────────────────────────────────────────────
# Sidebar credentials (UI only)
st.sidebar.header("🔐 Credentials")
OPENAI_KEY = st.sidebar.text_input("OpenAI API Key", type="password")
TENANT_ID  = st.sidebar.text_input("Azure Tenant ID",  value="ba129fe2-5c7b-4f4b-9670-ed7494972f23")
CLIENT_ID  = st.sidebar.text_input("Azure Client ID",  value="770a4905-e32f-493e-817f-9731db47761b")
CLIENT_SECRET = st.sidebar.text_input("Azure Client Secret (VALUE)", type="password")

# Optional toggles
ALLOW_AUTOINSTALL = st.sidebar.checkbox(
    "Allow auto-install missing libs (matplotlib/plotly/seaborn/fpdf2)",
    value=False,
)
show_msal_debug = st.sidebar.checkbox("Show MSAL token debug", value=True)

# 🔍 fpdf2 startup check (in sidebar)
with st.sidebar.expander("📄 PDF library check", expanded=False):
    try:
        spec = importlib.util.find_spec("fpdf")
        if spec is None:
            st.error("❌ `fpdf2` is not installed. Please `pip install fpdf2` (or add to requirements.txt).")
        else:
            import fpdf  # noqa
            ver = getattr(fpdf, "__version__", None)
            if ver is None:
                st.warning("⚠️ Detected old `fpdf` (1.x). Uninstall it and install `fpdf2`.\n"
                           "Commands: `pip uninstall -y fpdf` then `pip install fpdf2`")
            else:
                st.success(f"✅ fpdf2 is installed (version {ver})")
    except Exception as e:
        st.error(f"PDF check error: {e}")

if not all([OPENAI_KEY, TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
    st.sidebar.warning("Please fill in OpenAI key and Azure credentials to continue.")
    st.stop()

# OpenAI client
client = OpenAI(api_key=OPENAI_KEY)

# ─────────────────────────────────────────────────────────────
# Token helpers (with debug meta)
@st.cache_data(ttl=3300, show_spinner=False)
def get_powerbi_token_with_meta(tenant_id: str, client_id: str, client_secret: str):
    app = ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret,
    )
    result = app.acquire_token_for_client(scopes=["https://analysis.windows.net/powerbi/api/.default"])
    return result.get("access_token", ""), result  # (token, full result dict)

@st.cache_data(ttl=3300, show_spinner=False)
def get_fabric_token_with_meta(tenant_id: str, client_id: str, client_secret: str):
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://api.fabric.microsoft.com/.default",
    }
    try:
        r = requests.post(token_url, data=data, timeout=60)
        meta = {"http_status": r.status_code}
        if not r.ok:
            try:
                meta.update(r.json())
            except Exception:
                meta["text"] = r.text[:500]
            return "", meta
        j = r.json()
        meta.update({k: j.get(k) for k in ("token_type", "expires_in")})
        return j.get("access_token", ""), meta
    except Exception as e:
        return "", {"exception": str(e)}

# ─────────────────────────────────────────────────────────────
# REST helpers
@st.cache_data(ttl=300, show_spinner=False)
def list_datasets(pbi_token: str, workspace_id: str) -> list[dict]:
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets"
    r = requests.get(url, headers={"Authorization": f"Bearer {pbi_token}"}, timeout=60)
    if not r.ok:
        return []
    return r.json().get("value", [])

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

# ─────────────────────────────────────────────────────────────
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
# Safe code execution (+ optional auto-install libs)
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
    if "matplotlib" in code_str:
        pkgs_needed.append("matplotlib")
        # Preload plt for GPT code
        try:
            import matplotlib.pyplot as plt  # noqa
            local_vars["plt"] = plt
        except Exception:
            pass
    if "seaborn" in code_str:
        pkgs_needed.append("seaborn")
    if "plotly" in code_str:
        pkgs_needed.append("plotly")
    if "fpdf" in code_str:
        pkgs_needed.append("fpdf2")

    # Check missing packages
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
                importlib_imported = importlib.import_module(pkg)
            except ImportError:
                st.error(f"`{pkg}` still not importable after install. Please add it to requirements.txt.")
                return
        # If matplotlib just got installed, preload plt
        if "matplotlib" in missing:
            try:
                import matplotlib.pyplot as plt  # noqa
                local_vars["plt"] = plt
            except Exception:
                pass

    # Auto‑prepend font setup so fp is always available even if GPT forgets
    prelude = (
        "import os
"
        "import matplotlib.pyplot as plt\n"
        "try:\n"
        "    ok = enable_matplotlib_chinese(FONT_PATH)\n"
        "except Exception:\n"
        "    ok = False\n"
        "try:\n"
        "    fp = get_tc_fontprops()\n"
        "except Exception:\n"
        "    fp = None\n"
    )

    # Execute and capture stdout
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            exec(prelude + code_str, {}, local_vars)
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
            msg = str(e)
            if "Can not load face" in msg or "freetype" in msg.lower():
                st.error(
                    "Font load error (FreeType). Ensure './fonts/NotoSansTC-Regular.otf' exists and is valid. "
                    "The code auto-calls enable_matplotlib_chinese(FONT_PATH) and uses FontProperties via fp."
                )
                return
            st.error(f"Error running GPT code: {e}")
            return
    out = buf.getvalue()
    if out:
        st.text(out)

    # Try to display charts (plotly or matplotlib)
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
st.title("📊 Power BI → Fabric Tables → GPT-4o (Chinese font ready)")

# Acquire tokens (with debug)
pbi_token, pbi_meta = get_powerbi_token_with_meta(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
fabric_token, fabric_meta = get_fabric_token_with_meta(TENANT_ID, CLIENT_ID, CLIENT_SECRET)

# Sidebar debug panel
if show_msal_debug:
    with st.sidebar.expander("🔎 MSAL / Token Debug", expanded=False):
        st.caption("Power BI token result (MSAL):")
        if pbi_token:
            st.success("Power BI token acquired ✅")
        else:
            st.error("Power BI token FAILED ❌")
        if isinstance(pbi_meta, dict):
            keys = ["error", "error_description", "correlation_id", "ext_expires_in", "expires_in"]
            dbg = {k: pbi_meta.get(k) for k in keys if k in pbi_meta}
            st.json(dbg or {"info": "no error fields returned"})

        st.caption("Fabric token result (HTTP):")
        if fabric_token:
            st.success("Fabric token acquired ✅")
        else:
            st.error("Fabric token FAILED ❌")
        st.json(fabric_meta if isinstance(fabric_meta, dict) else {"meta": str(fabric_meta)})

# Fail fast if missing Power BI token
if not pbi_token:
    st.error("Failed to get Power BI token. Check the debug panel on the left for details.")
    st.stop()

# (Optional warning if Fabric token missing — table discovery may fail)
if not fabric_token:
    st.warning("Fabric token not available. Table discovery via Fabric may fail. Check the debug panel.")

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
    "data_dict": dfs,          # original dict (keys are full table names)
    "x_axis": x_axis,
    "long_df": make_long_df(dfs),
    "table_vars": {},          # will be filled below
    "make_unicode_pdf": make_unicode_pdf,
    "enable_matplotlib_chinese": enable_matplotlib_chinese,
    "FONT_PATH": FONT_PATH,
    "FONT_NAME": FONT_NAME,
}
# add per-table variables with slugified names
for orig_name, df in dfs.items():
    var = slugify(orig_name)
    safe_locals[var] = df
    safe_locals["table_vars"][var] = {"orig": orig_name, "columns": df.columns.tolist()}

# Add font helpers for GPT-generated code
from matplotlib.font_manager import FontProperties
def get_tc_fontprops():
    """Return FontProperties for the TC font if present, else None."""
    return FontProperties(fname=FONT_PATH) if os.path.exists(FONT_PATH) else None
safe_locals["FontProperties"] = FontProperties
safe_locals["get_tc_fontprops"] = get_tc_fontprops

# ─────────────────────────────────────────────────────────────
# Multi-turn chat
if "chat" not in st.session_state:
    st.session_state.chat = [
        {"role": "system", "content": """You are a helpful data analyst.

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
```python
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

