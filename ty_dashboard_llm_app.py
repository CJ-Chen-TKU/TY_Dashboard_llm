 # py code beginning

# ty_dashboard_llm_app.py
import streamlit as st
import pandas as pd
import requests
import openai
import io
import contextlib
import re

from msal import ConfidentialClientApplication

# ------------------ CONFIG ------------------
PBI_BASE = "https://api.powerbi.com/v1.0/myorg"

# Static dataset list for speed
DATASETS = {
    "F-①電子零組件製造業": "08984f8c-149e-4d62-90b0-5a328c5450aa",
    "F-②電腦、電子產品及光學製品製造業": "ed57710b-5313-45f4-ad1b-c7202df47914",
    "F-③汽車及其零件製造業": "38634388-7bf8-4c29-a62b-db15e8251458",
    "F-④金屬製品製造業": "e5c850e8-a199-4f29-8cce-f384b6cea90e",
    "F-⑤產業用機械設備維修及安裝業": "5831ffc0-50bf-4f87-9697-9c4d90477c0d",
}
DEFAULT_WORKSPACE_ID = "41bfeb46-6ff1-4baa-824a-9681be3a586d"

# ------------------ SIDEBAR: Credentials ------------------
st.sidebar.header("🔐 Credentials")
OPENAI_KEY = st.sidebar.text_input("OpenAI API Key", type="password")
TENANT_ID = st.sidebar.text_input("Azure Tenant ID", value="ba129fe2-5c7b-4f4b-9670-ed7494972f23")
CLIENT_ID = st.sidebar.text_input("Azure Client ID", value="770a4905-e32f-493e-817f-9731db47761b")
CLIENT_SECRET = st.sidebar.text_input("Azure Client Secret", type="password")

if not all([OPENAI_KEY, TENANT_ID, CLIENT_ID, CLIENT_SECRET]):
    st.warning("Please fill in all credentials in the sidebar.")
    st.stop()

openai.api_key = OPENAI_KEY

# ------------------ POWER BI TOKEN ------------------
@st.cache_data(ttl=3500)
def get_powerbi_token():
    app = ConfidentialClientApplication(
        CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT_ID}",
        client_credential=CLIENT_SECRET
    )
    result = app.acquire_token_for_client(scopes=["https://analysis.windows.net/powerbi/api/.default"])
    return result.get("access_token")

token = get_powerbi_token()
if not token:
    st.error("Failed to get Power BI token.")
    st.stop()

# ------------------ FETCH TABLE DATA ------------------
@st.cache_data(ttl=1800)
def fetch_table(dataset_name, top_n=200):
    dataset_id = DATASETS[dataset_name]
    url = f"{PBI_BASE}/groups/{DEFAULT_WORKSPACE_ID}/datasets/{dataset_id}/executeQueries"
    dax = f"EVALUATE TOPN({top_n}, '{dataset_name}')"
    r = requests.post(
        url,
        json={"queries": [{"query": dax}]},
        headers={"Authorization": f"Bearer {token}"}
    )
    if not r.ok:
        return pd.DataFrame()
    t = r.json()["results"][0]["tables"][0]
    cols = [c["name"] for c in t["columns"]]
    return pd.DataFrame(t["rows"], columns=cols)

# ------------------ GPT HELPER ------------------
def ask_gpt(prompt, data_csv):
    messages = [
        {"role": "system", "content": "You are a helpful data analyst. If you generate code, wrap it in triple backticks with python."},
        {"role": "user", "content": f"{prompt}\n\nHere is the data:\n{data_csv}"}
    ]
    resp = openai.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.3)
    return resp.choices[0].message.content.strip()

# ------------------ SAFE CODE EXECUTION ------------------
def run_safe_code(code_str, local_vars):
    forbidden = ["import os", "subprocess", "open(", "shutil", "os.remove", "os.rmdir", "os.system"]
    if any(f in code_str for f in forbidden):
        st.error("⚠️ Unsafe code detected. Not running.")
        return
    with contextlib.redirect_stdout(io.StringIO()) as f:
        try:
            exec(code_str, {}, local_vars)
        except Exception as e:
            st.error(f"Error running GPT code: {e}")
    output = f.getvalue()
    if output:
        st.text(output)

# ------------------ UI ------------------
st.title("📊 Power BI + GPT-4o Dashboard")

picked_tables = st.multiselect("Select tables:", list(DATASETS.keys()), default=[list(DATASETS.keys())[0]])
if not picked_tables:
    st.stop()

data_frames = {}
for tbl in picked_tables:
    df = fetch_table(tbl)
    if df.empty:
        st.warning(f"No data for {tbl}")
    else:
        st.subheader(f"📄 {tbl}")
        st.dataframe(df.head(20))
        st.download_button(
            f"⬇ Download {tbl} CSV",
            df.to_csv(index=False).encode("utf-8"),
            file_name=f"{tbl}.csv",
            mime="text/csv"
        )
        data_frames[tbl] = df

prompt = st.text_area("Ask GPT-4o:", "Draw a line chart comparing the first table's values over time.")

if st.button("Ask GPT-4o"):
    merged_csv = "\n\n".join([f"Table: {t}\n{df.head(20).to_csv(index=False)}" for t, df in data_frames.items()])
    with st.spinner("🤖 GPT-4o thinking..."):
        answer = ask_gpt(prompt, merged_csv)

    code_match = re.search(r"```python(.*?)```", answer, re.S)
    if code_match:
        st.subheader("📊 GPT-4o Chart Output")
        code_str = code_match.group(1)
        run_safe_code(code_str, {"pd": pd, **data_frames})
    else:
        st.subheader("💬 GPT-4o Response")
        st.write(answer)


