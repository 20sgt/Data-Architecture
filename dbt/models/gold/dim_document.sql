-- One row per distinct matter attachment (document). document_id is pulled out of
-- the URL's "?ID=12345" query parameter with a regex.
--
-- Note the doubled backslash in '\\d': Spark SQL treats '\' as an escape inside a
-- string literal, so '\\d' is how we write the regex token \d (a digit).
with docs as (
    select distinct
        attachment_url  as document_url,
        attachment_name as document_title
    from {{ ref('int_attachments') }}
    where attachment_url is not null
)
select
    {{ sk('document_url') }}                         as document_sk,
    regexp_extract(document_url, '[?&]ID=(\\d+)', 1) as document_id,
    document_title,
    document_url
from docs
