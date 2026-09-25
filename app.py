from __future__ import annotations

import streamlit as st

from chartsight import raf
from chartsight.nlp import MODEL_LABEL, REGION, analyze, load_notes

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
        "3. **Ground** — re-select each code from official candidates\n"
        "4. **Review** — tag V28 HCCs, flag documentation gaps\n"
        "5. **Value** — V28 risk score + what each gap is worth"
    )
    st.divider()
    st.markdown("### Risk-score settings")
    segment = st.selectbox(
        "CMS-HCC segment", list(raf.SEGMENTS), format_func=lambda s: raf.SEGMENTS[s], key="segment"
    )
    base_rate = st.number_input(
        "Base rate ($ PMPM)",
        min_value=100.0,
        value=raf.USPCC_PMPM,
        step=10.0,
        help="Defaults to the CY2026 national FFS USPCC (aged + disabled). Use your plan's county "
        "benchmark for real revenue figures.",
    )
    override = st.checkbox("Override age/sex from the note")
    age_override = st.number_input("Age", min_value=0, max_value=120, value=70, disabled=not override)
    sex_override = st.radio("Sex", ["Female", "Male"], horizontal=True, disabled=not override)
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

demo = (
    raf.Demographics(int(age_override), 1 if sex_override == "Male" else 2)
    if override
    else raf.Demographics(**{k: v for k, v in result["raf"]["demographics"].items() if k != "label"})
)
risk = raf.assess([c["code"] for c in result["conditions"]], demo, segment, base_rate)

c1, c2, c3, c4 = st.columns(4)
c1.metric("PHI entities redacted", len(result["phi"]))
c2.metric("Conditions coded", len(result["conditions"]))
c3.metric("Documentation gaps", len(flagged))
c4.metric("Risk score (RAF)", f"{risk['payment']:.3f}", help="CMS-HCC V28 payment RAF, CY2026 adjustments")

tab_codes, tab_raf, tab_phi, tab_gaps = st.tabs(
    ["🏷️ ICD-10-CM codes", "💰 Risk score", "🔒 PHI redaction", "📋 Documentation gaps"]
)

with tab_codes:
    if not result["conditions"]:
        st.info("No codeable conditions detected.")
    else:
        rows = [
            {
                "Condition (evidence)": c["text"],
                "ICD-10-CM": c["code"],
                "Description": c["description"],
                "HCC (V28)": ", ".join(
                    h["hcc"] + (f" ({h['condition']})" if h["condition"] else "") for h in c["hcc_v28"]
                )
                or "—",
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
        st.caption(
            "Each code is linked to the exact text span that supports it — the evidence trail an auditor needs. "
            "HCCs come from the official CMS-HCC V28 crosswalk, never from the model."
        )

    if result.get("grounded"):
        changed = [c for c in result["conditions"] if c.get("first_pass_code") not in (None, c["code"])]
        with st.expander(f"🔎 Grounding: {len(changed)} code(s) corrected against the official code set"):
            st.caption(
                "Each condition's code was re-selected from candidates retrieved from the FY2026 "
                "ICD-10-CM code set (descriptions, inclusion terms, Alphabetic Index)."
            )
            for c in result["conditions"]:
                arrow = f"`{c['first_pass_code']}` → " if c in changed else ""
                st.markdown(f"**{c['text']}** — {arrow}`{c['code']}`")
                st.caption("Candidates: " + ", ".join(f"`{k['code']}`" for k in c.get("candidates", [])))

    ungrounded = result.get("ungrounded") or []
    if ungrounded:
        with st.expander(f"❔ {len(ungrounded)} condition(s) with no fitting official code"):
            for u in ungrounded:
                st.markdown(f"- {u['text']} (first pass: `{u['code']}`) — {u['reason']}")

    rejected = result.get("rejected_codes") or []
    if rejected:
        with st.expander(f"⚠️ {len(rejected)} code(s) discarded by the guardrail"):
            st.caption(
                "These were proposed but aren't in the official ICD-10-CM code set, so they were "
                "removed before reaching the table above."
            )
            for r in rejected:
                st.markdown(f"- `{r['code']}` ({r['text']}) — {r['reason']}")

with tab_raf:
    st.caption(
        f"CMS-HCC V28 · {risk['segment_label']} · {risk['demographics']['label']}. Raw model score "
        f"{risk['raw']:.3f} → payment RAF {risk['payment']:.3f} after the CY2026 normalization factor "
        f"(1.067) and MA coding-pattern adjustment (5.9%)."
    )
    left, right = st.columns([3, 2])
    with left:
        st.dataframe(
            [
                {"Term": t["variable"], "Description": t["label"], "Relative factor": t["coefficient"]}
                for t in risk["terms"]
            ],
            use_container_width=True,
            hide_index=True,
        )
        for lower, higher in risk["dropped_by_hierarchy"].items():
            st.caption(f"{lower} not counted: outranked by {higher} in the V28 hierarchy.")
    with right:
        st.metric("Illustrative annual revenue", f"${risk['annual_dollars']:,.0f}")
        st.caption(f"Payment RAF × ${risk['base_rate_pmpm']:,.2f} PMPM × 12.")

    st.markdown("#### Documentation opportunities")
    opps = risk["opportunities"]
    if not opps:
        st.write("Every coded condition is already documented at full specificity.")
    for o in opps:
        best = o["max_delta_dollars"]
        worst = min(x["delta_dollars"] for x in o["outcomes"]) if o["outcomes"] else 0.0
        headline = (
            f"${worst:,.0f} to ${best:,.0f}/yr, depending on what the record supports"
            if best > 0
            else "no RAF impact under V28"
        )
        with st.expander(f"`{o['code']}` {o['description']} — {headline}", expanded=best > 0):
            st.dataframe(
                [
                    {
                        "If documentation supports": x["label"],
                        "e.g. code": ", ".join(x["example_codes"]),
                        "Δ RAF": x["delta_raf"],
                        "Δ $/yr": x["delta_dollars"],
                    }
                    for x in o["outcomes"]
                ],
                use_container_width=True,
                hide_index=True,
            )
    st.caption(
        "Each outcome is scored for the whole patient (hierarchies, interactions, HCC count), and only "
        "more specific forms of the same condition are shown. Use this to prioritize provider queries: "
        "a code is justified only by what the record supports, never by what it pays."
    )

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
foot = (
    f"Analyzed with Amazon Bedrock · {MODEL_LABEL} · {REGION}"
    if live
    else "Sample output · connect AWS for live analysis"
)
st.caption(f"{foot}  |  Synthetic data only — no real PHI.")
