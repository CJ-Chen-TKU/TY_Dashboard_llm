# 📊 Streamlit + OpenAI Multi-Code-Block Executor

This app lets you **query your selected datasets** and **ask OpenAI** to produce both:
- Narrative analysis
- Python code (charts, stats, transformations, etc.)

It will automatically:
1. Send your **data context** and **tables** to OpenAI
2. Display any **text analysis** returned
3. **Execute all Python code blocks** from OpenAI’s reply in the app
4. Show results (charts, tables, etc.) directly in Streamlit

---

## 🚀 Features
- **Multi-code-block support** — executes all ```python``` code blocks in order.
- **Safe execution sandbox** with `run_safe_python`.
- **Session-aware** — avoids re-running the same code unless a new AI response arrives.
- **Full conversation history** stored in `st.session_state.chat`.
- **Dynamic dataset and table selection** via Power BI/Fabric API.
- **Preview tables** before asking questions.
- Works with **any question**, not just charts — text analysis, stats, transformation code, etc.

---

## 📦 Requirements
Install dependencies:

```bash
pip install -r requirements.txt
