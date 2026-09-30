# Starter brain template

Source for `context-layer brain init`. It is not a vault by itself: each top-level
folder here is a *role* (inbox, projects, knowledge, ...) that `brain init` places
under the folder name of the chosen layout, and `{{folder:<role>}}`, `{{hubs}}` and
`{{credit}}` are filled in at that time. Obsidian's own template placeholders such as
`{{date}}` and `{{title}}` are left alone.

The vault rule files (CLAUDE.md, AGENTS.md, LOG.md, BACKLOG.md) come from
`templates/vault/`. `obsidian/app.json` becomes `.obsidian/app.json`.

The folder layout is adapted from Avenox Beyin (see CREDITS.md at the repository
root). All text here was written for this project.
