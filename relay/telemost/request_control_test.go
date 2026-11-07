package telemost

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/kulikov0/headless-client"
)

func TestRetryAfter(t *testing.T) {
	now := time.Date(2026, 11, 7, 0, 0, 0, 0, time.UTC)
	for _, tc := range []struct {
		value string
		want  time.Duration
	}{
		{"60", time.Minute}, {"-1", 0}, {"invalid", 0},
		{now.Add(90 * time.Second).Format(http.TimeFormat), 90 * time.Second},
		{now.Add(-time.Hour).Format(http.TimeFormat), 0}, {"172800", 48 * time.Hour},
	} {
		if got := retryAfter(tc.value, now); got != tc.want {
			t.Fatalf("%q: got %v, want %v", tc.value, got, tc.want)
		}
	}
}

func TestManagedRetryAndBody(t *testing.T) {
	for _, method := range []string{"GET", "POST"} {
		t.Run(method, func(t *testing.T) {
			var mu sync.Mutex
			var releases []gateEvent
			gate := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var event gateEvent
				if err := json.NewDecoder(r.Body).Decode(&event); err != nil {
					t.Error(err)
				}
				if r.URL.Path == "/release" {
					mu.Lock()
					releases = append(releases, event)
					mu.Unlock()
				}
				w.Write([]byte(`{}`))
			}))
			defer gate.Close()
			t.Setenv("TM_API_GATE", gate.URL)
			calls := 0
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				body, _ := io.ReadAll(r.Body)
				if string(body) != "payload" {
					t.Error("Request body was not replayed")
				}
				if calls == 1 {
					w.Header().Set("Retry-After", "90")
					w.WriteHeader(429)
					return
				}
				w.Write([]byte("ok"))
			}))
			defer upstream.Close()
			req, _ := http.NewRequest(method, upstream.URL+"/request-states", strings.NewReader("payload"))
			data, status, err := managedRequest(upstream.Client(), req)
			if err != nil || status != 200 || string(data) != "ok" || calls != 2 {
				t.Fatalf("Unexpected retry result: %d %v", status, err)
			}
			mu.Lock()
			defer mu.Unlock()
			if len(releases) != 2 || releases[0].RetryAfter != 90 || !releases[0].RetryPresent {
				t.Fatal("Retry-After not reported to shared gate")
			}
		})
	}
}

func TestCreateNotReplayedAndGateFailsClosed(t *testing.T) {
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(429) }))
	defer upstream.Close()
	gate := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(`{}`)) }))
	t.Setenv("TM_API_GATE", gate.URL)
	req, _ := http.NewRequest("POST", upstream.URL+"/conferences", strings.NewReader(`{}`))
	_, status, err := managedRequest(upstream.Client(), req)
	if err != nil || status != 429 || calls != 1 {
		t.Fatal("Room creation was replayed")
	}
	gate.Close()
	_, _, err = managedRequest(upstream.Client(), req)
	if err == nil || calls != 1 {
		t.Fatal("Upstream request escaped unavailable gate")
	}
}

func TestBoundedRetryAndSanitizedEndpoint(t *testing.T) {
	gate := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(`{}`)) }))
	defer gate.Close()
	t.Setenv("TM_API_GATE", gate.URL)
	calls := 0
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(429) }))
	defer upstream.Close()
	req, _ := http.NewRequest("GET", upstream.URL+"/conferences/private-room/connection", nil)
	_, status, err := managedRequest(upstream.Client(), req)
	if err != nil || status != 429 || calls != 3 {
		t.Fatal("Retry budget not enforced")
	}
	if endpointName("/conferences/private-room") != "other" || endpointName(req.URL.Path) != "connection" {
		t.Fatal("Endpoint label leaks identifier")
	}
}

func TestTransientReadRetryAndNonReplayablePost(t *testing.T) {
	gate := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(`{}`)) }))
	defer gate.Close()
	t.Setenv("TM_API_GATE", gate.URL)
	for _, method := range []string{"GET", "POST"} {
		calls := 0
		upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			calls++
			if calls == 1 {
				connection, _, _ := w.(http.Hijacker).Hijack()
				connection.Close()
				return
			}
			w.Write([]byte("ok"))
		}))
		req, _ := http.NewRequest(method, upstream.URL+"/connection", nil)
		data, status, err := managedRequest(upstream.Client(), req)
		upstream.Close()
		if method == "GET" && (err != nil || status != 200 || string(data) != "ok" || calls != 2) {
			t.Fatalf("Transient GET was not recovered: %d %v", status, err)
		}
		if method == "POST" && (err == nil || calls != 1) {
			t.Fatal("Ambiguous POST was replayed")
		}
	}
}

func TestChromeTransportDoesNotReplayAmbiguousPost(t *testing.T) {
	gate := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte(`{}`)) }))
	defer gate.Close()
	t.Setenv("TM_API_GATE", gate.URL)
	var posts atomic.Int64
	upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			posts.Add(1)
			io.Copy(io.Discard, r.Body)
			connection, _, _ := w.(http.Hijacker).Hijack()
			connection.Close()
			return
		}
		w.Write([]byte("ok"))
	}))
	defer upstream.Close()
	client := &http.Client{Transport: headless.ChromeWindows.Transport(headless.TLSOptions{InsecureSkipVerify: true})}
	defer client.CloseIdleConnections()
	warmup, _ := http.NewRequest(http.MethodGet, upstream.URL, nil)
	if _, _, err := managedRequest(client, warmup); err != nil {
		t.Fatal(err)
	}
	request, _ := http.NewRequest(http.MethodPost, upstream.URL+"/conferences", strings.NewReader("payload"))
	if _, _, err := managedRequest(client, request); err == nil || posts.Load() != 1 {
		t.Fatalf("ambiguous POST must execute once: requests=%d error=%v", posts.Load(), err)
	}
}
