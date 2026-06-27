# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — dim_matter (accumulating snapshot)
# MAGIC One row per matter: lifecycle, final_disposition, and milestone dates derived from the
# MAGIC silver staging tables. Run after the matter + meeting silver notebooks.

# COMMAND ----------

CATALOG, SILVER, GOLD = "workspace", "silver", "gold"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{GOLD}")

# COMMAND ----------

from pyspark.sql import functions as F

# status (lower-cased) -> final_disposition. Terminal = an outcome the dashboard reports.
# Anything not listed becomes 'UNMAPPED' so a NEW status fails loud instead of hiding as "other".
TERMINAL = {
    "passed": "passed", "approved": "passed", "adopted": "passed",
    "finally passed": "passed", "ordinance enacted": "passed", "mayor approved": "passed",
    "filed": "filed", "killed": "killed",
}
IN_PROGRESS = {
    "30 day rule", "consent agenda", "first reading", "first reading, consent",
    "mayors office", "new business", "pending committee action",
    "scheduled for committee hearing", "unfinished business-final passage",
    "pending board action", "assigned", "continued", "special order", "in committee",
}

def disposition_expr():
    c = F.lower(F.trim(F.col("status")))
    e = F.when(c.isNull(), F.lit("in_progress"))
    for k, v in TERMINAL.items():
        e = e.when(c == k, F.lit(v))
    e = e.when(c.isin(list(IN_PROGRESS)), F.lit("in_progress"))
    return e.otherwise(F.lit("UNMAPPED"))

def lifecycle_expr():
    d = F.col("final_disposition")
    return (F.when(d == "passed", F.lit("passed"))
             .when(d.isin("filed", "killed"), F.lit("terminal_other"))
             .when(d == "in_progress", F.lit("in_progress"))
             .otherwise(F.lit("UNMAPPED")))

def build_dim_matter(matters, actions):
    first_cmte = (actions.filter(F.lower(F.col("body")).like("%committee%"))
                  .groupBy("matter_file").agg(F.min("action_date").alias("first_committee_date")))
    m = (matters
         .withColumn("matter_id", F.regexp_extract("detail_url", r"[?&]ID=(\d+)", 1))
         .withColumn("final_disposition", disposition_expr())
         .withColumn("lifecycle", lifecycle_expr()))
    return (m.join(first_cmte, "matter_file", "left")
            .withColumn("matter_sk", F.xxhash64("matter_file"))   # stable across runs
            .select("matter_sk", "matter_file", "matter_id",
                F.col("name").alias("matter_name"), F.col("type").alias("matter_type"),
                F.col("title").alias("matter_title"), "in_control",
                "status", "lifecycle", "final_disposition",
                "introduced_date", "first_committee_date", "final_action_date",
                "enactment_date", "enactment_number"))

# COMMAND ----------

matters = spark.table(f"{CATALOG}.{SILVER}.stg_matters")
actions = spark.table(f"{CATALOG}.{SILVER}.stg_actions")
dim = build_dim_matter(matters, actions)

(dim.write.format("delta").mode("overwrite")
    .saveAsTable(f"{CATALOG}.{GOLD}.dim_matter"))
print("dim_matter rows:", dim.count())   # expect 55

# COMMAND ----------

# MAGIC %md ## Validate

# COMMAND ----------

print("UNMAPPED (must be 0):",
      dim.filter((F.col("final_disposition") == "UNMAPPED") | (F.col("lifecycle") == "UNMAPPED")).count())
display(dim.groupBy("final_disposition").count().orderBy(F.desc("count")))
display(dim.groupBy("matter_type").agg(
    F.count("*").alias("n"),
    F.sum(F.col("first_committee_date").isNotNull().cast("int")).alias("has_cmte"),
    F.sum(F.col("final_action_date").isNotNull().cast("int")).alias("has_final"),
    F.sum(F.col("enactment_date").isNotNull().cast("int")).alias("has_enact")))
display(dim.filter(F.col("final_disposition") != "in_progress")
           .select("matter_file", "matter_type", "status", "final_disposition",
                   "first_committee_date", "final_action_date", "enactment_date", "enactment_number"))
