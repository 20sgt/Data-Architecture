# Databricks notebook source
# MAGIC %md
# MAGIC # Gold — incremental build with MERGE
# MAGIC Replaces the overwrite gold notebooks. Three things make it incremental-safe:
# MAGIC 1. **Latest-wins dedup** — collapse silver to each matter's/meeting's most recent scrape.
# MAGIC 2. **Stable keys** — facts key on `history_id`, not array position (which shifts on re-scrape).
# MAGIC 3. **MERGE** — `dim_matter` updates in place when a bill changes; facts/dims/bridges insert-only.

# COMMAND ----------

CATALOG, SILVER, GOLD = "workspace", "silver", "gold"
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{GOLD}")
from pyspark.sql import functions as F, Window
from delta.tables import DeltaTable

def sk(*cols): return F.xxhash64(F.concat_ws("|", *[F.col(c) for c in cols]))
stg = lambda n: spark.table(f"{CATALOG}.{SILVER}.{n}")

# COMMAND ----------

# ---------- 1. LATEST-WINS DEDUP ----------
# Keep each matter's most recent scrape; tie its child rows to that same scrape via (matter_file, ingest_date).
wm = Window.partitionBy("matter_file").orderBy(F.col("ingest_date").desc())
latest_matters = stg("stg_matters").withColumn("_rn", F.row_number().over(wm)).filter("_rn=1").drop("_rn")
mkey = latest_matters.select("matter_file", "ingest_date")

actions  = stg("stg_actions").join(mkey,  ["matter_file", "ingest_date"], "inner")
votes    = stg("stg_votes").join(mkey,    ["matter_file", "ingest_date"], "inner")
sponsors = stg("stg_sponsors").join(mkey, ["matter_file", "ingest_date"], "inner")
attach   = stg("stg_attachments").join(mkey, ["matter_file", "ingest_date"], "inner")
matters  = latest_matters

we = Window.partitionBy("meeting_id").orderBy(F.col("ingest_date").desc())
latest_meetings = stg("stg_meetings").withColumn("_rn", F.row_number().over(we)).filter("_rn=1").drop("_rn")
ekey = latest_meetings.select("meeting_id", "ingest_date")
agenda   = stg("stg_agenda_items").join(ekey, ["meeting_id", "ingest_date"], "inner")
meetings = latest_meetings

# COMMAND ----------

# ---------- 2. BUILD GOLD (from the deduped 'latest' silver) ----------
TERMINAL = {"passed": "passed", "approved": "passed", "adopted": "passed", "finally passed": "passed",
            "ordinance enacted": "passed", "mayor approved": "passed", "filed": "filed", "killed": "killed"}
IN_PROGRESS = {"30 day rule", "consent agenda", "first reading", "first reading, consent", "mayors office",
               "new business", "pending committee action", "scheduled for committee hearing",
               "unfinished business-final passage", "pending board action", "assigned", "continued",
               "special order", "in committee"}

def disposition():
    c = F.lower(F.trim(F.col("status"))); e = F.when(c.isNull(), F.lit("in_progress"))
    for k, v in TERMINAL.items(): e = e.when(c == k, F.lit(v))
    return e.when(c.isin(list(IN_PROGRESS)), F.lit("in_progress")).otherwise(F.lit("UNMAPPED"))

first_cmte = (actions.filter(F.lower("body").like("%committee%")).groupBy("matter_file")
              .agg(F.min("action_date").alias("first_committee_date")))
dim_matter = (matters.withColumn("matter_id", F.regexp_extract("detail_url", r"[?&]ID=(\d+)", 1))
    .withColumn("final_disposition", disposition())
    .withColumn("lifecycle", F.when(F.col("final_disposition") == "passed", "passed")
        .when(F.col("final_disposition").isin("filed", "killed"), "terminal_other")
        .when(F.col("final_disposition") == "in_progress", "in_progress").otherwise("UNMAPPED"))
    .join(first_cmte, "matter_file", "left").withColumn("matter_sk", sk("matter_file"))
    .select("matter_sk", "matter_file", "matter_id", F.col("name").alias("matter_name"),
            F.col("type").alias("matter_type"), F.col("title").alias("matter_title"), "in_control",
            "status", "lifecycle", "final_disposition", "introduced_date", "first_committee_date",
            "final_action_date", "enactment_date", "enactment_number"))

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

dim_document = (attach.select(F.col("attachment_url").alias("document_url"), F.col("attachment_name").alias("document_title"))
    .filter(F.col("document_url").isNotNull()).distinct()
    .withColumn("document_id", F.regexp_extract("document_url", r"[?&]ID=(\d+)", 1))
    .withColumn("document_sk", sk("document_url"))
    .select("document_sk", "document_id", "document_title", "document_url"))

dim_meeting = meetings.select(sk("meeting_id").alias("meeting_sk"), "meeting_id",
    sk("body_name").alias("committee_sk"), "body_name", "meeting_date", "meeting_time",
    "meeting_subtype", "agenda_status", "agenda_url")

h2m = (agenda.filter(F.col("history_id").isNotNull()).select("history_id", "meeting_id").distinct()
       .groupBy("history_id").agg(F.first("meeting_id").alias("meeting_id"))
       .withColumn("meeting_sk", sk("meeting_id")).select("history_id", "meeting_sk"))

# STABLE action key: history_id, falling back to matter+date+type for the ~11% without one
_akey = F.coalesce(F.col("history_id"), F.concat_ws("|", "matter_file", F.col("action_date").cast("string"), "action_type"))
fact_matter_action = (actions.withColumn("matter_action_sk", F.xxhash64(_akey))
    .withColumn("matter_sk", sk("matter_file")).withColumn("committee_sk", sk("body"))
    .join(h2m, "history_id", "left")
    .select("matter_action_sk", "matter_sk", "committee_sk", "meeting_sk",
            F.col("action_type").alias("action_type_code"), "action_date", "action_result", "history_id")
    .dropDuplicates(["matter_action_sk"]))

_body = actions.select("matter_file", "action_seq", "body")
fact_vote = (votes.join(_body, ["matter_file", "action_seq"], "left")
    .withColumn("vote_sk", sk("history_id", "person_id"))      # STABLE: history_id + person_id
    .withColumn("matter_sk", sk("matter_file")).withColumn("person_sk", sk("person_id"))
    .withColumn("committee_sk", sk("body")).join(h2m, "history_id", "left")
    .select("vote_sk", "matter_sk", "person_sk", "committee_sk", "meeting_sk",
            F.col("action_date").alias("vote_date"), F.col("vote_value_raw").alias("vote_value"), "history_id")
    .dropDuplicates(["vote_sk"]))

_name2sk = dim_person.select(F.col("full_name").alias("sponsor_name"), "person_sk").distinct()
bridge_matter_sponsor = (sponsors.withColumn("matter_sk", sk("matter_file"))
    .withColumn("sponsor_type", F.when(F.col("sponsor_pos") == 0, "primary").otherwise("co"))
    .join(_name2sk, "sponsor_name", "left").select("matter_sk", "person_sk", "sponsor_type")
    .dropDuplicates(["matter_sk", "person_sk"]))
bridge_matter_document = (attach.select("matter_file", F.col("attachment_url").alias("document_url"))
    .filter(F.col("document_url").isNotNull()).withColumn("matter_sk", sk("matter_file"))
    .withColumn("document_sk", sk("document_url")).select("matter_sk", "document_sk")
    .dropDuplicates(["matter_sk", "document_sk"]))

# COMMAND ----------

# ---------- 3. MERGE (upsert) ----------
def upsert(df, table, keys, update):
    full = f"{CATALOG}.{GOLD}.{table}"
    src = df.dropDuplicates(keys)                      # MERGE needs a key-unique source
    if not spark.catalog.tableExists(full):
        src.write.format("delta").saveAsTable(full); print(f"  created {table:<22} {src.count()} rows"); return
    cond = " AND ".join(f"t.{k}=s.{k}" for k in keys)
    m = DeltaTable.forName(spark, full).alias("t").merge(src.alias("s"), cond)
    m = m.whenMatchedUpdateAll() if update else m
    m.whenNotMatchedInsertAll().execute()
    print(f"  merged  {table:<22} -> now {spark.table(full).count()} rows")

# dim_matter UPDATES in place (a bill's status/dates change); everything else is insert-only.
upsert(dim_matter,             "dim_matter",            ["matter_sk"], update=True)
upsert(dim_person,             "dim_person",            ["person_sk"], update=False)
upsert(dim_committee,          "dim_committee",         ["committee_sk"], update=False)
upsert(dim_document,           "dim_document",          ["document_sk"], update=False)
upsert(dim_meeting,            "dim_meeting",           ["meeting_sk"], update=False)
upsert(fact_matter_action,     "fact_matter_action",    ["matter_action_sk"], update=False)
upsert(fact_vote,              "fact_vote",             ["vote_sk"], update=False)
upsert(bridge_matter_sponsor,  "bridge_matter_sponsor", ["matter_sk", "person_sk"], update=False)
upsert(bridge_matter_document, "bridge_matter_document",["matter_sk", "document_sk"], update=False)

# COMMAND ----------

# Integrity — all zeros expected
dmat = spark.table(f"{CATALOG}.{GOLD}.dim_matter")
print("fact_vote orphans :", fact_vote.join(dmat, "matter_sk", "left_anti").count())
print("UNMAPPED dispositions:", dmat.filter(F.col("final_disposition") == "UNMAPPED").count())
print("dim_matter rows:", dmat.count(), "| fact_vote rows:", spark.table(f"{CATALOG}.{GOLD}.fact_vote").count())
