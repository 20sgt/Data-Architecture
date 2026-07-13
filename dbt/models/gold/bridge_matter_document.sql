-- Links each matter to its attached documents. matter_sk and document_sk use the
-- same sk() formulas as dim_matter and dim_document, so both sides join cleanly.
with rows as (
    select
        {{ sk('matter_file') }}    as matter_sk,
        {{ sk('attachment_url') }} as document_sk
    from {{ ref('int_attachments') }}
    where attachment_url is not null
)

select *
from rows
-- one row per (matter, document)
qualify row_number() over (partition by matter_sk, document_sk order by matter_sk) = 1
