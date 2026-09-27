# 🌯 3P Recovery Pilot

Reconciles **one store's** Toast orders against DoorDash, Uber Eats and Grubhub CSV exports. It flags error charges, cancelled-after-made orders, commission overcharges, unpaid orders and price drift, then builds a dispute worklist sorted by deadline.

The full build plan is in [`docs/loop-rebuild-plan.md`](docs/loop-rebuild-plan.md).

## Run it

```
pip install -r requirements.txt
streamlit run streamlit_app.py
```

The app opens on **sample data**, which is made up and has problems planted in it. Switch to *Upload my CSVs* in the sidebar to run a real store. The **What CSVs to pull** tab lists the exact exports and columns to bring.

## Layout

- `recon/loaders.py`: finds columns by alias for each export and normalizes them to one row per order.
- `recon/engine.py`: T1/T2/T3 order matching and rules R1–R7.
- `recon/sample.py`: synthetic data generator (`python -m recon.sample` writes `sample_data/`).
- `tests/`: `python -m pytest tests`.
