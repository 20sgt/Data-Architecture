# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — full star (dims + facts + bridges)
# MAGIC Run after the two silver notebooks and the dim_matter notebook. Builds the remaining
# MAGIC dimensions, both facts, and the bridges into `workspace.gold`, then checks referential integrity.

# COMMAND ----------

CATALOG, SILVER, GOLD = "workspace", "silver", "gold"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{GOLD}")
from pyspark.sql import functions as F

def sk(*cols):  # deterministic surrogate key — identical every run
    return F.xxhash64(F.concat_ws("|", *[F.col(c) for c in cols]))

# COMMAND ----------

# read silver
stg = lambda n: spark.table(f"{CATALOG}.{SILVER}.{n}")
matters, actions, votes  = stg("stg_matters"), stg("stg_actions"), stg("stg_votes")
sponsors, attach         = stg("stg_sponsors"), stg("stg_attachments")
meetings, agenda         = stg("stg_meetings"), stg("stg_agenda_items")

# history_id -> meeting_sk (cross-slice resolver, one meeting per history_id)
h2m = (agenda.filter(F.col("history_id").isNotNull()).select("history_id", "meeting_id").distinct()
       .groupBy("history_id").agg(F.first("meeting_id").alias("meeting_id"))
       .withColumn("meeting_sk", sk("meeting_id")).select("history_id", "meeting_sk"))

# COMMAND ----------

# ---- dimensions ----
dim_committee = (actions.select(F.col("body").alias("committee_name"))
    .union(meetings.select(F.col("body_name").alias("committee_name")))
    .union(matters.select(F.col("in_control").alias("committee_name")))
    .filter(F.col("committee_name").isNotNull()).distinct()
    .select(sk("committee_name").alias("committee_sk"), "committee_name"))

voters = (votes.select("person_id", F.col("person_name").alias("full_name"))
    .filter(F.col("person_id").isNotNull()).distinct()
    .withColumn("person_sk", sk("person_id")).withColumn("id_source", F.lit("vote")))
sponsor_only = (sponsors.select(F.col("sponsor_name").alias("full_name")).distinct()
    .join(voters.select("full_name").distinct(), "full_name", "left_anti")
    .withColumn("person_id", F.lit(None).cast("string"))
    .withColumn("person_sk", F.xxhash64(F.concat(F.lit("NAME:"), F.col("full_name"))))
    .withColumn("id_source", F.lit("sponsor_only")))
dim_person = voters.unionByName(sponsor_only).select("person_sk", "person_id", "full_name", "id_source")

dim_document = (attach.select(F.col("attachment_url").alias("document_url"),
                              F.col("attachment_name").alias("document_title"))
    .filter(F.col("document_url").isNotNull()).distinct()
    .withColumn("document_id", F.regexp_extract("document_url", r"[?&]ID=(\d+)", 1))
    .withColumn("document_sk", sk("document_url"))
    .select("document_sk", "document_id", "document_title", "document_url"))

dim_meeting = meetings.select(sk("meeting_id").alias("meeting_sk"), "meeting_id",
    sk("body_name").alias("committee_sk"), "body_name", "meeting_date", "meeting_time",
    "meeting_subtype", "agenda_status", "agenda_url")

# ---- facts ----
fact_matter_action = (actions
    .withColumn("matter_action_sk", sk("matter_file", "action_seq"))
    .withColumn("matter_sk", sk("matter_file")).withColumn("committee_sk", sk("body"))
    .join(h2m, "history_id", "left")
    .select("matter_action_sk", "matter_sk", "committee_sk", "meeting_sk",
            F.col("action_type").alias("action_type_code"), "action_date", "action_result", "history_id"))

_body = actions.select("matter_file", "action_seq", "body")
fact_vote = (votes.join(_body, ["matter_file", "action_seq"], "left")
    .withColumn("vote_sk", sk("matter_file", "action_seq", "person_id"))
    .withColumn("matter_sk", sk("matter_file")).withColumn("person_sk", sk("person_id"))
    .withColumn("committee_sk", sk("body")).join(h2m, "history_id", "left")
    .select("vote_sk", "matter_sk", "person_sk", "committee_sk", "meeting_sk",
            F.col("action_date").alias("vote_date"), F.col("vote_value_raw").alias("vote_value"), "history_id"))

# ---- bridges ----
_name2sk = dim_person.select(F.col("full_name").alias("sponsor_name"), "person_sk").distinct()
bridge_matter_sponsor = (sponsors.withColumn("matter_sk", sk("matter_file"))
    .withColumn("sponsor_type", F.when(F.col("sponsor_pos") == 0, "primary").otherwise("co"))
    .join(_name2sk, "sponsor_name", "left")
    .select("matter_sk", "person_sk", "sponsor_type", "sponsor_name"))

bridge_matter_document = (attach.select("matter_file", F.col("attachment_url").alias("document_url"))
    .filter(F.col("document_url").isNotNull()).distinct()
    .withColumn("matter_sk", sk("matter_file")).withColumn("document_sk", sk("document_url"))
    .select("matter_sk", "document_sk"))

# COMMAND ----------

# write everything as Delta tables
tables = {"dim_committee": dim_committee, "dim_person": dim_person, "dim_document": dim_document,
          "dim_meeting": dim_meeting, "fact_matter_action": fact_matter_action, "fact_vote": fact_vote,
          "bridge_matter_sponsor": bridge_matter_sponsor, "bridge_matter_document": bridge_matter_document}
for n, df in tables.items():
    df.write.format("delta").mode("overwrite").saveAsTable(f"{CATALOG}.{GOLD}.{n}")
    print(f"  wrote {n:<24} {df.count():>5} rows")

# COMMAND ----------

# MAGIC %md ## Referential integrity — every line should print 0

# COMMAND ----------

dmat = spark.table(f"{CATALOG}.{GOLD}.dim_matter")
print("fact_vote.matter_sk orphans  :", fact_vote.join(dmat, "matter_sk", "left_anti").count())
print("fact_vote.person_sk orphans  :", fact_vote.join(dim_person, "person_sk", "left_anti").count())
print("fact_action.matter_sk orphans:", fact_matter_action.join(dmat, "matter_sk", "left_anti").count())
print("sponsor bridge unresolved    :", bridge_matter_sponsor.filter(F.col("person_sk").isNull()).count())
print("doc bridge orphans           :", bridge_matter_document.join(dim_document, "document_sk", "left_anti").count())
print("name collisions              :",
      dim_person.filter(F.col("person_id").isNotNull()).groupBy("full_name")
      .agg(F.countDistinct("person_id").alias("ids")).filter(F.col("ids") > 1).count())
print("meeting_sk fill  action:", fact_matter_action.filter(F.col('meeting_sk').isNotNull()).count(), "/", fact_matter_action.count(),
      " vote:", fact_vote.filter(F.col('meeting_sk').isNotNull()).count(), "/", fact_vote.count())
