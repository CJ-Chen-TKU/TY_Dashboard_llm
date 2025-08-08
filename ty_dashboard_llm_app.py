 # py code beginning

# ty_dashboard_llm_app.py

import streamlit as st
import pandas as pd
import requests
from msal import ConfidentialClientApplication
import openai

# ─── 1. Sidebar: Secrets & IDs ─────────────────────────────────────────────────
st.sidebar.header("🔐 Power BI & Azure AD Credentials")

# OpenAI key
OPENAI_KEY = st.sidebar.text_input("OpenAI API Key", type="password")
if not OPENAI_KEY:
    st.sidebar.error("Enter your OpenAI API key")
    st.stop()
openai.api_key = OPENAI_KEY

# Azure AD / AAD app
CLIENT_ID     = st.sidebar.text_input("Azure Client ID", value="770a4905-e32f-493e-817f-9731db47761b")
TENANT_ID     = st.sidebar.text_input("Azure Tenant ID", value="ba129fe2-5c7b-4f4b-9670-ed7494972f23")

raw_secret    = st.sidebar.text_input("Azure Client Secret", type="password")
CLIENT_SECRET   = raw_secret.strip()   # remove any leading/trailing whitespace
st.sidebar.write("Secret being sent (repr):", repr(CLIENT_SECRET))

# Power BI identifiers
WORKSPACE_ID  = st.sidebar.text_input("Power BI Workspace ID", value="41bfeb46-6ff1-4baa-824a-9681be3a586d")
DATASET_ID    = st.sidebar.text_input("Power BI Dataset ID", value="08984f8c-149e-4d62-90b0-5a328c5450aa")

# Validate sidebar inputs
if not all([TENANT_ID, CLIENT_ID, CLIENT_SECRET, WORKSPACE_ID, DATASET_ID]):
    st.sidebar.error("Fill in all Power BI / Azure AD fields above.")
    st.stop()

# ─── 2. Define Your Table Names ───────────────────────────────────────────────
TABLE_NAMES = [
    "F-①電子零組件製造業",
    "F-②電腦、電子產品及光學製品製造業",
    "F-③汽車及其零件製造業",
    "F-④金屬製品製造業",
    "F-⑤產業用機械設備維修及安裝業",
]

# ─── 3. Power BI Helper Functions ─────────────────────────────────────────────
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
    # Debug: show MSAL result in sidebar
    st.sidebar.write("MSAL token response:", result)

    token = result.get("access_token")
    if not token:
        err  = result.get("error")
        desc = result.get("error_description")
        st.error(f"❌ Token error: {err}\n{desc}")
    return token or ""

def fetch_powerbi_table(name: str) -> pd.DataFrame:
    token = get_powerbi_token()
    if not token:
        st.error("❌ Unable to acquire Power BI token. Check your Azure credentials.")
        return pd.DataFrame()

    url = (
        f"https://api.powerbi.com/v1.0/myorg/groups/"
        f"{WORKSPACE_ID}/datasets/{DATASET_ID}/tables/{name}/rows"
    )
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})

    # 🔧 Add this debug section
    if not resp.ok:
        st.error(f"❌ HTTP {resp.status_code} Error while fetching table: {name}")
        st.code(resp.text, language="json")
        return pd.DataFrame()

    return pd.DataFrame(resp.json().get("value", []))


# ─── 4. GPT-4o Helper Function ────────────────────────────────────────────────
def ask_gpt4o(prompt: str, data_csv: str) -> str:
    """
    Send the user's prompt plus a CSV snippet to GPT-4o
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

# 5.1 Let user select one or more tables
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

    # 5.4 Show a preview of the first 20 rows
    st.dataframe(df.head(20))

    # 5.5 Q&A UI for this table
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

