"""
Knowledge Base RAG — downloads all .md files from GitHub repo,
chunks them, builds embeddings index, and retrieves relevant context
for bug analysis.
"""
import os
import logging
import asyncio
import aiohttp
from openai import AsyncOpenAI
from agent3_pm.task_agent import _get_client

logger = logging.getLogger(__name__)

KB_REPO = "Deci1337/mai-knowledge-base"
KB_API = f"https://api.github.com/repos/{KB_REPO}/git/trees/main?recursive=1"
KB_RAW = f"https://raw.githubusercontent.com/{KB_REPO}/main"

CHUNK_SIZE = 800
CHUNK_OVERLAP = 100
SKIP_FOLDERS = {"user/calls", "user/bugs", "calls/tasks"}

_chunks: list[dict] = []  # [{"text": ..., "file": ..., "embedding": [...]}]
_initialized = False


def _split_text(text: str, filename: str) -> list[dict]:
    lines = text.split("\n")
    chunks = []
    current = []
    current_len = 0

    for line in lines:
        line_len = len(line)
        if current_len + line_len > CHUNK_SIZE and current:
            chunk_text = "\n".join(current)
            chunks.append({"text": chunk_text, "file": filename})
            overlap_lines = []
            overlap_len = 0
            for l in reversed(current):
                if overlap_len + len(l) > CHUNK_OVERLAP:
                    break
                overlap_lines.insert(0, l)
                overlap_len += len(l)
            current = overlap_lines
            current_len = overlap_len
        current.append(line)
        current_len += line_len

    if current:
        chunks.append({"text": "\n".join(current), "file": filename})

    return chunks


async def _fetch_tree() -> list[dict]:
    try:
        async with aiohttp.ClientSession() as http:
            headers = {"Accept": "application/vnd.github.v3+json"}
            gh_token = os.getenv("GITHUB_TOKEN", "")
            if gh_token:
                headers["Authorization"] = f"token {gh_token}"
            async with http.get(KB_API, headers=headers) as resp:
                if resp.status != 200:
                    logger.warning(f"KB tree fetch failed: {resp.status}")
                    return []
                data = await resp.json()
                return [f for f in data.get("tree", [])
                        if f["type"] == "blob"
                        and f["path"].endswith(".md")
                        and not any(f["path"].startswith(skip) for skip in SKIP_FOLDERS)]
    except Exception:
        logger.exception("Failed to fetch KB tree")
        return []


async def _fetch_file(path: str) -> str:
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{KB_RAW}/{path}") as resp:
                if resp.status != 200:
                    return ""
                return await resp.text()
    except Exception:
        logger.exception(f"Failed to fetch KB file {path}")
        return ""


async def _get_embeddings(texts: list[str]) -> list[list[float]]:
    client = _get_client()
    batch_size = 100
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i:i + batch_size]
        resp = await client.embeddings.create(
            model="text-embedding-3-small",
            input=batch,
        )
        all_embeddings.extend([d.embedding for d in resp.data])
    return all_embeddings


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def init_kb():
    global _chunks, _initialized
    logger.info("Initializing KB context (RAG)...")

    files = await _fetch_tree()
    if not files:
        logger.warning("No KB files found")
        _initialized = True
        return

    logger.info(f"Found {len(files)} KB files, downloading...")

    all_chunks = []
    for f in files:
        content = await _fetch_file(f["path"])
        if content:
            chunks = _split_text(content, f["path"])
            all_chunks.extend(chunks)

    if not all_chunks:
        logger.warning("No chunks created from KB files")
        _initialized = True
        return

    logger.info(f"Created {len(all_chunks)} chunks, building embeddings...")

    texts = [c["text"] for c in all_chunks]
    embeddings = await _get_embeddings(texts)

    for chunk, emb in zip(all_chunks, embeddings):
        chunk["embedding"] = emb

    _chunks = all_chunks
    _initialized = True
    logger.info(f"KB context ready: {len(_chunks)} chunks from {len(files)} files")


async def search_kb(query: str, top_k: int = 5) -> list[dict]:
    if not _chunks:
        return []

    query_emb = (await _get_embeddings([query]))[0]

    scored = []
    for chunk in _chunks:
        sim = _cosine_sim(query_emb, chunk["embedding"])
        scored.append((sim, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [{"text": c["text"], "file": c["file"], "score": round(s, 3)}
            for s, c in scored[:top_k]]


async def get_context_for_complaint(complaint_text: str) -> str:
    results = await search_kb(complaint_text, top_k=5)
    if not results:
        return ""

    parts = ["КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ КОМПАНИИ (используй для понимания проблемы):\n"]
    for r in results:
        parts.append(f"--- {r['file']} (релевантность: {r['score']}) ---")
        parts.append(r["text"])
        parts.append("")

    return "\n".join(parts)
