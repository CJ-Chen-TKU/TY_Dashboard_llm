 # py code beginning
# ty_dashboard_llm_app.py

import os
import streamlit as st
import pandas as pd
import requests
from msal import ConfidentialClientApplication
import openai

# ─── 1. Configuration & Secret Management ────────────────────────────────────
# OpenAI API key (set in env var TY_dashboard or enter manually)
openai.api_key = os.getenv("TY_dashboard", "")
if not openai.api_key:
    openai.api_key = st.text_input("Enter your OpenAI API key", type="password")
if not openai.api_key:
    st.warning("🔑 An OpenAI API key is required to proceed.")
    st.stop()

# Azure AD & Power BI service principal details
TENANT_ID     = "ba129fe2-5c7b-4f4b-9670-ed7494972f23"   # Directory (tenant) ID
CLIENT_ID     = "770a4905-e32f-493e-817f-9731db47761b"   # Application (client) ID
CLIENT_SECRET   = os.getenv("eaa575b7-b4d6-48f2-8451-c4d0fe3c2ad4", "")    # Client secret (set securely)

# Power BI workspace & dataset IDs (set as env vars)
WORKSPACE_ID  = os.getenv("41bfeb46-6ff1-4baa-824a-9681be3a586d", "")
DATASET_ID    = os.getenv("08984f8c-149e-4d62-90b0-5a328c5450aa", "")

# ─── 2. Define Your Table Names ───────────────────────────────────────────────
# Replace these with the exact names of your Power BI tables
TABLE_NAMES = [
    "F-①電子零組件製造業"
]

#    F-②電腦、電子產品及光學製品製造業 ed57710b-5313-45f4-ad1b-c7202df47914
#    F-③汽車及其零件製造業 38634388-7bf8-4c29-a62b-db15e8251458
#    F-④金屬製品製造業 e5c850e8-a199-4f29-8cce-f384b6cea90e
#    F-⑤產業用機械設備維修及安裝業 5831ffc0-50bf-4f87-9697-9c4d90477c0d


# ─── 3. Power BI Helper Functions ─────────────────────────────────────────────
def get_powerbi_token() -> str:
    """
    Authenticate to Azure AD via MSAL and return an access token
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
    return result.get("access_token", "")

def fetch_powerbi_table(name: str) -> pd.DataFrame:
    """
    Retrieve all rows from the given Power BI table and
    return as a pandas DataFrame.
    """
    token = get_powerbi_token()
    if not token:
        st.error("❌ Unable to acquire Power BI token. Check your Azure credentials.")
        return pd.DataFrame()
    url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/"
        f"{WORKSPACE_ID}/datasets/{DATASET_ID}/tables/{name}/rows"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()
    return pd.DataFrame(resp.json().get("value", []))

# ─── 4. GPT-4o Helper Function ────────────────────────────────────────────────
def ask_gpt4o(prompt: str, data_csv: str) -> str:
    """
    Send the user's question plus a CSV snippet to GPT-4o,
    and return the model’s text response.
    """
    messages = [
        {"role": "system", "content": "You are a helpful data analyst."},
        {"role": "user",   "content": f"{prompt}\n\nHere is the data:\n{data_csv}"}
    ]
    resp = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.3,
    )
    return resp.choices[0].message.content.strip()

# ─── 5. Streamlit App UI ──────────────────────────────────────────────────────
st.title("📊 Power BI → GPT-4o Multi-Table Analysis")

# 5.1 Let user select one or more of the predefined tables
selected_tables = st.multiselect(
    "Select one or more Power BI tables to analyze:",
    options=TABLE_NAMES
)

if not selected_tables:
    st.info("👉 Please select at least one table above to proceed.")
    st.stop()

# 5.2 Loop over each selected table
for table_name in selected_tables:
    st.markdown(f"---\n## Table: `{table_name}`")
    with st.spinner(f"Fetching `{table_name}`…"):
        df = fetch_powerbi_table(table_name)

    # 5.3 Handle empty data
    if df.empty:
        st.warning(f"⚠️ No data returned for `{table_name}`.")
        continue

    # 5.4 Show a preview of the first rows
    st.dataframe(df.head(20))

    # 5.5 Allow the user to ask a question about this table
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


