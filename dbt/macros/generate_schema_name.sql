{#
  Small but important fix to a dbt default.

  Out of the box, when a model asks to live in schema "silver", dbt actually
  names the schema "<your-default-schema>_silver" (it glues the two together).
  That surprises almost everyone. This override says instead:

    - if a model names its own schema (e.g. "silver" or "gold"), use that name
      EXACTLY, and
    - if a model doesn't name one, fall back to the default schema from the
      connection.

  Result: our tables land in plain `silver` and `gold`, matching the old
  notebooks.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
