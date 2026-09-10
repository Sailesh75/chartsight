from __future__ import annotations

import streamlit as st

from medical_nlp import MODEL_LABEL, REGION, analyze, load_notes

st.set_page_config(page_title="Clinical Documentation Intelligence", page_icon="🩺", layout="wide")

st.markdown(
    """
    <style>
      .block-container {max-width: 1080px; padding-top: 2.2rem;}
      h1 {font-weight: 700; letter-spacing: -0.02em;}
      div[data-testid="stMetricValue"] {font-size: 1.6rem;}
      section[data-testid="stSidebar"] {background: #fafafa;}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Clinical Documentation Intelligence")
st.caption(
    "Extract ICD-10-CM codes, remove PHI, and flag risk-adjustment (HCC) documentation gaps "
    "— powered by Amazon Bedrock."
)

with st.sidebar:
    st.markdown("### How it works")
    st.markdown(
        "1. **De-identify** — detect & redact PHI\n"
        "2. **Code** — infer ICD-10-CM with evidence\n"
        "3. **Review** — flag HCC documentation gaps"
    )
    st.divider()
    st.markdown("### Built on AWS")
    st.markdown(f"- Amazon Bedrock\n- {MODEL_LABEL}")

# --- input ------------------------------------------------------------------ #
notes = load_notes()
labels = {n["title"]: n for n in notes}
choice = st.selectbox("Sample clinical note", list(labels.keys()))
text = st.text_area("Clinical note (editable — paste your own)", value=labels[choice]["text"], height=200)

if st.button("Analyze note", type="primary"):
    with st.spinner("Analyzing…"):
        st.session_state["result"] = analyze(text)

result = st.session_state.get("result")
if not result:
    st.info("Select a note and click **Analyze note**.")
    st.stop()

live = result["engine"].startswith("Amazon")

# --- summary ---------------------------------------------------------------- #
gaps = result["insights"]["gaps"]
flagged = [g for g in gaps if "HCC" in g or "risk-adjust" in g]

c1, c2, c3 = st.columns(3)
c1.metric("PHI entities redacted", len(result["phi"]))
c2.metric("Conditions coded", len(result["conditions"]))
c3.metric("Documentation gaps", len(flagged))

tab_codes, tab_phi, tab_gaps = st.tabs(["🏷️ ICD-10-CM codes", "🔒 PHI redaction", "📋 Documentation gaps"])

with tab_codes:
    if not result["conditions"]:
        st.info("No codeable conditions detected.")
    else:
        rows = [
            {
                "Condition (evidence)": c["text"],
                "ICD-10-CM": c["code"],
                "Description": c["description"],
                "Confidence": round(c["confidence"] * 100, 1),
            }
            for c in result["conditions"]
        ]
        st.dataframe(
            rows,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Confidence": st.column_config.ProgressColumn(
                    "Confidence", format="%.1f%%", min_value=0, max_value=100
                )
            },
        )
        st.caption("Each code is linked to the exact text span that supports it — the evidence trail an auditor needs.")

with tab_phi:
    st.caption("Protected health information is detected and removed before any downstream processing.")
    if result["phi"]:
        st.write("**Redacted:** " + ", ".join(sorted({p["type"] for p in result["phi"]})))
    else:
        st.write("**No PHI detected.**")
    st.text_area("De-identified note", value=result["redacted"], height=200, disabled=True)

with tab_gaps:
    for g in gaps:
        st.markdown(f"- {g}")

st.divider()
foot = f"Analyzed with Amazon Bedrock · {MODEL_LABEL} · {REGION}" if live else "Sample output · connect AWS for live analysis"
st.caption(f"{foot}  |  Synthetic data only — no real PHI.")
