# Changelog

All notable changes to this project are documented in this file.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Project foundation: packaging metadata, lint/type/test tooling, contributor documentation,
  architecture and roadmap documents, CI and security workflows.
- Configuration from a TOML file and `LANPILOT_*` environment variables, including
  `server.public_url`, `server.allowed_hosts` and `server.trusted_proxies`.
- SQLite database with migrations that ship in the package; an existing database is backed up
  before its schema is changed.
- Commands `lanpilot serve`, `lanpilot db upgrade`, `lanpilot db current` and
  `lanpilot config show`; health endpoint `/healthz`.
- Accounts with the roles viewer, operator and admin; sign-in with throttling, server-side
  sessions, password change, and user management for administrators.
- Commands `lanpilot user create`, `lanpilot user set-password` and `lanpilot user list`.
- Request checks: `Host` header validation, origin check and CSRF tokens for state-changing
  requests, security headers including a strict Content-Security-Policy.
- Audit log of sign-ins and account changes.
- Adapter framework for game support: capabilities, connection schemas, discovery of adapters
  through the `lanpilot.adapters` entry point group, contract checks for adapter authors, and
  a simulated demo server (`enable_demo_adapter`).
- Server setup for administrators: add, edit and delete game servers through a form that is
  generated from the game's connection settings, and test the connection.
- Live status of every server (map, players, load), a page per server with map change,
  match restart and level reload for operators, and an audit log view for administrators.
- Dashboard with one card per server that updates itself without reloading, and a
  searchable map picker.
- Map preview images: administrators upload an image per map; it is scaled down, stripped
  of metadata and shown on the dashboard and the server page.
- Requests larger than 11 MB are refused.
- Packages for Debian, Ubuntu, Fedora, Rocky Linux and Arch that contain the complete Python
  environment and install without internet access; they create the service user and the
  encryption key and keep key, configuration and data across upgrades and removal.
- **Counter-Strike 1.6** support: status, map list, map change, round restart, level reload
  and server load (CPU, FPS) over RCON.
- Documentation for installation, configuration, reverse proxies and writing a game adapter.
- Server-sent events that tell open pages when a server's state changed.
- Encryption of game server credentials with a key outside the database
  (`secret_key` / `secret_key_file`, `lanpilot secret generate-key`); known secrets are
  redacted from the log.
