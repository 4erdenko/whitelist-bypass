package chromehttp1

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestRequestCancellationBoundsNetworkWait(t *testing.T) {
	for _, stage := range []string{"write", "headers", "body", "close"} {
		t.Run(stage, func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			finished := make(chan struct{})
			go func() {
				defer close(finished)
				if stage == "write" {
					time.Sleep(400 * time.Millisecond)
					server.Close()
					return
				}
				request, err := http.ReadRequest(bufio.NewReader(server))
				if err != nil {
					return
				}
				io.Copy(io.Discard, request.Body)
				if stage != "headers" {
					fmt.Fprint(server, "HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\n")
				}
				time.Sleep(400 * time.Millisecond)
				server.Close()
			}()
			ctx, cancel := context.WithTimeout(context.Background(), 40*time.Millisecond)
			defer cancel()
			request, _ := http.NewRequestWithContext(ctx, "POST", "https://example.test/", strings.NewReader("payload"))
			connection := NewConn(client)
			started := time.Now()
			response, err := connection.RoundTrip(request)
			if err == nil {
				if stage == "body" {
					_, err = io.ReadAll(response.Body)
				}
				closeErr := response.Body.Close()
				if err == nil {
					err = closeErr
				}
			}
			elapsed := time.Since(started)
			if elapsed > 200*time.Millisecond {
				t.Errorf("cancellation did not bound %s: %s", stage, elapsed)
			}
			if connection.Reusable() {
				t.Error("cancelled connection returned to the pool")
			}
			if stage != "close" && err == nil {
				t.Error("cancelled operation succeeded")
			}
			<-finished
		})
	}
}

func TestFinishedRequestCancellationDoesNotCloseReusedConnection(t *testing.T) {
	client, server := net.Pipe()
	defer client.Close()
	defer server.Close()
	go func() {
		reader := bufio.NewReader(server)
		for range 2 {
			if _, err := http.ReadRequest(reader); err != nil {
				return
			}
			fmt.Fprint(server, "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
		}
	}()
	connection := NewConn(client)
	for range 2 {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		request, _ := http.NewRequestWithContext(ctx, "GET", "https://example.test/", nil)
		response, err := connection.RoundTrip(request)
		if err != nil {
			cancel()
			t.Fatal(err)
		}
		body, err := io.ReadAll(response.Body)
		response.Body.Close()
		cancel()
		if err != nil || string(body) != "ok" || !connection.Reusable() {
			t.Fatalf("connection reuse failed: %q %v", body, err)
		}
	}
}
