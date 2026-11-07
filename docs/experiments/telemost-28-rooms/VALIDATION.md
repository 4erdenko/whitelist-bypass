# Fork migration validation

Base: upstream `a26b5555728e5b3e990b8204f7663cc742c9de3b` (`v0.5.1`). These checks validate the source migration and portable harness, separately from the historical two-hour measurements in [REPORT.md](REPORT.md).

Passed on macOS arm64 with Go 1.27.3:

- `python3 -m unittest discover -s experiments/telemost -p 'test_*.py' -v`: 13 tests, including shared rate limits, SSH retry bounds, run ownership, configuration validation, and real loopback HTTP health/download/upload with SHA-256 checks.
- `go test ./...`: all root-module packages.
- `go test -race ./relay/telemost ./relay/tunnel github.com/kulikov0/headless-client/internal/chromehttp1`: API retry/cancellation and HTTP/1.1 deadline/reuse coverage. The tunnel package has no test files.
- `go vet ./... github.com/kulikov0/headless-client/internal/chromehttp1`.
- Creator and joiner builds for macOS arm64, plus a creator build for Linux amd64 with `CGO_ENABLED=0`, all using `-trimpath`.

The five protocol source/regression files match the original tested experiment byte for byte. The vendored dependency contains every file from `headless-client@v0.1.0`; only `internal/chromehttp1/conn.go` is changed and `internal/chromehttp1/cancel_test.go` is added. Its upstream license remains included.

Whitespace checks pass for our source changes. A check of the complete vendored addition also reports existing whitespace in upstream patch files, an HTTP grammar comment and an AUTHORS file; those public module bytes are intentionally preserved.

Secret review covers the files being added or changed, rather than pre-existing upstream history. Gitleaks found five matches in unchanged upstream dependency files: a public WebSocket sample nonce and QUIC key-label variable assignments. They were reviewed as false positives and verified byte-identical to the public module. The experiment changes contain no personal machine paths, public host addresses, account email addresses, concrete room URLs, session exports, raw logs or binaries. Only aggregate historical measurements are included.

The delivered source and reports are plain-text Git blobs, which do not carry filesystem extended attributes. macOS retains its system-managed `com.apple.provenance` attribute on local working files after `xattr -c`; it is not part of the committed content. Normal Git commit identity and timestamps remain required repository metadata.

No new provider experiment, Android integration, production deployment, or mobile allowlist check was performed during this migration. The new remote preparation/ownership wrapper has mocked lifecycle tests; its systemd behavior still requires a subsequent run on a dedicated test host.
