# Architecture Diagrams

Open this file in VS Code's Markdown Preview (`Cmd+Shift+V`) to render the diagrams.
GitHub also renders Mermaid natively.

---

## 1. Pipeline — data flow (Bronze → Silver → Gold)

```mermaid
flowchart TD
    SRC["🌐 sfgov.legistar.com\nLegistar website"]

    SCRAPER["legistar_scrape.py\nPlaywright search → requests/bs4 detail scrape\nrate-limited · deterministic · no LLM"]

    subgraph BRONZE ["☁️  BRONZE — Raw landing zone (immutable, append-only)"]
        RAW["raw/matters/ingest_date=YYYY-MM-DD/\n&lt;file_number&gt;.json\none file per matter per run"]
    end

    subgraph SILVER ["🔄  SILVER — Staging (1-to-1 with JSON, typed)"]
        direction LR
        STG_M["stg_matters\nfile_number · status · title\nintroduced_raw · in_control · lifecycle"]
        STG_A["stg_actions\nmatter_id · action_seq\nbody · action · result · history_url"]
        STG_V["stg_votes\nmatter_id · action_seq\nperson_name · vote_value"]
        STG_AT["stg_attachments\nmatter_id · attachment_seq\ndocument_id · url"]
        STG_SP["stg_sponsors\nmatter_id · sponsor_pos\nsponsor_name"]
    end

    subgraph GOLD ["⭐  GOLD — Star schema (what the dashboard queries)"]
        direction TB

        subgraph DIMS ["Dimensions"]
            direction LR
            DIM_C["dim_committee\nseeded from bodies.json\n(stable reference data)"]
            DIM_P["dim_person\nSCD type 2\nseeded from persons.json"]
            DIM_M["dim_matter\nSCD type 2\n+ status / lifecycle fields"]
            DIM_D["dim_document"]
            DIM_S["dim_subject\n⚠️ data source TBD"]
            DIM_MTG["dim_meeting\n👥 teammate's slice"]
        end

        subgraph FACTS ["Facts"]
            direction LR
            FACT_A["fact_matter_action\n(committee hearings,\nboard votes, referrals)"]
            FACT_V["fact_vote\n(per-member roll call)"]
        end

        subgraph BRIDGES ["Bridges  (many-to-many)"]
            direction LR
            BR_SP["bridge_matter_sponsor"]
            BR_D["bridge_matter_document"]
            BR_S["bridge_matter_subject"]
        end
    end

    DASH["📊 Streamlit dashboard\nvoting records · keyword search · weekly diff"]

    SRC -->|"weekly scrape:\nnew matters + re-scrape open matters"| SCRAPER
    SCRAPER --> RAW

    RAW -->|"loader\n(next increment)"| STG_M & STG_A & STG_V & STG_AT & STG_SP

    STG_M -->|"dedupe latest,\nSCD type 2 upsert"| DIM_M
    STG_A -->|"resolve body name\n→ committee_sk"| FACT_A
    STG_V -->|"resolve person_name\n→ person_sk"| FACT_V
    STG_AT --> DIM_D
    STG_SP -->|"pos 0=primary\npos 1+=co"| BR_SP

    GOLD --> DASH
```

---

## 2. Star schema — entity-relationship diagram

Tables with a **†** are owned by the legislation slice (Lynn).
Tables with a **‡** are owned by the meeting slice (teammate).
`meeting_sk` on both fact tables is **nullable** — resolved when meeting data is present.

> `dim_matter` and `dim_person` use **SCD type 2**: when something changes (e.g. a bill's
> status or a supervisor's district), the old row is closed (`effective_to`, `is_current=false`)
> and a new row is inserted. Query current state with `WHERE is_current = true`.

```mermaid
erDiagram

    dim_committee["dim_committee †"] {
        int committee_sk PK
        int committee_id "BodyId from bodies.json"
        text committee_name
        boolean is_active
    }

    dim_person["dim_person †"] {
        int person_sk PK
        int person_id
        text full_name
        int district
        date effective_from "SCD type 2"
        date effective_to   "NULL = open"
        boolean is_current
    }

    dim_matter["dim_matter †"] {
        int matter_sk PK
        int matter_id "natural key (Legistar ID)"
        text matter_file "human-facing (260439)"
        text matter_name
        text matter_type
        text status "PROPOSED ADDITION"
        text lifecycle "passed|in_works|other"
        int controlling_committee_sk FK
        date effective_from "SCD type 2"
        date effective_to   "NULL = open"
        boolean is_current
    }

    dim_document["dim_document †"] {
        int document_sk PK
        int document_id "from View.ashx ID= param"
        text document_title
        text document_type
        text body_text "PDF text if scraped"
    }

    dim_subject["dim_subject † ⚠️"] {
        int subject_sk PK
        int subject_id
        text subject_name "data source TBD"
    }

    dim_meeting["dim_meeting ‡"] {
        int meeting_sk PK
        int meeting_id
        int committee_sk FK
        date meeting_date
        text agenda_url
    }

    fact_matter_action["fact_matter_action †"] {
        int matter_action_sk PK
        int matter_sk FK
        int meeting_sk FK "nullable"
        text action_type_code "e.g. RECOMMENDED"
        date action_date
        text action_result "PROPOSED: Pass|Fail"
    }

    fact_vote["fact_vote †"] {
        int vote_sk PK
        int matter_sk FK
        int meeting_sk FK "nullable"
        int person_sk FK
        date vote_date
        text vote_value "Aye|No|Absent|Excused|Recused"
    }

    bridge_matter_sponsor["bridge_matter_sponsor †"] {
        int matter_sk FK
        int person_sk FK
        text sponsor_type "primary | co"
    }

    bridge_matter_document["bridge_matter_document †"] {
        int matter_sk FK
        int document_sk FK
    }

    bridge_matter_subject["bridge_matter_subject †"] {
        int matter_sk FK
        int subject_sk FK
    }

    dim_matter  }o--||  dim_committee        : "controlled by"
    dim_meeting }o--||  dim_committee        : "held by"

    fact_matter_action }|--||  dim_matter    : ""
    fact_matter_action }o--o|  dim_meeting   : "nullable"

    fact_vote  }|--||  dim_matter            : ""
    fact_vote  }o--o|  dim_meeting           : "nullable"
    fact_vote  }|--||  dim_person            : ""

    bridge_matter_sponsor  }|--||  dim_matter  : ""
    bridge_matter_sponsor  }|--||  dim_person  : ""

    bridge_matter_document }|--||  dim_matter  : ""
    bridge_matter_document }|--||  dim_document : ""

    bridge_matter_subject  }|--||  dim_matter  : ""
    bridge_matter_subject  }|--||  dim_subject : ""
```
