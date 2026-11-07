# Telemost room stability experiment

This is the portable version of the 28-room experiment, based on upstream `v0.5.1` (`a26b5555728e5b3e990b8204f7663cc742c9de3b`). The [historical report](../../docs/experiments/telemost-28-rooms/REPORT.md) and its aggregate JSON files describe the original run. Portability changes have local regression coverage; this version of the runner has not had another two-hour network run.

## Included changes

- Optional shared API gate (`TM_API_GATE`): account and egress queues, bounded attempts, `Retry-After`, cancellation, and idempotent completion reports. Without the gate, API requests retain upstream behavior.
- `TM_WAITING_POLL`: slower waiting-room polling after a peer connects; admission polling stays at five seconds.
- Existing-room startup without a cookie; room creation still requires an authenticated session.
- HTTP/1.1 cancellation in the [vendored dependency](../../third_party/README.md), and closing an abandoned SOCKS connection.
- Bounded SSH retries, independent reverse forwarding, reconciled remote starts, and cleanup scoped to one test run.

The default plan reuses one authorized account and the same 28 existing rooms: ten minutes with all creators on the Mac, followed by two hours with ten creators on a Linux VPS and eighteen on the Mac. All joiners stay on the Mac. The VPS also serves the synthetic HTTP payload to its local creators; the Mac serves the same payload to its creators.

```text
Mac joiner -> Telemost -> Mac or VPS creator -> local synthetic HTTP sink
     |                        |
     +--- local API gate -----+
              ^
       SSH reverse forward for VPS API requests
```

## Local checks and builds

Use macOS for the controller, Python 3.10 or newer, Go at least the version in `go.mod`, and a Go-compatible C toolchain for race tests. The VPS needs Linux, Python 3, systemd with a unified cgroup hierarchy, and SSH access able to manage the dedicated transient units. There are no external Python dependencies.

From the repository root:

```sh
python3 -m unittest discover -s experiments/telemost -p 'test_*.py' -v
go test ./...
go test -race ./relay/telemost ./relay/tunnel github.com/kulikov0/headless-client/internal/chromehttp1
go vet ./... github.com/kulikov0/headless-client/internal/chromehttp1
umask 077
mkdir -p .my_local_dev/telemost
go build -trimpath -o .my_local_dev/telemost/creator-mac ./headless/telemost
go build -trimpath -o .my_local_dev/telemost/joiner-mac ./headless/telemost-joiner
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -o .my_local_dev/telemost/creator-linux ./headless/telemost
```

Select the VPS architecture explicitly if it differs from `amd64`. Unit tests use loopback services and mocks; they do not create rooms or connect to the provider.

## Private setup

Use a fresh `.my_local_dev/telemost/` directory for each run; keep previous results elsewhere before preparing another run. `TM_LAB_STATE_DIR` can select another short private directory. The runner refuses to overwrite existing run evidence. Keep the directory mode `0700` and private files mode `0600`.

1. Copy `config.example.json` to `config.private.json` in the state directory. Set the dedicated test host, SSH port, identity path and absolute remote directory. Verify its SSH host key beforehand; host-key verification stays enabled. Copy `plan.example.json` there as `plan.json`.
2. Place a fresh authorized test cookie export in `account-a.cookies.private.json`. Use only a disposable copy: cleanup deletes this file locally and on the VPS. Never place credentials in arguments or tracked files.
3. Place the 28 previously created conference links in `rooms.private.json`, a JSON object with keys `a-001` through `a-028` and their Telemost URLs as values. The comparison reuses these rooms; it does not measure room creation throughput or discover an account quota.
4. On the dedicated VPS, create the configured remote directory and its `session.private/` subdirectory, both mode `0700`. Transfer `remote.py`, `sink.py`, and the built `creator-linux` into the remote directory. Transfer the cookie copy into `session.private/account-a.cookies.private.json` with mode `0600`; make the binary executable. Use the same identity, port and target as the private configuration. Do not overwrite an active test directory.
5. Reserve loopback ports `18093` (sink), `18990` (gate/reverse forwarding) and local SOCKS ports `18501`–`18528`. The remote unit namespace `telemost-lab-*` is dedicated to this experiment, with at most one run per host. The test directory must be fresh, without old phase outputs or an active owner.

The controller checks local memory and caps worker usage. The VPS workers share a 400 MiB memory and one-CPU slice with a 96 MiB cap per creator; it refuses a start below 350 MiB available memory and stops the phase below 220 MiB. These are experimental bounds, not production sizing recommendations.

## Run and inspect

After preparing the private files and dedicated host:

```sh
python3 "$PWD/experiments/telemost/run.py"
```

In another terminal:

```sh
python3 experiments/telemost/progress.py
python3 experiments/telemost/summarize.py --state .my_local_dev/telemost
```

The foreground controller must stay awake and connected. Interrupting it with Ctrl-C or SIGTERM runs cleanup. The remote helper starts a five-hour cleanup timer in case the controller disappears. Its private owner token prevents a different run from stopping its workers. Local and remote raw output may contain room identifiers; all such files stay in private state.

If the controller is killed abruptly, inspect its `runner.private.json` and the remote `owner.private.json` before recovery. Invoke `remote.py` over the configured SSH connection with a JSON stdin object containing `action: "cleanup"` and the matching `run_id`; do not delete an owner marker to override an active run. Check `cleanup.json` for both stopped remote workers and removed session copies; a failed SSH cleanup requires a host check, not an assumption of success.

`results.json` records completed phases. `summary.json` contains aggregates, matched ten-minute windows, API errors, and HTTP failures grouped by overlap with file transfers. A completed hold is not a clean pass: every health probe and file transfer must succeed for `clean=true`. Files are 1 MiB with a checked SHA-256; throughput is a median of periodic transfers, not continuous saturated capacity.

The default request gate stops ramp-up after a 429 and honors shared backoff. It does not rotate accounts or addresses to defeat throttling. This experiment cannot establish mobile allowlist availability or a provider account/IP quota when no 429 is observed. Private logs, sessions, machine addresses and room URLs are excluded from Git; publish only reviewed aggregate reports.
