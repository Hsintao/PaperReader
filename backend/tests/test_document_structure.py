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


def test_previews_use_each_pdf_page_and_include_multiple_table_captions(isolated_storage):
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
