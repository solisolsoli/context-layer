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
credentials. The manual CLI has no private-data mode. The command reads the
entire JSON input and sends the fixed questionnaire to OpenAI only with
`--send`; the API key is read only for that explicit call. It does not save the
request or answer in the vault. Your own input file, terminal output and shell
history remain under your control. Provider retention follows your OpenAI
account's terms and data controls.

Answers are advisory model estimates. A probability or confidence is not a
fact, source citation, calibrated threshold, approval or permission to act.
Open the original source before making a claim. A refused answer or transport
failure cannot be interpreted as a positive result. No live API call or
account entitlement is proven by the offline test suite.

This feature replaces the active Jev advisor commands. Older `.context/jev*`
files are legacy local artifacts; the new command does not read them. See
[privacy](privacy.md) for manual cleanup and [CLI conventions](cli.md) for exit
codes.
