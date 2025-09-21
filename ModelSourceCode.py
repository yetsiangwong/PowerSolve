!pip install -q pandas numpy boto3 pyarrow fastparquet matplotlib

import boto3, json, io, pandas as pd, numpy as np
from datetime import datetime, timedelta, timezone

REGION = "ap-southeast-5"
RAW_BUCKET   = "crowd-ctrl-raw-powersolve"         
TABLE_EVENTS = "crowd-event-status-powersolve"     
VENUE_FOCUS  = "bukit-jalil"                        

s3 = boto3.client("s3", region_name=REGION)
dynamodb = boto3.resource("dynamodb", region_name=REGION)

def s3_exists(bucket, key):
    try:
        s3.head_object(Bucket=bucket, Key=key); return True
    except s3.exceptions.ClientError:
        return False

SCHED_KEY = "ml-models/mrt-schedule.json" if s3_exists(RAW_BUCKET, "ml-models/mrt-schedule.json") \
            else "transport-schedules/mrt-schedule.json"

assert s3_exists(RAW_BUCKET, "venue-layouts/venue_nodes.csv"), "Upload venue_nodes.csv to S3 first."
assert s3_exists(RAW_BUCKET, "venue-layouts/venue_edges.csv"), "Upload venue_edges.csv to S3 first."
assert s3_exists(RAW_BUCKET, SCHED_KEY), f"Missing {SCHED_KEY} in s3://{RAW_BUCKET}/"

print("OK — inputs present:",
      "\n  nodes:", "venue-layouts/venue_nodes.csv",
      "\n  edges:", "venue-layouts/venue_edges.csv",
      "\n  schedule:", SCHED_KEY)

from boto3.dynamodb.conditions import Attr

try:
    t = dynamodb.Table(TABLE_EVENTS)
    scan = t.scan(FilterExpression=Attr("venue_id").eq(VENUE_FOCUS))
    items = scan.get("Items", [])
    if items:
        latest = max(items, key=lambda r: int(r.get("timestamp", 0)))
        target_attendance = int(latest.get("expected_attendance", 60000))
        print(f"Using expected_attendance from DynamoDB ({VENUE_FOCUS}):", target_attendance)
    else:
        target_attendance = 60000
        print("No events found for venue in DynamoDB — using fallback:", target_attendance)
except Exception as e:
    target_attendance = 60000
    print("DynamoDB not available — using fallback:", target_attendance, "|", e)

rail_share = 0.70
car_share  = 0.30
assert rail_share + car_share == 1.0

sched = json.loads(s3.get_object(Bucket=RAW_BUCKET, Key=SCHED_KEY)["Body"].read().decode("utf-8"))
tz = sched.get("metadata", {}).get("tz", "Asia/Kuala_Lumpur")

event_start_local = pd.Timestamp("2025-09-20 19:00", tz=tz) 
window_start = event_start_local - pd.Timedelta("45min")     
window_end   = event_start_local + pd.Timedelta("90min")      
bin_minutes  = 5
ts = pd.date_range(window_start, window_end, freq=f"{bin_minutes}min")

def tod(t): return t.strftime("%H:%M")
def in_range(t, st, ed): return (tod(t) >= st) and (tod(t) < ed)

def headway_for(st_conf, t, weekend):
    blocks = st_conf["headway_minutes_weekend"] if weekend else st_conf["headway_minutes_weekday"]
    for b in blocks:
        if in_range(t, b["from"], b["to"]): return float(b["min"])
    return None

def ext_headway(st_conf, t):
    for e in st_conf.get("event_extensions", []):
        for b in e.get("extra_trains", []):
            if in_range(t, b["from"], b["to"]): return float(b["min"])
    return None

is_weekend = event_start_local.weekday() >= 5
rows=[]

for st_name, st_conf in sched["stations"].items():
    cap = float(st_conf.get("train_capacity_estimate", 800))
    for t in ts:
        hw_base = headway_for(st_conf, t, is_weekend)
        if hw_base is None: 
            continue
        hw = ext_headway(st_conf, t) or hw_base
        trains = bin_minutes / hw
        pax = trains * cap
        minutes_to_start = (t - event_start_local).total_seconds()/60.0
        surge = 1.0 + 0.6*np.exp(-(minutes_to_start/25.0)**2)
        pax *= surge

        for m in st_conf["station_to_gate_map"]:
            rows.append({
                "timestamp": int(t.tz_convert("UTC").timestamp()),
                "gate_id": m["gate"],
                "arrivals": int(max(0, round(pax * float(m["share"])))),
                "mode": "rail"
            })

forecast = pd.DataFrame(rows)
if not len(forecast):
    raise RuntimeError("No rail arrivals generated. Check schedule JSON and time window.")

rail_target = int(rail_share * target_attendance)
rail_cur = int(forecast["arrivals"].sum())
rail_scale = (rail_target / rail_cur) if rail_cur > 0 else 1.0
forecast["arrivals"] = (forecast["arrivals"] * rail_scale).round().astype(int)

car_gates = {"GateE":0.4, "GateF":0.3, "GateG":0.3}   
car_bins = ts[ts >= (event_start_local - pd.Timedelta("45min"))]
car_total = int(car_share * target_attendance)
per_bin = car_total / max(1, len(car_bins))
car_rows=[]
for t in car_bins:
    for g, w in car_gates.items():
        car_rows.append({"timestamp": int(t.tz_convert("UTC").timestamp()),
                         "gate_id": g, "arrivals": int(round(per_bin*w)), "mode":"car"})

if car_rows:
    forecast = (pd.concat([forecast, pd.DataFrame(car_rows)])
                .groupby(["timestamp","gate_id"], as_index=False)["arrivals"].sum()
                .sort_values(["timestamp","gate_id"]))

print("Bins:", len(ts), "| Gates:", forecast["gate_id"].nunique(),
      "| Total arrivals:", int(forecast["arrivals"].sum()))
forecast.head(8)

OUT_KEY_PARQUET = "historical-events/entry_forecast.parquet"
OUT_KEY_CSV     = "historical-events/entry_forecast.csv"
tmp_parquet = "/tmp/entry_forecast.parquet"
tmp_csv     = "/tmp/entry_forecast.csv"

forecast.to_parquet(tmp_parquet, index=False)
s3.upload_file(tmp_parquet, RAW_BUCKET, OUT_KEY_PARQUET)

forecast.to_csv(tmp_csv, index=False)
s3.upload_file(tmp_csv, RAW_BUCKET, OUT_KEY_CSV)

print(f"Uploaded to s3://{RAW_BUCKET}/{OUT_KEY_PARQUET} and {OUT_KEY_CSV}")

import csv, os, random, datetime, pathlib, boto3, botocore

RAW_BUCKET = "crowd-ctrl-raw-powersolve"   
REGION = "ap-southeast-5"
OUT_PREFIX = "historical-events"
EVENT_START = datetime.datetime(2025, 9, 20, 19, 0, tzinfo=datetime.timezone.utc)

pathlib.Path(OUT_PREFIX).mkdir(exist_ok=True)

def epoch(dt): 
    return int(dt.timestamp())

def make_halftime(outdir=OUT_PREFIX):
    rows=[]
    halftime = EVENT_START + datetime.timedelta(minutes=45)
    for offset in range(-2, 4):
        t = epoch(halftime + datetime.timedelta(minutes=offset))
        rows.append((t, "GateA", 1200 if offset==0 else 400))
        rows.append((t, "GateB", 900 if offset==0 else 300))
        rows.append((t, "GateE", 700 if offset==0 else 200))
    fn = os.path.join(outdir, "halftime_exit_forecast.csv")
    with open(fn,"w",newline="", encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["timestamp","gate_id","arrivals"])
        for r in rows: w.writerow(r)
    return fn

def make_evacuation(outdir=OUT_PREFIX):
    rows=[]
    evac_start = EVENT_START + datetime.timedelta(hours=1)
    total_att = 5000
    minutes = 15
    exits = ["ExitA","ExitB","ExitC","ExitD","ExitE","ExitF"]
    per_min = total_att // minutes
    for m in range(minutes):
        t = epoch(evac_start + datetime.timedelta(minutes=m))
        for g in exits:
            rows.append((t, g, per_min // len(exits)))
    fn = os.path.join(outdir, "evacuation_forecast.csv")
    with open(fn,"w",newline="", encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["timestamp","gate_id","arrivals"])
        for r in rows: w.writerow(r)
    return fn

def make_festival(outdir=OUT_PREFIX):
    rows=[]
    start = EVENT_START - datetime.timedelta(minutes=60)
    end = EVENT_START + datetime.timedelta(minutes=120)
    bin_minutes = 5
    gates = ["GateA","GateB","GateC","GateE","GateF","GateG","FoodA","FoodB","FoodC","FoodD"]
    t = start
    while t <= end:
        for g in gates:
            arrivals = random.randint(50,250) if random.random() < 0.25 else random.randint(0,50)
            if arrivals>0:
                rows.append((epoch(t), g, arrivals))
        t += datetime.timedelta(minutes=bin_minutes)
    fn = os.path.join(outdir, "festival_congestion.csv")
    with open(fn,"w",newline="", encoding="utf-8") as f:
        w=csv.writer(f); w.writerow(["timestamp","gate_id","arrivals"])
        for r in rows: w.writerow(r)
    return fn

hf = make_halftime()
ev = make_evacuation()
fc = make_festival()
print("Generated local files:")
for f in (hf, ev, fc):
    print("  ", f)

s3 = boto3.client("s3", region_name=REGION)
for local in (hf, ev, fc):
    key = f"{OUT_PREFIX}/{os.path.basename(local)}"
    try:
        s3.upload_file(local, RAW_BUCKET, key, ExtraArgs={"ContentType":"text/csv"})
        print(f"Uploaded s3://{RAW_BUCKET}/{key}")
    except botocore.exceptions.ClientError as e:
        print("Upload failed for", local, "-> s3://{}/{}".format(RAW_BUCKET,key))
        print("Error:", e)
        raise

print("\nPreview (first 12 lines) of halftime file:")
with open(hf, "r", encoding="utf-8") as f:
    for i, line in enumerate(f):
        print(line.strip())
        if i >= 11: break

print("\nS3 listing for historical-events/ (first 50 objects):")
paginator = s3.get_paginator('list_objects_v2')
pages = paginator.paginate(Bucket=RAW_BUCKET, Prefix=OUT_PREFIX + "/")
count = 0
for page in pages:
    for obj in page.get("Contents", []):
        print(" ", obj["Key"], obj["Size"])
        count += 1
        if count >= 50:
            break
    if count >= 50:
        break