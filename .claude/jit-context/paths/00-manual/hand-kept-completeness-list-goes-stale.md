---
title: "A static 'every site is guarded' check fed a hand-kept list only proves the list, not the guard"
description: "A test enumerating names in a tuple/array passes the moment the list is complete as of the day it was written, and says nothing the day a new site is added -- confirmed by a fifth marker that the guard test's own _MARKERS tuple never saw."
match: (scripts/.*\.sh|tests/.*\.py)$
---

`#653` added `marker_write_ok()` to refuse a write to a marker path that exists and is not a
regular file (a `>` on a FIFO with no reader blocks in `open(2)` before any redirection error
exists to catch), and wrapped every marker write site with it. Two places then separately claimed
completeness: the helper's own comment ("Every marker write goes through this") and a static
test whose `_MARKERS` tuple enumerated the four names the author had in mind at the time.

A fifth marker, `FAILURE_MARKER`, was written unguarded at a site the tuple never listed --
found on the very next release audit (#656), fixed in `fdd90a4` (#659) by adding the marker to
`_MARKERS` and guarding the write. The tuple was not wrong when written; it was complete as of
that day and stopped being complete the moment a new marker was declared elsewhere in the file,
with nothing forcing the two to stay in sync.

**The trap, generalized:** a check that verifies "every X is Y" by testing membership in a
hand-kept list of X's is a check that every X *on the list* is Y. It cannot see an X added after
the list was written, and it will report green regardless. If you are writing this kind of check
(a completeness assertion over a set of call sites, config keys, marker names, exported symbols,
anything enumerated by hand in a test or a comment), derive the set from the source instead: for
markers specifically, every `NAME="${DIR}/..."` assignment is a marker, and every `> "$NAME"`
write near it should be guarded -- so a new marker enters scope the moment it's declared, and the
test fails on the omission rather than trusting a list to have kept up with it.
