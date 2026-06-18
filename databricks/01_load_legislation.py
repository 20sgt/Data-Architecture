# Databricks notebook source
# This file can be imported into Databricks as a notebook:
#   Workspace > Import > File > select this .py
#
# Prerequisites:
#   Upload Parquet files from warehouse/exports/ to a Unity Catalog Volume:
#
#   Option A — Databricks UI (no CLI needed):
#     Catalog (left sidebar) > main > Create Schema "legislation"
#     > Volumes tab > Create Volume "parquet"
#     > Upload to Volume > drag warehouse/exports/*.parquet
#
#   Option B — Databricks CLI:
#     pip install databricks-cli
#     databricks configure --token   # host = your workspace URL, token = PAT from Settings
#     databricks fs cp warehouse/exports/ /Volumes/main/legislation/parquet/ --recursive
#
# After running, query with:
#   SELECT * FROM main.legislation.dim_matter WHERE is_current = true LIMIT 20

# COMMAND ----------

# Adjust if your catalog or schema name differs
CATALOG    = "workspace"
SCHEMA     = "legislation"
VOLUME     = "parquet"
PARQUET_DIR = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"

TABLES = [
    "dim_committee",
    "dim_person",
    "dim_matter",
    "dim_document",
    "fact_vote",
    "fact_matter_action",
    "bridge_matter_sponsor",
    "bridge_matter_document",
]

# COMMAND ----------

# Create the database (idempotent)
spark.sql("CREATE DATABASE IF NOT EXISTS legislation")
spark.sql("USE legislation")
print("Using database: legislation")

# COMMAND ----------

# Load each Parquet file as a Delta table (overwrite on re-run = idempotent)
for table in TABLES:
    path = f"{PARQUET_DIR}/{table}.parquet"
    df = spark.read.parquet(path)
    (df.write
       .format("delta")
       .mode("overwrite")
       .option("overwriteSchema", "true")
       .saveAsTable(table))
    print(f"  {table:<30} {df.count():>5} rows")

# COMMAND ----------

# Verify: list all tables in the legislation database
spark.sql("SHOW TABLES IN legislation").show()

# COMMAND ----------

# Quick sanity check: current matters with their status
spark.sql("""
    SELECT m.matter_file,
           m.matter_type,
           m.status,
           m.lifecycle,
           p.full_name   AS primary_sponsor,
           m.effective_from
    FROM   dim_matter m
    LEFT JOIN bridge_matter_sponsor s ON s.matter_sk = m.matter_sk AND s.sponsor_type = 'primary'
    LEFT JOIN dim_person p            ON p.person_sk  = s.person_sk
    WHERE  m.is_current = true
    ORDER  BY m.effective_from DESC
    LIMIT  20
""").show(truncate=False)

# COMMAND ----------

# Vote rollup by supervisor (use case 1: voting records by member)
spark.sql("""
    SELECT p.full_name,
           v.vote_value,
           COUNT(*) AS n
    FROM   fact_vote  v
    JOIN   dim_person p ON p.person_sk = v.person_sk
    GROUP  BY p.full_name, v.vote_value
    ORDER  BY p.full_name, v.vote_value
""").show(50, truncate=False)
