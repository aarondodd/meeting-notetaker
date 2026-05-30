Also, alongside the synthesis above, please flag any references to
documents, files, or attachments that came up during the meeting --
the kind of thing someone might have shared via the meeting invite
or dropped into chat. Examples:

- "Take a look at the budget rollup spreadsheet."
- "Per slide 4 of the deck I sent..."
- "The architecture doc Bob shared yesterday."

Put your list in a section titled exactly:

```
## Referenced Attachments (auto-extracted)
```

The body should be a single fenced JSON code block containing an
array of objects:

```json
[
  {"name": "budget rollup spreadsheet", "context": "discussed when reviewing Q3 spend"},
  {"name": "architecture deck",         "context": "Bob mentioned slide 4 specifically"}
]
```

Ground rules:

- Only include items the meeting actually referenced. Don't list
  attachments that were merely present on the calendar invite if
  nobody talked about them.
- `name` is your best guess at the file / doc name as discussed
  (e.g. "Q3 budget spreadsheet", "Acme architecture deck"). It
  doesn't need to be the exact filename.
- `context` is a one-sentence note on how / when it came up in the
  conversation, so the user knows what to look for.
- Empty array `[]` is fine when the meeting didn't reference any
  shared materials.

This goes into the app's references tray so the user can match
mentioned items against the actual attachments they have. Thanks.
