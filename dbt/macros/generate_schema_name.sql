{#
  Decides which schema each model is built into.

  ---------------------------------------------------------------------------
  THE PROBLEM THIS SOLVES

  Out of the box, dbt glues the two names together: a model asking for schema
  "silver" lands in "<your-default-schema>_silver". That surprises people, so the
  original version of this macro used the model's schema name EXACTLY — giving
  plain `silver` and `gold`, matching the old notebooks.

  But that also threw away the thing the gluing was FOR: keeping each developer
  out of production. With exact names, `dbt run` from anyone's laptop targeted the
  real `silver` and `gold` — not a copy, not a sandbox. Nothing stood between a
  local run and the live warehouse except the fact that teammates happened to lack
  CREATE permission. That is protection by accident, not by design.

  ---------------------------------------------------------------------------
  HOW IT WORKS NOW

  Sandboxing is the DEFAULT, and production is opt-in:

    default (any developer)   ->  <target.schema>   e.g. everything in dev_lynn
    with prod_schemas: true   ->  <name>            e.g. silver / gold

  In development every model lands in the ONE schema you own, rather than in
  per-layer copies (dev_lynn_silver, dev_lynn_gold, ...). That means a developer
  needs no CREATE SCHEMA privilege on the catalog — owning their own schema is
  enough. Model names are already unique across layers (stg_*, int_*, dim_*,
  fact_*, bridge_*), so nothing collides.

  So the dangerous behaviour now requires someone to ask for it in writing. A
  forgotten flag builds a harmless sandbox; it can no longer overwrite production.

  Only the weekly Databricks Job passes the flag — see the dbt_task in
  databricks.yml, which runs:

      dbt build --vars '{prod_schemas: true}'

  We key on an explicit variable rather than `target.name` on purpose: the Job's
  dbt task uses a profiles.yml that DATABRICKS generates, so its target name is
  not ours to control or verify. A variable we set in databricks.yml is something
  we can read back off the deployed job.

  To build production from a laptop (rare — prefer running the Job), be explicit:

      dbt build --vars '{prod_schemas: true}'

  The comparison against a list, rather than a bare truthiness check, is
  deliberate: `--vars '{prod_schemas: "false"}'` passes the STRING "false", which
  Jinja considers true. That would silently point a dev run at production — the
  exact failure this macro exists to prevent.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}

    {%- if custom_schema_name is none -%}
        {#- model didn't name a schema: use the connection's default as-is -#}
        {{ target.schema }}

    {%- elif var('prod_schemas', false) in [true, 'true', 'True', 'TRUE'] -%}
        {#- production: exact names, e.g. `silver` / `gold` -#}
        {{ custom_schema_name | trim }}

    {%- else -%}
        {#- development: everything into the developer's own schema, e.g. `dev_lynn` -#}
        {{ target.schema }}

    {%- endif -%}

{%- endmacro %}
