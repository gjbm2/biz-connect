---
name: feedback-ingest
description: Run the feedback roundtrip for a compose pipeline — capture reviewer comments and direct edits from the review Google Doc, assimilate them into triaged open points (one high-reasoning pass), persist them to the Notion open-points register, and digest the open gated points into a deliberation brief for the team. Use after reviewers have marked up a rendered submission and you want their feedback lifted into the pipeline so the next drafting turn addresses it. Pairs with the `doc-pipeline` and `register` skills.
allowed-tools: Bash(python *), Read, Edit, Write
---

# Feedback roundtrip (review → register → next turn)

Reviewers mark up the rendered document in Google Docs; this loop lifts that feedback back
into the pipeline as triaged, referenced **open points**, so the next draft is made *with
respect to* them. The register (Notion) is the stateful spine; nothing is silently dropped or
re-surfaced (dedupe is by comment-id).

Run all verbs through the launcher, from inside the consuming repo — or, for an umbrella repo
that hosts deliverables under `deliverables/<slug>/`, from inside the deliverable folder (the
engine then scopes the register, docs-registry and Doc binding to that `deliverables.<slug>`).
Let `final` be the rendered submission (e.g. `final/response.md`) and `FB=response/build/feedback`.

## The loop

1. **Capture** the feedback into the bundle the assimilate stage reads:
   ```bash
   BC='python "${CLAUDE_PLUGIN_ROOT}/scripts/bizconnect.py"'
   $BC gdoc comments final/response.md --out $FB/feedback.bundle.md   # anchored comments + threads
   $BC gdoc diff     final/response.md >> $FB/feedback.bundle.md       # direct edits (unified diff)
   ```
2. **Refresh** the register projection so assimilate dedupes against current rows:
   `$BC register pull`
3. **Assimilate** (the one high-reasoning pass): `$BC compose run assimilate` writes a prompt
   into `build/`. **You** run it — interpret each item, triage by **disposition** (finesse /
   tonal / rethink / research / discussion), route to the **layer** (answer / spec /
   house-position / prompt), cluster across items — and save the result (a review-cycle plan
   **plus a ```json deltas block**) to `$FB/cycle.gen.md`.
4. **Persist** to the register: `$BC register upsert $FB/cycle.gen.md` (creates/updates rows,
   dedupes by comment-id, appends History, refreshes the projection, journals the cycle).
5. **Clear the machine-executable points now** (disposition `finesse`/`tonal`): apply the
   edit / re-draft, drop the `[…: ISS-nnn …]` markers the plan specifies, then rebuild only
   what went stale (`compose status` → `compose run draft <id>` → `render`).
6. **Digest the gated points** (`rethink`/`research`/`discussion`) for the team:
   `$BC compose run digest` → write `$FB/brief.gen.md` → promote to the brief → `gdoc push`
   it (or push to the review Doc) for deliberation.
7. **Decide & close the loop.** The team works the gated rows in Notion; their agreed steps
   become source edits (answer / spec / house-position / prompt). Editing those makes the
   right `compose` targets stale; the **next** `draft`/`spec`/`critique` turn injects the open
   points for that question (`{{OPEN_POINTS}}`) and must resolve them — `critique` checks it
   did. `register resolve ISS-nnn` + `gdoc resolve` close out each point.

## Disposition → re-entry (what the plan routes to)

| disposition | the point is… | action |
|---|---|---|
| finesse | wording, no claim change | patch the answer; no re-run |
| tonal | register/emphasis off | note on spec; re-run `draft` |
| rethink | the position/argument is wrong | re-run `spec` (± house-position), then `draft` — **gated** |
| research | needs a fact/source we lack | exits to `inputs`/corpus, then `draft` — **gated** |
| discussion | ambiguity / conflict / strategic call | `[DECISION]`, escalate to the team — **gated** |

`lint` cross-checks every `[…: ISS-nnn …]` marker against the register (and vice versa), so a
point and its in-text marker can't drift apart.
