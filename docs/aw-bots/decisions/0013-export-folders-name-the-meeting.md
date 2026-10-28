# 0013 — Export folders are named by meeting

## Context

The exporter's folder in `aw-chatworks-transcribe` could be named by an opaque id or by the
meeting. Operators look meetings up in S3.

## Decision

The folder is `<platform>_<native meeting id>_<start UTC>` (`exporter/naming.py`), where the
native id is the Meet code, the Zoom number or the Jitsi room (owner, 2026-10-12). Room names
also stay in logs.

## Consequence

A meeting's folder can be found by its link. The native id is treated as operational metadata
in S3 keys and logs; participant names, audio and transcript text never go into logs.

Layout: [export and webhooks](../export-and-webhooks.md).
