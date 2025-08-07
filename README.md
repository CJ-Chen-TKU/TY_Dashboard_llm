# GPT-4o Report Catalog Analyzer

A Streamlit app to upload a report catalog Excel file, select reports, preview their data, and interactively query the data using OpenAI's GPT-4o model. Export GPT-generated insights as downloadable PDF reports.

---

## Features

- Upload an Excel catalog listing reports and their data file paths
- Select reports from the catalog and preview data in tabular form
- Ask GPT-4o questions about the selected data directly within the app
- Download GPT-generated answers as PDF files
- Supports data files in CSV, XLS, and XLSX formats
- Works locally, in Google Colab, or deployed on cloud platforms
- Secure API key input, compatible with Colab and local environments

---

## Getting Started

### Prerequisites

- Python 3.8 or newer
- OpenAI API key with access to GPT-4o
- Required Python packages (see `requirements.txt`)

### Installation

1. Clone this repository:

   ```bash
   git clone https://github.com/yourusername/your-repo.git
   cd your-repo
