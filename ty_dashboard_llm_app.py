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
import matplotlib.pyplot as plt

import pandas as pd
import requests
import streamlit as st
from msal import ConfidentialClientApplication
from openai import OpenAI

from datetime import datetime
from zoneinfo import ZoneInfo  # Python 3.9+
ts = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y%m%d_%H%M%S")


try:
    importlib.import_module("fpdf2")
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "fpdf2"], check=True)



# ─────────────────────────────────────────────────────────────
# Static workspace (TYCGEDB FORMAL). Change if needed.
WORKSPACE_ID = "41bfeb46-6ff1-4baa-824a-9681be3a586d"
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

# Unicode font (Traditional Chinese) for PDFs and charts

# === Local font path (no download) ===
FONT_DIR  = "./fonts"
FONT_PATH = os.path.join(FONT_DIR, "NotoSansTC-Regular.ttf")

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
def run_safe_python(code_str: str, local_vars: dict, allow_autoinstall: bool = False):
    import contextlib, io, importlib, subprocess, sys, re, difflib
    import streamlit as st

    # --------- 1) Block obviously dangerous stuff ----------
    forbidden = [
        "import os", "subprocess", "shutil", "sys.exit", "import sys",
        "open(", "exec(", "eval(", "__import__", "os.system", "os.remove", "os.rmdir",
        "Path(", "pickle", "dotenv", "requests.delete", "requests.put", "requests.post("
    ]
    if any(s in code_str for s in forbidden):
        st.error("⚠️ Unsafe code detected. Execution blocked.")
        return

    # --------- 2) Optional: ensure plotting deps (no PDF libs here) ----------
    pkgs_needed = []
    if "matplotlib" in code_str: pkgs_needed.append("matplotlib")
    if "seaborn"   in code_str: pkgs_needed.append("seaborn")
    if "plotly"    in code_str: pkgs_needed.append("plotly")

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
                "\n\nEnable 'Allow auto-install missing libs' or add them to requirements.txt."
            )
            return
        for pkg in missing:
            res = subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg],
                capture_output=True, text=True, check=False
            )
            if res.returncode != 0:
                st.error(f"Could not install `{pkg}` (exit {res.returncode}).\n\n{res.stderr[:500]}")
                return
            try:
                importlib.import_module(pkg)
            except ImportError:
                st.error(f"`{pkg}` still not importable after install. Please add it to requirements.txt.")
                return

    # --------- 3) Prepare helpful diagnostics ----------
    table_vars = local_vars.get("table_vars", {}) or {}
    valid_vars = sorted(table_vars.keys())

    def show_var_help(missing_name: str):
        # Suggest close matches
        suggestions = difflib.get_close_matches(missing_name, valid_vars, n=3, cutoff=0.5)
        msg = [f"❌ 未知的變數或表名：`{missing_name}`"]
        if valid_vars:
            msg.append("✅ 可用的表變數（請使用這些名稱，勿自行創造）：")
            msg.append(", ".join(f"`{v}`" for v in valid_vars))
            if suggestions:
                msg.append("💡 你是不是想用： " + ", ".join(f"`{s}`" for s in suggestions))
            # Also show columns per variable (brief)
            msg.append("\n**各變數可用欄位（前 12 欄）**：")
            for v in valid_vars:
                cols = table_vars.get(v, {}).get("columns", [])
                msg.append(f"- `{v}`: {', '.join(map(str, cols[:12]))}" + (" …" if len(cols) > 12 else ""))
        else:
            msg.append("（目前沒有可用的表變數；請先在左側選擇表格。）")
        st.error("\n\n".join(msg))

    def show_column_help(col_name: str):
        # We don't know which df was intended, so list columns per var
        msg = [f"❌ 找不到欄位：`{col_name}`"]
        if valid_vars:
            msg.append("✅ 各變數可用欄位（前 12 欄）：")
            for v in valid_vars:
                cols = table_vars.get(v, {}).get("columns", [])
                if cols:
                    hit = " ← 包含" if col_name in cols else ""
                    msg.append(f"- `{v}`: {', '.join(map(str, cols[:12]))}{' …' if len(cols) > 12 else ''}{hit}")
        st.error("\n\n".join(msg))
    # --------- 4) Execute and capture stdout ----------
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            # If plotting, make sure Chinese font is honored globally
            pre_fignums = set()
            if "matplotlib" in code_str:
                try:
                    import matplotlib.pyplot as plt
                    from matplotlib import font_manager as fm
                    fam = fm.FontProperties(fname=local_vars["FONT_PATH"]).get_name()
                    plt.rcParams["font.sans-serif"] = [fam, "Arial", "DejaVu Sans"]
                    plt.rcParams["axes.unicode_minus"] = False
                    pre_fignums = set(plt.get_fignums())  # <— snapshot existing figs
                except Exception as e:
                    st.error(f"Matplotlib font init failed before exec: {e}")
                    return

            exec(code_str, {}, local_vars)

        except NameError as e:
            missing_name = str(e).split("'")[1] if "'" in str(e) else str(e)
            show_var_help(missing_name)
            return
        except KeyError as e:
            missing_key = str(e).strip("'").strip('"')
            if re.fullmatch(r"[A-Za-z0-9_\-一-鿿\s]+", missing_key or ""):
                show_column_help(missing_key)
            else:
                st.error(f"KeyError: {e}")
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

    # --------- 5) Try to display ALL charts ----------
    try:
        # 5a) If the code set a 'fig' object, show it first
        if "fig" in local_vars:
            fig = local_vars["fig"]
            try:
                import plotly.graph_objects as go  # noqa: F401
                if hasattr(fig, "to_plotly_json") or hasattr(fig, "to_dict"):
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    import matplotlib.pyplot as plt  # noqa: F401
                    st.pyplot(fig)
            except Exception:
                # fall through and try matplotlib below
                try:
                    import matplotlib.pyplot as plt  # noqa: F401
                    st.pyplot(fig)
                except Exception:
                    pass

        # 5b) Then show every NEW matplotlib figure created by this code
        if "matplotlib" in code_str:
            import matplotlib.pyplot as plt  # noqa: F401
            post_fignums = set(plt.get_fignums())
            new_fignums = [n for n in post_fignums if n not in pre_fignums]
            for n in sorted(new_fignums):
                st.pyplot(plt.figure(n))
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

# === DATASET PICKER (filtered) START ===
# 1) Get all datasets in the workspace
datasets = list_datasets(pbi_token, WORKSPACE_ID)
if not datasets:
    st.error("No datasets found in the workspace (or missing permissions).")
    st.stop()

# 2) Restrict to ONLY these dataset names
allowed_dataset_names = {
    "F-①電子零組件製造業",
    "F-②電腦、電子產品及光學製品製造業",
    "F-③汽車及其零件製造業",
    "F-④金屬製品製造業",
    "F-⑤產業用機械設備維修及安裝業",
}

# Keep only allowed datasets (exact match)
filtered = [d for d in datasets if d.get("name") in allowed_dataset_names]

# Optional fallback: allow prefix matches if your service appends suffixes
if not filtered:
    tmp = []
    for d in datasets:
        nm = d.get("name", "")
        if any(nm.startswith(a) for a in allowed_dataset_names):
            tmp.append(d)
    # de-dupe while preserving order
    seen = set()
    filtered = [d for d in tmp if not (d["id"] in seen or seen.add(d["id"]))]

if not filtered:
    st.error(
        "None of the allowed datasets are present in this workspace.\n\n"
        "Allowed:\n- " + "\n- ".join(sorted(allowed_dataset_names)) + "\n\n"
        "Found:\n- " + "\n- ".join(d.get("name", "(no name)") for d in datasets)
    )
    st.stop()

# 3) Let user pick ONLY among the filtered datasets
ds_labels = [d["name"] for d in filtered]
ds_idx = st.selectbox("Select a dataset", options=range(len(filtered)), format_func=lambda i: ds_labels[i])

# Use this dataset_id for table discovery
dataset_id = filtered[ds_idx]["id"]
# === DATASET PICKER (filtered) END ===

# Discover tables via Fabric model (for the chosen dataset)
schema = get_model_schema_via_fabric(WORKSPACE_ID, dataset_id, fabric_token)
tables_dict = schema.get("tables", {})
table_names = sorted(tables_dict.keys())

if not table_names:
    st.error("Couldn’t discover tables from Fabric model. Check Fabric permissions.")
    st.stop()

picked_tables = st.multiselect("Select tables", options=table_names, default=table_names[:1])
top_n = st.slider("Rows per table (TOPN)", 50, 5000, 200, 50)

if not picked_tables:
    st.info("Pick at least one table to continue.")
    st.stop()


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
            "content": r"""You are a helpful data analyst.

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
#\`\`\`python

# Font setup (already injected in the runtime as well)
#ok = enable_matplotlib_chinese(FONT_PATH)
#fp = get_tc_fontprops()

#\`\`\`
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
        placeholder="e.g., 用 long_df 依 `_table` 分組，畫每月趨勢線；或：請撰寫分析報告（中文）。",
        height=120,
    )
    c1, c2, _ = st.columns([1.3, 1, 4])
    ask_clicked = c1.form_submit_button("🤖 Ask OpenAI")
    reset_clicked = c2.form_submit_button("🔄 Reset")

if reset_clicked:
    # keep only the system message and clear any prior report
    sys_msg = st.session_state.chat[0]
    st.session_state.chat = [sys_msg]
    st.session_state.pop("last_report", None)
    st.session_state.pop("is_report", None)
    st.rerun()

if ask_clicked:
    if not user_q.strip():
        st.warning("Please enter a question.")
    else:
        # Build payload and ask the model
        payload = build_user_payload(user_q, safe_locals, per_table_rows=50)
        st.session_state.chat.append({"role": "user", "content": payload})
        with st.spinner("Calling GPT-4o…"):
            resp = client.chat.completions.create(
                model="gpt-4o",
                messages=st.session_state.chat,
                temperature=0.3,
            )

        reply = (resp.choices[0].message.content or "").strip()
        st.session_state.chat.append({"role": "assistant", "content": reply})

        # Find ALL python code blocks, not just the first
        code_blocks = re.findall(r"```python(.*?)```", reply, re.S | re.I)

        if code_blocks:
            st.session_state.is_report = False
            st.markdown("**🛠 Executing GPT-generated code…**")

            # Ensure 'fpdf' module exists if GPT code tries to make PDFs
            import importlib, subprocess, sys
            try:
                importlib.import_module("fpdf")
            except ImportError:
                st.info("Installing `fpdf2` for PDF export…")
                res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "fpdf2"],
                    capture_output=True, text=True
                )
                if res.returncode != 0:
                    st.error("Failed to install `fpdf2`. Please add it to requirements.txt.")

            # Execute all code blocks in order
            for idx, code_block in enumerate(code_blocks, start=1):
                st.caption(f"Running block {idx}/{len(code_blocks)}…")
                run_safe_python(code_block, safe_locals, allow_autoinstall=ALLOW_AUTOINSTALL)

            # After running code blocks, no need to rerun here—output already shown
        else:
            # No code blocks → treat as a narrative report
            st.session_state.is_report = True
            st.session_state.last_report = reply

            st.markdown("### 📝 LLM Report")
            st.markdown(reply)

            # Offer PDF download of the narrative
            # Ensure fpdf2 (module `fpdf`) exists for PDF export
            try:
                import importlib
                importlib.import_module("fpdf")
            except Exception:
                import sys, subprocess
                st.info("Installing `fpdf2` for PDF export…")
                res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "fpdf2"],
                    capture_output=True, text=True
                )
                if res.returncode != 0:
                    st.error("Failed to install `fpdf2`. Please add it to requirements.txt.")
                    # We continue without PDF in this case
                else:
                    pass

            # Build the PDF in memory with Chinese font and robust wrapping
pdf_bytes = None
try:
    from fpdf import FPDF

    pdf = FPDF(format="A4", orientation="P", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.set_left_margin(12)
    pdf.set_right_margin(12)
    pdf.add_page()

    # Load your TC font (TTF/OTF) and set a normal size
    pdf.add_font("NotoSansTC", "", FONT_PATH, uni=True)
    pdf.set_font("NotoSansTC", "", 12)

    # Effective page width (printable width)
    epw = pdf.w - pdf.l_margin - pdf.r_margin
    line_h = 7.0

    def write_line_safe(txt: str):
        """Write one logical line, but safely wrap long unbreakable tokens."""
        if not txt.strip():
            pdf.ln(line_h)
            return

        # fpdf2 wraps on spaces by default; long URLs/IDs can overflow.
        # We chunk any token that exceeds epw into smaller pieces using string width.
        parts = txt.split(" ")
        rebuilt = []
        for p in parts:
            if not p:
                rebuilt.append(p)
                continue
            w = pdf.get_string_width(p)
            if w <= epw:
                rebuilt.append(p)
            else:
                # break the token by character so each piece fits
                buf = ""
                for ch in p:
                    if pdf.get_string_width(buf + ch) > epw:
                        if buf:
                            rebuilt.append(buf)
                        buf = ch
                    else:
                        buf += ch
                if buf:
                    rebuilt.append(buf)

        # Now write using multi_cell over the re-joined text
        final_text = " ".join(rebuilt)
        pdf.multi_cell(w=epw, h=line_h, txt=final_text, align="L")

    for raw_line in (reply or "").split("\n"):
        write_line_safe(raw_line)

    # Get bytes safely (fpdf2 may return str/bytes/bytearray)
    out = pdf.output(dest="S")
    pdf_bytes = out.encode("latin-1", "ignore") if isinstance(out, str) else bytes(out)

except Exception as e:
    st.warning(f"PDF export failed: {e}")
    pdf_bytes = None




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

        # --- Ensure fpdf2 is available BEFORE executing GPT code ---
        import sys, subprocess, importlib
        try:
            importlib.import_module("fpdf")   # <-- check the right module
        except ImportError:
            st.info("Installing `fpdf2`…")
            res = subprocess.run(
                [sys.executable, "-m", "pip", "pip", "install", "fpdf2"],  # ok if already installed
                capture_output=True, text=True
            )
            # fallback if 'pip pip' typo—use the correct one:
            if res.returncode != 0:
                res = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "fpdf2"],
                    capture_output=True, text=True
                )
                if res.returncode != 0:
                    st.error("Failed to install `fpdf2`. Please add it to requirements.txt.")
                    st.stop()

        # Now run the GPT code safely
        run_safe_python(code_block, safe_locals, allow_autoinstall=ALLOW_AUTOINSTALL)




