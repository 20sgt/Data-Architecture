# Databricks notebook source
# MAGIC %md
# MAGIC # Silver loader — meetings (run AFTER the matter notebook)
# MAGIC Builds 3 meeting staging tables, then joins agenda items to your matter actions on
# MAGIC `history_id` to show the cross-slice meeting link working.
# MAGIC Edit `REPO_PATH` (same value as the matter notebook), then Run all.

# COMMAND ----------

# ===== EDIT THIS (same as the matter notebook) =====
REPO_PATH = "/Workspace/Users/CHANGE_ME/Data-Architecture"
CATALOG, SCHEMA, VOLUME = "workspace", "silver", "raw"

# COMMAND ----------

# Copy the meeting samples into the same Volume, alongside the matters.
import shutil, glob
VOL = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"
shutil.copytree(f"{REPO_PATH}/sample/meetings", f"{VOL}/meetings", dirs_exist_ok=True)
MTG_GLOB = f"{VOL}/meetings/ingest_date=*/*.json"
print("copied; meeting json files found:", len(glob.glob(MTG_GLOB)))   # expect 7

# COMMAND ----------

# Explicit schema for the meeting JSON.
from pyspark.sql.types import StructType, StructField, StringType, LongType, ArrayType

AGENDA_ITEM = StructType([
    StructField("item_seq", LongType()), StructField("matter_file", StringType()),
    StructField("matter_url", StringType()), StructField("agenda_number", StringType()),
    StructField("matter_name", StringType()), StructField("matter_type", StringType()),
    StructField("matter_status", StringType()), StructField("title", StringType()),
    StructField("action_raw", StringType()), StructField("action_result", StringType()),
    StructField("history_id", StringType()), StructField("history_url", StringType()),
])
MEETING_DOC = StructType([
    StructField("document_source", StringType()), StructField("document_title", StringType()),
    StructField("document_url", StringType()), StructField("body_text", StringType()),
])
MEETING = StructType([
    StructField("meeting_id", StringType()), StructField("event_guid", StringType()),
    StructField("body_name", StringType()), StructField("meeting_date", StringType()),
    StructField("meeting_time", StringType()), StructField("location", StringType()),
    StructField("meeting_subtype", StringType()), StructField("agenda_status", StringType()),
    StructField("minutes_status", StringType()), StructField("agenda_url", StringType()),
    StructField("minutes_url", StringType()), StructField("video_clip_id", StringType()),
    StructField("documents", ArrayType(MEETING_DOC)), StructField("agenda_items", ArrayType(AGENDA_ITEM)),
])

# COMMAND ----------

from pyspark.sql import functions as F
_DATE_FMT = "M/d/yyyy"
def _d(c): return F.to_date(F.col(c), _DATE_FMT)
_LIN = ["ingest_date", "source_file", "loaded_at"]

def read_meetings(glob_path):
    df = spark.read.schema(MEETING).option("multiLine", True).json(glob_path)
    return (df
        .withColumn("source_file", F.col("_metadata.file_path"))
        .withColumn("ingest_date", F.to_date(
            F.regexp_extract("source_file", r"ingest_date=(\d{4}-\d{2}-\d{2})", 1)))
        .withColumn("loaded_at", F.current_timestamp()))

def stg_meetings(r):
    return r.select("meeting_id", "event_guid", "body_name",
        F.col("meeting_date").alias("meeting_date_raw"), _d("meeting_date").alias("meeting_date"),
        "meeting_time", "location", "meeting_subtype", "agenda_status", "minutes_status",
        "agenda_url", "minutes_url", "video_clip_id",
        F.size(F.coalesce("agenda_items", F.array())).alias("n_agenda_items"),
        F.size(F.coalesce("documents", F.array())).alias("n_documents"), *_LIN)

def stg_agenda_items(r):
    e = r.select("meeting_id", _d("meeting_date").alias("meeting_date"), *_LIN,
                 F.explode("agenda_items").alias("it"))
    return e.select("meeting_id", "meeting_date",
        F.col("it.item_seq").alias("item_seq"),
        F.col("it.matter_file").alias("matter_file"),                  # link to matter slice
        F.col("it.history_id").alias("history_id"),                    # cross-slice join key
        F.col("it.agenda_number").alias("agenda_number"),
        F.col("it.matter_name").alias("matter_name"),
        F.col("it.matter_type").alias("matter_type"),
        F.col("it.matter_status").alias("matter_status_at_meeting"),   # point-in-time snapshot
        F.col("it.title").alias("title"),
        F.col("it.action_raw").alias("action_raw"),
        F.col("it.action_result").alias("action_result"),
        F.col("it.matter_url").alias("matter_url"),
        F.col("it.history_url").alias("history_url"), *_LIN)

def stg_meeting_documents(r):
    e = r.select("meeting_id", *_LIN, F.posexplode("documents").alias("document_seq", "doc"))
    return e.select("meeting_id", "document_seq",
        F.col("doc.document_source").alias("document_source"),
        F.col("doc.document_title").alias("document_title"),
        F.col("doc.document_url").alias("document_url"),
        F.col("doc.body_text").alias("body_text"), *_LIN)

BUILDERS = {"stg_meetings": stg_meetings, "stg_agenda_items": stg_agenda_items,
            "stg_meeting_documents": stg_meeting_documents}

# COMMAND ----------

# Run the load.
raw = read_meetings(MTG_GLOB)
print("meetings read:", raw.count())
for name, build in BUILDERS.items():
    df = build(raw)
    (df.write.format("delta").mode("overwrite").partitionBy("ingest_date")
       .saveAsTable(f"{CATALOG}.{SCHEMA}.{name}"))
    print(f"  wrote {name:<22} {df.count():>4} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cross-slice link: which matter actions happened in a meeting?
# MAGIC Join your matter actions to agenda items on `history_id`. A match means that action
# MAGIC took place in a known meeting (its future `meeting_sk`). No match = a non-meeting action
# MAGIC (Mayor approval, clerk referral) or a meeting we haven't sampled — correctly NULL.

# COMMAND ----------

acts   = spark.table(f"{CATALOG}.{SCHEMA}.stg_actions")
agenda = spark.table(f"{CATALOG}.{SCHEMA}.stg_agenda_items")

linked = (acts.join(agenda.select("history_id", "meeting_id", "meeting_date"),
                    on="history_id", how="inner")
              .select("matter_file", "action_date", "body", "action_type",
                      "action_result", "meeting_id", "meeting_date"))

print("matter actions that resolve to a meeting:", linked.count())
display(linked.orderBy("meeting_date").limit(25))
