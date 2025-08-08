 # py code beginning


# ty_dashboard_llm_app.py
# 📊 Power BI (dynamic) → Tables → GPT-4o
# - Defaults for Tenant/Client IDs
# - Default workspace auto-select (TYCGEDB FORMAL)
# - Quick-pick presets for known datasets
# - Fabric TMSL table discovery (works for Import/DirectQuery)
# - Per-table analysis + Union selected tables → single analysis

from urllib.parse import quote
import json
import base64
import time

import streamlit as st
import pandas as pd
import requests
from msal import ConfidentialClientApplication
import openai

PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

# ──────────────────────────────────────────────────────────────────────────────
# ⚡ Quick-pick presets (all in TYCGEDB FORMAL)
TYCGEDB_FORMAL_WS = "41bfeb46-6ff1-4baa-824a-9681be3a586d"
QUICK_PRESETS = {
    "F-①電子零組件製造業": "08984f8c-149e-4d62-90b0-5a328c5450aa",
    "F-②電腦、電子產品及光學製品製造業": "ed57710b-5313-45f4-ad1b-c7202df47914",
    "F-③汽車及其零件製造業": "38634388-7bf8-4c29-a62b-db15e8251458",
    "F-④金屬製品製造業": "e5c850e8-a199-4f29-8cce-f384b6cea90e",
    "F-⑤產業用機械設備維修及安裝業": "5831ffc0-50bf-4f87-9697-9c4d90477c0d",
}

# ──────────────────────────────────────────────────────────────────────────────
# Sidebar: credentials WITH DEFAULTS (secrets left blank on purpose)
st.sidebar.header("🔐 Power BI & Azure AD")

OPENAI_KEY = st.sidebar.text_input(
    "OpenAI API Key",
    value="",  # ← optional: put your default OpenAI key here
    type="password"
)

TENANT_ID = st.sidebar.text_input(
    "Azure Tenant ID",
    value="ba129fe2-5c7b-4f4b-9670-ed7494972f23"  # ← your updated tenant id
)
CLIENT_ID = st.sidebar.text_input(
    "Azure Client ID",
    value="770a4905-e32f-493e-817f-9731db47761b"  # ← your updated client id
)
raw_secret = st.sidebar.text_input(
    "Azure Client Secret (VALUE, not ID)",
    value="",  # ← keep empty for safety; paste at runtime
    type="password"
)
CLIENT_SECRET = (raw_secret or "").strip()

if not all([OPENAI_KEY, TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
    st.sidebar.warning("Fill all fields above to continue.")
    st.stop()

openai.api_key = OPENAI_KEY

with st.sidebar.expander("🔎 Sanity check"):
    st.write("Tenant:", (TENANT_ID[:8] + "…") if TENANT_ID else "—")
    st.write("Client:", (CLIENT_ID[:8] + "…") if CLIENT_ID else "—")
    st.write("Secret set?:", bool(CLIENT_SECRET))

# ──────────────────────────────────────────────────────────────────────────────
# Auth (Power BI / Entra)
@st.cache_data(show_spinner=False, ttl=50)  # short TTL; tokens expire quickly
def get_powerbi_token_cached(tenant_id: str, client_id: str, client_secret: str) -> dict:
    app = ConfidentialClientApplication(
        client_id,
        authority=f"https://login.microsoftonline.com/{tenant_id}",
        client_credential=client_secret
    )
    return app.acquire_token_for_client(scopes=["https://analysis.windows.net/powerbi/api/.default"])

def get_powerbi_token() -> str:
    result = get_powerbi_token_cached(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
    # Show non-sensitive bits to help debugging
    st.sidebar.write("MSAL:", {k: result[k] for k in result if k != "access_token"})
    tok = result.get("access_token")
    if not tok:
        st.error(f"Token error: {result.get('error')} – {result.get('error_description')}")
    return tok or ""

# ──────────────────────────────────────────────────────────────────────────────
# Power BI helpers (REST)
@st.cache_data(show_spinner=False, ttl=300)
def list_groups(token: str) -> list[dict]:
    url = f"{PBI_BASE}/groups"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if not r.ok:
        return []
    return r.json().get("value", [])

@st.cache_data(show_spinner=False, ttl=300)
def list_datasets(token: str, workspace_id: str) -> list[dict]:
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if not r.ok:
        return []
    return r.json().get("value", [])

@st.cache_data(show_spinner=False, ttl=300)
def list_tables_rest(token: str, workspace_id: str, dataset_id: str) -> list[str]:
    """Works mainly for Push/Streaming datasets."""
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/tables"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if not r.ok:
        return []
    return [t["name"] for t in r.json().get("value", [])]

def get_rows_push(token: str, workspace_id: str, dataset_id: str, table_name: str) -> pd.DataFrame:
    """Only works for Push/Streaming datasets."""
    safe_table = quote(table_name, safe="")
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/tables/{safe_table}/rows"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if not r.ok:
        # Import/DirectQuery /rows will fail — that's fine, we'll DAX fallback.
        return pd.DataFrame()
    return pd.DataFrame(r.json().get("value", []))

def run_dax_query(token: str, workspace_id: str, dataset_id: str, dax: str) -> pd.DataFrame:
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
    r = requests.post(
        url,
        json={"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=120,
    )
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
        # normalize column names like "Table[Column]" → "Column"
        try:
            df.columns = [c.split("[")[-1].rstrip("]") for c in df.columns]
        except Exception:
            pass
        return df
    return pd.DataFrame(columns=cols)

def fetch_table_auto_by_ids(token: str, workspace_id: str, dataset_id: str, table_name: str, top_n: int = 200) -> pd.DataFrame:
    """Try Push/Streaming rows first, then DAX TOPN for Import/DirectQuery."""
    df = get_rows_push(token, workspace_id, dataset_id, table_name)
    if not df.empty:
        return df
    dax = f"EVALUATE TOPN({top_n}, '{table_name}')"
    return run_dax_query(token, workspace_id, dataset_id, dax)

# ──────────────────────────────────────────────────────────────────────────────
# Fabric helpers: semantic model (TMSL) table discovery
@st.cache_data(show_spinner=False, ttl=300)
def get_fabric_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Client credentials token for Fabric API (scope: https://api.fabric.microsoft.com/.default)."""
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    token_data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://api.fabric.microsoft.com/.default",
    }
    r = requests.post(token_url, data=token_data, timeout=60)
    if not r.ok:
        return ""
    return r.json().get("access_token", "")

@st.cache_data(show_spinner=False, ttl=300)
def get_model_schema_via_fabric(workspace_id: str, dataset_id: str, fabric_token: str) -> dict:
    """
    Calls Fabric 'getDefinition?format=TMSL' long-running op, polls until ready,
    decodes the model JSON, and returns {"tables": {table_name: [col, ...]}, "raw": model_json}.
    """
    if not fabric_token:
        return {"tables": {}, "raw": {}}

    fh = {"Authorization": f"Bearer {fabric_token}"}
    # 1) start export
    start = requests.post(
        f"https://api.fabric.microsoft.com/v1/workspaces/{workspace_id}/semanticModels/{dataset_id}/getDefinition?format=TMSL",
        headers=fh, timeout=60
    )
    # Some tenants return 202 directly; consider non-OK as pending if Location is present
    if not start.ok and start.status_code != 202:
        return {"tables": {}, "raw": {}}

    resp = start
    # 2) poll Location until done
    while resp.status_code == 202:
        op_url = resp.headers.get("Location")
        if not op_url:
            break
        retry = int(resp.headers.get("Retry-After", "3"))
        time.sleep(retry)
        resp = requests.get(op_url, headers=fh, timeout=60)

    # 3) fetch result
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

def list_tables_via_fabric(token_pbi: str, workspace_id: str, dataset_id: str,
                           tenant_id: str, client_id: str, client_secret: str) -> list[str]:
    """
    Fabric-backed table discovery. Returns table names. Falls back to REST /tables first.
    """
    # Try standard REST first (works for push/streaming datasets)
    rest_tables = list_tables_rest(token_pbi, workspace_id, dataset_id)
    if rest_tables:
        return rest_tables

    # Otherwise use Fabric model export
    fab_tok = get_fabric_token(tenant_id, client_id, client_secret)
    schema = get_model_schema_via_fabric(workspace_id, dataset_id, fab_tok)
    return sorted(schema.get("tables", {}).keys())

# ──────────────────────────────────────────────────────────────────────────────
# GPT helper
def ask_gpt4o(prompt: str, data_csv: str) -> str:
    msgs = [
        {"role": "system", "content": "You are a helpful data analyst."},
        {"role": "user",   "content": f"{prompt}\n\nHere is the data:\n{data_csv}"},
    ]
    # If your installed OpenAI SDK uses the newer client API, adapt accordingly.
    r = openai.ChatCompletion.create(model="gpt-4o", messages=msgs, temperature=0.3)
    return r.choices[0].message.content.strip()

# ──────────────────────────────────────────────────────────────────────────────
# UI — Workspace + Dataset (Quick-pick) → Tables
st.title("📊 Power BI (dynamic) → Tables → GPT-4o")

token = get_powerbi_token()
if not token:
    st.stop()

# Sidebar quick-pick (optional)
with st.sidebar.expander("⚡ Quick-pick (TYCGEDB FORMAL)"):
    quick_label = st.selectbox(
        "Choose a preset (optional):",
        options=["—"] + list(QUICK_PRESETS.keys()),
        index=0,
        key="quick_pick"
    )

# Workspaces
with st.spinner("Loading workspaces…"):
    groups = list_groups(token)

if not groups:
    st.error("No workspaces available (or missing permissions).")
    st.stop()

# If a quick-pick is selected, force workspace to TYCGEDB FORMAL if you can see it
forced_ws = None
if quick_label != "—":
    ws_ids = {g["id"] for g in groups}
    if TYCGEDB_FORMAL_WS in ws_ids:
        forced_ws = TYCGEDB_FORMAL_WS
    else:
        st.sidebar.warning("Preset workspace (TYCGEDB FORMAL) is not visible. Please select a visible workspace.")

# Determine workspace_id (forced if quick-pick + visible; otherwise picker preselecting formal when possible)
DEFAULT_WORKSPACE_ID = TYCGEDB_FORMAL_WS
default_idx = next((i for i, g in enumerate(groups) if g["id"] == DEFAULT_WORKSPACE_ID), None)

if forced_ws:
    workspace_id = forced_ws
    chosen_group = next(g for g in groups if g["id"] == workspace_id)
    st.success(f"Using preset workspace: **{chosen_group['name']}** ({workspace_id[:8]}…)")
    # Allow override
    if st.checkbox("Change workspace", value=False, key="override_ws"):
        group_labels = [f"{g['name']} ({g['id'][:8]}…)" for g in groups]
        g_idx = st.selectbox(
            "Select a workspace (group):",
            options=range(len(groups)),
            index=(default_idx or 0),
            format_func=lambda i: group_labels[i],
        )
        workspace_id = groups[g_idx]["id"]
else:
    group_labels = [f"{g['name']} ({g['id'][:8]}…)" for g in groups]
    g_idx = st.selectbox(
        "Select a workspace (group):",
        options=range(len(groups)),
        index=(default_idx or 0),
        format_func=lambda i: group_labels[i],
    )
    workspace_id = groups[g_idx]["id"]

# Datasets
with st.spinner("Loading datasets…"):
    datasets = list_datasets(token, workspace_id)

if not datasets:
    st.error("No datasets found in this workspace.")
    st.stop()

# Build id→index map for defaulting
id_to_idx = {d["id"]: i for i, d in enumerate(datasets)}

# Resolve default dataset (via quick-pick)
default_ds_idx = 0
if quick_label != "—":
    target_id = QUICK_PRESETS[quick_label]
    if target_id in id_to_idx:
        default_ds_idx = id_to_idx[target_id]
        st.sidebar.success(f"Using preset dataset: {quick_label}")
    else:
        st.sidebar.warning("Preset dataset not found in this workspace; select from list below.")

ds_labels = [f"{d['name']} ({d['id'][:8]}…)" for d in datasets]
ds_idx = st.selectbox(
    "Select a dataset:",
    options=range(len(datasets)),
    index=default_ds_idx,
    format_func=lambda i: ds_labels[i],
)
dataset_id = datasets[ds_idx]["id"]

# Tables (REST first; Fabric fallback)
with st.spinner("Loading tables…"):
    tables = list_tables_via_fabric(
        token_pbi=token,
        workspace_id=workspace_id,
        dataset_id=dataset_id,
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
    )

# Optional: show columns from Fabric schema for context
show_schema = st.checkbox("Show table columns (via Fabric model)", value=False)
fabric_schema = {}
if show_schema:
    fab_tok = get_fabric_token(TENANT_ID, CLIENT_ID, CLIENT_SECRET)
    schema = get_model_schema_via_fabric(workspace_id, dataset_id, fab_tok)
    fabric_schema = schema.get("tables", {})
    if fabric_schema:
        with st.expander("Model schema (first few columns per table)"):
            for t in sorted(fabric_schema.keys()):
                cols = fabric_schema[t][:10]
                st.write(f"- **{t}**: {cols}")

if not tables:
    st.error("No tables discovered (even via Fabric model). Check permissions / dataset.")
    st.stop()

picked_tables = st.multiselect("Select tables to preview/analyze:", options=tables)
if not picked_tables:
    st.info("Pick at least one table.")
    st.stop()

# Options
top_n = st.slider("Rows to fetch if DAX fallback is used (TOPN):", 50, 5000, 200, 50)
sample_rows = st.slider("Rows to include in GPT sample:", 5, 100, 20, 5)
st.markdown("---")

# ──────────────────────────────────────────────────────────────────────────────
# Fetch & analyze (per-table + unioned multi-table)

# 1) Fetch all selected tables ONCE
dfs = {}
with st.spinner("Fetching selected tables…"):
    for tbl in picked_tables:
        df_tbl = fetch_table_auto_by_ids(token, workspace_id, dataset_id, tbl, top_n=top_n)
        if not df_tbl.empty:
            df_tbl["__table__"] = tbl  # provenance
        dfs[tbl] = df_tbl

# 2) Per-table preview, export, and LLM
for tbl in picked_tables:
    st.markdown(f"### Table: `{tbl}`  \nWorkspace `{workspace_id}` / Dataset `{dataset_id}`")

    df = dfs.get(tbl, pd.DataFrame())
    if df.empty:
        st.warning(f"No data returned for `{tbl}` (table may be empty or DAX blocked).")
        continue

    st.dataframe(df.head(50))

    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            "⬇️ Download full table as CSV",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name=f"{tbl}.csv",
            mime="text/csv",
            key=f"dl_{dataset_id}_{tbl}",
        )
    with col2:
        st.download_button(
            "⬇️ Download head() sample as CSV",
            data=df.head(sample_rows).to_csv(index=False).encode("utf-8"),
            file_name=f"{tbl}_sample.csv",
            mime="text/csv",
            key=f"dl_sample_{dataset_id}_{tbl}",
        )

    q = st.text_area(f"Ask GPT-4o about `{tbl}`:", key=f"q_{dataset_id}_{tbl}")
    if st.button(f"Analyze `{tbl}`", key=f"btn_{dataset_id}_{tbl}"):
        sample = df.head(sample_rows).to_csv(index=False)
        with st.spinner("🤖 GPT-4o is thinking…"):
            try:
                ans = ask_gpt4o(q, sample)
                st.subheader("💬 GPT-4o Response")
                st.write(ans)
            except Exception as e:
                st.error(f"OpenAI error: {e}")

# 3) UNION of selected tables → one CSV + one LLM analysis
st.markdown("---")
st.subheader("🧩 Union selected tables → single analysis")

# Choose how to union columns
union_mode = st.radio(
    "Column alignment for union:",
    options=["Common columns only (intersection)", "All columns (outer)"],
    index=0,
    help="Intersection keeps only columns present in all tables. Outer keeps all columns and fills missing values."
)

# Build the combined DataFrame
valid_dfs = [dfs[t] for t in picked_tables if not dfs[t].empty]
combined_df = pd.DataFrame()

if valid_dfs:
    if union_mode.startswith("Common"):
        # Find common columns across all dfs (keep __table__ regardless)
        common_cols = set(valid_dfs[0].columns)
        for d in valid_dfs[1:]:
            common_cols &= set(d.columns)
        common_cols = list(sorted(common_cols | {"__table__"}))  # include provenance
        combined_df = pd.concat([d[common_cols] for d in valid_dfs], axis=0, ignore_index=True)
    else:
        # Outer union across all columns
        combined_df = pd.concat(valid_dfs, axis=0, ignore_index=True, sort=False)

    # Move __table__ to front if present
    if "__table__" in combined_df.columns:
        cols = ["__table__"] + [c for c in combined_df.columns if c != "__table__"]
        combined_df = combined_df[cols]

    st.write(f"Combined rows: **{len(combined_df):,}**  |  Columns: **{len(combined_df.columns):,}**")
    st.dataframe(combined_df.head(50))

    st.download_button(
        "⬇️ Download combined CSV",
        data=combined_df.to_csv(index=False).encode("utf-8"),
        file_name=f"combined_{dataset_id}.csv",
        mime="text/csv",
        key=f"dl_combined_{dataset_id}",
    )

    # One LLM analysis for the combined sample
    q_all = st.text_area("Ask GPT-4o about the **combined** tables:", key=f"q_combined_{dataset_id}")
    if st.button("Analyze combined tables", key=f"btn_combined_{dataset_id}"):
        sample_all = combined_df.head(sample_rows).to_csv(index=False)
        with st.spinner("🤖 GPT-4o is thinking on the combined dataset…"):
            try:
                ans = ask_gpt4o(q_all, sample_all)
                st.subheader("💬 GPT-4o Response (Combined)")
                st.write(ans)
            except Exception as e:
                st.error(f"OpenAI error: {e}")
else:
    st.info("No valid tables returned to union. Fetch tables first or adjust selection.")


