# Adopt strictspec for the owned config files (and retire the hand-rolled migration engine)

## Context

This repo owns several JSON config surfaces (the profile registry,
tokens.json, shared-settings.json, and related owned files) and maintains its
own hand-rolled numeric `_schema_version` migration replay engine in Python —
including hard-won ordering discipline (the "keep migration 2 even though
migration 4 undoes it" lesson).

strictspec (PyPI, `strictspec>=0.1.0`) provides exactly this machinery as a
toolchain: schema-driven hard-error validation, integer `format_version`
gates, and a migration engine with dry-run, chains, and
revalidate-by-construction. The hand-rolled engine is a maintained
reimplementation of it.

## Proposed work — per-DOCUMENT adoption, not per-repo

The unit of adoption is the document schema:

- IN SCOPE: the files this repo fully owns (profile registry, tokens.json,
  shared-settings.json, and any other wholly-owned JSON). Each gets a
  strictspec schema + generated validator at its load/save boundary, and its
  existing numeric `_schema_version` maps onto `format_version` (audit
  whether current version numbers can carry over 1:1; if so the gate is
  nearly free).
- EXPLICITLY OUT: any file whose schema is owned externally (per-profile
  `settings.json` follows the Claude Code schema and evolves on Anthropic's
  schedule — no version authority exists there; strictspec cannot and should
  not gate it). Leave those untouched.
- The hand-rolled migration replay engine is deleted once every owned
  document's migrations are re-expressed as strictspec migration files
  (author-supplied down ops; the ordering lesson maps onto strictspec's
  chain semantics — verify with a red-green test reproducing that exact
  ordering scenario before deleting the old engine).

## Effort

Medium — several documents, migration re-expression, and careful scoping of
owned-vs-foreign files. The deletion payoff (the whole migration engine) is
the point.
