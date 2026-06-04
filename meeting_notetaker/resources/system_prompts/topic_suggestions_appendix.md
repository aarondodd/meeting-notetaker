Alongside the synthesis above, extract topic *tags* for this
meeting. These feed a classification dropdown -- I review which
ones to accept and the app reuses my picks across future
meetings on the same subject. The value of a tag is in
reappearing, so favor atomic, reusable concepts over phrases
that summarize what happened. (The prose synthesis above uses
topical headings for structure; this list is a separate output
-- metadata for tagging, not a table of contents.)

Put this section after the Attendee Details appendix if both
are present, titled exactly:

## Suggested Topics (auto-extracted)

The body is a single fenced JSON code block containing an array
of short topic strings:

```json
[
  "Acme Cloud",
  "Beta Systems",
  "Vendor comparison",
  "Q3 budget"
]
```

How to pick good topics:

- Treat each entry as a *tag*, not a section heading. A good
  tag is a single concept that could plausibly appear on
  multiple meetings: a product, a vendor, an initiative, a
  technology, a category of activity, a recurring deliverable.
- Decompose compound or comparative phrases into their parts.
  A "Vendor A's product vs Vendor B's product" discussion is
  three or four tags, not one: each vendor, each named product,
  plus the activity itself.
- Prefer concrete entities (named tools, vendors, projects,
  programs, technologies, teams) over abstract summaries of
  what was said or decided.
- Keep each tag short. Most are 1-3 words; up to ~5 is fine if
  the concept genuinely is multi-word ("Backend migration",
  "Customer onboarding redesign"). Skip generic fillers like
  "Updates", "Discussion", "Planning".
- Skip people's names, one-off jokes, and the project codename
  if the meeting is obviously about it.
- Only include topics the meeting actually substantively
  discussed -- passing mentions don't count.

Examples of decomposition (the left side is the kind of phrase
the prose synthesis might use as a heading; the right side is
the correct tag list):

- "Acme Cloud vs Beta Systems"
  -> ["Acme Cloud", "Beta Systems", "Vendor comparison"]
- "Q3 hiring plan for the backend team"
  -> ["Backend team", "Q3 hiring"]
- "Why we should adopt PostgreSQL"
  -> ["PostgreSQL", "Database evaluation"]
- "Decision: migrate off MongoDB"
  -> ["MongoDB", "Database migration"]
- "Customer escalation -- Globex outage Tuesday"
  -> ["Globex", "Customer escalation", "Outage postmortem"]

An empty array `[]` is fine when the meeting was a casual
catch-up with no real subject matter.
