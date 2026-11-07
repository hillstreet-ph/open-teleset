"""PDF and Office attachments are read as text, without ever trusting the XML.

Every fixture is built in memory, so these tests need no sample files and no
network. The XML parts refuse a DTD, and a document that carries an entity
declaration is skipped rather than expanded.
"""

import io
import zipfile
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import knowledge


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("OPERATOR_STORE_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    yield


@pytest.fixture
def client(tmp_path, monkeypatch):
    app = FastAPI()

    @app.middleware("http")
    async def sign_in(request, call_next):
        request.state.user = SimpleNamespace(id="operator-1", role="admin")
        return await call_next(request)

    app.include_router(knowledge.router)
    return TestClient(app)


def _zip(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _docx(*paragraphs: str) -> bytes:
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    return _zip({"word/document.xml": (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>")})


def _pptx(*slides: str) -> bytes:
    members = {}
    for index, text in enumerate(slides, start=1):
        members[f"ppt/slides/slide{index}.xml"] = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f"<p:cSld><p:spTree><a:p><a:r><a:t>{text}</a:t></a:r></a:p>"
            "</p:spTree></p:cSld></p:sld>")
    return _zip(members)


def _xlsx() -> bytes:
    shared = ('<?xml version="1.0" encoding="UTF-8"?>'
              '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              "<si><t>Name</t></si><si><t>City</t></si></sst>")
    sheet = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData><row r="1">'
             '<c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c>'
             '</row><row r="2">'
             '<c r="A2" t="inlineStr"><is><t>Alice</t></is></c>'
             '<c r="B2" t="inlineStr"><is><t>Lisbon</t></is></c>'
             "</row></sheetData></worksheet>")
    return _zip({"xl/sharedStrings.xml": shared, "xl/worksheets/sheet1.xml": sheet})


def _odf(text: str) -> bytes:
    return _zip({"content.xml": (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
        f"<office:body><office:text><text:p>{text}</text:p>"
        "<text:p>Second line.</text:p></office:text></office:body>"
        "</office:document-content>")})


def _pdf(text: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_docx_paragraphs_are_extracted():
    pairs, reason = knowledge.extract_texts("notes.docx", _docx("Refunds run 30 days.", "Second line."))

    assert reason is None
    assert pairs == [("notes.docx", "Refunds run 30 days.\nSecond line.")]


def test_pptx_slides_are_extracted_in_order():
    pairs, reason = knowledge.extract_texts("deck.pptx", _pptx("Slide one title", "Slide two title"))

    assert reason is None
    assert pairs[0][1] == "Slide one title\n\nSlide two title"


def test_xlsx_shared_and_inline_strings_are_extracted():
    pairs, reason = knowledge.extract_texts("table.xlsx", _xlsx())

    assert reason is None
    assert pairs[0][1] == "Name\tCity\nAlice\tLisbon"


@pytest.mark.parametrize("suffix", [".odt", ".ods", ".odp"])
def test_open_document_text_is_extracted(suffix):
    pairs, reason = knowledge.extract_texts(f"notes{suffix}", _odf("Refunds run 30 days."))

    assert reason is None
    assert pairs[0][1] == "Refunds run 30 days.\nSecond line."


def test_rtf_control_words_are_stripped():
    payload = b"{\\rtf1\\ansi Refunds run 30 days.\\par Second line.}"

    pairs, reason = knowledge.extract_texts("notes.rtf", payload)

    assert reason is None
    assert pairs[0][1] == "Refunds run 30 days.\nSecond line."


def test_pdf_text_is_extracted():
    pairs, reason = knowledge.extract_texts("policy.pdf", _pdf("Refunds run 30 days."))

    assert reason is None
    assert "Refunds run 30 days." in pairs[0][1]


def test_a_document_with_a_dtd_is_refused():
    payload = _zip({"word/document.xml": (
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&lol;</w:t></w:r></w:p></w:body></w:document>")})

    pairs, reason = knowledge.extract_texts("bomb.docx", payload)

    assert pairs == []
    assert "could not be read" in reason


def test_an_empty_document_reports_no_readable_text():
    pairs, reason = knowledge.extract_texts("empty.docx", _docx())

    assert pairs == []
    assert "no readable text" in reason


def test_a_zip_can_carry_a_document():
    payload = _zip({"notes.docx": _docx("Refunds run 30 days."),
                    "readme.md": "A plain note."})

    pairs, reason = knowledge.extract_texts("bundle.zip", payload)

    assert reason is None
    assert [name for name, _ in pairs] == ["bundle.zip/notes.docx", "bundle.zip/readme.md"]
    assert pairs[0][1] == "Refunds run 30 days."


def test_legacy_office_formats_get_a_clear_reason():
    pairs, reason = knowledge.extract_texts("old.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")

    assert pairs == []
    assert "save legacy Office files" in reason


def test_extracted_text_is_capped():
    payload = _docx("x" * (knowledge.MAX_EXTRACTED_CHARS + 5000))

    pairs, reason = knowledge.extract_texts("huge.docx", payload)

    assert reason is None
    assert len(pairs[0][1]) == knowledge.MAX_EXTRACTED_CHARS


def test_a_docx_upload_imports_as_a_note(client):
    response = client.post("/api/knowledge/import", files=[
        ("files", ("policy.docx", _docx("Refunds run 30 days."), "application/vnd.openxmlformats-"
                   "officedocument.wordprocessingml.document")),
    ])

    body = response.json()
    assert body["imported"] == 1
    assert body["files"][0]["name"] == "policy.docx"
    documents = client.get("/api/knowledge/documents").json()["documents"]
    assert [doc["title"] for doc in documents] == ["policy.docx"]
    stored = client.get(f"/api/knowledge/documents/{documents[0]['id']}").json()["document"]
    assert stored["text"] == "Refunds run 30 days."
