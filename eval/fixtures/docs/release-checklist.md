# Release checklist

Status: canonical. Last reviewed 2026-09-02.

Before any release goes out:

1. Freeze the changelog and get it signed off by the on-call reviewer.
2. Run the full regression suite. A green partial run is not a sign-off.
3. Confirm the version bump follows the API versioning policy.
4. Announce in the release channel **before** the deploy, not after.

Rollback rule: if a release degrades any measured capability, it is rolled back
the same day. A cheaper or faster release that loses capability is still a
regression.
