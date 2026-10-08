# Manual Decisions API assessment

`context-layer decisions assess` is an optional, manual call for **public or
synthetic text**. It is separate from vault search, MCP, the prompt hook, memory
records and live Beyin. It does not read a vault, choose sources, write memory,
or change a retrieval packet. Importing the transport makes no network call.

The command asks one fixed question with OpenAI's Decisions API, using
`gpt-6-luna` at `POST https://api.openai.com/v1/decisions`. The three tasks are:

| `--task` | Input JSON fields | Typed answer |
| --- | --- | --- |
| `relevance` | `query`, `passage` | `predicate`: estimated probability that the shown passage is directly useful |
| `claim_support` | `claim`, `passage` | `choice`: `supports`, `contradicts`, or `unresolved` |
| `review_priority` | `item`, `rubric` | `score`: position across `Later`, `Soon`, `Now` |

Save a JSON object containing the task's fields in a local file. For example,
with a fictional public guide:

```json
{"query":"How do I set a timer?","passage":"The public guide says to hold SET for three seconds."}
```

```sh
context-layer decisions assess --task relevance --data-scope public --input example.json
```

The default is a local preview: it reports the model, question name, input
length and `network_call: false`, without printing or sending the input text.
After checking that **every field** is public or synthetic, set
`OPENAI_API_KEY` in your environment and add `--send`:

```sh
context-layer decisions assess --task relevance --data-scope public --input example.json --send
```

`--data-scope` is a caller assertion, not an automatic privacy detector. Do not
use this command for private notes, confidential prompts, personal data or
credentials. The manual CLI has no private-data mode. The command reads a
bounded UTF-8 JSON object containing exactly the task's fields, with no duplicate
keys. Each field is limited to 4,000 characters and the input file or stdin to
12,000 bytes. Symlinks and nonregular files are refused. A few obvious credential
patterns are refused locally, including at the transport boundary; that check
does not establish that input is public. It sends the fixed questionnaire only with
`--send`; the API key is read only for that explicit call. It does not save the
request or answer in the vault. Your own input file, terminal output and shell
history remain under your control. Provider retention follows your OpenAI
account's terms and data controls.

Answers are advisory model estimates. A probability or confidence is not a
fact, source citation, calibrated threshold, approval or permission to act.
Open the original source before making a claim. A refused answer or transport
failure cannot be interpreted as a positive result. No live API call or
account entitlement is proven by the offline test suite.

A successful call has `status: answered`; a per-question refusal has
`status: refused`, a typed refusal answer and CLI exit code 1. Choice and score
answers preserve the API's `probabilities` array, including each value and its
probability (and score level label). The bounded transport also supports
boolean choice values, distinct from strings such as `"true"`; the three CLI
tasks use their fixed question types. Malformed distributions, duplicate JSON
keys and a missing or mismatched model are refused. No retries occur. Responses
are bounded to 256,000 bytes, and transport timeout defaults to 10 seconds
(maximum 30).

Official references: [Decisions guide](https://developers.openai.com/api/docs/guides/decisions)
and [Create a decision](https://developers.openai.com/api/reference/resources/decisions/methods/create).
The guide describes a public beta with `gpt-6-luna`; this is a documentation
status, not a test of your account's access or a measured speed improvement.

This feature replaces the active Jev advisor commands. Older `.context/jev*`
files are legacy local artifacts; the new command does not read them. See
[privacy](privacy.md) for manual cleanup and [CLI conventions](cli.md) for exit
codes.
