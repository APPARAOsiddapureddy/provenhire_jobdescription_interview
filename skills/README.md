# skills/ — Company playbooks + rubrics

Versioned, git-tracked **company interview playbooks** and **scoring rubrics**, stored as
**Markdown + YAML frontmatter**. This is the data moat: de-identified, generalized playbooks
promoted from real interview runs (`{company} × {role} × {level}`).

- Each file = one skill. Frontmatter schema: see [`SCHEMA.md`](./SCHEMA.md).
- A **distill → review → promote** pipeline (WP-10) proposes deltas to a review queue;
  nothing auto-merges. Promotion bumps `version`, dedupes, and **scrubs PII**.
- Always attach provenance ("based on N reports, last verified Z"); company facts pass a
  human/LLM review gate to avoid compounding hallucinated claims.

## Pack index

<!-- PACK-INDEX:START — generated, do not edit by hand -->
| Pack | Company | Role | Level | Status | Confidence | Questions | Verified |
|---|---|---|---|---|---|---|---|
| [generic-backend-engineer-senior](./generic-backend-engineer-senior.md) | generic | backend-engineer | senior | promoted | 0.50 | 7 | 2026-07-25 |
| [generic-frontend-engineer-mid](./generic-frontend-engineer-mid.md) | generic | frontend-engineer | mid | promoted | 0.50 | 7 | 2026-07-25 |
| [generic-software-engineer-junior](./generic-software-engineer-junior.md) | generic | software-engineer | junior | promoted | 0.50 | 7 | 2026-07-25 |
| [examplecorp-backend-senior](./example-corp-backend-senior.md) | ExampleCorp | backend-engineer | senior | draft | 0.30 | 2 | 2026-06-08 |
<!-- PACK-INDEX:END -->

Regenerate after adding or editing packs:

```bash
uv --directory apps/agent run python -m proven_hire_agent.skilllib.gen_index
```

`example-corp-backend-senior.md` is a **fictional** sample showing the format.

The `generic-*.md` packs are hand-curated, company-agnostic fallbacks (matched
for any company when no company-specific pack exists) — use them as the model
for contributing new question banks (see issue #38). Set `company: generic`, a
kebab-case `role` slug, and `status: draft` on contributions; maintainers flip
status on review. See SCHEMA.md for how retrieval matches and ranks packs.

Before opening a PR, validate your pack and read the content policy
(recollection-based, generalized, de-identified — hard requirement):

```bash
pnpm proven-hire skills lint   # after `pnpm build`; see CONTRIBUTING.md §6
```
