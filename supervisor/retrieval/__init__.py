"""Retrieval Quality + Evidence Control Layer for SentinelAI.

Modules:
  bm25              — Pure-Python BM25 scorer (Robertson et al.)
  source_confidence — Source type tiers + staleness decay
  hybrid_retriever  — Fuses BM25 + TF-IDF cosine + source confidence
  reranker          — Rule-based cross-encoder reranker
  retrieval_cache   — TTL in-memory cache with disk persistence
  telemetry         — Structured retrieval telemetry log
"""
