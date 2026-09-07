"""
NeuroStack - Knowledge Module
RAG-ready knowledge management and retrieval.

This module provides:
- KnowledgeRetriever: Semantic search in vector database
- KnowledgeIndexer: Document indexing and chunking
- Domain models for knowledge operations
"""

from src.knowledge.indexer import KnowledgeIndexer
from src.knowledge.models import (
    KnowledgeChunk,
    KnowledgeDocument,
    SearchQuery,
    SearchResult,
)
from src.knowledge.retriever import KnowledgeRetriever

__all__ = [
    "KnowledgeRetriever",
    "KnowledgeIndexer",
    "KnowledgeChunk",
    "KnowledgeDocument",
    "SearchQuery",
    "SearchResult",
]
