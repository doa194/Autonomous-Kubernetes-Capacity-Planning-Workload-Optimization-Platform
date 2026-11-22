"""S18 operational knowledge base: governed ingestion and authorization-aware hybrid search.

Ingestion order is fixed (S09 §7.1): principal/source allowlist -> classification check ->
redact -> chunk -> embed (optional) -> store. Restricted documents are refused outright.

Search order is fixed too: the caller's authorized scope, classification ceiling, active
state and time validity are applied in SQL *before* lexical or vector ranking, so a
document outside scope can never influence ranks, counts, snippets or pagination. The two
ranked lists are fused with Reciprocal Rank Fusion and then re-ordered deterministically by
collection precedence and recency. Every result is labelled untrusted historical evidence.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field

from capacity_platform.contracts.canonical import digest
from capacity_platform.contracts.errors import ErrorCategory, PlatformError
from capacity_platform.shared.db import Connection
from capacity_platform.shared.redaction import REDACTION_REVISION, redact

CHUNKER_REVISION = "markdown-sections-v1"
MAX_CHUNK_CHARS = 1200
RRF_K = 60
COLLECTION_PRECEDENCE = {"verified_outcomes": 0, "incidents": 1, "runbooks": 2, "configuration_history": 3}
CLASS_ORDER = {"Public": 0, "Internal": 1, "Confidential": 2}
ALLOWED_INGEST_PRINCIPALS = {"knowledge-curator", "executor-outcome-feed", "scenario-evidence-feed"}


class SourceDocument(BaseModel):
    documentId: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,80}$")
    collection: Literal["runbooks", "incidents", "configuration_history", "verified_outcomes"]
    title: str = Field(max_length=200)
    clusterId: str
    namespaceScope: str
    workloadId: str | None = None
    classification: Literal["Public", "Internal", "Confidential", "Restricted"]
    sourceKind: str
    validFrom: datetime
    validTo: datetime | None = None
    syntheticOrigin: bool = False
    body: str = Field(max_length=200_000)


class IngestResult(BaseModel):
    documentId: str
    revision: int
    chunks: int
    redactions: list[str]
    contentDigest: str
    embedded: bool


@dataclass(frozen=True)
class Chunk:
    ordinal: int
    start: int
    end: int
    text: str


def chunk_markdown(text: str) -> list[Chunk]:
    """Split on headings, then hard-wrap long sections, keeping exact source offsets."""
    boundaries = [m.start() for m in re.finditer(r"(?m)^#{1,4} ", text)] or [0]
    if boundaries[0] != 0:
        boundaries.insert(0, 0)
    boundaries.append(len(text))
    chunks: list[Chunk] = []
    for a, b in itertools.pairwise(boundaries):
        pos = a
        while pos < b:
            end = min(b, pos + MAX_CHUNK_CHARS)
            if end < b:
                cut = text.rfind("\n", pos, end)
                end = cut if cut > pos + 200 else end
            piece = text[pos:end].strip()
            if piece:
                chunks.append(Chunk(len(chunks), pos, end, piece))
            pos = end
    return chunks


class KnowledgeStore:
    def __init__(self, conn: Connection, embed=None, embedding_model: str | None = None) -> None:
        self.conn, self._embed, self._embedding_model = conn, embed, embedding_model

    def ingest(self, doc: SourceDocument, principal: str) -> IngestResult:
        if principal not in ALLOWED_INGEST_PRINCIPALS:
            raise PlatformError("ingest_principal_denied", ErrorCategory.AUTHORIZATION, "ingestion not permitted")
        if doc.classification == "Restricted":
            raise PlatformError(
                "restricted_content_refused", ErrorCategory.VALIDATION, "Restricted documents are never ingested"
            )
        clean, hits = redact(doc.body)  # redact BEFORE chunking and embedding
        content_digest = digest({"body": clean, "title": doc.title})
        prev = self.conn.execute(
            "SELECT revision, content_digest FROM knowledge.source_document WHERE document_id = %s "
            "ORDER BY revision DESC LIMIT 1",
            (doc.documentId,),
        ).fetchone()
        if prev and prev["content_digest"] == content_digest:
            return IngestResult(
                documentId=doc.documentId,
                revision=prev["revision"],
                chunks=0,
                redactions=hits,
                contentDigest=content_digest,
                embedded=False,
            )
        revision = (prev["revision"] + 1) if prev else 1
        if prev:
            self.conn.execute(
                "UPDATE knowledge.source_document SET state = 'superseded', valid_to = now() "
                "WHERE document_id = %s AND state = 'active'",
                (doc.documentId,),
            )
        self.conn.execute(
            "INSERT INTO knowledge.source_document (document_id, revision, collection, title, cluster_id, "
            "namespace_scope, workload_id, classification, source_kind, ingest_principal, content_digest, "
            "redaction_rev, valid_from, valid_to, state, synthetic_origin) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)",
            (
                doc.documentId,
                revision,
                doc.collection,
                doc.title,
                doc.clusterId,
                doc.namespaceScope,
                doc.workloadId,
                doc.classification,
                doc.sourceKind,
                principal,
                content_digest,
                REDACTION_REVISION,
                doc.validFrom,
                doc.validTo,
                doc.syntheticOrigin,
            ),
        )
        chunks = chunk_markdown(clean)
        vectors = None
        if self._embed is not None and chunks:
            try:
                vectors = self._embed([c.text for c in chunks])
            except Exception:
                vectors = None  # lexical-only degraded mode; embeddings can be backfilled later
        for c in chunks:
            vec = vectors[c.ordinal] if vectors else None
            self.conn.execute(
                "INSERT INTO knowledge.chunk (chunk_id, document_id, revision, ordinal, char_start, char_end, content, "
                "embedding, embedding_model, chunker_rev) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    f"{doc.documentId}:{revision}:{c.ordinal}",
                    doc.documentId,
                    revision,
                    c.ordinal,
                    c.start,
                    c.end,
                    c.text,
                    _vector_literal(vec),
                    self._embedding_model if vec else None,
                    CHUNKER_REVISION,
                ),
            )
        return IngestResult(
            documentId=doc.documentId,
            revision=revision,
            chunks=len(chunks),
            redactions=hits,
            contentDigest=content_digest,
            embedded=vectors is not None,
        )

    def revoke(self, document_id: str) -> None:
        """Revoked sources disappear from retrieval immediately (state filter), before any purge."""
        self.conn.execute(
            "UPDATE knowledge.source_document SET state = 'revoked', valid_to = now() "
            "WHERE document_id = %s AND state IN ('active', 'quarantined')",
            (document_id,),
        )

    def search(
        self,
        *,
        query: str,
        cluster_id: str,
        namespaces: list[str],
        max_classification: str,
        collections: list[str] | None = None,
        top_k: int = 5,
        at: datetime | None = None,
        query_vector: list[float] | None = None,
    ) -> dict:
        if not namespaces:
            return {"results": [], "mode": "empty_scope"}
        top_k = max(1, min(top_k, 10))
        at = at or datetime.now(UTC)
        allowed_classes = [c for c, o in CLASS_ORDER.items() if o <= CLASS_ORDER[max_classification]]
        collections = collections or list(COLLECTION_PRECEDENCE)
        # Authorized prefilter: scope, class ceiling, active state, temporal validity.
        prefilter = """
            d.state = 'active' AND d.cluster_id = %(cluster)s
            AND (d.namespace_scope = ANY(%(ns)s))
            AND d.classification = ANY(%(cls)s) AND d.collection = ANY(%(col)s)
            AND d.valid_from <= %(at)s AND (d.valid_to IS NULL OR d.valid_to > %(at)s)
        """
        params = {
            "cluster": cluster_id,
            "ns": namespaces,
            "cls": allowed_classes,
            "col": collections,
            "at": at,
            "q": query[:500],
            "lim": top_k * 4,
        }
        lexical = self.conn.execute(
            f"""SELECT c.chunk_id FROM knowledge.chunk c
                JOIN knowledge.source_document d USING (document_id, revision)
                WHERE {prefilter} AND c.content_tsv @@ websearch_to_tsquery('english', %(q)s)
                ORDER BY ts_rank_cd(c.content_tsv, websearch_to_tsquery('english', %(q)s)) DESC, c.chunk_id
                LIMIT %(lim)s""",
            params,
        ).fetchall()
        vector_rows: list = []
        mode = "lexical_only"
        if query_vector is not None:
            params["v"] = _vector_literal(query_vector)
            vector_rows = self.conn.execute(
                f"""SELECT c.chunk_id FROM knowledge.chunk c
                    JOIN knowledge.source_document d USING (document_id, revision)
                    WHERE {prefilter} AND c.embedding IS NOT NULL
                      AND vector_dims(c.embedding) = vector_dims(%(v)s::vector)
                    ORDER BY c.embedding <=> %(v)s::vector, c.chunk_id LIMIT %(lim)s""",
                params,
            ).fetchall()
            mode = "hybrid"
        scores: dict[str, float] = {}
        for ranked in (lexical, vector_rows):
            for rank, row in enumerate(ranked):
                scores[row["chunk_id"]] = scores.get(row["chunk_id"], 0.0) + 1.0 / (RRF_K + rank + 1)
        if not scores:
            return {"results": [], "mode": mode}
        rows = self.conn.execute(
            """SELECT c.chunk_id, c.content, c.char_start, c.char_end, d.document_id, d.revision, d.title,
                      d.collection, d.namespace_scope, d.workload_id, d.classification, d.content_digest,
                      d.valid_from, d.synthetic_origin, d.redaction_rev
               FROM knowledge.chunk c JOIN knowledge.source_document d USING (document_id, revision)
               WHERE c.chunk_id = ANY(%s)""",
            (list(scores),),
        ).fetchall()
        rows.sort(
            key=lambda r: (
                -round(scores[r["chunk_id"]], 9),
                COLLECTION_PRECEDENCE[r["collection"]],
                -r["valid_from"].timestamp(),
                r["chunk_id"],
            )
        )
        results = [
            {
                "chunkId": r["chunk_id"],
                "documentId": r["document_id"],
                "documentRevision": r["revision"],
                "title": r["title"],
                "collection": r["collection"],
                "namespaceScope": r["namespace_scope"],
                "workloadId": r["workload_id"],
                "classification": r["classification"],
                "contentDigest": r["content_digest"],
                "offsets": [r["char_start"], r["char_end"]],
                "validFrom": r["valid_from"].isoformat(),
                "syntheticOrigin": r["synthetic_origin"],
                "evidenceKind": "retrieved_document",
                "trust": "untrusted_historical_evidence",
                "score": round(scores[r["chunk_id"]], 6),
                "snippet": r["content"][:600],
            }
            for r in rows[:top_k]
        ]
        return {"results": results, "mode": mode, "retrievalRevision": f"rrf-k{RRF_K}-v1"}


def _vector_literal(vec: list[float] | None) -> str | None:
    if vec is None:
        return None
    return "[" + ",".join(f"{float(x):.7g}" for x in vec) + "]"
