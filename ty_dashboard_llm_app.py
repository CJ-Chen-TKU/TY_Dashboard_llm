 # py code beginning

# ty_dashboard_llm_app.py

import os
import streamlit as st
import pandas as pd
import requests
from msal import ConfidentialClientApplication
import openai

# ─── 1. Configuration & Secret Management ────────────────────────────────────
# OpenAI API key (env var TY_dashboard or manual input)
openai.api_key = os.getenv("TY_dashboard", "")
if not openai.api_key:
    openai.api_key = st.text_input("Enter your OpenAI API key", type="password")
if not openai.api_key:
    st.warning("🔑 An OpenAI API key is required to proceed.")
    st.stop()

# Azure AD & Power BI service principal details
TENANT_ID     = "ba129fe2-5c7b-4f4b-9670-ed7494972f23"   # Directory (tenant) ID
CLIENT_ID     = "770a4905-e32f-493e-817f-9731db47761b"   # Application (client) ID
CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")    # Client secret (set via env var)

# Power BI workspace & dataset IDs (set via env vars)
WORKSPACE_ID  = os.getenv("POWERBI_WORKSPACE_ID", "")
DATASET_ID    = os.getenv("POWERBI_DATASET_ID", "")

# ─── 2. Debug Sidebar: Verify env vars are loaded ────────────────────────────
st.sidebar.header("🔧 Debug Environment")
st.sidebar.write("CLIENT_SECRET set:", bool(CLIENT_SECRET))
st.sidebar.write("WORKSPACE_ID:", WORKSPACE_ID or "⚠️ Not set")
st.sidebar.write("DATASET_ID:",  DATASET_ID  or "⚠️ Not set")

# ─── 3. Define Your Table Names ───────────────────────────────────────────────
# Replace or extend this list with all your exact Power BI table names
TABLE_NAMES = [
    "F-①電子零組件製造業",
    "F-②電腦、電子產品及光學製品製造業",
    "F-③汽車及其零件製造業",
    "F-④金屬製品製造業",
    "F-⑤產業用機械設備維修及安裝業",
]

# ─── 4. Power BI Helper Functions ─────────────────────────────────────────────
def get_powerbi_token() -> str:
    """
    Authenticate via MSAL to Azure AD and return an access token
    for the Power BI REST API.
    """
    app = ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(
        scopes=["https://analysis.windows.net/powerbi/api/.default"]
    )
    # Debug: show MSAL response in sidebar
    st.sidebar.write("MSAL response:", result)

    token = result.get("access_token")
    if not token:
        err = result.get("error")
        desc = result.get("error_description")
        st.error(f"❌ Token error: {err}\n{desc}")
    return token or ""

def fetch_powerbi_table(name: str) -> pd.DataFrame:
    """
    Retrieve all rows from the given Power BI table
    and return as a pandas DataFrame.
    """
    token = get_powerbi_token()
    if not token:
        return pd.DataFrame()
    url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/"
        f"{WORKSPACE_ID}/datasets/{DATASET_ID}/tables/{name}/rows"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return pd.DataFrame(resp.json().get("value", []))

# ─── 5. GPT-4o Helper Function ────────────────────────────────────────────────
def ask_gpt4o(prompt: str, data_csv: str) -> str:
    """
    Send the user's prompt plus a CSV snippet to GPT-4o
    and return the model’s text response.
    """
    messages = [
        {"role": "system", "content": "You are a helpful data analyst."},
        {"role": "user",   "content": f"{prompt}

Here is the data:\n{data_csv}"}
    ]
    resp = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()

# ─── 6. Streamlit App UI ──────────────────────────────────────────────────────
st.title("📊 Power BI → GPT-4o Multi-Table Analysis")

# 6.1 Let user select one or more tables
selected_tables = st.multiselect(
    "Select one or more Power BI tables to analyze:",
    options=TABLE_NAMES
)

if not selected_tables:
    st.info("👉 Please select at least one table above to proceed.")
    st.stop()

# 6.2 Loop over each selected table
for table_name in selected_tables:
    st.markdown(f"---\n## Table: `{table_name}`")
    with st.spinner(f"Fetching `{table_name}`…"):
        df = fetch_powerbi_table(table_name)

    # 6.3 Handle empty data
    if df.empty:
        st.warning(f"⚠️ No data returned for `{table_name}`.")
        continue

    # 6.4 Show a preview of the first 20 rows
    st.dataframe(df.head(20))

    # 6.5 Question & Answer UI for this table
    question_key = f"question_{table_name}"
    button_key   = f"btn_{table_name}"

    question = st.text_area(
        f"Ask GPT-4o about `{table_name}`:",
        key=question_key
    )
    if st.button(f"Ask GPT-4o ( `{table_name}` )", key=button_key):
        sample_csv = df.head(20).to_csv(index=False)
        with st.spinner("🤖 GPT-4o is thinking..."):
            try:
                answer = ask_gpt4o(question, sample_csv)
                st.subheader("💬 GPT-4o Response")
                st.write(answer)
            except Exception as e:
                st.error(f"❌ GPT-4o API error:\n{e}")

