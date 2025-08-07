 # py code beginning

import streamlit as st
import pandas as pd
import openai
import os
from fpdf import FPDF
import io

# --- Get OpenAI API key ---
api_key = os.getenv("TY_dashboard")
if not api_key:
    api_key = st.text_input("Enter your OpenAI API key", type="password")

if not api_key:
    st.warning("OpenAI API key is required to proceed.")
    st.stop()

openai.api_key = api_key

# --- GPT-4o Query Function ---
def ask_gpt4o(prompt, data_csv):
    messages = [
        {"role": "system", "content": "You are a helpful data analyst."},
        {"role": "user", "content": f"{prompt}\n\nHere is the data:\n{data_csv}"}
    ]
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.3,
    )
    return response.choices[0].message.content.strip()

# Create PDF bytes from text
def create_pdf_bytes(text, title="GPT-4o Report"):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", size=12)
    pdf.cell(0, 10, title, ln=True, align="C")
    pdf.ln(10)
    for line in text.split('\n'):
        pdf.multi_cell(0, 10, line)
    pdf_buffer = io.BytesIO()
    pdf.output(pdf_buffer)
    pdf_buffer.seek(0)
    return pdf_buffer

st.title("📊 Report Catalog Upload & GPT-4o Interactive Analysis with PDF Export")

if "catalog_df" not in st.session_state:
    st.session_state.catalog_df = None

uploaded_catalog = st.file_uploader("Upload Report Catalog Excel File (.xlsx/.xls)", type=["xlsx", "xls"])

if uploaded_catalog and st.session_state.catalog_df is None:
    try:
        st.session_state.catalog_df = pd.read_excel(uploaded_catalog)
        st.success("✅ Catalog file loaded successfully!")
    except Exception as e:
        st.error(f"Failed to load catalog file: {e}")

if st.session_state.catalog_df is not None:
    catalog_df = st.session_state.catalog_df

    required_columns = {"ReportName", "DataFilePath"}
    if not required_columns.issubset(set(catalog_df.columns)):
        st.error(f"Catalog file must contain columns: {required_columns}")
    else:
        reports = catalog_df["ReportName"].tolist()
        selected_report = st.selectbox("Select a report", reports)

        if selected_report:
            data_path = catalog_df.loc[catalog_df["ReportName"] == selected_report, "DataFilePath"].values[0]

            if not os.path.isfile(data_path):
                st.error(f"Data file not found:\n{data_path}\nMake sure the app can access this file path.")
            else:
                try:
                    if data_path.lower().endswith(".csv"):
                        data_df = pd.read_csv(data_path)
                    elif data_path.lower().endswith((".xls", ".xlsx")):
                        data_df = pd.read_excel(data_path)
                    else:
                        st.error("Data file must be .csv, .xls, or .xlsx")
                        data_df = pd.DataFrame()
                except Exception as e:
                    st.error(f"Failed to load data file:\n{e}")
                    data_df = pd.DataFrame()

                if not data_df.empty:
                    st.markdown(f"### Preview of report: **{selected_report}**")
                    st.dataframe(data_df)

                    user_question = st.text_area("Ask GPT-4o about this data", key="question_input")

                    if st.button("Ask GPT-4o"):
                        csv_sample = data_df.head(20).to_csv(index=False)
                        with st.spinner("GPT-4o is analyzing..."):
                            try:
                                answer = ask_gpt4o(user_question, csv_sample)
                                st.subheader("GPT-4o Response")
                                st.write(answer)

                                pdf_bytes = create_pdf_bytes(answer, title=f"GPT-4o Report - {selected_report}")

                                st.download_button(
                                    label="📥 Download GPT Answer as PDF",
                                    data=pdf_bytes,
                                    file_name=f"GPT4o_answer_{selected_report.replace(' ', '_')}.pdf",
                                    mime="application/pdf"
                                )
                            except Exception as e:
                                st.error(f"Error calling GPT-4o API:\n{e}")
                else:
                    st.warning("Loaded data file is empty.")
else:
    st.info("Please upload the report catalog Excel file to get started.")

