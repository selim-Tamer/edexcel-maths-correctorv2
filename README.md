# edexcel-maths-correctorv2

## Install and run

Production installation uses exactly one manifest — `requirements.txt`, the same
file Streamlit Community Cloud deploys:

    python -m pip install -r requirements.txt
    streamlit run app5_1_automated.py

Test/benchmark-only dependencies (ReportLab fixtures, SymPy offline
verification research) live in `tests/pdf-requirements.txt`. Constraint policy,
 automated scanning and update flow are documented in
[TESTING.md](TESTING.md) (Section "Dependency policy and supply chain").
