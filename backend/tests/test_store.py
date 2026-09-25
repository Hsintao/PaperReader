"""Names a document's products are published under."""

from app.models.store import (
    annotated_pdf_filename,
    merged_pdf_filename,
    normalized_source_filename,
    translated_pdf_filename,
)


def test_every_published_product_gets_its_own_name():
    """Two products of one document must never share a path.

    The bilingual PDF the backend used to publish and the side-by-side merge
    both resolved to ``<stem>_双语对照.pdf``, so one overwrote the other.
    """
    source = "attention_is_all_you_need.pdf"

    names = {
        "translated": translated_pdf_filename(source),
        "annotated": annotated_pdf_filename(source),
        "merged": merged_pdf_filename(source),
    }

    assert len(set(names.values())) == len(names)
    assert all(name.endswith(".pdf") for name in names.values())
    # Stable for the same input, whatever the source filename contains.
    assert names == {
        "translated": translated_pdf_filename(source),
        "annotated": annotated_pdf_filename(source),
        "merged": merged_pdf_filename(source),
    }


def test_product_names_derive_from_the_normalized_source_name():
    source = normalized_source_filename("weird: name*.pdf")

    assert source == "weird_ name_.pdf"
    stem = "weird_ name_"
    assert translated_pdf_filename(source) == f"{stem}_Chinese_ver.pdf"
    assert merged_pdf_filename(source) == f"{stem}_双语对照.pdf"
    assert annotated_pdf_filename(source) == f"{stem}_原文标注.pdf"
