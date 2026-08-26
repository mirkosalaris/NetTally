# 0002 — Exclude "Wake Requests" lines from the pmset event regex

Date: 2026-08-19 (commit ad69f75)

## Context

Almost every gap was being classified `sleep_then_full_wake`, even ones that should have been
`dark_wake_only`. `pmset -g log` contains lines like `... Wake Requests ...` (a request *to*
wake, logged for many routine background reasons) which is not the same event as an actual
`Wake` line (the system genuinely leaving sleep into full wake). The original regex matched
both, because it matched on the word `Wake` as a prefix without checking what followed.

## Decision

Add a negative lookahead, `(?!\s*Requests\b)`, to the event-type regex so a line starting
"Wake Requests" is excluded, while a bare "Wake" event line still matches.

## Consequences

- This is a narrow, targeted regex fix, not a rewrite of the classification logic.
- Any future change to the event regex should keep this exclusion and ideally add a unit test
  with a "Wake Requests" line mixed into otherwise-normal log output, to catch a regression.
