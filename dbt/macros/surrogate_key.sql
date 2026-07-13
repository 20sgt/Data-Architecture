{#
  A "surrogate key" is just a single ID column we make up for each row, so every
  table has one clean column to join on. We build it by hashing one or more of
  the row's natural columns together. Hashing the same inputs always gives the
  same number, which is what lets us re-run the pipeline safely — a row keeps its
  ID forever.

  This copies the exact formula the old Python pipeline used:
        xxhash64( concat_ws("|", col1, col2, ...) )
  ("xxhash64" is just a fast hashing function built into Databricks; concat_ws
  glues the columns together with a "|" between them.)

  Keeping the SAME formula is deliberate: it makes the dbt tables come out with
  the exact same IDs as the old notebooks, so we can compare the two row-for-row
  and prove the rewrite is correct.

  Use it inside a model like this:
        {{ sk('matter_file') }}                as matter_sk
        {{ sk('history_id', 'person_id') }}    as vote_sk
#}
{% macro sk(*columns) -%}
    xxhash64(concat_ws('|', {{ columns | join(', ') }}))
{%- endmacro %}
