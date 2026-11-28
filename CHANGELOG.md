# Changelog

## Unreleased

- Make SQLite the only store backend and the default.
- Refuse the removed `files` configuration and projects with leftover file-protocol state.
- Remove the file queue and multi-file commit implementation. Record bodies remain files by design.
