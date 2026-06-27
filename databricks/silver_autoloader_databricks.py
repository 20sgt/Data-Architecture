# Databricks notebook source
# MAGIC %md
# MAGIC # Silver — incremental ingestion with Auto Loader
# MAGIC Replaces the batch silver notebooks. Reads ONLY new files each run (checkpoint = memory),
# MAGIC fans out to the 8 staging tables. Run it, then run again → the second run processes 0 files.
# MAGIC
# MAGIC **Source today:** a managed Volume (`/Volumes/.../raw/...`).
# MAGIC **Source once GCS is connected:** change `SRC` to `gs://cotc_raw` — that's the only edit.

# COMMAND ----------

CATALOG, SCHEMA = "workspace", "silver"
SRC  = f"/Volumes/{CATALOG}/{SCHEMA}/raw"        # <-- later: SRC = "gs://cotc_raw"
CKPT = f"/Volumes/{CATALOG}/{SCHEMA}/raw/_checkpoints"   # Auto Loader's memory lives here
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

from pyspark.sql import functions as F
from pyspark.sql.types import StructType, StructField, StringType, LongType, ArrayType

# COMMAND ----------

# ---------- schemas (explicit; no inference) ----------
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

# ---------- staging builders (same logic as the batch loaders) ----------
def _d(c): return F.to_date(F.col(c), "M/d/yyyy")
LIN = ["ingest_date", "source_file", "loaded_at"]

def matter_tables(r):
    yield "stg_matters", r.select(
        F.col("file_number").alias("matter_file"), "detail_url", "name", "title", "type", "status",
        F.col("introduced").alias("introduced_raw"), _d("introduced").alias("introduced_date"),
        F.col("on_agenda").alias("on_agenda_raw"), _d("on_agenda").alias("on_agenda_date"),
        F.col("final_action").alias("final_action_raw"), _d("final_action").alias("final_action_date"),
        F.col("enactment_date").alias("enactment_date_raw"), _d("enactment_date").alias("enactment_date"),
        "enactment_number", "in_control", "related_files",
        F.size(F.coalesce("actions", F.array())).alias("n_actions"),
        F.size(F.coalesce("sponsors", F.array())).alias("n_sponsors"), *LIN)
    a = r.select(F.col("file_number").alias("matter_file"), *LIN, F.posexplode("actions").alias("action_seq", "a"))
    yield "stg_actions", a.select("matter_file", "action_seq",
        F.col("a.date").alias("action_date_raw"), _d("a.date").alias("action_date"),
        F.col("a.body").alias("body"), F.col("a.action").alias("action_type"),
        F.col("a.result").alias("action_result"), F.col("a.history_id").alias("history_id"),
        F.col("a.history_url").alias("history_url"),
        F.size(F.coalesce("a.votes", F.array())).alias("n_votes"), *LIN)
    v = a.select("matter_file", "action_seq", *LIN, _d("a.date").alias("action_date"),
                 F.col("a.history_id").alias("history_id"), F.explode("a.votes").alias("v"))
    yield "stg_votes", v.select("matter_file", "action_seq", "action_date", "history_id",
        F.col("v.person_id").alias("person_id"), F.col("v.person_name").alias("person_name"),
        F.col("v.vote_value").alias("vote_value_raw"), *LIN)
    at = r.select(F.col("file_number").alias("matter_file"), *LIN, F.posexplode("attachments").alias("attachment_seq", "x"))
    yield "stg_attachments", at.select("matter_file", "attachment_seq",
        F.col("x.name").alias("attachment_name"), F.col("x.url").alias("attachment_url"), *LIN)
    sp = r.select(F.col("file_number").alias("matter_file"), *LIN, F.posexplode("sponsors").alias("sponsor_pos", "sponsor_name"))
    yield "stg_sponsors", sp.select("matter_file", "sponsor_pos", "sponsor_name", *LIN)

def meeting_tables(r):
    yield "stg_meetings", r.select("meeting_id", "event_guid", "body_name",
        F.col("meeting_date").alias("meeting_date_raw"), _d("meeting_date").alias("meeting_date"),
        "meeting_time", "location", "meeting_subtype", "agenda_status", "minutes_status",
        "agenda_url", "minutes_url", "video_clip_id",
        F.size(F.coalesce("agenda_items", F.array())).alias("n_agenda_items"),
        F.size(F.coalesce("documents", F.array())).alias("n_documents"), *LIN)
    ai = r.select("meeting_id", _d("meeting_date").alias("meeting_date"), *LIN, F.explode("agenda_items").alias("it"))
    yield "stg_agenda_items", ai.select("meeting_id", "meeting_date",
        F.col("it.item_seq").alias("item_seq"), F.col("it.matter_file").alias("matter_file"),
        F.col("it.history_id").alias("history_id"), F.col("it.agenda_number").alias("agenda_number"),
        F.col("it.matter_name").alias("matter_name"), F.col("it.matter_type").alias("matter_type"),
        F.col("it.matter_status").alias("matter_status_at_meeting"), F.col("it.title").alias("title"),
        F.col("it.action_raw").alias("action_raw"), F.col("it.action_result").alias("action_result"),
        F.col("it.matter_url").alias("matter_url"), F.col("it.history_url").alias("history_url"), *LIN)
    md = r.select("meeting_id", *LIN, F.posexplode("documents").alias("document_seq", "doc"))
    yield "stg_meeting_documents", md.select("meeting_id", "document_seq",
        F.col("doc.document_source").alias("document_source"), F.col("doc.document_title").alias("document_title"),
        F.col("doc.document_url").alias("document_url"), F.col("doc.body_text").alias("body_text"), *LIN)

# COMMAND ----------

# ---------- the Auto Loader runner ----------
def add_lineage(df):
    return (df.withColumn("source_file", F.col("_metadata.file_path"))
              .withColumn("ingest_date", F.to_date(F.regexp_extract("source_file", r"ingest_date=(\d{4}-\d{2}-\d{2})", 1)))
              .withColumn("loaded_at", F.current_timestamp()))

def run_autoloader(name, subdir, schema, table_fn):
    stream = (spark.readStream.format("cloudFiles")        # <-- the Databricks-only bit
              .option("cloudFiles.format", "json")
              .option("cloudFiles.schemaLocation", f"{CKPT}/{name}/schema")
              .option("multiLine", "true")
              .schema(schema)
              .load(f"{SRC}/{subdir}"))
    raw = add_lineage(stream)

    def process(batch_df, _bid):
        for tbl, df in table_fn(batch_df):
            (df.write.format("delta").mode("append")
               .partitionBy("ingest_date").saveAsTable(f"{CATALOG}.{SCHEMA}.{tbl}"))

    q = (raw.writeStream.foreachBatch(process)
         .option("checkpointLocation", f"{CKPT}/{name}/write")
         .trigger(availableNow=True).start())
    q.awaitTermination()
    n = sum(p.get("numInputRows", 0) for p in q.recentProgress)
    print(f"{name}: processed {n} new files this run")

# COMMAND ----------

# MAGIC %md
# MAGIC ### (one-time) reset — run ONLY when switching from the batch notebooks to Auto Loader
# MAGIC Drops the staging tables and checkpoints so Auto Loader starts as the clean owner.
# MAGIC Skip this on normal runs.

# COMMAND ----------

# for t in ["stg_matters","stg_actions","stg_votes","stg_attachments","stg_sponsors",
#           "stg_meetings","stg_agenda_items","stg_meeting_documents"]:
#     spark.sql(f"DROP TABLE IF EXISTS {CATALOG}.{SCHEMA}.{t}")
# dbutils.fs.rm(CKPT, recurse=True)

# COMMAND ----------

# ---------- run both sources ----------
run_autoloader("matters",  "matters",  MATTER,  matter_tables)
run_autoloader("meetings", "meetings", MEETING, meeting_tables)

# COMMAND ----------

# Re-run the cell above and you'll see "processed 0 new files" — the checkpoint remembered.
for t in ["stg_matters","stg_votes","stg_meetings","stg_agenda_items"]:
    print(f"{t:<20}", spark.table(f"{CATALOG}.{SCHEMA}.{t}").count())
