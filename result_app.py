# streamlit_app.py
# -------------------------------------------------------------
# "Result Beautifier" — upload quarterly results image/PDF → clean Excel
# Uses Google Gemini (1.5 Pro/Flash) with your guardrailed master prompt
# -------------------------------------------------------------
# How to run:
# 1) pip install -r requirements.txt
# 2) Set your Gemini key as env var GOOGLE_API_KEY or paste it in the sidebar.
# 3) streamlit run streamlit_app.py
# -------------------------------------------------------------

import io
import json
import os
import re
from datetime import datetime
from typing import Dict, List, Tuple

import pandas as pd
from PIL import Image
import streamlit as st
import google.generativeai as genai  # Ensure you have google-generativeai installed
# Lazy import of the Gemini SDK to avoid import errors on read
# try:
#     import google.generativeai as genai
# except Exception:
#     genai = None

st.set_page_config(page_title="Result Beautifier (Gemini)", page_icon="📊", layout="wide")

# ========== Sidebar ========== #
st.sidebar.header("⚙️ Settings")
API_KEY = st.sidebar.text_input("Google Gemini API Key", type="password", value=os.getenv("GOOGLE_API_KEY", ""))
model_name = st.sidebar.selectbox("Gemini Model", [
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    "gemini-2.0-flash",
    "gemini-2.5-flash"
])

rounding = st.sidebar.number_input("Decimals (Excel)", min_value=0, max_value=6, value=2)

def _configure_gemini(api_key: str):
    if genai is None:
        st.error("google-generativeai library is not installed. Add it to requirements.txt.")
        st.stop()
    if not api_key:
        st.error("Please provide your Google Gemini API key in the sidebar.")
        st.stop()
    genai.configure(api_key=api_key)

# ========== Prompt (user supplied) ========== #
MASTER_PROMPT = r"""
Guardrail #1 – Component Checklist: you must first lay out (and add) every operating-cost line that appears in the statement before you are allowed
to compute “Total Expense.”
Guardrail #2 – Auto-Reconciliation: if the PDF/Excel/image itself shows a “Total expenses” subtotal, your computed sum must equal it. If
there’s even a ₹ 0.01 difference, you must flag a reconciliation warning and use the statement’s subtotal instead.
✅ Master Prompt —
Financial-Statement Normalisation & Horizontal Comparison (v4, “No-Miss” Edition)
Use this prompt any time you convert raw financial
statements (image, PDF, spreadsheet, XBRL, HTML, screener snapshot, etc.) into analysis-ready data and period-over-period tables.
🅰️ Extract & Label
Periods Identify each period-end date, then convert to Indian-fiscal “QFY” labels:
30 Jun 2025 → Q1FY26 • 30 Sep 2025 → Q2FY26 • 31 Dec 2025 → Q3FY26 • 31 Mar 2026 → Q4FY26
Sort periods most-recent → oldest.
🅱️ Unit Normalisation
Detect the original unit (Thousands/Lakhs/Millions/Crores) and convert all numbers to ₹ Crores, rounded to 2 decimals.
🅲 Mandatory “Expense
Components” Sub-Table (Guardrail #1)
For every period, create a mini table before any profit calculation:
Expense Component (use exact row names)QFY…Consumption of RM, components & services (2a)
Purchase of stock-in-trade (2b)
Changes in inventories (2c)
Employee benefits (2d)
Finance cost (EXCLUDE from OpEx) (2e)——Depreciation & amortisation (EXCLUDE) (2f)——Foreign-exchange loss / (gain) (2g)
Other expenses (2h)
Subtotal – Calculated Operating Expenses (sum of 2a + 2b + 2c + 2d + 2g + 2h)
Subtotal – As Reported “Total expenses” (if present)
Reconciliation Status (“OK” or “DIFF ₹ x.xx”)
If a “DIFF” appears, override your calculated subtotal with the statement’s reported figure and add a footnote.
🅳 Compute Key Metrics
Revenue = “Revenue from operations” + “Other operating income.”
Total Expense = the reconciled subtotal from section 🅲.
Operating Profit = Revenue − Total Expense.
Operating Margin % = Operating Profit ÷ Revenue × 100.
Other Income = non-operating income line.
Depreciation/Amortisation = row 2f.
Finance Cost = row 2e.
PBT = Operating Profit + Other Income − Depreciation −
Finance Cost.
Tax = Current Tax + Deferred Tax.
Net Profit = PBT − Tax.
Net Margin % = (Net Profit) ÷ Revenue × 100.
🅴 Built‑in Quarter‑on‑Quarter
(QoQ) & Year‑on‑Year
(YoY) Metrics
For every numeric row already shown in the horizontal table, calculate and display two additional comparison columns inside the same table:
QoQ % Δ = (Current Q −
      Previous Q) ÷ Previous Q × 100
YoY % Δ = (Current Q −
      Same Q prior FY) ÷ Same Q prior FY × 100
Place these two percentage columns immediately to the right of each metric’s value columns, so the final layout is:
| Metric | Q4FY26 | Q3FY26 | QoQ % Δ | Q4FY25 |YoY % Δ | …
|
Retain all formatting rules:
Two‑decimal
      precision, no commas, no “₹” symbol.
If the comparison base is zero or missing, output “N/A”.
Ensure the QoQ and YoY columns remain aligned newest → oldest with the rest of the table.
Present this table first in Markdown:
MetricQ4FY26Q3FY26Q4FY25…Revenue
Total Expense
Operating Profit
Operating Margin %
Other Income
Depreciation/Amort.
Finance Cost
PBT
Tax
Net Profit
Net Margin %
No commas or ₹ symbol inside numbers; 2-decimal precision throughout.
🅵 Footnotes &
Validation (Guardrail #2)
After the table, list any reconciliation notes flagged in 🅲. If no reconciliation differences, state “All subtotals tie to the published statement.”
🧠 Checklist Before You
Output
Did you build the expense-components sub-table?
Does each period’s calculated subtotal match the statement (or did you flag and override)?
Are all numeric rows present, even if zero?
Are units in ₹ Crores, 2 decimals, no commas?
Is the horizontal table sorted newest → oldest?
Follow these steps verbatim to avoid omissions like the one we just corrected.
"""

# Strict JSON output schema to make parsing reliable.
JSON_INSTRUCTIONS = r"""
Return ONLY a strict JSON object with this schema (no markdown, no backticks):
{
  "unit": "₹ Crores",
  "periods": ["Q1FY26", "Q4FY25", "Q1FY25", ...],  // newest → oldest
  "expense_components": {
    "Consumption of RM, components & services (2a)": {"Q1FY26": 0.00, ...},
    "Purchase of stock-in-trade (2b)": {...},
    "Changes in inventories (2c)": {...},
    "Employee benefits (2d)": {...},
    "Foreign-exchange loss / (gain) (2g)": {...},
    "Other expenses (2h)": {...},
    "Subtotal – Calculated Operating Expenses": {...},
    "Subtotal – As Reported “Total expenses”": {...},
    "Reconciliation Status": {"Q1FY26": "OK"}
  },
  "other_income_label": "Other Income",
  "metrics": {
    "Revenue": {"Q1FY26": 0.00, ...},
    "Total Expense": {"Q1FY26": 0.00, ...},
    "Operating Profit": {...},
    "Operating Margin %": {...},
    "Other Income": {...},
    "Depreciation/Amort.": {...},
    "Finance Cost": {...},
    "PBT": {...},
    "Tax": {...},
    "Net Profit": {...},
    "Net Margin %": {...}
  },
  "footnotes": ["..."]
}
All numeric values must already be converted to ₹ Crores with 2-decimal rounding and must be numbers (not strings). Do NOT include commas or currency symbols anywhere in numbers. If a component is absent in the source, include it with 0.00. Ensure "periods" are newest→oldest QFY labels.
"""

SYSTEM_SUFFIX = (
    "\nOUTPUT RULES: Respond with pure JSON only. No explanations, no markdown, no prose."
)

# ========== Helpers ========== #

def load_image_bytes(uploaded_file) -> bytes:
    if uploaded_file is None:
        return None
    if uploaded_file.type in ("image/png", "image/jpeg", "image/jpg"):
        # Return raw bytes
        return uploaded_file.read()
    elif uploaded_file.type == "application/pdf":
        # For PDFs, we send bytes directly to Gemini as a file part
        return uploaded_file.read()
    else:
        # Try to open as PIL image fallback
        try:
            img = Image.open(uploaded_file)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()
        except Exception:
            return uploaded_file.read()


def extract_json_strict(txt: str) -> str:
    """Extract the first top-level JSON object from model text output."""
    # If the model obeys instructions, txt is already pure JSON.
    if txt.strip().startswith("{") and txt.strip().endswith("}"):
        return txt.strip()
    # Fallback: grab the outermost {...}
    match = re.search(r"\{[\s\S]*\}", txt)
    if match:
        return match.group(0)
    raise ValueError("No JSON object found in model response.")


def dict_to_dataframe(block: Dict[str, Dict[str, float]], periods: List[str]) -> pd.DataFrame:
    rows = []
    for metric, pervals in block.items():
        row = {"Metric": metric}
        for p in periods:
            row[p] = pervals.get(p, 0.0)
        rows.append(row)
    df = pd.DataFrame(rows)
    df = df.set_index("Metric")
    return df


def compute_qoq_yoy(df: pd.DataFrame, periods: List[str]) -> pd.DataFrame:
    """Add QoQ and YoY %Δ columns after each period column block.
    Expected order: newest → oldest in `periods`.
    """
    # Work on a copy, numbers only
    out_cols: List[str] = []
    # Build new column order with QoQ and YoY interleaved
    for i, p in enumerate(periods):
        out_cols.append(p)
        # QoQ vs previous period (i+1)
        if i + 1 < len(periods):
            prev = periods[i + 1]
            qoq = (df[p] - df[prev]) / df[prev] * 100
            qoq = qoq.where(df[prev] != 0, other=pd.NA)
            df[f"{p} QoQ %Δ"] = qoq
            out_cols.append(f"{p} QoQ %Δ")
        # YoY vs same quarter prior FY (i+4)
        if i + 4 < len(periods):
            same = periods[i + 4]
            yoy = (df[p] - df[same]) / df[same] * 100
            yoy = yoy.where(df[same] != 0, other=pd.NA)
            df[f"{p} YoY %Δ"] = yoy
            out_cols.append(f"{p} YoY %Δ")
    # Reorder
    df = df[[c for c in out_cols if c in df.columns]]
    return df


def format_numeric(df: pd.DataFrame, decimals: int = 2) -> pd.DataFrame:
    def fmt(x):
        if pd.isna(x):
            return "N/A"
        try:
            return f"{float(x):.{decimals}f}"
        except Exception:
            return x
    return df.applymap(fmt)


def reconcile_expenses(exp_df_raw: pd.DataFrame, periods: List[str]) -> Tuple[pd.DataFrame, List[str]]:
    """Apply Guardrail #2 programmatically: check calc sum vs reported total.
    Returns possibly adjusted dataframe and footnotes list.
    """
    notes = []
    # Ensure required rows exist; fill if missing
    required = [
        "Consumption of RM, components & services (2a)",
        "Purchase of stock-in-trade (2b)",
        "Changes in inventories (2c)",
        "Employee benefits (2d)",
        "Foreign-exchange loss / (gain) (2g)",
        "Other expenses (2h)",
        "Subtotal – Calculated Operating Expenses",
        "Subtotal – As Reported “Total expenses”",
        "Reconciliation Status",
    ]
    for r in required:
        if r not in exp_df_raw.index:
            exp_df_raw.loc[r] = 0.0

    # Compute calc subtotal and compare per-period
    calc_rows = [
        "Consumption of RM, components & services (2a)",
        "Purchase of stock-in-trade (2b)",
        "Changes in inventories (2c)",
        "Employee benefits (2d)",
        "Foreign-exchange loss / (gain) (2g)",
        "Other expenses (2h)",
    ]

    for p in periods:
        calc_sum = exp_df_raw.loc[calc_rows, p].astype(float).sum()
        if "Subtotal – As Reported “Total expenses”" in exp_df_raw.index:
            reported = float(exp_df_raw.at["Subtotal – As Reported “Total expenses”", p])
        else:
            reported = calc_sum
        diff = round(calc_sum - reported, 2)
        # Update the calculated subtotal row to equal calc_sum (for visibility)
        exp_df_raw.at["Subtotal – Calculated Operating Expenses", p] = round(calc_sum, 2)
        # Guardrail: if diff != 0 within 0.01 tolerance, override and note
        if abs(diff) >= 0.01:
            notes.append(
                f"{p}: DIFF ₹ {abs(diff):.2f} — overriding Calculated Operating Expenses with reported total."
            )
            exp_df_raw.at["Subtotal – Calculated Operating Expenses", p] = reported
            if "Reconciliation Status" in exp_df_raw.index:
                exp_df_raw.at["Reconciliation Status", p] = f"DIFF ₹ {abs(diff):.2f}"
        else:
            if "Reconciliation Status" in exp_df_raw.index:
                exp_df_raw.at["Reconciliation Status", p] = "OK"

    if not notes:
        notes = ["All subtotals tie to the published statement."]

    return exp_df_raw, notes


# ========== UI ========== #
st.title("📊 Result Beautifier — Image → Clean Excel (Gemini)")
st.caption("Upload an image/PDF of quarterly results. The app extracts, reconciles, and exports a tidy Excel with metrics and QoQ/YoY.")

uploaded = st.file_uploader("Upload results image/PDF", type=["png", "jpg", "jpeg", "pdf"])

colA, colB = st.columns([1, 1])
if uploaded is not None:
    with colA:
        st.subheader("Preview")
        if uploaded.type.startswith("image/"):
            st.image(uploaded, use_column_width=True)
        else:
            st.info("PDF uploaded — will be sent to Gemini as file bytes.")

with colB:
    st.subheader("Extraction Prompt")
    with st.expander("Show / Edit Prompt", expanded=False):
        # Let advanced users tweak without breaking the core schema
        edited_prompt = st.text_area("Master Prompt", MASTER_PROMPT, height=320)
        st.markdown("**JSON Output Schema (read-only):**")
        st.code(JSON_INSTRUCTIONS, language="markdown")

run = st.button("🚀 Extract & Generate Excel")

if run:
    _configure_gemini(API_KEY)

    # Prepare content parts: prompt + schema + rule suffix + file
    sys_prompt = edited_prompt.strip() + "\n\n" + JSON_INSTRUCTIONS.strip() + SYSTEM_SUFFIX

    # Build content parts list
    parts = [sys_prompt]

    file_bytes = load_image_bytes(uploaded)
    if file_bytes is None:
        st.error("Please upload a valid image or PDF file.")
        st.stop()

    # Attach the file as a content part
    mime = uploaded.type or "image/png"
    file_part = {"mime_type": mime, "data": file_bytes}

    with st.spinner("Calling Gemini and parsing structured output…"):
        try:
            model = genai.GenerativeModel(model_name)
            resp = model.generate_content([parts[0], file_part])
            raw_text = resp.text or ""
            json_str = extract_json_strict(raw_text)
            payload = json.loads(json_str)
        except Exception as e:
            st.error(f"Gemini extraction failed: {e}")
            st.stop()

    # Parse payload → DataFrames
    try:
        periods: List[str] = payload.get("periods", [])
        unit = payload.get("unit", "₹ Crores")
        expense_components = payload.get("expense_components", {})
        metrics = payload.get("metrics", {})
        footnotes_llm = payload.get("footnotes", []) or []

        exp_df = dict_to_dataframe(expense_components, periods)
        # Ensure numeric (strings → float)
        for p in periods:
            exp_df[p] = pd.to_numeric(exp_df[p], errors="coerce").fillna(0.0)

        # Programmatic reconciliation (Guardrail #2 double-check)
        exp_df_checked, notes_prog = reconcile_expenses(exp_df.copy(), periods)
        # Merge notes
        footnotes = list(dict.fromkeys(footnotes_llm + notes_prog))

        # Metrics block
        met_df = dict_to_dataframe(metrics, periods)
        for p in periods:
            met_df[p] = pd.to_numeric(met_df[p], errors="coerce")

        # Add QoQ & YoY columns (computed here, even if LLM did it)
        met_df = compute_qoq_yoy(met_df, periods)

        # Final formatting strings for display
        exp_df_disp = format_numeric(exp_df_checked.copy(), decimals=rounding)
        met_df_disp = format_numeric(met_df.copy(), decimals=rounding)

        # ---------- Show in UI ---------- #
        st.success(f"Parsed {len(periods)} period(s). Units: {unit}.")
        st.subheader("Expense Components (Guardrailed)")
        st.dataframe(exp_df_disp, use_container_width=True)

        st.subheader("Key Metrics with QoQ/YoY")
        st.dataframe(met_df_disp, use_container_width=True)

        st.subheader("Footnotes & Reconciliation")
        st.write("\n".join(f"• {n}" for n in footnotes))

        # ---------- Create Excel ---------- #
        xbuf = io.BytesIO()
        with pd.ExcelWriter(xbuf, engine="xlsxwriter") as writer:
            # Numeric versions in Excel (not the formatted strings)
            exp_df_checked.to_excel(writer, sheet_name="Expense Components")
            met_df.to_excel(writer, sheet_name="Metrics QoQ YoY")
            pd.DataFrame({"Footnotes": footnotes}).to_excel(writer, sheet_name="Footnotes", index=False)

            # Optional: set column widths & number format
            wb = writer.book
            num_fmt = wb.add_format({"num_format": f"0.{''.join(['0']*rounding)}" if rounding>0 else "0"})
            for sheet in ("Expense Components", "Metrics QoQ YoY"):
                ws = writer.sheets[sheet]
                ws.set_column(0, 0, 38)
                ws.set_column(1, 200, 16, num_fmt)

        xbuf.seek(0)
        fname = f"result_beautifier_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        st.download_button(
            "⬇️ Download Excel",
            data=xbuf,
            file_name=fname,
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    except Exception as e:
        st.error(f"Failed to build tables: {e}")


# -------------------------------------------------------------
# requirements.txt (copy into a separate file)
# -------------------------------------------------------------
# streamlit
# google-generativeai
# pandas
# pillow
# openpyxl
# xlsxwriter
# -------------------------------------------------------------
