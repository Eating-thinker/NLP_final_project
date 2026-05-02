from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

import streamlit as st


APP_DIR = Path(__file__).parent
SETTINGS_FILE = APP_DIR / "settings.json"
DATA_DIR = APP_DIR / "data"
ENGLISH_DOCS_DIR = APP_DIR / "English_docs"
CHROMA_DIR = DATA_DIR / "chroma_db"
BM25_ROWS_PATH = DATA_DIR / "bm25_chunks.jsonl"
WHOOSH_INDEX_DIR = DATA_DIR / "whoosh_index"
COLLECTION_NAME = "nlp_final_project_docs"
SUPPORTED_EXTENSIONS = ("*.pdf", "*.docx", "*.doc", "*.odt", "*.txt")
ANSWER_TOP_K = 10
EMBED_BATCH_SIZE = 32
SECRETS_CREDENTIALS_PATH = DATA_DIR / ".gcp_service_account.json"


st.set_page_config(page_title="NLP Final Project RAG", page_icon="📚", layout="wide")


def default_settings() -> dict:
    return {
        "project_id": "",
        "location": "us-central1",
        "gemini_model": "gemini-2.5-flash",
        "index_access_password": "",
        "documents_source": "english_docs",
        "documents_folder": str(ENGLISH_DOCS_DIR),
        "translate_query_to_english": True,
        "embedding_model": "text-multilingual-embedding-002",
        "chunk_size": 600,
        "chunk_overlap": 100,
        "vector_top_k": 6,
        "bm25_top_k": 8,
        "final_top_k": ANSWER_TOP_K,
    }


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return default_settings() | json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return default_settings()


def save_settings(settings: dict) -> None:
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def get_streamlit_secret(name: str):
    local_secret_paths = [
        Path.home() / ".streamlit" / "secrets.toml",
        APP_DIR / ".streamlit" / "secrets.toml",
    ]
    should_try_streamlit_secrets = any(path.exists() for path in local_secret_paths) or bool(
        os.environ.get("STREAMLIT_SHARING_MODE") or os.environ.get("STREAMLIT_CLOUD")
    )
    if not should_try_streamlit_secrets:
        return None

    try:
        return st.secrets.get(name)
    except Exception:
        return None


def ensure_google_application_credentials() -> None:
    if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return

    service_account_secret = get_streamlit_secret("gcp_service_account")

    if not service_account_secret:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if isinstance(service_account_secret, str):
        secret_payload = json.loads(service_account_secret)
    else:
        secret_payload = dict(service_account_secret)

    SECRETS_CREDENTIALS_PATH.write_text(
        json.dumps(secret_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(SECRETS_CREDENTIALS_PATH)


def resolve_google_project_id(settings: dict) -> str:
    configured_project_id = settings.get("project_id", "").strip()
    if configured_project_id:
        return configured_project_id

    service_account_secret = get_streamlit_secret("gcp_service_account")

    if service_account_secret:
        if isinstance(service_account_secret, str):
            secret_payload = json.loads(service_account_secret)
        else:
            secret_payload = dict(service_account_secret)
        return str(secret_payload.get("project_id", "")).strip()

    try:
        ensure_google_application_credentials()
        import google.auth

        _, detected_project_id = google.auth.default(scopes=GOOGLE_AUTH_SCOPES)
        return str(detected_project_id or "").strip()
    except Exception:
        pass

    return ""


GOOGLE_AUTH_SCOPES = [
    "https://www.googleapis.com/auth/generative-language",
    "https://www.googleapis.com/auth/cloud-platform",
]


def normalize_folder(path_text: str) -> Path:
    path = Path(path_text.strip() or str(ENGLISH_DOCS_DIR))
    if not path.is_absolute():
        path = (APP_DIR / path).resolve()
    return path


def detect_documents_source(path_text: str) -> str:
    normalized = normalize_folder(path_text)
    if normalized == ENGLISH_DOCS_DIR.resolve():
        return "english_docs"
    return "custom"


def get_doc_files(settings: dict) -> list[Path]:
    doc_root = normalize_folder(settings.get("documents_folder", str(ENGLISH_DOCS_DIR)))
    if not doc_root.exists():
        return []
    files: list[Path] = []
    for ext in SUPPORTED_EXTENSIONS:
        files.extend(doc_root.rglob(ext))
    return sorted(files)


def inspect_documents(settings: dict) -> dict:
    files = get_doc_files(settings)
    chunk_size = int(settings["chunk_size"])
    chunk_overlap = int(settings["chunk_overlap"])

    summary = {
        "files": files,
        "success_count": 0,
        "total_chunks": 0,
        "total_tokens": 0,
        "errors": [],
        "samples": [],
    }

    for file_path in files:
        text = parse_file(file_path)
        if not text.strip():
            summary["errors"].append(f"{file_path.name}: parse returned empty text")
            continue

        chunks = chunk_text(text, chunk_size, chunk_overlap)
        if not chunks:
            summary["errors"].append(f"{file_path.name}: chunk_text returned no chunks")
            continue

        token_count = 0
        token_error = ""
        try:
            token_count = len(tokenize(text))
        except Exception as exc:
            token_error = f"{type(exc).__name__}: {exc}"

        summary["success_count"] += 1
        summary["total_chunks"] += len(chunks)
        summary["total_tokens"] += token_count
        summary["samples"].append(
            {
                "name": file_path.name,
                "text_length": len(text),
                "chunk_count": len(chunks),
                "token_count": token_count,
                "token_error": token_error,
                "preview": chunks[0][:300].replace("\n", " "),
            }
        )

    return summary


def parse_file(file_path: Path) -> str:
    odt_text_ns = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
    docx_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    work_tmp_root = APP_DIR / ".parse_tmp"

    def safe_rmtree(path: Path) -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    def make_work_tmp_dir(prefix: str) -> Path:
        work_tmp_root.mkdir(parents=True, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(work_tmp_root)))

    def find_libreoffice_executable() -> str:
        candidates = [
            shutil.which("libreoffice"),
            shutil.which("soffice"),
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files\LibreOffice\program\soffice.COM",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return str(candidate)
        return ""

    def extract_with_libreoffice(path: Path) -> str:
        libreoffice = find_libreoffice_executable()
        if not libreoffice:
            return ""
        tmpdir = make_work_tmp_dir("doc_parse_")
        try:
            result = subprocess.run(
                [
                    libreoffice,
                    "--headless",
                    "--convert-to",
                    "txt:Text",
                    "--outdir",
                    str(tmpdir),
                    str(path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                return ""
            txt_files = sorted(tmpdir.glob("*.txt"))
            if not txt_files:
                return ""
            return txt_files[0].read_text(encoding="utf-8", errors="ignore")
        finally:
            safe_rmtree(tmpdir)

    def extract_with_word_com(path: Path) -> str:
        import win32com.client

        word = None
        doc = None
        try:
            word = win32com.client.gencache.EnsureDispatch("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(
                str(path.resolve()),
                ConfirmConversions=False,
                ReadOnly=True,
                AddToRecentFiles=False,
                OpenAndRepair=True,
            )
            text = (doc.Content.Text or "").strip()
            if text:
                return text
            return ""
        except Exception:
            return ""
        finally:
            try:
                if doc is not None:
                    doc.Close(False)
            except Exception:
                pass
            try:
                if word is not None:
                    word.Quit()
            except Exception:
                pass

    def extract_docx_xml(path: Path) -> str:
        with zipfile.ZipFile(path, "r") as zf:
            raw = zf.read("word/document.xml")
        root = ET.fromstring(raw)
        paragraph_tag = f"{{{docx_ns}}}p"
        text_tag = f"{{{docx_ns}}}t"
        lines: list[str] = []
        for para in root.iter(paragraph_tag):
            parts = [node.text for node in para.iter(text_tag) if node.text]
            text = " ".join("".join(parts).split())
            if text:
                lines.append(text)
        return "\n".join(lines)

    def extract_odt_xml(path: Path) -> str:
        with zipfile.ZipFile(path, "r") as zf:
            raw = zf.read("content.xml")
        root = ET.fromstring(raw)
        valid_tags = {f"{{{odt_text_ns}}}h", f"{{{odt_text_ns}}}p"}
        lines: list[str] = []
        for elem in root.iter():
            if elem.tag not in valid_tags:
                continue
            text = " ".join("".join(elem.itertext()).split())
            if text:
                lines.append(text)
        return "\n".join(lines)

    try:
        suffix = file_path.suffix.lower()
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(file_path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        if suffix == ".docx":
            try:
                from docx import Document

                doc = Document(str(file_path))
                text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
                if text.strip():
                    return text
            except Exception:
                pass
            return extract_docx_xml(file_path)
        if suffix == ".doc":
            text = extract_with_word_com(file_path)
            if text.strip():
                return text
            return extract_with_libreoffice(file_path)
        if suffix == ".odt":
            try:
                text = extract_odt_xml(file_path)
                if text.strip():
                    return text
            except Exception:
                pass
            return extract_with_libreoffice(file_path)
        if suffix == ".txt":
            for encoding in ("utf-8", "utf-16", "cp950", "big5", "latin1"):
                try:
                    text = file_path.read_text(encoding=encoding, errors="ignore")
                    if text.strip():
                        return text
                except Exception:
                    continue
    except Exception:
        return ""
    return ""


def chunk_text(text: str, size: int = 600, overlap: int = 100) -> list[str]:
    raw = re.split(r"(?<=[。！？.!?\n])", text)
    sentences = [s.strip() for s in raw if s.strip()]
    if not sentences:
        return [text.strip()] if text.strip() else []

    chunks: list[str] = []
    i = 0
    while i < len(sentences):
        char_count = 0
        j = i
        while j < len(sentences):
            char_count += len(sentences[j])
            j += 1
            if char_count >= size:
                break
        if j == i:
            j = i + 1
        chunks.append("".join(sentences[i:j]))

        overlap_chars = 0
        next_start = j
        for k in range(j - 1, i, -1):
            overlap_chars += len(sentences[k])
            if overlap_chars >= overlap:
                next_start = k
                break
        i = next_start if next_start > i else j
    return chunks


def tokenize(text: str) -> list[str]:
    import jieba

    base = re.sub(r"\s+", " ", text.strip().lower())
    chinese_tokens = [token.strip() for token in jieba.lcut(base) if token.strip()]
    latin_tokens = re.findall(r"[a-z0-9_+-]+", base)
    return chinese_tokens + latin_tokens


def contains_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def write_bm25_rows(rows: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with BM25_ROWS_PATH.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_bm25_rows() -> list[dict]:
    if not BM25_ROWS_PATH.exists():
        return []
    rows: list[dict] = []
    with BM25_ROWS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def build_whoosh_index(rows: list[dict]) -> None:
    from whoosh import index
    from whoosh.fields import ID, KEYWORD, NUMERIC, Schema, TEXT

    if WHOOSH_INDEX_DIR.exists():
        shutil.rmtree(WHOOSH_INDEX_DIR, ignore_errors=True)
    WHOOSH_INDEX_DIR.mkdir(parents=True, exist_ok=True)

    schema = Schema(
        doc_id=ID(stored=True, unique=True),
        source=TEXT(stored=True),
        path=TEXT(stored=True),
        chunk_id=NUMERIC(stored=True),
        text=TEXT(stored=True),
        keywords=KEYWORD(stored=False, lowercase=True, commas=False, scorable=True),
        content=KEYWORD(stored=False, lowercase=True, commas=False, scorable=True),
    )
    ix = index.create_in(str(WHOOSH_INDEX_DIR), schema)
    writer = ix.writer()
    for doc_id, row in enumerate(rows):
        token_text = " ".join(tokenize(row["text"]))
        keyword_text = " ".join(sorted(set(tokenize(row["text"]))))
        writer.add_document(
            doc_id=str(doc_id),
            source=str(row["meta"]["source"]),
            path=str(row["meta"]["path"]),
            chunk_id=int(row["meta"]["chunk_id"]),
            text=row["text"],
            keywords=keyword_text,
            content=token_text,
        )
    writer.commit()


def search_whoosh_candidates(keywords: list[str], limit: int) -> list[int]:
    from whoosh import index
    from whoosh.qparser import OrGroup, QueryParser
    from whoosh.query import Or, Term

    if not WHOOSH_INDEX_DIR.exists() or not keywords:
        return []

    candidate_tokens: list[str] = []
    for keyword in keywords:
        candidate_tokens.extend(tokenize(keyword))
    candidate_tokens = [token for token in candidate_tokens if token]
    if not candidate_tokens:
        return []

    ix = index.open_dir(str(WHOOSH_INDEX_DIR))
    with ix.searcher() as searcher:
        parser = QueryParser("keywords", schema=ix.schema, group=OrGroup)
        query = parser.parse(" ".join(sorted(set(candidate_tokens))))
        results = searcher.search(query, limit=limit)
        return [int(hit["doc_id"]) for hit in results]


def search_whoosh_bm25(question: str, candidate_ids: list[int], top_k: int) -> list[dict]:
    from whoosh import index, scoring
    from whoosh.qparser import OrGroup, QueryParser
    from whoosh.query import Or, Term

    if not WHOOSH_INDEX_DIR.exists():
        return []

    query_tokens = [token for token in tokenize(question) if token]
    if not query_tokens:
        return []

    ix = index.open_dir(str(WHOOSH_INDEX_DIR))
    with ix.searcher(weighting=scoring.BM25F()) as searcher:
        parser = QueryParser("content", schema=ix.schema, group=OrGroup)
        query = parser.parse(" ".join(query_tokens))
        filter_query = None
        if candidate_ids:
            filter_query = Or([Term("doc_id", str(doc_id)) for doc_id in candidate_ids])
        results = searcher.search(query, limit=top_k, filter=filter_query)
        hits: list[dict] = []
        for hit in results:
            hits.append(
                {
                    "text": hit["text"],
                    "meta": {
                        "source": hit["source"],
                        "path": hit["path"],
                        "chunk_id": int(hit["chunk_id"]),
                    },
                    "score": float(hit.score),
                    "kind": "bm25",
                }
            )
        return hits


@st.cache_resource(show_spinner=False)
def load_embedder(project_id: str, location: str, model_name: str):
    import google.auth
    import google.auth.transport.requests
    import requests

    ensure_google_application_credentials()

    resolved_project_id = project_id.strip() or resolve_google_project_id({})

    class VertexEmbeddingClient:
        def __init__(self, project_id: str, location: str, model_id: str):
            self.project_id = project_id
            self.location = location
            self.model_id = model_id
            self.creds, _ = google.auth.default(scopes=GOOGLE_AUTH_SCOPES)
            self._auth_req = google.auth.transport.requests.Request()
            self.endpoint = (
                f"https://{location}-aiplatform.googleapis.com/v1/"
                f"projects/{project_id}/locations/{location}/"
                f"publishers/google/models/{model_id}:predict"
            )

        def _get_token(self) -> str:
            if not self.creds.valid:
                self.creds.refresh(self._auth_req)
            return self.creds.token

        def encode(self, texts: list[str], normalize_embeddings: bool = True) -> list[list[float]]:
            import requests

            headers = {
                "Authorization": f"Bearer {self._get_token()}",
                "Content-Type": "application/json",
            }
            all_vectors: list[list[float]] = []
            for batch in batched(texts, EMBED_BATCH_SIZE):
                body = {"instances": [{"content": text} for text in batch]}
                response = requests.post(self.endpoint, headers=headers, json=body, timeout=60)
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    detail = response.text.strip()
                    if len(detail) > 500:
                        detail = detail[:500] + "..."
                    raise RuntimeError(
                        f"Vertex embedding request failed with status {response.status_code}: {detail}"
                    ) from exc
                predictions = response.json()["predictions"]
                vectors = [item["embeddings"]["values"] for item in predictions]
                if normalize_embeddings:
                    vectors = [normalize_vector(vector) for vector in vectors]
                all_vectors.extend(vectors)
            return all_vectors

    return VertexEmbeddingClient(resolved_project_id, location, model_name)


def normalize_vector(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def batched(items: list[str], batch_size: int) -> Iterable[list[str]]:
    for start in range(0, len(items), batch_size):
        yield items[start:start + batch_size]


@st.cache_resource(show_spinner=False)
def load_chroma_collection():
    import chromadb

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return client, client.get_or_create_collection(name=COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


def clear_indexes() -> None:
    st.cache_resource.clear()
    try:
        shutil.rmtree(CHROMA_DIR, ignore_errors=True)
    except Exception:
        pass
    try:
        shutil.rmtree(WHOOSH_INDEX_DIR, ignore_errors=True)
    except Exception:
        pass
    for path in (BM25_ROWS_PATH,):
        try:
            if path.exists():
                path.unlink()
        except Exception:
            pass


def build_indexes(settings: dict) -> tuple[int, int, list[str]]:
    files = get_doc_files(settings)
    if not files:
        return 0, 0, [f"找不到可索引的文件，請先把文章放進 {ENGLISH_DOCS_DIR.name} 資料夾。"]

    clear_indexes()
    chunk_size = int(settings["chunk_size"])
    chunk_overlap = int(settings["chunk_overlap"])

    all_rows: list[dict] = []
    errors: list[str] = []
    total_chunks = 0

    progress = st.progress(0, text="建立索引中...")
    for index, file_path in enumerate(files, start=1):
        progress.progress((index - 1) / max(len(files), 1), text=f"解析中 {index}/{len(files)}: {file_path.name}")
        text = parse_file(file_path)
        if not text.strip():
            errors.append(f"{file_path.name}: 無法解析")
            continue

        chunks = chunk_text(text, chunk_size, chunk_overlap)
        if not chunks:
            errors.append(f"{file_path.name}: 沒有可切分內容")
            continue

        metas = [{"source": file_path.name, "path": str(file_path), "chunk_id": i} for i in range(len(chunks))]

        for chunk, meta in zip(chunks, metas):
            all_rows.append({"text": chunk, "meta": meta})
        total_chunks += len(chunks)

    write_bm25_rows(all_rows)
    build_whoosh_index(all_rows)
    progress.progress(1.0, text="索引完成")
    st.cache_resource.clear()
    return len(files), total_chunks, errors


def bm25_search(
    question: str,
    top_k: int,
    settings: dict,
    keywords: list[str] | None = None,
) -> tuple[list[dict], list[str], int, dict[str, float]]:
    timings = {
        "keyword_extraction_sec": 0.0,
        "candidate_search_sec": 0.0,
        "bm25_ranking_sec": 0.0,
    }
    rows = load_bm25_rows()
    if not rows:
        return [], [], 0, timings

    resolved_keywords = keywords or []
    if not resolved_keywords:
        started = time.perf_counter()
        resolved_keywords = extract_keywords_for_retrieval(question, settings)
        timings["keyword_extraction_sec"] = time.perf_counter() - started

    started = time.perf_counter()
    candidate_ids = search_whoosh_candidates(resolved_keywords, limit=max(top_k * 20, 200))
    timings["candidate_search_sec"] = time.perf_counter() - started
    if not candidate_ids:
        candidate_ids = list(range(len(rows)))

    started = time.perf_counter()
    ranked = search_whoosh_bm25(question, candidate_ids, top_k)
    timings["bm25_ranking_sec"] = time.perf_counter() - started
    return ranked, resolved_keywords, len(candidate_ids), timings


def vector_search(question: str, settings: dict) -> list[dict]:
    return []


def rrf_merge(vector_hits: list[dict], bm25_hits: list[dict], top_k: int) -> list[dict]:
    merged: dict[tuple[str, int], dict] = {}
    rank_constant = 60

    for rank, hit in enumerate(vector_hits, start=1):
        key = (hit["meta"]["source"], int(hit["meta"]["chunk_id"]))
        merged.setdefault(key, hit | {"rrf_score": 0.0})
        merged[key]["rrf_score"] += 1.0 / (rank_constant + rank)

    for rank, hit in enumerate(bm25_hits, start=1):
        key = (hit["meta"]["source"], int(hit["meta"]["chunk_id"]))
        merged.setdefault(key, hit | {"rrf_score": 0.0})
        merged[key]["rrf_score"] += 1.0 / (rank_constant + rank)

    ranked = sorted(merged.values(), key=lambda item: item["rrf_score"], reverse=True)
    return ranked[:top_k]


def build_context(hits: list[dict]) -> str:
    parts = []
    for index, hit in enumerate(hits, start=1):
        parts.append(f"[{index}] Source: {hit['meta']['source']}\n{hit['text']}")
    return "\n\n".join(parts)


def generate_gemini_text(prompt: str, settings: dict, temperature: float = 0.2) -> str:
    import google.auth
    import google.auth.transport.requests
    import requests

    ensure_google_application_credentials()

    project_id = resolve_google_project_id(settings)
    location = settings.get("location", "us-central1").strip() or "us-central1"
    model = settings.get("gemini_model", "gemini-2.5-flash").strip() or "gemini-2.5-flash"
    if not project_id:
        return "尚未取得 GCP project_id，請確認 settings.json 或 Streamlit secrets 中的 gcp_service_account 設定。"

    creds, _ = google.auth.default(scopes=GOOGLE_AUTH_SCOPES)
    auth_req = google.auth.transport.requests.Request()
    if not creds.valid:
        creds.refresh(auth_req)

    url = (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{location}/"
        f"publishers/google/models/{model}:generateContent"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    headers = {
        "Authorization": f"Bearer {creds.token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    response = requests.post(url, headers=headers, json=body, timeout=90)
    response.raise_for_status()
    data = response.json()
    candidates = data.get("candidates", [])
    if not candidates:
        return "Gemini 沒有回傳可用答案。"
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(part.get("text", "") for part in parts).strip() or "Gemini 沒有回傳文字答案。"


def translate_question_for_retrieval(question: str, settings: dict) -> str:
    prompt = f"""
You are helping an RAG retrieval system whose source documents are mainly in English.
Translate the user's question into a concise English retrieval query.

Rules:
1. Keep the meaning unchanged.
2. Use natural English keywords that would match English lecture notes.
3. Do not answer the question.
4. Output English only.
5. Output one line only.

User question:
{question}
""".strip()
    translated = generate_gemini_text(prompt, settings, temperature=0.0)
    return translated.strip() or question


def preprocess_cjk_question_for_retrieval(question: str, settings: dict) -> tuple[str, list[str]]:
    prompt = f"""
You are helping an RAG retrieval system whose source documents are mainly in English.
For the user's Chinese question, do both tasks at once:
1. produce a concise English retrieval query
2. extract the most important retrieval keywords

Output format:
QUERY: <one-line English retrieval query>
KEYWORDS: <comma-separated keywords>

Rules:
1. Do not answer the question.
2. Keep the retrieval query short and natural.
3. Keywords should be 3 to 8 items.
4. Prefer technical terms, tasks, models, methods, and concepts.
5. Output English keywords when appropriate.

User question:
{question}
""".strip()
    raw = generate_gemini_text(prompt, settings, temperature=0.0)
    if not raw or "gemini 沒有" in raw.lower() or "尚未設定" in raw:
        fallback_keywords = [token for token in tokenize(question) if len(token.strip()) > 1][:8]
        return question, fallback_keywords

    retrieval_query = question
    keywords: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("QUERY:"):
            retrieval_query = stripped.split(":", 1)[1].strip() or question
        elif upper.startswith("KEYWORDS:"):
            raw_keywords = stripped.split(":", 1)[1].strip()
            keywords = [part.strip().lower() for part in re.split(r"[,;]", raw_keywords) if part.strip()]

    if not keywords:
        keywords = [token for token in tokenize(retrieval_query) if len(token.strip()) > 1][:8]
    return retrieval_query, keywords[:8]


def extract_keywords_for_retrieval(question: str, settings: dict) -> list[str]:
    prompt = f"""
You are helping a search system.
Extract the most important retrieval keywords from the user's question.

Rules:
1. Return only keywords, not a sentence.
2. Prefer content words such as topics, methods, models, tasks, and technical terms.
3. Remove filler words and question wording.
4. Output 3 to 8 keywords.
5. Output in one line separated by commas.
6. If the question is Chinese, you may return English technical keywords when appropriate.

User question:
{question}
""".strip()
    raw = generate_gemini_text(prompt, settings, temperature=0.0)
    if raw and "gemini 沒有" not in raw.lower() and "尚未設定" not in raw:
        parts = [part.strip().lower() for part in re.split(r"[,;\n]", raw) if part.strip()]
        cleaned = [token for token in parts if token]
        if cleaned:
            return cleaned[:8]
    fallback = tokenize(question)
    return [token for token in fallback if len(token.strip()) > 1][:8]


def answer_with_gemini(question: str, context: str, settings: dict) -> str:
    response_language = "繁體中文" if contains_cjk(question) else "English"
    prompt = f"""
你是一個問答助理。請優先參考提供的文件片段回答問題，但不必被文件內容完全限制。

規則：
1. 使用 {response_language} 回答。
2. 提供的 chunks 只是輔助參考，你可以結合你已知的通用知識一起整理答案。
3. 如果文件片段和既有知識不一致，優先以文件片段為主。
4. 不需要列出參考來源，也不要輸出 chunks。
5. 直接回答問題即可，避免多餘前言。

使用者問題：
{question}

參考文件片段：
{context}
""".strip()
    return generate_gemini_text(prompt, settings, temperature=0.2)


def ask_rag(question: str, settings: dict) -> dict:
    total_started = time.perf_counter()
    retrieval_question = question
    translated_query = ""
    retrieval_keywords: list[str] = []
    timings = {
        "preprocess_sec": 0.0,
        "keyword_extraction_sec": 0.0,
        "candidate_search_sec": 0.0,
        "bm25_ranking_sec": 0.0,
        "context_build_sec": 0.0,
        "answer_generation_sec": 0.0,
        "total_sec": 0.0,
    }
    if settings.get("translate_query_to_english", True) and contains_cjk(question):
        started = time.perf_counter()
        retrieval_question, retrieval_keywords = preprocess_cjk_question_for_retrieval(question, settings)
        timings["preprocess_sec"] = time.perf_counter() - started
        translated_query = retrieval_question
    else:
        retrieval_keywords = [token for token in tokenize(question) if len(token.strip()) > 1][:8]

    bm25_hits, retrieval_keywords, candidate_count, search_timings = bm25_search(
        retrieval_question,
        int(settings["bm25_top_k"]),
        settings,
        keywords=retrieval_keywords,
    )
    timings.update(search_timings)
    merged_hits = bm25_hits[:ANSWER_TOP_K]
    if not merged_hits:
        timings["total_sec"] = time.perf_counter() - total_started
        return {
            "answer": "找不到相關內容，請先確認文件是否已建立索引，或換個問法。",
            "hits": [],
            "vector_hits": [],
            "bm25_hits": bm25_hits,
            "retrieval_question": retrieval_question,
            "translated_query": translated_query,
            "retrieval_keywords": retrieval_keywords,
            "candidate_count": candidate_count,
            "timings": timings,
        }

    started = time.perf_counter()
    context = build_context(merged_hits)
    timings["context_build_sec"] = time.perf_counter() - started

    started = time.perf_counter()
    answer = answer_with_gemini(question, context, settings)
    timings["answer_generation_sec"] = time.perf_counter() - started
    timings["total_sec"] = time.perf_counter() - total_started
    return {
        "answer": answer,
        "hits": merged_hits,
        "vector_hits": [],
        "bm25_hits": bm25_hits,
        "retrieval_question": retrieval_question,
        "translated_query": translated_query,
        "retrieval_keywords": retrieval_keywords,
        "candidate_count": candidate_count,
        "timings": timings,
    }


def count_indexed_chunks() -> int:
    rows = load_bm25_rows()
    return len(rows)


def count_indexed_sources() -> int:
    rows = load_bm25_rows()
    source_counter = {row["meta"]["source"] for row in rows}
    return len(source_counter)


def render_index_panel(settings: dict) -> None:
    st.subheader("資料準備")
    doc_root = normalize_folder(settings.get("documents_folder", str(ENGLISH_DOCS_DIR)))
    files = get_doc_files(settings)
    col1, col2, col3 = st.columns(3)
    col1.metric("文件資料夾", str(doc_root))
    col2.metric("已找到文件", len(files))
    col3.metric("已索引 chunks", count_indexed_chunks())

    if st.button("測試解析與切塊", use_container_width=True):
        with st.spinner("正在檢查文件是否能成功解析並切成 chunks..."):
            summary = inspect_documents(settings)
        if not summary["files"]:
            st.error(f"{doc_root} 目前沒有可解析的支援文件。")
        else:
            st.success(
                f"成功解析 {summary['success_count']}/{len(summary['files'])} 份文件，"
                f"共產生 {summary['total_chunks']} 個 chunks。"
            )
            if summary["errors"]:
                st.warning("部分文件未成功通過解析檢查：\n" + "\n".join(summary["errors"][:10]))
            with st.expander("解析樣本", expanded=False):
                for sample in summary["samples"][:10]:
                    st.markdown(
                        f"**{sample['name']}** | chars={sample['text_length']} | "
                        f"chunks={sample['chunk_count']} | tokens={sample['token_count']}"
                    )
                    if sample["token_error"]:
                        st.caption(f"Tokenize warning: {sample['token_error']}")
                    st.write(sample["preview"] + ("..." if len(sample["preview"]) == 300 else ""))

    if st.button("重新建立索引", type="primary", use_container_width=True):
        with st.spinner("索引建立中，請稍候..."):
            file_count, chunk_count, errors = build_indexes(settings)
        if file_count == 0:
            st.error(errors[0] if errors else "沒有可建立索引的文件")
        else:
            st.success(f"索引完成：{file_count} 份文件，{chunk_count} 個 chunks。")
            if errors:
                st.warning("部分文件未成功解析：\n" + "\n".join(errors[:10]))

    with st.expander("目前文件清單", expanded=False):
        if files:
            for file_path in files:
                st.write(f"- {file_path.name}")
        else:
            st.write("目前還沒有文件。")


def render_protected_index_panel(settings: dict) -> None:
    password = settings.get("index_access_password", "")
    if not password:
        st.warning("建立者設定區：請先在這裡完成索引建立與文件更新設定。")
        render_index_panel(settings)
        return

    session_key = "index_panel_unlocked"
    if st.session_state.get(session_key):
        st.warning("建立者設定區：請先在這裡完成索引建立與文件更新設定。")
        render_index_panel(settings)
        return

    st.subheader("建立索引")
    st.error("建立者設定區：請先完成索引建立與更新，Demo 使用者不需要操作這個頁面。")
    st.info("這個頁面需要密碼才能進入。")
    typed_password = st.text_input("索引頁密碼", type="password", key="index_access_password_input")
    if st.button("進入建立索引頁", use_container_width=True):
        if typed_password == password:
            st.session_state[session_key] = True
            st.rerun()
        else:
            st.error("密碼錯誤。")


def render_qa_panel(settings: dict) -> None:
    st.subheader("RAG 問答")
    question = st.text_area("輸入你的問題", height=120, placeholder="例如：這些文章主要討論了哪些研究方向？")
    if st.button("開始查詢", use_container_width=True):
        if not question.strip():
            st.warning("請先輸入問題。")
            return
        if count_indexed_chunks() == 0:
            st.warning("目前還沒有索引，請先建立索引。")
            return

        with st.spinner("檢索與生成答案中..."):
            try:
                result = ask_rag(question.strip(), settings)
            except Exception as exc:
                st.error(f"查詢失敗：{exc}")
                return

        st.markdown("### 回答")
        st.write(result["answer"])

        timings = result.get("timings", {})
        if timings:
            st.markdown("### 各階段耗時")
            st.write(f"總時間: {timings.get('total_sec', 0.0):.3f} 秒")
            st.write(f"LLM 前處理（中文轉英文檢索句 + 關鍵字）: {timings.get('preprocess_sec', 0.0):.3f} 秒")
            st.write(f"額外關鍵字抽取: {timings.get('keyword_extraction_sec', 0.0):.3f} 秒")
            st.write(f"Whoosh 候選檢索: {timings.get('candidate_search_sec', 0.0):.3f} 秒")
            st.write(f"Whoosh BM25F 排名: {timings.get('bm25_ranking_sec', 0.0):.3f} 秒")
            st.write(f"組合 context: {timings.get('context_build_sec', 0.0):.3f} 秒")
            st.write(f"Gemini 生成答案: {timings.get('answer_generation_sec', 0.0):.3f} 秒")

        if result["hits"]:
            st.markdown("### 使用到的 Chunks")
            for idx, hit in enumerate(result["hits"], start=1):
                st.markdown(f"**[{idx}] {hit['meta']['source']}**")
                st.caption(f"chunk_id={hit['meta']['chunk_id']}")
                st.write(hit["text"])


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ENGLISH_DOCS_DIR.mkdir(parents=True, exist_ok=True)

    st.title("NLP Final Project RAG")

    settings = load_settings()
    st.caption("Demo 模式會直接使用預先寫好的設定，不顯示左側設定欄。")

    tab1, tab2 = st.tabs(["1. 問答介面", "2. 建立索引"])
    with tab1:
        render_qa_panel(settings)
    with tab2:
        render_protected_index_panel(settings)


if __name__ == "__main__":
    main()
