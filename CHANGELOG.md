# Changelog

## Unreleased

- Added GitHub Actions CI for lint, tests, compile, locked MCP install, and build.
- Added `itamx mcp-config` for MCP client configuration.
- Added shared Matrix request-building helpers used by both CLI and MCP.
- Added sanitized response fixture tests.
- Replaced the starter package `hello()` with real public exports and version metadata.
- Added core flight service functions and `show_flight_details` MCP tool.
- Added opt-in live smoke tests for Matrix lookup, search, and detail endpoints.

## 0.2.0

- Added `itamx-mcp` and `itamx-mcp-http` MCP entry points.
- Added MCP tools for flight search, date search, location lookup, and airline lookup.
- Added MIT license metadata and README credit for the `fli` project.
