Table of contents

1. Project overview

2. Architecture (logical)

3. Components & services used

4. Data sources & file formats

5. Quick start — deployment & local testing

6. Running simulations & examples

7. ML Notebook (prediction / visualization)

8. File / bucket naming conventions

Project overview

This project helps event organisers and operators plan and react to crowd patterns without new sensors — by using existing structured datasets (ticketing, seating, gates, transport schedules). It supports scenario simulation (e.g., entry_rush, festival_congestion, evacuation) and transport-aware spikes (train arrivals → arrival spikes, auto-suggest additional trains). Outputs include hotspot maps, action recommendations and operator alerts.

Example use-cases:

1. Predict and mitigate entry rushes for concerts/stadiums.

2. Simulate emergency evacuation and identify chokepoints.

4. Produce operational alerts (open extra gates, temporary signage, request additional trains).

Architecture (logical)
Data Sources (ticketing, schedules, venue layouts, mrt-schedule.json, historical-events/*.csv)
      ↓
Ingestion layer (S3 raw buckets + seed Lambdas / PutEvents)
      ↓
Simulation & Forecasting
   - Simulator (Python lambda / job) → scenario CSV / parquet (binned arrivals)
   - SageMaker Notebook / model → forecasting & feature importance
      ↓
Hotspot Detection (analysis on simulation output)
      ↓
Actions & Alerts
   - AWS Step Functions (orchestration)
   - SNS / Lambda to persist decisions
   - Optional: DynamoDB for state & errors

Key architecture phrase used during design:
Data → Ingestion (S3/DynamoDB) → Simulation & Forecasting (Lambda / SageMaker) → Hotspot Detection → Actions & Alerts (Step Functions → SNS)

Components & AWS services used

S3 — raw data, scenarios, simulation output (CSV / Parquet)
Example buckets in this project:

1. crowd-ctrl-raw-powersolve

2. crowd-venues-powersolve

3. crowd-event-status-powersolve

Lambda — seeders, simulator runner, alert publisher, failure handler

Step Functions — orchestration of simulation → detection → publish alerts, with Retry/Catch/HandleFailure patterns

SageMaker / JupyterLab — training or model inference for forecasting (region: ap-southeast-5)
Notebook: recommended name cc-ml-notebook

DynamoDB — status tables, optionally state-machine-errors for logged failures

SNS — immediate operator alerts / notifications

Data sources & formats

Typical inputs:

historical-events/*.csv — binned historical counts (used for seeding forecast & simulator)
Example: historical-events/entry_forecast.csv

mrt-schedule.json — train arrival times used for transport-aware spikes

venue_nodes.csv, venue_layouts/* — venue gates and mapping

Scenario JSON (small example):

{
  "scenario": "entry_rush",
  "gate_open": ["GateA","GateB"],
  "closed_nodes": [],
  "forecast_key": "historical-events/entry_forecast.csv"
}

Simulation outputs: CSV / Parquet with time bin, gate id, arrivals, hotspots, s3_prefix metadata

Quick start — setup & deployment (local → AWS)

Prerequisites:

1. AWS account with privileges to create S3, Lambda, Step Functions, SageMaker notebook and IAM roles

2. AWS CLI configured to ap-southeast-5 or select region via console

3. Python 3.12 for simulator scripts

Running simulations (example)

Use the simulator script (e.g., simulate.py or lambda Simulate task) to generate binned arrivals and hotspot data. The simulator reads scenario JSON from S3 and writes CSV/Parquet back to S3.

Transport-aware mode: replace generator cell / function with the "spike-by-train" implementation that reads mrt-schedule.json and spikes bins coinciding with train arrivals.

Example scenario flow:

1. Put scenario JSON into scenarios/entry_rush/config.json

2. Trigger Step Function or invoke simulator lambda

3. Simulator writes scenarios/entry_rush/outputs/<run_id>/... and returns { "outputs": { "s3_prefix": "scenarios/entry_rush/<run_id>", "hotspots": n, "actions": m } }

ML Notebook (forecasting & visualization)

Notebook (SageMaker) name suggestion: cc-ml-notebook

Workflow:

1. Load historical CSVs from S3

2. Preprocess into features / target (time-of-day, day-of-week, event-type, train-arrivals, gate-mapping)

3. Train a simple model (RandomForest / XGBoost) to forecast per-bin arrivals

4. Evaluate MAE / RMSE and plot Actual vs Predicted

5. Plot feature importances (useful for operational insights)

6. Install libs inside notebook terminal:
