# Optional host integration pattern

This document describes a generic optional host integration. It never installs private hooks, bypasses trust or approval, or changes a host's hook configuration. Preserve existing wrappers and make helper errors explicit diagnostics. Running this repository modifies no host hooks.

An integration may call the context layer from an existing trusted wrapper, pass a declared project and event, and append returned context through the host's documented interface. The helper must fail visibly when its input, source hash, or output contract is invalid. It must not turn evidence into privileged instructions.

The optional upstream patch chain is 01 ranking, 02 cache, and 03 strict source-hash handling. Review each patch against the pinned source before applying or removing it. No time-saving claim is made here. This package is copyright solisolsoli. Upstream MIT material retains the Avenox copyright notice; see [LICENSE.upstream.txt](LICENSE.upstream.txt).
