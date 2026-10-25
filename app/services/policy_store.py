"""Policy ingestion and retrieval (the RAG half of the system).

Division of labour, stated once because it is the design decision reviewers
ask about:

* Retrieval answers "what does the policy say?"   -> this module.
* SQL/APIs answer "what is true about this order?" -> app/services/orders.py.

Retrieved text is untrusted input. It is never executed, never treated as an
instruction, and its similarity score is never evidence that a refund is valid.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import REPO_ROOT, get_settings
from app.db.models import PolicyChunk, PolicyDocument
from app.services.common import as_aware, iso, utcnow
from app.services.embedding import embed, hybrid_score
from app.services.errors import ErrorCode, ToolError

POLICY_DIR = REPO_ROOT / "app" / "policies"

# A hit must score at least this fraction of the best hit to be cited.
RELATIVE_SCORE_FLOOR = 0.55

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_SECTION_RE = re.compile(r"^##\s+\[(?P<ref>[\w.\-]+)\]\s+(?P<heading>.+)$", re.MULTILINE)

# Phrases that look like an instruction aimed at the agent rather than policy
# prose. Flagged at ingest time so an injected document is visible in the
# audit trail instead of silently influencing a proposal.
_INJECTION_PATTERNS = (
    "ignore previous",
    "ignore the above",
    "disregard the",
    "you are now",
    "system prompt",
    "approve the refund",
    "issue the refund immediately",
    "no approval is needed",
    "skip approval",
    "act as",
)


@dataclass(slots=True)
class PolicyHit:
    chunk_id: str
    document_id: str
    document_title: str
    version: str
    section_ref: str
    heading: str
    content: str
    score: float
    citation: str
    suspicious: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "document_title": self.document_title,
            "version": self.version,
            "section_ref": self.section_ref,
            "heading": self.heading,
            "content": self.content,
            "score": round(self.score, 4),
            "citation": self.citation,
            "suspicious": self.suspicious,
        }


# --------------------------------------------------------------------------- #
# Ingestion
# --------------------------------------------------------------------------- #
def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        raise ValueError("policy document is missing front matter")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, text[match.end() :]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


def _split_sections(body: str) -> list[tuple[str, str, str]]:
    matches = list(_SECTION_RE.finditer(body))
    if not matches:
        raise ValueError("policy document has no '## [section_x] Heading' sections")
    sections: list[tuple[str, str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        sections.append((match.group("ref"), match.group("heading").strip(), content))
    return sections


def _relative_uri(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT)).replace("\\", "/")
    except ValueError:
        return path.name


def _flag_injection(content: str) -> bool:
    lowered = content.lower()
    return any(pattern in lowered for pattern in _INJECTION_PATTERNS)


def ingest_file(session: Session, path: Path) -> dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(raw)
    doc_id = meta["id"]
    version = meta["version"]
    checksum = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    document = session.get(PolicyDocument, doc_id)
    if document is None:
        document = PolicyDocument(id=doc_id)
        session.add(document)
    document.title = meta.get("title", doc_id)
    document.version = version
    document.effective_from = _parse_date(meta.get("effective_from")) or utcnow()
    document.effective_to = _parse_date(meta.get("effective_to"))
    document.source_uri = meta.get("source_uri") or _relative_uri(path)
    document.checksum = checksum
    session.flush()

    session.execute(delete(PolicyChunk).where(PolicyChunk.document_id == doc_id))

    flagged: list[str] = []
    sections = _split_sections(body)
    for section_ref, heading, content in sections:
        suspicious = _flag_injection(content)
        if suspicious:
            flagged.append(f"{doc_id}:{section_ref}")
        chunk_text = f"{document.title} -- {heading}\n{content}"
        session.add(
            PolicyChunk(
                id=f"{doc_id}:{version}:{section_ref}",
                document_id=doc_id,
                section_ref=section_ref,
                heading=heading,
                content=content,
                embedding=embed(chunk_text),
                token_count=len(content.split()),
                meta={
                    "topics": [t.strip() for t in meta.get("topics", "").split(",") if t.strip()],
                    "suspicious": suspicious,
                },
            )
        )
    session.flush()
    return {
        "document_id": doc_id,
        "version": version,
        "sections": len(sections),
        "flagged_sections": flagged,
        "checksum": checksum,
    }


def ingest_all(session: Session, directory: Path | None = None) -> list[dict[str, Any]]:
    directory = directory or POLICY_DIR
    results = []
    for path in sorted(directory.glob("*.md")):
        results.append(ingest_file(session, path))
    return results


# --------------------------------------------------------------------------- #
# Retrieval
# --------------------------------------------------------------------------- #
def search(
    session: Session,
    query: str,
    *,
    top_k: int | None = None,
    as_of: datetime | None = None,
    document_ids: list[str] | None = None,
    topics: list[str] | None = None,
    min_score: float | None = None,
) -> list[PolicyHit]:
    settings = get_settings()
    top_k = top_k or settings.retrieval_top_k
    min_score = settings.retrieval_min_score if min_score is None else min_score
    as_of = as_aware(as_of) or utcnow()

    documents = {
        doc.id: doc
        for doc in session.scalars(select(PolicyDocument)).all()
        if _version_applies(doc, as_of)
    }
    if document_ids:
        documents = {k: v for k, v in documents.items() if k in document_ids}
    if not documents:
        return []

    query_vec = embed(query)
    hits: list[PolicyHit] = []
    for chunk in session.scalars(
        select(PolicyChunk).where(PolicyChunk.document_id.in_(list(documents)))
    ).all():
        if topics:
            chunk_topics = set((chunk.meta or {}).get("topics") or [])
            if not chunk_topics.intersection(topics):
                continue
        document = documents[chunk.document_id]
        text = f"{document.title} -- {chunk.heading}\n{chunk.content}"
        score = hybrid_score(query, query_vec, list(chunk.embedding), text)
        if score < min_score:
            continue
        hits.append(
            PolicyHit(
                chunk_id=chunk.id,
                document_id=document.id,
                document_title=document.title,
                version=document.version,
                section_ref=chunk.section_ref,
                heading=chunk.heading,
                content=chunk.content,
                score=score,
                citation=f"{document.id}_{document.version}:{chunk.section_ref}",
                suspicious=bool((chunk.meta or {}).get("suspicious")),
            )
        )
    hits.sort(key=lambda hit: hit.score, reverse=True)
    hits = hits[:top_k]
    if hits:
        # Relevance-gap filter. Without it, top_k always returns top_k results
        # and a clear single-section answer gets padded with loosely related
        # sections that then show up as citations on a reviewer's screen.
        cutoff = max(min_score, RELATIVE_SCORE_FLOOR * hits[0].score)
        hits = [hit for hit in hits if hit.score >= cutoff]
    return hits


def _version_applies(document: PolicyDocument, as_of: datetime) -> bool:
    start = as_aware(document.effective_from)
    end = as_aware(document.effective_to)
    if start and as_of < start:
        return False
    return not (end and as_of >= end)


def require_policy(hits: list[PolicyHit]) -> None:
    """No applicable policy -> escalate. Never invent eligibility."""
    if not hits:
        raise ToolError(
            ErrorCode.NO_APPLICABLE_POLICY,
            "No applicable policy section could be established for this request.",
        )


def detect_conflicts(hits: list[PolicyHit]) -> list[dict[str, Any]]:
    """Two documents of the same topic disagreeing is a review signal, not a
    tie to be broken by similarity score."""
    conflicts: list[dict[str, Any]] = []
    by_topic: dict[str, list[PolicyHit]] = {}
    for hit in hits:
        for topic in ("refund_eligibility", "damaged_goods", "shipping_delay"):
            if topic in hit.citation or topic in hit.heading.lower().replace(" ", "_"):
                by_topic.setdefault(topic, []).append(hit)
    for topic, group in by_topic.items():
        docs = {hit.document_id for hit in group}
        if len(docs) > 1:
            conflicts.append({"topic": topic, "documents": sorted(docs)})
    for hit in hits:
        if hit.suspicious:
            conflicts.append(
                {
                    "topic": "untrusted_content",
                    "documents": [hit.document_id],
                    "detail": f"{hit.citation} contains instruction-like text and was ignored.",
                }
            )
    return conflicts


def list_documents(session: Session) -> list[dict[str, Any]]:
    out = []
    for doc in session.scalars(select(PolicyDocument).order_by(PolicyDocument.id)).all():
        chunk_count = len(doc.chunks)
        out.append(
            {
                "document_id": doc.id,
                "title": doc.title,
                "version": doc.version,
                "effective_from": iso(doc.effective_from),
                "effective_to": iso(doc.effective_to),
                "source_uri": doc.source_uri,
                "sections": chunk_count,
                "checksum": doc.checksum[:12],
            }
        )
    return out
