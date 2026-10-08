# Offline API route planner

`context-layer api plan` previews a route from an explicit need and data-scope
assertion. It does not read a task, vault, environment key or source; it makes no
network call, starts no process and dispatches nothing. The command is a routing
hint, not an answer or an approval.

```sh
context-layer api plan
context-layer api plan --need claim_support --data-scope public
context-layer api plan --need generate --data-scope synthetic --model gpt-example-1
```

The defaults are `--need local` and `--data-scope private`. Needs are the closed
set `local`, `relevance`, `claim_support`, `review_priority` and `generate`;
scopes are `public`, `synthetic` and `private`.

| Request | Planned route |
| --- | --- |
| `local` | Local retrieval, regardless of scope. |
| Nonlocal request with `private` scope | `blocked`; private text is not sent to a provider. |
| `relevance`, `claim_support`, `review_priority` with public or synthetic scope | A fixed advisory Decisions task. The output includes a preview command template without `--send`; the CLI makes a provider call only when `--send` is present. Existing scoped authorization can govern that flag; the planner creates no additional approval gate. |
| `generate` with public or synthetic scope and no model | `needs_configuration`. |
| `generate` with public or synthetic scope and an explicit model | Responses API route suggestion, with a preview CLI template that omits `--send`. |

The JSON is compact and contains the selected route, a machine-readable reason,
whether the provider CLI needs its explicit `--send` flag, and
`source_status: NOT_CHECKED`.
Every route is advisory and requires source verification before a factual claim.
Decisions estimates do not establish truth, and no route grants permission or
automatically approves an action. A scope value is the caller's assertion, not
a privacy detector.

The planner is deterministic routing only. It does not inspect free-form task
text, call either API, read credentials, or claim semantic-quality gains or
measured token savings. A model must match
`[A-Za-z0-9][A-Za-z0-9._:-]{0,99}`; the planner rejects a model supplied for a
non-generation need. A Responses route means only that an explicit model was
provided; it does not establish model availability or account access.
