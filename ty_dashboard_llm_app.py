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

import pandas as pd
import requests
import streamlit as st
from msal import ConfidentialClientApplication
from openai import OpenAI

# ─────────────────────────────────────────────────────────────
# Static workspace (TYCGEDB FORMAL). Change if needed.
WORKSPACE_ID = "41bfeb46-6ff1-4baa-824a-9681be3a586d"
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

# ─────────────────────────────────────────────────────────────
# Sidebar credentials (UI only)
st.sidebar.header("🔐 Credentials")
OPENAI_KEY = st.sidebar.text_input("OpenAI API Key", type="password")
TENANT_ID  = st.sidebar.text_input("Azure Tenant ID",  value="ba129fe2-5c7b-4f4b-9670-ed7494972f23")
CLIENT_ID  = st.sidebar.text_input("Azure Client ID",  value="770a4905-e32f-493e-817f-9731db47761b")
CLIENT_SECRET = st.sidebar.text_input("Azure Client Secret (VALUE)", type="password")

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
# Safe code execution + auto-install plotting libs
def ensure_package(pkg: str):
    """Install a package if missing (first time only)."""
    if importlib.util.find_spec(pkg) is None:
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

def run_safe_python(code_str: str, local_vars: dict):
    # Block dangerous stuff
    forbidden = [
        "import os", "subprocess", "shutil", "sys.exit", "import sys",
        "open(", "exec(", "eval(", "__import__", "os.system", "os.remove", "os.rmdir",
        "Path(", "pickle", "dotenv", "requests.delete", "requests.put", "requests.post("
    ]
    if any(s in code_str for s in forbidden):
        st.error("⚠️ Unsafe code detected. Execution blocked.")
        return

    # If code references plotting libs, ensure they exist
    pkgs_needed = []
    if "matplotlib" in code_str: pkgs_needed.append("matplotlib")
    if "seaborn" in code_str:    pkgs_needed.append("seaborn")
    if "plotly" in code_str:     pkgs_needed.append("plotly")
    for p in pkgs_needed:
        try:
            ensure_package(p)
        except Exception as e:
            st.error(f"Could not install {p}: {e}")
            return

    # Execute and capture stdout
    with contextlib.redirect_stdout(io.StringIO()) as buf:
        try:
            exec(code_str, {}, local_vars)
        except Exception as e:
            st.error(f"Error running GPT code: {e}")
            return
    out = buf.getvalue()
    if out:
        st.text(out)

    # Try to display common figure variables if present
    # - matplotlib: look for 'fig' or gcf()
    # - plotly: 'fig' with show() or as a Figure
    try:
        if "fig" in local_vars:
            fig = local_vars["fig"]
            # Try plotly first
            try:
                import plotly.graph_objects as go  # noqa: F401
                import plotly.io as pio  # noqa: F401
                from plotly.basedatatypes import BaseFigure  # type: ignore
                if hasattr(fig, "to_dict") or hasattr(fig, "to_plotly_json"):
                    st.plotly_chart(fig, use_container_width=True)
                    return
            except Exception:
                pass
            # Else try matplotlib
            try:
                import matplotlib.pyplot as plt  # type: ignore
                st.pyplot(fig)  # if 'fig' is a mpl Figure
                return
            except Exception:
                pass

        # If no 'fig', still try matplotlib current figure
        if "matplotlib" in code_str:
            try:
                import matplotlib.pyplot as plt  # type: ignore
                st.pyplot(plt.gcf())
                return
            except Exception:
                pass
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
# UI
st.title("📊 Power BI → Fabric Tables → GPT-4o (multi-turn, safe charts)")

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

# ─────────────────────────────────────────────────────────────
# Multi-turn chat
if "chat" not in st.session_state:
    st.session_state.chat = [
        {"role": "system", "content":
         "You are a helpful data analyst.\n"
         "- You have access to pandas DataFrames in a dict named `data_dict` (keys = table names).\n"
         "- If you generate Python code, wrap it in triple backticks with the language tag 'python'.\n"
         "- Prefer a common x-axis for multi-table charts; one may be available as `x_axis`.\n"
         "- Use matplotlib or plotly for plots. If possible, store the chart in a variable named `fig`."}
    ]

st.subheader("💬 Chat with GPT-4o")
for msg in st.session_state.chat:
    if msg["role"] == "user":
        st.markdown(f"**You:** {msg['content']}")
    elif msg["role"] == "assistant":
        st.markdown(f"**Assistant:** {msg['content']}")

user_q = st.text_area("Your question / request:", placeholder="e.g., Plot total amount by Month for all selected tables.")
run_btn = st.button("Send")

def build_sample_csv(dfs: dict[str, pd.DataFrame], per_table_rows=50) -> str:
    chunks = []
    for name, d in dfs.items():
        chunks.append(f"### Table: {name}\n{d.head(per_table_rows).to_csv(index=False)}")
    return "\n\n".join(chunks)

if run_btn and user_q.strip():
    sample_csv = build_sample_csv(dfs, per_table_rows=50)
    st.session_state.chat.append({"role": "user",
                                  "content": f"{user_q}\n\nHere are samples of the selected tables:\n{sample_csv}\n\nCommon X-axis (if any): {x_axis}"})
    with st.spinner("🤖 GPT-4o is thinking…"):
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=st.session_state.chat,
            temperature=0.3
        )
    reply = resp.choices[0].message.content
    st.session_state.chat.append({"role": "assistant", "content": reply})
    # Prefer modern rerun; fallback if older Streamlit
    try:
        st.rerun()
    except AttributeError:
        st.rerun()

# After rerun: execute code from last assistant message if present
if st.session_state.chat and st.session_state.chat[-1]["role"] == "assistant":
    last = st.session_state.chat[-1]["content"]
    code_match = re.search(r"```python(.*?)```", last, re.S | re.I)
    if code_match:
        st.markdown("**🛠 Executing GPT-generated code…**")
        code_block = code_match.group(1)
        run_safe_python(
            code_block,
            {
                "pd": pd,
                "st": st,
                "data_dict": dfs,   # all selected tables as DataFrames
                "x_axis": x_axis,   # suggested common x-axis
            }
        )


