# Responses API: selected task requests

Use this command only when a specific task benefits from a separate Responses API call, such as a bounded public research or drafting request. It is not a background hook, an automatic retrieval step, or a replacement for source inspection. The Decisions API has its own module and question workflow.

The command accepts a reviewed UTF-8 file containing **public or synthetic** text. The scope flag is an operator assertion, not a privacy detector. Do not send private vault notes, conversations, credentials, or unpublished source material. A few obvious credential patterns are refused locally, but that check cannot establish that text is public. No request occurs without `--send`; the default prints metadata without the input text.

```sh
context-layer responses run --input-file /path/to/reviewed-public-task.txt --data-scope public --model MODEL
context-layer responses run --input-file /path/to/reviewed-public-task.txt --data-scope public --model MODEL --send
```

`--web-search` explicitly enables at most one web search tool call. There are no tools by default. `--max-output-tokens` defaults to 1024 (allowed 64–4096) and `--timeout` defaults to 10 seconds (maximum 30). The client sends one POST to `https://api.openai.com/v1/responses`, refuses redirects, and uses `OPENAI_API_KEY` from the process environment. Keep the key out of prompts, files, logs, and command arguments.

The request sets `store: false`. This controls response storage for the API request; it does not establish zero data retention or override OpenAI's applicable data controls. The returned text is labeled `advisory_only` and `VERIFY_WITH_ORIGINAL_SOURCES`: open original sources before presenting factual claims. For web search responses, validated `url_citation` annotations are returned as `citations` with URL, title, and character offsets into the returned `text`; plain text responses return an empty list. These are **external candidate sources**, not verified local evidence or a `SUPPORTED` status. When displaying web sourced claims to end users, make citations visible and clickable. A local preview or offline test is not evidence that a model is available to the account or that output quality improved.

Input is limited to 32,768 UTF-8 bytes, the serialized request to 40,000 bytes,
the response body to 256,000 bytes and the returned answer to 64,000 characters.
The direct transport applies the same obvious credential-pattern check as the
command. Duplicate JSON keys, invalid Unicode and malformed answer shapes are
refused with fixed error codes. A model refusal returns `response_refused` and
CLI exit code 1, without echoing the provider's refusal text. The transport
does not retry failures or follow redirects.

Official references: [Responses overview](https://developers.openai.com/api/reference/responses/overview), [Create response](https://developers.openai.com/api/reference/python/resources/responses/methods/create), [web search citations](https://developers.openai.com/api/docs/guides/tools-web-search), and [data controls](https://developers.openai.com/api/docs/guides/your-data).
