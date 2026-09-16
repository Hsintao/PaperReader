import shutil
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.models.store import DocumentRecord
from app.services.document_structure import build_document_structure


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

    source = isolated_storage / 'main.tex'
    source.write_text(r'\begin{document}\begin{figure}\caption{Demo}\end{figure}\begin{table}\caption{Stats}\end{table}\end{document}')
    _write_pdf_with_artwork(isolated_storage / 'original.pdf', 'Figure 1. Demo', 'Table 1. Stats')
    _write_pdf_with_artwork(isolated_storage / 'translated.pdf', 'Figure 1. Translated demo', 'Table 1. Translated stats')
    record = DocumentRecord(document_id='artwork', owner_user_id=1, source_type='tex', source_path=source,
                            original_pdf_url='/data/original.pdf', translated_pdf_url='/data/translated.pdf')
    structure = build_document_structure(record)
    for side in ('figures', 'translated_figures'):
        figure, table = structure[side]
        assert figure['page'] == 1 and table['page'] == 1
        assert figure['caption'].startswith('Figure 1') and table['caption'].startswith('Table 1')
        images = [Image.open((isolated_storage / item['url'].removeprefix('/data/')))
                  for item in (figure, table)]
        # Artwork spans (50, 720)-(350, 770) and the rules span (50, 450)-(350, 601):
        # previews must crop those regions, not the 489x633 full page.
        assert all(image.size[0] < 350 for image in images)
        assert images[0].size[1] < 120 and 100 < images[1].size[1] < 300


def test_caption_line_anchor_wins_over_body_reference(isolated_storage):
    source = isolated_storage / 'main.tex'
    source.write_text(r'\begin{document}\begin{table}\caption{A}\end{table}\begin{table}\caption{B}\end{table}\end{document}')
    _write_pdf(isolated_storage / 'original.pdf', ['See results in Table 2. They are body text.', 'Table 2. Real caption'])
    record = DocumentRecord(document_id='anchored', owner_user_id=1, source_type='tex', source_path=source,
                            original_pdf_url='/data/original.pdf')
    figures = build_document_structure(record)['figures']
    assert figures[1]['page'] == 2
    assert figures[1]['locate_text'] == 'Table 2. Real caption'


def test_merged_subfigure_caption_still_locates(isolated_storage):
    source = isolated_storage / 'main.tex'
    source.write_text(r'\begin{document}\begin{figure}\caption{Demo}\end{figure}\end{document}')
    _write_pdf(isolated_storage / 'original.pdf', ['Legend a bFigure 1. Real caption here'])
    record = DocumentRecord(document_id='merged', owner_user_id=1, source_type='tex', source_path=source,
                            original_pdf_url='/data/original.pdf')
    figures = build_document_structure(record)['figures']
    assert figures[0]['page'] == 1
    assert figures[0]['locate_text'] == 'Figure 1. Real caption here'
    source = isolated_storage / 'main.tex'
    source.write_text(r'\begin{document}\begin{table}\caption{First}\caption{Second}\end{table}\end{document}')
    _write_pdf(isolated_storage / 'original.pdf', ['Table 1. First', 'Table 2. Second'])
    _write_pdf(isolated_storage / 'translated.pdf', ['Introduction', 'Table 1. Translated first', 'Table 2. Translated second'])
    record = DocumentRecord(document_id='previews', owner_user_id=1, source_type='tex', source_path=source,
                            original_pdf_url='/data/original.pdf', translated_pdf_url='/data/translated.pdf')
    structure = build_document_structure(record)
    assert [x['page'] for x in structure['figures']] == [1, 2]
    assert [x['page'] for x in structure['translated_figures']] == [2, 3]
    assert structure['translated_figures'][0]['locate_text'] == 'Table 1. Translated first'
    assert all((isolated_storage / x['url'].removeprefix('/data/')).is_file() for x in structure['figures'])


_EXAMPLES = Path(__file__).resolve().parents[2] / 'testexamples'


@pytest.mark.skipif(not (_EXAMPLES / 'Denoise.tar.gz').exists(), reason='local Denoise samples are not distributed')
def test_denoise_pdf_and_tex_figures(isolated_storage):
    from app.services.project_archive import extract_project_archive
    project = isolated_storage / 'project'
    extract_project_archive((_EXAMPLES / 'Denoise.tar.gz').read_bytes(), 'Denoise.tar.gz', project,
                            max_file_bytes=100 * 1024**2, max_total_bytes=500 * 1024**2)
    shutil.copy2(_EXAMPLES / 'Denoise.pdf', isolated_storage / 'Denoise.pdf')
    record = DocumentRecord(document_id='denoise', owner_user_id=1, source_type='tex_project',
                            source_path=project / 'main.tex', original_pdf_url='/data/Denoise.pdf')
    figures = build_document_structure(record)['figures']
    assert len(figures) == 24
    assert [x['label'] for x in figures if x['kind'] == 'figure'] == [f'Figure {n}' for n in range(1, 12)]
    assert [x['label'] for x in figures if x['kind'] == 'table'] == [f'Table {n}' for n in range(1, 14)]
    assert [x['page'] for x in figures] == [1, 4, 4, 5, 6, 6, 6, 7, 7, 7, 8, 8, 8, 9, 9, 10, 11, 11, 11, 11, 15, 16, 17, 18]
    assert all(x['url'].endswith('.png') for x in figures)
