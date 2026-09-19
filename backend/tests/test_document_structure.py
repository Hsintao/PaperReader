from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.models.store import DocumentRecord
from app.services.document_structure import _with_pdf_previews, build_document_structure


def _write_pdf(path, texts):
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(f'BT /F1 12 Tf 50 700 Td ({text}) Tj ET'.encode())
        page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)


def _write_pdf_with_artwork(path, figure_caption, table_caption):
    """A page with vector artwork above a figure caption and rules around a table caption."""
    from pypdf.generic import ArrayObject, FloatObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    artwork = DecodedStreamObject()
    artwork[NameObject('/Type')] = NameObject('/XObject')
    artwork[NameObject('/Subtype')] = NameObject('/Form')
    artwork[NameObject('/BBox')] = ArrayObject([FloatObject(0), FloatObject(0), FloatObject(300), FloatObject(50)])
    artwork.set_data(b'0 0 300 50 re f')
    page[NameObject('/Resources')] = DictionaryObject({
        NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)}),
        NameObject('/XObject'): DictionaryObject({NameObject('/Fm0'): writer._add_object(artwork)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(
        f'BT /F1 12 Tf 50 700 Td ({figure_caption}) Tj ET '
        f'BT /F1 12 Tf 50 400 Td ({table_caption}) Tj ET '
        'q 1 0 0 1 50 720 cm /Fm0 Do Q '
        '50 600 300 1 re f 50 450 300 1 re f'.encode())
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(path)


def test_previews_crop_embedded_artwork_beside_captions(isolated_storage):
    from PIL import Image

    _write_pdf_with_artwork(isolated_storage / 'original.pdf', 'Figure 1. Demo', 'Table 1. Stats')
    _write_pdf_with_artwork(isolated_storage / 'translated.pdf', 'Figure 1. Translated demo', 'Table 1. Translated stats')
    record = DocumentRecord(document_id='artwork', source_type='pdf',
                            source_path=isolated_storage / 'artwork.pdf',
                            original_pdf_url='/data/original.pdf', translated_pdf_url='/data/translated.pdf')
    figures = [
        {'kind': 'figure', 'label': 'Figure 1', 'caption': 'Figure 1. Demo', 'page': 1, 'url': ''},
        {'kind': 'table', 'label': 'Table 1', 'caption': 'Table 1. Stats', 'page': 1, 'url': ''},
    ]
    for side in ('original', 'translated'):
        structure = {'figures': _with_pdf_previews(record, figures, side)}
        figure, table = structure['figures']
        assert figure['page'] == 1 and table['page'] == 1
        assert figure['caption'].startswith('Figure 1') and table['caption'].startswith('Table 1')
        images = [Image.open((isolated_storage / item['url'].removeprefix('/data/')))
                  for item in (figure, table)]
        # Artwork spans (50, 720)-(350, 770) and the rules span (50, 450)-(350, 601):
        # previews must crop those regions, not the 489x633 full page.
        assert all(image.size[0] < 350 for image in images)
        assert images[0].size[1] < 120 and 100 < images[1].size[1] < 300


def test_caption_line_anchor_wins_over_body_reference(isolated_storage):
    _write_pdf(isolated_storage / 'original.pdf', ['See results in Table 2. They are body text.', 'Table 2. Real caption'])
    record = DocumentRecord(document_id='anchored', source_type='pdf',
                            source_path=isolated_storage / 'anchored.pdf',
                            original_pdf_url='/data/original.pdf')
    figures = [
        {'kind': 'table', 'label': 'Table 1', 'caption': 'Table 1. First', 'page': 1, 'url': ''},
        {'kind': 'table', 'label': 'Table 2', 'caption': 'Table 2', 'page': 1, 'url': ''},
    ]
    located = _with_pdf_previews(record, figures, 'original')
    # The line-start caption wins over the mid-sentence reference on page 1.
    assert located[1]['page'] == 2
    assert located[1]['locate_text'] == 'Table 2. Real caption'


def test_merged_subfigure_caption_still_locates(isolated_storage):
    _write_pdf(isolated_storage / 'original.pdf', ['Legend a bFigure 1. Real caption here'])
    record = DocumentRecord(document_id='merged', source_type='pdf',
                            source_path=isolated_storage / 'merged.pdf',
                            original_pdf_url='/data/original.pdf')
    figures = [{'kind': 'figure', 'label': 'Figure 1', 'caption': 'Figure 1. Demo', 'page': 1, 'url': ''}]
    located = _with_pdf_previews(record, figures, 'original')
    assert located[0]['page'] == 1
    assert located[0]['locate_text'] == 'Figure 1. Real caption here'


def test_captions_locate_independently_in_each_pdf(isolated_storage):
    _write_pdf(isolated_storage / 'original.pdf', ['Table 1. First', 'Table 2. Second'])
    _write_pdf(isolated_storage / 'translated.pdf', ['Introduction', 'Table 1. Translated first', 'Table 2. Translated second'])
    record = DocumentRecord(document_id='previews', source_type='pdf',
                            source_path=isolated_storage / 'previews.pdf',
                            original_pdf_url='/data/original.pdf', translated_pdf_url='/data/translated.pdf')
    figures = [
        {'kind': 'table', 'label': 'Table 1', 'caption': 'Table 1. First', 'page': 1, 'url': ''},
        {'kind': 'table', 'label': 'Table 2', 'caption': 'Table 2. Second', 'page': 2, 'url': ''},
    ]
    original = _with_pdf_previews(record, figures, 'original')
    translated = _with_pdf_previews(record, figures, 'translated')
    assert [x['page'] for x in original] == [1, 2]
    assert [x['page'] for x in translated] == [2, 3]
    assert translated[0]['locate_text'] == 'Table 1. Translated first'
    assert all((isolated_storage / x['url'].removeprefix('/data/')).is_file() for x in original)
