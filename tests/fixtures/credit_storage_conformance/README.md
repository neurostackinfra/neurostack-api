# Parser fixtures: `POST /v1/knowledge/documents/upload`

Inputs of the multipart DOCX/PDF knowledge upload (neurostack-credit-system
Group 4a, owner decision H). The tests read these bytes; `generate.py`
records how they were made with the locked `pypdf` and `python-docx`.

| File | Before `credit_enforcement_at` | After it |
|------|--------------------------------|----------|
| `positive/textual.pdf` | 201, text stored | 201, RAG reservation debited |
| `positive/textual.docx` | 201, paragraphs and table cells stored | 201, RAG reservation debited |
| `negative/ocr-only.pdf` (an image, no text) | 422 `knowledge_file_unreadable` | same |
| `negative/password-protected.pdf` (user password) | 422 `knowledge_file_unreadable` | same |
| `negative/corrupt.pdf` | 422 `knowledge_file_unreadable` | same |
| `negative/corrupt.docx` | 422 `knowledge_file_unreadable` | same |
| `negative/invalid-format.bin` (a PNG signature; Group 5) | 422 `knowledge_file_unsupported` | same |

An unreadable or unsupported file is refused at any time. A PDF encrypted
with an owner password only (empty user password: readable) is built in
`tests/api/test_credit_rag.py`: accepted before the enforcement, 422
`knowledge_file_password_protected` after it. The other commercial limits
(20 MB per file, 500,000 extracted characters, 10 files) also apply only
after the enforcement; a technical request cap applies always.

Group 5's storage conformance suite (`tests/integration/test_credit_storage_s3.py`)
also copies every file here, byte for byte, through the disposable S3-compatible
bucket and checks SHA-256 and `VersionId` across tombstone, restore and purge.
