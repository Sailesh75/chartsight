import os
os.environ["FORCE_MOCK"] = "1"
import streamlit, boto3  # noqa
from medical_nlp import load_notes, analyze

notes = load_notes()
print("streamlit:", streamlit.__version__, "| boto3:", boto3.__version__)
print("notes loaded:", len(notes), "->", [n["title"] for n in notes])
r = analyze(notes[1]["text"])
print("engine:", r["engine"])
print("phi redacted:", len(r["phi"]), "| conditions:", len(r["conditions"]), "| gaps:", len(r["insights"]["gaps"]))
print("sample code:", r["conditions"][0]["code"] if r["conditions"] else "none")
print("OK")
