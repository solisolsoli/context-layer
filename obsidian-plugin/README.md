# Context Layer Brain View

Context Layer Brain View is an Obsidian plugin that draws a 3D view of your
vault's explicit link graph and, on top of it, the activation trace of the last
`context-layer` synaptic retrieval: which notes the retrieval started from,
which linked notes it reached, which links it followed, and which notes ended
up in the evidence packet.

The brain words are names, not a mechanism. A "neuron" in this view is a note,
a "synapse" is an explicit link between two notes (a wikilink, embed, Markdown
link or frontmatter link that Obsidian resolved), and "synaptic" is the name of
a `context-layer` retrieval method: full-text search picks seed notes, then a
bounded spreading-activation step follows explicit links a few hops out and
gives each reached note an activation score. Nothing in this plugin is a
learned model, and no model runs inside it. The view shows the link graph and
records of one retrieval; it does not show how an AI model reasons.

A lit-up graph does not mean the retrieval was good. It shows what the
retrieval touched, not whether that was the right evidence for your question.

## What you see

- **Notes as points.** Linked notes fill a sphere. A layout worker in the
  background treats every resolved link as a spring that pulls its two notes
  together (links of notes with many links pull more softly), makes notes
  repel each other, and keeps the sphere evenly filled with a radial term that
  pulls each note toward the radius its rank from the centre would have in an
  evenly filled sphere, so it does not shuffle notes. Notes that link to each
  other therefore sit together, and groups of linked notes form visible
  regions. Notes without links form a faint outer shell (can be hidden).
- **A stable map.** Without a saved layout, the same vault always gives the
  same layout, whatever order Obsidian lists the files in: it depends only on
  the notes' paths and links. When links change, only notes that gained a link
  and new notes are placed again (a new note next to the notes it links to);
  every other note stays exactly where it is, and a note that loses its last
  link moves to the shell. A renamed note keeps its place. Unlinked notes keep
  their shell slots as other unlinked notes come and go (a new one rarely
  displaces another), except when the shell grows (each time the number of
  unlinked notes passes 80% of the slots), which reassigns the slots.
- **Links as curved ribbons.** Every ribbon is one link that Obsidian resolved
  in `metadataCache.resolvedLinks`. Nothing is inferred or invented.
- **Colour** by link count (a fixed scale, so a note keeps its colour when
  unrelated notes change) or by region (top-level folder or first tag). The
  eight largest regions get one of eight colours chosen to stay apart under
  the common colour-vision deficiencies (see Accessibility); smaller regions
  share "Other". No folder or tag names are built in.
- **Orbit, zoom, hover, click, keys.** Drag to rotate, scroll to zoom, hover
  for the note's path and neighbours, click to open the note in a new tab
  (modifier-clicks follow Obsidian's conventions for a split or a new window).
  The canvas takes keyboard focus: arrow keys turn the view, plus and minus
  zoom, Home resets it, Escape leaves a focused region. In region mode, the
  legend entries are buttons: click one (or press Enter on it) to turn toward
  that region and dim the rest.
- **Retrieval overlay**, **advisor overlay** and, off by default, the
  **advisor layer** panel (described below).

## Install

The plugin is not in the community plugin list. Install it manually:

1. Create the folder `<your vault>/.obsidian/plugins/context-layer-brain/`.
2. Copy three files into it: `dist/main.js` (as `main.js`), `manifest.json` and
   `styles.css`.
3. In Obsidian, open Settings, Community plugins, turn off Restricted mode if
   needed, and enable **Context Layer Brain View**.
4. Open it from the ribbon (brain icon) or the command palette:
   **Context Layer Brain View: Open view**.

A BRAT-style install works the same way if a release attaches those three
files (`main.js`, `manifest.json`, `styles.css`) as assets.

The committed `dist/main.js` is built from `src/` by `build.py`; you do not need
to build anything to install.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| Open on startup | off | Open the view when the vault loads. |
| Colour notes by | Link count | Link count or Region. |
| Regions come from | Top-level folder | Top-level folder or first tag (frontmatter tags first, then inline). |
| Show unlinked notes | on | Draw notes without links as the outer shell. Notes of the shown retrieval are drawn either way. |
| Link thickness | 1 | Ribbon width multiplier. |
| Bloom | off | Soft glow; costs extra GPU time. Bloom strength appears when on. |
| Slow auto-rotation | on | Rotate slowly after a few seconds without input. |
| Respect reduced motion | on | When the system asks for reduced motion, stop rotation, breathing and pulses. |
| Show activation overlay | on | Show the last retrieval (see below). |
| Activation file | `.context/activation.json` | Vault-relative path of the trace. Only an `activation*.json` file directly inside a `.context` folder is accepted. |
| Freshness window | 10 min | Older traces are not drawn. From 1 to 120 minutes; a larger value in `data.json` is clamped to 120. |
| Show advisor (shadow) | off | Draw advisor verdicts that were not applied, labelled "would" (see below). |
| Show advisor layer | off | A read-only panel about what the advisor did in the last retrieval (see "The advisor layer"). Also switched by the button at the bottom right of the view. |
| Remember layout | on | Save the settled positions of linked notes in the plugin's data file. The next time the view opens, a note whose links did not change is drawn where it was, and nothing is solved when no links changed. |
| Developer diagnostics | off | Show frame rate, frame time and layout state in the corner. Local only. |

## The activation overlay

After a synaptic retrieval, `context-layer` writes one small file,
`<vault>/.context/activation.json`. That happens for
`context-layer search --method synaptic`, for the MCP `search_vault` tool with
method `synaptic`, and for the prompt hook when it uses the synaptic method. See
the `context-layer` documentation for how to run those.

The plugin notices the new file within about a second and, while the trace is
fresh (by default generated in the last 10 minutes):

- **Seeds** (notes found by full-text search, hop 0) are drawn in warm amber.
- **Hop notes** (reached by following links) are drawn in cyan that whitens
  with the activation score. Size also grows with the score.
- **In the packet vs reached only.** Notes that contributed a passage to the
  evidence packet are bright; notes that were reached but not used are muted
  grey-blue and fainter. A key under the HUD line explains the styles.
- **Traversed links** are redrawn on top and pulse in the order the retrieval
  followed them: the links followed at the first step, then those followed at
  the second step, then a pause, then again. A link pulses at the step that
  followed it, even when it leads back to a note found earlier. A link and its
  reverse are one ribbon. Only the explicit link kinds in the trace contract
  (`wikilink`, `embed`, `mdlink`, `frontmatter`, `backlink`) are drawn;
  anything else is dropped.
- **Everything else dims**, so the retrieval stands out.

The HUD line reads, with parts separated by a middle dot:
`Last retrieval at <generated_at> . <method> [<mode>] . <passages> passages .
~<est_tokens> tokens . <age>`, followed when it applies by:

- `no evidence found` when the packet status is `NOT_FOUND`. The status
  `PARTIAL` (some evidence was found; it may not answer the question) adds
  nothing, because the passage count already says so, and `OK`, written by
  older traces, is read the same way. Any other status is shown as written.
- `<m> of <n> notes in this vault` when some notes of the trace are not notes
  of this vault.

`<mode>` appears when the trace says which packet it describes: `superset`
(the full-text packet plus graph extras) or `compact`. For a compact packet the
key adds that it can leave out evidence the full-text packet would carry.

The overlay is not shown, and nothing is dimmed, in these cases, and the HUD
says which one applies:

- the trace lists no notes (for example `NOT_FOUND`);
- none of the trace's notes is in this vault (`0 of <n> notes are in this
  vault`), for example because `context-layer` indexed a different folder than
  the one Obsidian opened;
- the trace is older than the freshness window (`stale`);
- you cleared it (`cleared`).

Paths are compared after Unicode normalization (NFC) on both sides, so a note
whose name was written in decomposed form still matches.

The activation score is shown only as a number between 0 and 1 in the hover
tooltip ("activation score 0.42"). It is not a percentage, a probability or a
confidence, and the plugin never presents it as one.

Commands:

- **Focus last retrieval** brings the view to the front, then turns and zooms
  the camera onto the activated notes. When there is nothing to focus, a notice
  says why (no trace yet, overlay turned off, cleared, stale, no notes, or not
  in this vault).
- **Clear activation** hides the current trace for the rest of the session. A
  trace with a newer `generated_at` or a different `run_id` shows again.

A missing, partial, oversized (over 512 KB), wrong-version or malformed file is
ignored without an error. Overlay links come from the trace, so a traversed
link that Obsidian itself does not resolve (for example some frontmatter
relations) is still drawn between its two notes.

## The advisor overlay

`context-layer` has an optional advisor (off by default) that can judge
retrieved notes. When the trace carries an advisor block (`jev`), the plugin
reads only its enums, booleans and counters: `mode` (`off`, `shadow`, `on`),
`applied`, `superset`, `provider_kind`, `gate_passed`, `kept`, `flagged`,
`rescued`, `degraded`, and per note one verdict (`rescued`, `on_topic`,
`off_topic`, `local_only` or `not_judged`). Any other field, text or number is
ignored.

- **Applied verdicts** (mode `on`, applied): a rescued note gets a solid ring;
  a note judged off-topic is dimmed and gets a dashed ring. The HUD adds
  `advisor on . rescued <n> . flagged <n> . kept <n>`.
- **Verdicts that were not applied** (mode `shadow`, or `on` without
  `applied`): nothing is drawn unless **Show advisor (shadow)** is on. Then the
  rings are faint, never change a note's colour, the HUD says
  `advisor shadow, not applied . would rescue <n> . would flag <n>`, and the
  key, tooltips and summary say "would".
- The key says "advisor judgement, not evidence".
- A trace without the block, or with mode `off`, is drawn exactly as without
  an advisor.

The plugin never calls the advisor or any model and makes no network request;
it only draws what the trace records.

## The advisor layer

A read-only panel, hidden by default, that says what the optional advisor did
in the last retrieval. Turn it on with the **Show advisor layer** button at the
bottom right of the view or with the setting of the same name; the choice is
kept in the plugin's data file. It is separate from the overlay marks above and
changes nothing that the overlay draws.

It uses only what the activation trace records (the advisor block's enums,
booleans and counters, and one verdict per listed note), so it shows:

- the mode, and whether it was applied to the packet: `on, applied`, `on, but
  not applied`, or `shadow, not applied (the packet was not changed)`;
- the provider kind, the counters the writer records (rescued, flagged off
  topic, kept; for a run that was not applied, "would rescue" and "would flag
  off topic"; a trace from an older writer has no "would rescue" counter and the
  line is then left out), the
  topic-gate result, "not a superset of the fts packet" and "degraded" when the
  trace says so, and how many of the listed notes carry each verdict (only the
  notes in the trace, which is capped at 200);
- **Notes the advisor rescued**: the notes it added to the packet, each a button
  that opens the note. A note that is not in this vault is listed without a
  button;
- for a run that was not applied, **Judged on topic, not in the packet**: notes
  the advisor judged on topic that the packet did not carry. This is derived
  from two trace fields (verdict `on_topic`, `selected: false`) and is capped
  with the trace; the "Would rescue" line above is the writer's own counter
  (`would_rescue` in the trace's advisor block), not a count of this list.

It always ends with "Advisory only: the advisor's judgement is not evidence and
not a check of correctness."

When there is nothing to show it says why instead of staying blank: no trace
yet, the activation overlay turned off, the trace cleared or older than the
freshness window, a trace with no advisor data (the advisor is off, which is the
default, or that search was not a synaptic search with tracing on), or an
advisor block with mode `off`.

The layer reads the same single trace file as the overlay. It does not read the
advisor's call log or configuration, calls no advisor or model, never reads a
key and makes no network request. To try it without an advisor installed, use
the `--advisor` option of the demo script below.

## Accessibility

- **Keyboard.** The canvas, the legend entries and the retrieval summary are
  reachable with Tab. See "What you see" for the canvas keys.
- **Retrieval summary.** A collapsible panel lists the last retrieval as text:
  its status line and every activated note with its hop, role, packet
  membership and any advisor verdict. Each entry is a button that opens the
  note. The status is also written to a polite live region, which announces a
  change of state (not the ticking age).
- **Colour.** Every pair of region colours, "Other" included, stays at least
  10 CIEDE2000 units apart under simulated protanopia, deuteranopia and
  tritanopia (Machado et al. 2009, full severity), both as drawn on the black
  stage and as legend dots (`tests/palette-cvd.test.js`). Colour is never the
  only cue: the legend names every region and can isolate it, overlay styles
  also differ in size and brightness, and advisor marks differ in shape.
- **Motion.** Reduced motion is respected (see Settings).
- **Theme.** The 3D stage is dark by design: the renderer draws light on
  black. The text layers on top of it (HUD, legend, summary, tooltip) use CSS
  variables such as `--nb-text` and `--nb-panel`, and Obsidian's font, size
  and radius variables, so a theme or CSS snippet can restyle them.

## Privacy

What the plugin reads:

- The list of files and folders in the vault, and each note's path and name,
  through Obsidian's vault API.
- Obsidian's resolved-link table (`metadataCache.resolvedLinks`). Note contents
  are never read.
- In "first tag" region mode only: the tags in Obsidian's metadata cache.
- The trace file `.context/activation.json`, through `app.vault.adapter`
  (`stat` about once a second while the view is visible and when the window
  gains focus; `read` only when its size or modification time changed). Obsidian
  does not index dot folders, so the file is not reachable as a normal note.
  From the file the plugin uses `version`, `generated_at`, `run_id`, `method`,
  `mode`, the node fields `path`, `activation`, `hop`, `role`, `selected` and
  `jev`, the edge fields `from`, `to`, `kind`, `weight` and `hop` (or
  `depth`),
  `packet.passages`, `packet.est_tokens`, `packet.status`, and the enum and
  counter fields of the advisor block listed above. `budget_tokens` is parsed
  but not shown.
- Query text: `context-layer` writes `"query": null` by default, so the file
  does not contain your prompt unless you opted in to recording it. Even when a
  query (or a query hash) is present, the plugin ignores it and never displays
  it.

What the plugin writes: nothing in your notes and nothing in `.context/`. The
only file it writes is its own Obsidian plugin data file,
`.obsidian/plugins/context-layer-brain/data.json`, which holds the settings and,
with **Remember layout** on, the 3D position of each linked note keyed by the
note's path, with a 32-bit hash of the note's neighbour paths for settled
positions. Turn **Remember layout** off to keep note paths out of that file.

Network: none. The plugin makes no network requests and loads no remote code
or fonts. The layout worker is created from code inside the plugin.

These properties are checked by tests in this directory: a static test scans
`src/` for network calls, vault writes, file reads other than the trace, and
inline styles; the end-to-end test asserts that the plugin never writes
through the vault adapter; and a view test runs with network functions that
fail if called.

## Performance

Rendering uses WebGL 1 with instanced ribbons (one GPU instance per link) and a
layout worker, so the main thread mostly uploads buffers.

- An earlier build of this renderer was tested on a ~7,000-note vault on a
  2022 laptop-class Apple Silicon machine at about 60 FPS. That measurement is not
  reproduced by any test in this repository, was not repeated after the 0.3.0
  refactor, and should not be read as a promise for other vaults or machines.
- Known limitation: creating the WebGL context can take more than 50 ms on the
  first open on some machines (about 60 ms was measured on that same
  machine), which can show as a single long task when the view opens.
- Opening the view walks the vault and builds the link plan in small slices so
  Obsidian stays responsive; the automated tests check that this yields to the
  event loop on a generated 5,000-note vault, not how long it takes.
- Vault changes (create, delete, rename, metadata updates) do constant or
  per-link work when they arrive; the graph is rebuilt at most once about
  150 ms after the last change, and at least once a second during a long burst.
  While the view is hidden, changes only mark it out of date, and it is rebuilt
  once when shown. `node tests/view.test.js` prints the cost of 1,000 delete and
  1,000 rename events on a generated 10,000-note vault.
- The layout worker stops after 300 iterations for a full solve and 150 for an
  incremental one. `node tests/layout.test.js` prints the time it takes for a
  7,000-note fixture.
- Rendering and the layout worker pause while the view is in a background tab
  or the window is hidden.

## Prior art

Obsidian's built-in **Graph view** and **Local graph** already show the link
graph, including a depth-limited neighbourhood around the current note; this
plugin does not replace them. Several community plugins draw the vault as a 3D
graph, some with brain or neuron styling. The community plugin **Neural Vault**
lights up notes in Obsidian's 2D graph as Claude Code reads them. The layout
uses well-known techniques: force-directed placement with Barnes-Hut
repulsion, and a Hilbert curve for the starting placement (see
[THIRD_PARTY.md](THIRD_PARTY.md)). What this plugin adds is narrower: it draws
the activation trace that a `context-layer` retrieval wrote (seeds, hops,
traversed explicit links, packet membership and token estimate) for evidence
that `context-layer` pins by content hash, in its own 3D view.

## Limitations

- Desktop only (`isDesktopOnly: true`). The code uses no Node or Electron APIs,
  but it has not been tried on mobile and its GPU budget assumes a desktop.
- Needs WebGL with the `ANGLE_instanced_arrays` extension to draw links;
  without it only notes are drawn and the counters line says so, and without
  WebGL the view shows a message.
- The overlay shows at most 200 notes and 400 links (the trace contract caps).
- Trace changes are picked up by polling, so there is up to about a second of
  delay, and none while the view is hidden (it catches up when shown).
- The trace must come from the vault folder Obsidian opened; a trace written
  for a parent or child folder shows as `0 of <n> notes`.
- A vault shaped like a few hubs with thousands of links each forms one
  cluster per hub, so its sphere is filled less evenly than a vault of many
  smaller groups.
- The automated tests run against stand-ins for Obsidian, the DOM, WebGL and
  Web Workers. They check behaviour and wiring, not pixels; the GLSL sources
  are checked statically, not compiled; live rendering in Obsidian is checked
  by hand.

## Try it on a fictional vault

```sh
python3 demo/make-demo-vault.py /path/to/new/empty/folder --notes 400
```

This writes generated notes with links and tags plus a fresh sample
`.context/activation.json`. Open the folder as a vault, install the plugin as
above, and open the view; the sample trace is fresh for 10 minutes (run the
script again on the same folder to refresh it). Add `--advisor on` or
`--advisor shadow` to include a sample advisor block.

## Development

No npm packages are used. Python 3 builds, plain Node runs the tests:

```sh
python3 build.py            # bundle src/*.js into dist/main.js
python3 build.py --check    # fail if dist/main.js is out of date
node tests/run-all.js       # all tests, non-zero exit on failure
```

`tests/contract.test.js` reads traces written by the real `context-layer`
writer on the fictional development vault. From the repository root,
`python3 obsidian-plugin/tests/fixtures/make_writer_traces.py` regenerates
them, and `--check` fails when the writer's trace format has changed.
`tests/advisor-layer.test.js` does the same for advisor-annotated traces
(`writer-trace-jev-on.json`, `writer-trace-jev-shadow.json`), produced by the
real advisor code with a scripted provider by
`python3 obsidian-plugin/tests/fixtures/make_jev_writer_traces.py` (also with
`--check`); no model or network is involved.

Source layout: `src/main.js` (plugin entry), `src/view.js` (view and renderer),
`src/graph.js` (link graph model), `src/layout.js` (layout solver and worker),
`src/edges.js`, `src/shaders.js`, `src/field.js`, `src/palette.js`,
`src/regions.js`, `src/activation.js` (trace parsing, overlay state and the
advisor layer's data),
`src/settings.js`, `src/math.js`, `src/metrics.js`, `src/gl-program.js`.

## License

MIT, see [LICENSE](LICENSE). Credits for a few well-known algorithms are in
[THIRD_PARTY.md](THIRD_PARTY.md).
