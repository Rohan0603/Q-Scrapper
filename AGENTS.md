# AGENTS.md — Lightweight GitHub Governance

Repository: `Rohan0603/Q-Scrapper`. Canonical development policy: [Rohan0603/repo-template/AGENTS.md](https://github.com/Rohan0603/repo-template/blob/main/AGENTS.md).

This repository uses a **lightweight** governance overlay. Follow applicable project-specific README/docs first for product architecture, tests and datasets. Do not replace application code or introduce unrelated workflow automation to adopt governance.

## Chief / Worker / Human

- **Chief**: investigate requirements, architecture, test evidence and blockers; prepare reviewable GitHub issues and independently assess PRs. Chief does not implement product code.
- **Worker**: implement only explicitly scoped and Chief-certified Ready issues, with tests, evidence, linked PRs and a rollback path.
- **Human**: owner-only approvals for sensitive data, credentials, releases, security, licensing, privacy and consequential decisions; track them on a `chief + human` issue.
- Primary routing is **exactly one** of `chief` and `worker`. `metadata-bootstrap` is allowed only with `worker` for specifically assigned GitHub metadata setup; it does not authorize implementation.

GitHub Issues are the work specifications. GitHub Projects v2 Status, Priority and Risk are authoritative **only after the intended Project and issue membership have been verified live**. The Project for this repository is currently **NOT VERIFIED**; do not claim an issue is `Ready` or `Done` on Project evidence until actual field read-back. Historical Linear references are provenance, not the live work queue.

## Mandatory work gates

1. Read the latest default branch and local README/instructions; inspect the issue and its comments, labels, dependencies, scope, acceptance and active work.
2. Never use a GitHub event to automatically launch Chief, Worker, Codex, OpenCode or other agents. The owner invokes or schedules them manually.
3. Chief only routes an issue to `worker` after a complete Definition of Ready: unambiguous outcome, current code SHA, dependencies resolved, implementation/test plan, security/privacy implications, rollback, and verified Project membership/Status/Priority. Otherwise keep under Chief or an explicit metadata-only bootstrap.
4. Worker records START, test outputs/exit codes, PR head SHA, limitations and review handoff. A failing test is FAILED; unexecuted tests are NOT RUN.
5. Preserve existing project behavior and stronger product-specific requirements. Use a normal PR with exact-head review and applicable GitHub protection checks. No force pushes, secret exposure or fabricated approvals.

## Repository-specific focus

Scraping utility: respect access constraints, rate limits, terms, data privacy and reproducible fixtures.

This file does not assert that branch protection or Projects v2 has been activated; those require separate provider read-back.
