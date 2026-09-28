---
title: "A feature request matches none of the priority table's eleven failure classes"
description: "The triage priority table (destroys/discloses/executes/containment/forges/ships-local-state/misdirects/splices/fails-to-preserve/misreports/overexposes) enumerates failure modes only. A feature request is not a failure and matches no row, forcing a silent default to the priority:low floor."
keywords: feature request
mode: remind
---

Observed on the 2026-09-28 triage sweep (#828, a README clones-badge request): the triager
applied `priority:low` only because that is the table's stated fallback when nothing matches --
not because any row actually fit, and not from an independent severity judgment.

**For a feature request (not a bug, not a regression), say so explicitly rather than silently
falling through the failure-class table to its floor.** State in the triage note that the issue is
a feature request, that no class in the ranking table applies by construction, and give the
priority a reasoned justification (scope, requester interest, maintenance cost) instead of citing
the table's default as if it were a verdict the table produced.
