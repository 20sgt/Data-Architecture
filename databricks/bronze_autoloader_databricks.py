# Databricks notebook source
# MAGIC %md
# MAGIC # Bronze — land raw scraped JSON as typed Delta tables
# MAGIC Auto Loader reads ONLY new files each run (its checkpoint is the memory of
# MAGIC what it has already seen) and lands them — nesting intact — into one Delta
# MAGIC table per source: `bronze.matters` and `bronze.meetings`.
# MAGIC
# MAGIC This notebook now does ONE job: **ingestion**. The flattening into staging
# MAGIC tables (silver) and the star build (gold) moved to dbt. Ingestion stays here
# MAGIC on purpose — dbt is batch SQL and can't do Auto Loader's file-by-file
# MAGIC incremental reads.
# MAGIC
# MAGIC **Compute:** run on a CLASSIC cluster whose service account can read the
# MAGIC bucket. Serverless blocks external-GCS egress — but that limit applies ONLY
# MAGIC to this ingestion step; dbt runs later on a serverless SQL warehouse.

# COMMAND ----------

CATALOG, BRONZE = "corn_off_the_cob", "bronze"
SRC = "gs://cotc_raw"                                      # the raw landing bucket
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{BRONZE}")
spark.sql(f"CREATE VOLUME IF NOT EXISTS {CATALOG}.{BRONZE}.checkpoints")
CKPT = f"/Volumes/{CATALOG}/{BRONZE}/checkpoints"          # Auto Loader's memory lives here

from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StructField, StringType, LongType, ArrayType

# COMMAND ----------

# ---------- schemas (explicit; no inference) ----------
# These stay HERE, not in dbt: applying an explicit schema at ingestion is our
# typed boundary — we fail loudly if the scraped shape drifts. Everything lands as
# strings/arrays exactly as scraped; dbt does the date parsing + flattening later.
VOTE = StructType([StructField("person_id", StringType()), StructField("person_name", StringType()),
                   StructField("vote_value", StringType())])
ACTION = StructType([StructField("date", StringType()), StructField("body", StringType()),
                     StructField("action", StringType()), StructField("result", StringType()),
                     StructField("history_id", StringType()), StructField("history_url", StringType()),
                     StructField("votes", ArrayType(VOTE))])
ATT = StructType([StructField("name", StringType()), StructField("url", StringType())])
MATTER = StructType([
    StructField("file_number", StringType()), StructField("detail_url", StringType()),
    StructField("name", StringType()), StructField("title", StringType()),
    StructField("type", StringType()), StructField("status", StringType()),
    StructField("introduced", StringType()), StructField("on_agenda", StringType()),
    StructField("final_action", StringType()), StructField("enactment_date", StringType()),
    StructField("enactment_number", StringType()), StructField("in_control", StringType()),
    StructField("sponsors", ArrayType(StringType())), StructField("related_files", ArrayType(StringType())),
    StructField("attachments", ArrayType(ATT)), StructField("actions", ArrayType(ACTION)),
    StructField("full_text", StringType())])

AGENDA = StructType([StructField("item_seq", LongType()), StructField("matter_file", StringType()),
    StructField("matter_url", StringType()), StructField("agenda_number", StringType()),
    StructField("matter_name", StringType()), StructField("matter_type", StringType()),
    StructField("matter_status", StringType()), StructField("title", StringType()),
    StructField("action_raw", StringType()), StructField("action_result", StringType()),
    StructField("history_id", StringType()), StructField("history_url", StringType())])
MDOC = StructType([StructField("document_source", StringType()), StructField("document_title", StringType()),
    StructField("document_url", StringType()), StructField("body_text", StringType())])
MEETING = StructType([StructField("meeting_id", StringType()), StructField("event_guid", StringType()),
    StructField("body_name", StringType()), StructField("meeting_date", StringType()),
    StructField("meeting_time", StringType()), StructField("location", StringType()),
    StructField("meeting_subtype", StringType()), StructField("agenda_status", StringType()),
    StructField("minutes_status", StringType()), StructField("agenda_url", StringType()),
    StructField("minutes_url", StringType()), StructField("video_clip_id", StringType()),
    StructField("documents", ArrayType(MDOC)), StructField("agenda_items", ArrayType(AGENDA))])

# COMMAND ----------

# ---------- lineage: stamp each row with where it came from ----------
# source_file  = the exact bucket path of the JSON file
# ingest_date  = the scrape-partition date, pulled out of that path
# loaded_at    = when this run landed it
def add_lineage(df):
    return (df.withColumn("source_file", F.col("_metadata.file_path"))
              .withColumn("ingest_date", F.to_date(F.regexp_extract("source_file", r"ingest_date=(\d{4}-\d{2}-\d{2})", 1)))
              .withColumn("loaded_at", F.current_timestamp()))

# COMMAND ----------

# ---------- the Auto Loader runner ----------
# Reads new JSON under gs://cotc_raw/<name>/, applies the explicit schema, adds
# lineage, and appends the whole nested row into bronze.<name>. No flattening.
def run_autoloader(name, schema):
    stream = (spark.readStream.format("cloudFiles")
              .option("cloudFiles.format", "json")
              .option("cloudFiles.schemaLocation", f"{CKPT}/{name}/schema")
              .option("multiLine", "true")
              .schema(schema)
              .load(f"{SRC}/{name}"))
    raw = add_lineage(stream)                              # nested structs + lineage, nothing flattened

    def land(batch_df, _bid):
        (batch_df.write.format("delta").mode("append")
           .partitionBy("ingest_date").saveAsTable(f"{CATALOG}.{BRONZE}.{name}"))

    q = (raw.writeStream.foreachBatch(land)
         .option("checkpointLocation", f"{CKPT}/{name}/write")
         .trigger(availableNow=True).start())
    q.awaitTermination()
    n = sum(p.get("numInputRows", 0) for p in q.recentProgress)
    print(f"{name}: landed {n} new files into {CATALOG}.{BRONZE}.{name}")

# COMMAND ----------

# MAGIC %md
# MAGIC ### (one-time) reset — run ONLY for the initial bootstrap onto this new layout
# MAGIC Clears the bronze tables + checkpoints so Auto Loader re-reads the WHOLE
# MAGIC bucket (the 2000→2026 backfill + every weekly partition to date) as one clean
# MAGIC first load. Also drops the OLD silver staging tables from the pre-dbt design —
# MAGIC dbt is their owner now. **Skip this cell on normal weekly runs.**

# COMMAND ----------

# for t in ["matters", "meetings"]:
#     spark.sql(f"DROP TABLE IF EXISTS {CATALOG}.{BRONZE}.{t}")
# dbutils.fs.rm(CKPT, recurse=True)
# # old silver staging tables from before the dbt migration (dbt rebuilds these):
# for t in ["stg_matters","stg_actions","stg_votes","stg_attachments","stg_sponsors",
#           "stg_meetings","stg_agenda_items","stg_meeting_documents"]:
#     spark.sql(f"DROP TABLE IF EXISTS {CATALOG}.silver.{t}")

# COMMAND ----------

# ---------- run both sources ----------
run_autoloader("matters",  MATTER)
run_autoloader("meetings", MEETING)

# COMMAND ----------

# Re-run the cell above and you'll see "landed 0 new files" — the checkpoint remembered.
for t in ["matters", "meetings"]:
    print(f"{CATALOG}.{BRONZE}.{t:<9}", spark.table(f"{CATALOG}.{BRONZE}.{t}").count())
