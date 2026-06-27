# Databricks notebook source
# MAGIC %md
# MAGIC # Silver loader — raw matter JSON → 5 typed staging tables
# MAGIC Run the cells top to bottom. The only thing you must edit is `REPO_PATH` in the next cell.
# MAGIC When it finishes you'll have 5 Delta tables under the `silver` schema.

# COMMAND ----------

# ===== EDIT THIS =====
# Path to your Git folder in the Databricks workspace.
# Find it: in the left sidebar file browser, click the ⋮ next to your repo folder → "Copy path".
# It usually looks like: /Workspace/Users/<your-email>/Data-Architecture
REPO_PATH = "/Workspace/Users/CHANGE_ME/Data-Architecture"

# These three are fine to leave as-is. CATALOG must be a catalog you can write to;
# "workspace" is the default on Free Edition. Check the catalog dropdown in Catalog Explorer
# if you're unsure, or run:  display(spark.sql("SHOW CATALOGS"))
CATALOG = "workspace"
SCHEMA  = "silver"
VOLUME  = "raw"

# COMMAND ----------

# Create the schema + a Volume (a Volume is just a folder Databricks can store files in),
# then copy the repo's sample JSON into the Volume so Spark can read it reliably.
import shutil, glob

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"CREATE VOLUME  IF NOT EXISTS {CATALOG}.{SCHEMA}.{VOLUME}")

VOL = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"          # the Volume's file path
shutil.copytree(f"{REPO_PATH}/sample/matters", f"{VOL}/matters", dirs_exist_ok=True)

RAW_GLOB = f"{VOL}/matters/ingest_date=*/*.json"
print("copied; json files found:", len(glob.glob(RAW_GLOB)))   # expect 91

# COMMAND ----------

# Explicit schema — we declare the JSON shape instead of letting Spark guess it.
from pyspark.sql.types import StructType, StructField, StringType, ArrayType

VOTE = StructType([
    StructField("person_id", StringType()), StructField("person_name", StringType()),
    StructField("vote_value", StringType()),
])
ACTION = StructType([
    StructField("date", StringType()), StructField("body", StringType()),
    StructField("action", StringType()), StructField("result", StringType()),
    StructField("history_id", StringType()), StructField("history_url", StringType()),
    StructField("votes", ArrayType(VOTE)),
])
ATTACHMENT = StructType([StructField("name", StringType()), StructField("url", StringType())])
MATTER = StructType([
    StructField("file_number", StringType()), StructField("detail_url", StringType()),
    StructField("name", StringType()), StructField("title", StringType()),
    StructField("type", StringType()), StructField("status", StringType()),
    StructField("introduced", StringType()), StructField("on_agenda", StringType()),
    StructField("final_action", StringType()), StructField("enactment_date", StringType()),
    StructField("enactment_number", StringType()), StructField("in_control", StringType()),
    StructField("sponsors", ArrayType(StringType())), StructField("related_files", ArrayType(StringType())),
    StructField("attachments", ArrayType(ATTACHMENT)), StructField("actions", ArrayType(ACTION)),
    StructField("full_text", StringType()),
])

# COMMAND ----------

# Read + flatten. Same logic as the local loader — just using the notebook's built-in `spark`.
from pyspark.sql import functions as F

_DATE_FMT = "M/d/yyyy"
def _d(c): return F.to_date(F.col(c), _DATE_FMT)
_LIN = ["ingest_date", "source_file", "loaded_at"]

def read_raw(glob_path):
    df = spark.read.schema(MATTER).option("multiLine", True).json(glob_path)
    return (df
        .withColumn("source_file", F.col("_metadata.file_path"))
        .withColumn("ingest_date", F.to_date(
            F.regexp_extract("source_file", r"ingest_date=(\d{4}-\d{2}-\d{2})", 1)))
        .withColumn("loaded_at", F.current_timestamp()))

def stg_matters(r):
    return r.select(
        F.col("file_number").alias("matter_file"), "detail_url", "name", "title", "type", "status",
        F.col("introduced").alias("introduced_raw"), _d("introduced").alias("introduced_date"),
        F.col("on_agenda").alias("on_agenda_raw"), _d("on_agenda").alias("on_agenda_date"),
        F.col("final_action").alias("final_action_raw"), _d("final_action").alias("final_action_date"),
        F.col("enactment_date").alias("enactment_date_raw"), _d("enactment_date").alias("enactment_date"),
        "enactment_number", "in_control", "related_files",
        F.size(F.coalesce("actions", F.array())).alias("n_actions"),
        F.size(F.coalesce("sponsors", F.array())).alias("n_sponsors"), *_LIN)

def stg_actions(r):
    e = r.select(F.col("file_number").alias("matter_file"), *_LIN,
                 F.posexplode("actions").alias("action_seq", "a"))
    return e.select("matter_file", "action_seq",
        F.col("a.date").alias("action_date_raw"), _d("a.date").alias("action_date"),
        F.col("a.body").alias("body"), F.col("a.action").alias("action_type"),
        F.col("a.result").alias("action_result"), F.col("a.history_id").alias("history_id"),
        F.col("a.history_url").alias("history_url"),
        F.size(F.coalesce("a.votes", F.array())).alias("n_votes"), *_LIN)

def stg_votes(r):
    a = r.select(F.col("file_number").alias("matter_file"), *_LIN,
                 F.posexplode("actions").alias("action_seq", "a"))
    v = a.select("matter_file", "action_seq", *_LIN,
                 _d("a.date").alias("action_date"), F.col("a.history_id").alias("history_id"),
                 F.explode("a.votes").alias("v"))
    return v.select("matter_file", "action_seq", "action_date", "history_id",
        F.col("v.person_id").alias("person_id"), F.col("v.person_name").alias("person_name"),
        F.col("v.vote_value").alias("vote_value_raw"), *_LIN)

def stg_attachments(r):
    e = r.select(F.col("file_number").alias("matter_file"), *_LIN,
                 F.posexplode("attachments").alias("attachment_seq", "at"))
    return e.select("matter_file", "attachment_seq",
        F.col("at.name").alias("attachment_name"), F.col("at.url").alias("attachment_url"), *_LIN)

def stg_sponsors(r):
    e = r.select(F.col("file_number").alias("matter_file"), *_LIN,
                 F.posexplode("sponsors").alias("sponsor_pos", "sponsor_name"))
    return e.select("matter_file", "sponsor_pos", "sponsor_name", *_LIN)

BUILDERS = {"stg_matters": stg_matters, "stg_actions": stg_actions, "stg_votes": stg_votes,
            "stg_attachments": stg_attachments, "stg_sponsors": stg_sponsors}

# COMMAND ----------

# Run the load: read once, build each staging table, write it as a Delta table.
raw = read_raw(RAW_GLOB).cache()
print("matters read:", raw.count())

for name, build in BUILDERS.items():
    df = build(raw)
    (df.write.format("delta").mode("overwrite")
       .partitionBy("ingest_date")
       .saveAsTable(f"{CATALOG}.{SCHEMA}.{name}"))
    print(f"  wrote {name:<16} {df.count():>5} rows")

# COMMAND ----------

# Verify — you should see your tables and rows. Try editing the table name to peek at others.
display(spark.sql(f"SHOW TABLES IN {CATALOG}.{SCHEMA}"))
display(spark.sql(f"SELECT * FROM {CATALOG}.{SCHEMA}.stg_votes LIMIT 20"))
