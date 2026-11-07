# Patched headless-client

`headless-client/` is the complete Go module `github.com/kulikov0/headless-client@v0.1.0`, distributed under its included MIT license. Its upstream module checksum is:

```text
h1:kA8oQqjQBtL2NQ3fzEQt1SIHi+nfjC0qMZlXx2hijl8=
```

Only these files differ from that module:

- `internal/chromehttp1/conn.go`: cancel blocked HTTP/1.1 I/O when the request context ends, and finish cancellation before returning a connection to the pool.
- `internal/chromehttp1/cancel_test.go`: deadline tests for request writing, response headers, body reading and body closing, plus a connection reuse regression test.

The root `go.mod` replaces the module with this directory so a normal build includes the fix. The upstream HTTP/2, WebRTC, TLS profiles and other module files are unchanged. A complete source copy avoids modifying the global module cache or depending on a private workstation path.

When updating the module, compare a clean upstream version with these two files, preserve the license, and rerun:

```sh
go test -race github.com/kulikov0/headless-client/internal/chromehttp1 ./relay/telemost
go vet github.com/kulikov0/headless-client/internal/chromehttp1 ./relay/telemost ./headless/telemost ./headless/telemost-joiner
```
