# Databricks notebook source
# This file can be imported into Databricks as a notebook:
#   Workspace > Import > File > select this .py
#
# Prerequisites:
#   Upload Parquet files from warehouse/exports/ to a Unity Catalog Volume:
#
# Produce the Parquet first with:  python warehouse/export_parquet.py
#
#   Option A — Databricks UI (no CLI needed): in the CATALOG set below
#     Catalog (left sidebar) > <CATALOG> > Create Schema "legislation"
#     > Volumes tab > Create Volume "parquet"
#     > Upload to Volume > drag warehouse/exports/*.parquet
#
#   Option B — Databricks CLI:
#     pip install databricks-cli
#     databricks configure --token   # host = your workspace URL, token = PAT from Settings
#     databricks fs cp warehouse/exports/ /Volumes/<CATALOG>/legislation/parquet/ --recursive
#
# After running, query with (dim_matter is flat; status lives in facts):
#   SELECT * FROM <CATALOG>.legislation.dim_matter LIMIT 20

# COMMAND ----------

# One catalog used consistently for the volume path AND table creation. Adjust to your workspace.
CATALOG    = "workspace"
SCHEMA     = "legislation"
VOLUME     = "parquet"
PARQUET_DIR = f"/Volumes/{CATALOG}/{SCHEMA}/{VOLUME}"

# The full milestone-3 gold star (matches warehouse/export_parquet.py).
TABLES = [
    "dim_committee", "dim_person", "dim_matter", "dim_subject", "dim_document",
    "dim_meeting", "dim_action_type",
    "fact_matter_action", "fact_vote", "fact_committee_membership",
    "bridge_matter_subject", "bridge_matter_sponsor", "bridge_matter_document",
    "bridge_meeting_document",
]

# COMMAND ----------

# Create the schema in the chosen catalog (idempotent)
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")
spark.sql(f"USE {CATALOG}.{SCHEMA}")
print(f"Using schema: {CATALOG}.{SCHEMA}")

# COMMAND ----------

# Load each Parquet file as a Delta table (overwrite on re-run = idempotent)
for table in TABLES:
    path = f"{PARQUET_DIR}/{table}.parquet"
    df = spark.read.parquet(path)
    (df.write
       .format("delta")
       .mode("overwrite")
       .option("overwriteSchema", "true")
       .saveAsTable(f"{CATALOG}.{SCHEMA}.{table}"))
    print(f"  {table:<30} {df.count():>5} rows")

# COMMAND ----------

# Verify: list all tables in the schema
spark.sql(f"SHOW TABLES IN {CATALOG}.{SCHEMA}").show()

# COMMAND ----------

# Quick sanity check: matters with their LATEST action (status is derived from facts in the
# milestone-3 flat dim_matter) and primary sponsor.
spark.sql("""
    SELECT m.matter_file,
           m.matter_type,
           fa.action_type_code AS latest_action,
           p.full_name         AS primary_sponsor
    FROM   dim_matter m
    LEFT JOIN bridge_matter_sponsor s ON s.matter_sk = m.matter_sk AND s.sponsor_type = 'Primary'
    LEFT JOIN dim_person p            ON p.person_sk  = s.person_sk
    LEFT JOIN (
        SELECT matter_sk, action_type_code,
               ROW_NUMBER() OVER (PARTITION BY matter_sk
                                  ORDER BY action_date DESC NULLS LAST) AS rn
        FROM fact_matter_action
    ) fa ON fa.matter_sk = m.matter_sk AND fa.rn = 1
    ORDER  BY m.matter_file DESC
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
