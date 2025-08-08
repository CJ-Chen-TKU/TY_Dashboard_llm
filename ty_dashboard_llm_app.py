 # py code beginning

# ty_dashboard_llm_app.py

import json
from urllib.parse import quote

import streamlit as st
import pandas as pd
import requests
from msal import ConfidentialClientApplication
import openai

PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

# ──────────────────────────────────────────────────────────────────────────────
# 0) Table → Dataset mapping (from your message)
#    If all tables live in the same workspace, you only need to set WORKSPACE_ID once.
TABLE_TO_DATASET = {
    "F-①電子零組件製造業": "08984f8c-149e-4d62-90b0-5a328c5450aa",
    "F-②電腦、電子產品及光學製品製造業": "ed57710b-5313-45f4-ad1b-c7202df47914",
    "F-③汽車及其零件製造業": "38634388-7bf8-4c29-a62b-db15e8251458",
    "F-④金屬製品製造業": "e5c850e8-a199-4f29-8cce-f384b6cea90e",
    "F-⑤產業用機械設備維修及安裝業": "5831ffc0-50bf-4f87-9697-9c4d90477c0d",
}

# If *some* tables are in other workspaces, you can optionally add a per-table workspace override:
# TABLE_TO_WORKSPACE = {"F-①電子零組件製造業": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", ...}
TABLE_TO_WORKSPACE = {}

# ──────────────────────────────────────────────────────────────────────────────
# 1) Sidebar: credentials & defaults
st.sidebar.header("🔐 Power BI & Azure AD")

OPENAI_KEY = st.sidebar.text_input("OpenAI API Key", type="password")

TENANT_ID  = st.sidebar.text_input(
    "Azure Tenant ID",
    value="fd9290f7-a3a2-4d0b-ac94-3a6e1896526e"  # ← your confirmed tenant
)
CLIENT_ID  = st.sidebar.text_input(
    "Azure Client ID",
    value="eaa575b7-b4d6-48f2-8451-c4d0fe3c2ad4"  # ← your confirmed client
)
raw_secret = st.sidebar.text_input("Azure Client Secret (VALUE, not ID)", type="password")
CLIENT_SECRET = (raw_secret or "").strip()

# Default workspace for all tables (unless overridden per table in TABLE_TO_WORKSPACE)
DEFAULT_WORKSPACE_ID = st.sidebar.text_input(
    "Default Power BI Workspace ID (groupId)",
    value="41bfeb46-6ff1-4baa-824a-9681be3a586d"
)

# Optional: allow editing of mapping JSON in UI (leave empty to use the dict above)
map_help = "Optional JSON to override/extend the built-in table→dataset mapping. Leave blank to use defaults."
map_json = st.sidebar.text_area("Table→Dataset mapping (JSON, optional)", value="", help=map_help, height=150)

if map_json.strip():
    try:
        TABLE_TO_DATASET.update(json.loads(map_json))
        st.sidebar.success("✅ Mapping JSON merged.")
    except Exception as e:
        st.sidebar.error(f"Mapping JSON error: {e}")
        st.stop()

# Quick sanity check (without leaking secret)
with st.sidebar.expander("🔎 Sanity check"):
    st.write("Tenant:", TENANT_ID[:8] + "…")
    st.write("Client:", CLIENT_ID[:8] + "…")
    st.write("Secret set?:", bool(CLIENT_SECRET))
    st.write("Default Workspace:", (DEFAULT_WORKSPACE_ID[:8] + "…") if DEFAULT_WORKSPACE_ID else "—")

if not all([OPENAI_KEY, TENANT_ID, CLIENT_ID, CLIENT_SECRET, DEFAULT_WORKSPACE_ID]):
    st.sidebar.warning("Fill all fields above to continue.")
    st.stop()

openai.api_key = OPENAI_KEY

# ──────────────────────────────────────────────────────────────────────────────
# 2) Auth
def get_powerbi_token() -> str:
    app = ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=["https://analysis.windows.net/powerbi/api/.default"])
    st.sidebar.write("MSAL:", {k: result[k] for k in result if k != "access_token"})
    tok = result.get("access_token")
    if not tok:
        st.error(f"Token error: {result.get('error')} – {result.get('error_description')}")
    return tok or ""

# ──────────────────────────────────────────────────────────────────────────────
# 3) Power BI helpers
def workspace_for_table(table_name: str) -> str:
    return TABLE_TO_WORKSPACE.get(table_name, DEFAULT_WORKSPACE_ID)

def dataset_for_table(table_name: str) -> str:
    ds = TABLE_TO_DATASET.get(table_name)
    if not ds:
        st.error(f"❌ No dataset ID mapped for table: {table_name}")
    return ds or ""

def list_tables(token: str, workspace_id: str, dataset_id: str):
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/tables"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if not r.ok:
        st.error(f"List tables failed ({r.status_code}) for dataset {dataset_id}:")
        st.code(r.text, language="json")
        return []
    return [t["name"] for t in r.json().get("value", [])]

def get_rows_push(token: str, workspace_id: str, dataset_id: str, table_name: str) -> pd.DataFrame:
    # Only works for Push/Streaming datasets
    safe_table = quote(table_name, safe="")
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/tables/{safe_table}/rows"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    if not r.ok:
        st.info(f"/rows failed for '{table_name}' ({r.status_code}) on dataset {dataset_id}. Will try DAX fallback.")
        st.code(r.text, language="json")
        return pd.DataFrame()
    return pd.DataFrame(r.json().get("value", []))

def run_dax_query(token: str, workspace_id: str, dataset_id: str, dax: str) -> pd.DataFrame:
    url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
    r = requests.post(url, json={"queries": [{"query": dax}]}, headers={"Authorization": f"Bearer {token}"})
    if not r.ok:
        st.error(f"DAX failed ({r.status_code}) on dataset {dataset_id}:")
        st.code(r.text, language="json")
        return pd.DataFrame()
    t = r.json()["results"][0]["tables"][0]
    cols = [c["name"] for c in t["columns"]]
    return pd.DataFrame(t["rows"], columns=cols)

def fetch_table_auto(token: str, table_name: str, top_n: int = 200) -> pd.DataFrame:
    ws_id = workspace_for_table(table_name)
    ds_id = dataset_for_table(table_name)
    if not ds_id:
        return pd.DataFrame()

    # Try Push rows first
    df = get_rows_push(token, ws_id, ds_id, table_name)
    if not df.empty:
        return df

    # Fall back to DAX TOPN for Import/DirectQuery datasets
    dax = f"EVALUATE TOPN({top_n}, '{table_name}')"
    return run_dax_query(token, ws_id, ds_id, dax)

# ──────────────────────────────────────────────────────────────────────────────
# 4) GPT helper
def ask_gpt4o(prompt: str, data_csv: str) -> str:
    msgs = [
        {"role": "system", "content": "You are a helpful data analyst."},
        {"role": "user",   "content": f"{prompt}\n\nHere is the data:\n{data_csv}"},
    ]
    r = openai.ChatCompletion.create(model="gpt-4o", messages=msgs, temperature=0.3)
    return r.choices[0].message.content.strip()

# ──────────────────────────────────────────────────────────────────────────────
# 5) UI
st.title("📊 Power BI → Multi-Dataset Table Routing → GPT-4o")

token = get_powerbi_token()
if not token:
    st.stop()

# Let user pick from mapped tables
all_tables = list(TABLE_TO_DATASET.keys())
picked = st.multiselect("Select tables to preview/analyze:", options=all_tables, default=all_tables[:1])
if not picked:
    st.info("Pick at least one table.")
    st.stop()

# Optional: list tables for each dataset to verify names (debug)
if st.checkbox("🔍 List tables per mapped dataset (debug)", value=False):
    seen = set()
    for tbl, ds in TABLE_TO_DATASET.items():
        if ds in seen:
            continue
        seen.add(ds)
        ws = TABLE_TO_WORKSPACE.get(tbl, DEFAULT_WORKSPACE_ID)
        st.write(f"Dataset {ds} (workspace {ws}):")
        names = list_tables(token, ws, ds)
        st.write(names or "—")

# Fetch & analyze
for tbl in picked:
    ws_id = workspace_for_table(tbl)
    ds_id = dataset_for_table(tbl)

    st.markdown(f"---\n### Table: `{tbl}`  \nUsing workspace `{ws_id}` / dataset `{ds_id}`")
    with st.spinner(f"Fetching `{tbl}`…"):
        df = fetch_table_auto(token, tbl, top_n=200)

    if df.empty:
        st.warning(f"No data returned for `{tbl}`.")
        continue

    st.dataframe(df.head(20))

    q = st.text_area(f"Ask GPT-4o about `{tbl}`:", key=f"q_{tbl}")
    if st.button(f"Analyze `{tbl}`", key=f"btn_{tbl}"):
        sample = df.head(20).to_csv(index=False)
        with st.spinner("🤖 GPT-4o is thinking…"):
            try:
                ans = ask_gpt4o(q, sample)
                st.subheader("💬 GPT-4o Response")
                st.write(ans)
            except Exception as e:
                st.error(f"OpenAI error: {e}")

