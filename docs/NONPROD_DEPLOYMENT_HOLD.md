# NONPROD integration hold

This change denies foundation, staged deployment, secret seeding, SAS rotation,
operator bootstrap and database-bootstrap CLI entry points before Azure authentication
or mutation. Pushes to main cannot provision through these workflows. Repository CI
remains available. The existing protected destroy workflow is unchanged.

There is no enable Boolean or environment-variable bypass. A later reviewed integration
must install the complete prerequisite contract and release-authorization verifier.
Release itself requires separate explicit authorization; merging either PR is not that
authorization. Do not merge this hold without the user's explicit approval.

Review order: hold, lifecycle integration against the hold, then a separately authorized
release. CodeRabbit review is required for both changes. No Azure/RBAC/state mutation or
workflow dispatch is part of preparing or testing these changes.
