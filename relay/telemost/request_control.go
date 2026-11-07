package telemost

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/google/uuid"
)

type gateEvent struct {
	ID            string  `json:"id"`
	Account       string  `json:"account"`
	Egress        string  `json:"egress"`
	Role          string  `json:"role"`
	Room          string  `json:"room"`
	Endpoint      string  `json:"endpoint"`
	Method        string  `json:"method"`
	Authenticated bool    `json:"authenticated"`
	Status        int     `json:"status"`
	RetryAfter    float64 `json:"retry_after_seconds"`
	RetryPresent  bool    `json:"retry_after_present"`
	Elapsed       float64 `json:"elapsed_seconds"`
	Protocol      string  `json:"protocol,omitempty"`
}

func endpointName(path string) string {
	parts := strings.Split(strings.Trim(path, "/"), "/")
	last := parts[len(parts)-1]
	switch last {
	case "conferences", "connection", "peers", "request-states", "admit", "kick":
		return last
	default:
		return "other"
	}
}

func retryAfter(value string, now time.Time) time.Duration {
	if seconds, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64); err == nil && seconds >= 0 {
		if seconds > int64((time.Duration(1<<63-1))/time.Second) {
			return time.Duration(1<<63 - 1)
		}
		return time.Duration(seconds) * time.Second
	}
	if when, err := http.ParseTime(value); err == nil && when.After(now) {
		return when.Sub(now)
	}
	return 0
}

func gateCall(ctx context.Context, address, action string, event gateEvent) error {
	u, err := url.Parse(address)
	if err != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.User != nil || u.Path != "" || u.RawQuery != "" {
		return fmt.Errorf("API gate must be an explicit loopback HTTP address")
	}
	data, err := json.Marshal(event)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", address+"/"+action, bytes.NewReader(data))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	client := &http.Client{Timeout: 130 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("API gate %s unavailable: %w", action, err)
	}
	defer resp.Body.Close()
	_, err = io.Copy(io.Discard, io.LimitReader(resp.Body, 8192))
	if err != nil {
		return err
	}
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("API gate %s refused: %d", action, resp.StatusCode)
	}
	return nil
}

func managedRequest(client *http.Client, req *http.Request) ([]byte, int, error) {
	gate := os.Getenv("TM_API_GATE")
	if gate == "" {
		resp, err := client.Do(req)
		if err != nil {
			return nil, 0, err
		}
		defer resp.Body.Close()
		data, err := io.ReadAll(resp.Body)
		return data, resp.StatusCode, err
	}
	ctx, cancel := context.WithTimeout(req.Context(), 180*time.Second)
	defer cancel()
	event := gateEvent{Account: os.Getenv("TM_ACCOUNT"), Egress: os.Getenv("TM_EGRESS"),
		Role: os.Getenv("TM_ROLE"), Room: os.Getenv("TM_ROOM"), Method: req.Method,
		Endpoint: endpointName(req.URL.Path), Authenticated: req.Header.Get("Cookie") != ""}
	canRetry := req.Method == "GET" || event.Endpoint == "request-states"
	for attempt := 0; attempt < 3; attempt++ {
		event.ID = uuid.NewString()
		event.Status, event.RetryAfter, event.Elapsed, event.RetryPresent = 0, 0, 0, false
		event.Protocol = ""
		var body io.ReadCloser
		if req.GetBody != nil {
			var err error
			body, err = req.GetBody()
			if err != nil {
				return nil, 0, err
			}
		}
		if err := gateCall(ctx, gate, "acquire", event); err != nil {
			if body != nil {
				body.Close()
			}
			return nil, 0, err
		}
		attemptCtx, attemptCancel := context.WithTimeout(ctx, 20*time.Second)
		cloned := req.Clone(attemptCtx)
		if body != nil {
			cloned.Body = body
		}
		if req.Method != http.MethodGet && req.Method != http.MethodHead {
			cloned.GetBody = nil
		}
		started := time.Now()
		resp, requestErr := client.Do(cloned)
		var data []byte
		if resp != nil {
			event.Status = resp.StatusCode
			event.Protocol = resp.Proto
			event.RetryPresent = resp.Header.Get("Retry-After") != ""
			event.RetryAfter = retryAfter(resp.Header.Get("Retry-After"), time.Now()).Seconds()
			data, requestErr = io.ReadAll(io.LimitReader(resp.Body, 8<<20))
			resp.Body.Close()
		}
		event.Elapsed = time.Since(started).Seconds()
		attemptCancel()
		var releaseErr error
		for reportAttempt := 0; reportAttempt < 2; reportAttempt++ {
			releaseCtx, releaseCancel := context.WithTimeout(context.Background(), 5*time.Second)
			releaseErr = gateCall(releaseCtx, gate, "release", event)
			releaseCancel()
			if releaseErr == nil {
				break
			}
		}
		if releaseErr != nil {
			return nil, event.Status, releaseErr
		}
		transientRead := req.Method == http.MethodGet && (requestErr != nil || event.Status == 502 || event.Status == 503 || event.Status == 504)
		rateLimited := event.Status == http.StatusTooManyRequests && canRetry
		if (!transientRead && !rateLimited) || attempt == 2 || ctx.Err() != nil {
			return data, event.Status, requestErr
		}
		if transientRead {
			timer := time.NewTimer(time.Duration(1<<attempt) * time.Second)
			select {
			case <-timer.C:
			case <-ctx.Done():
				timer.Stop()
				return nil, event.Status, ctx.Err()
			}
		}
	}
	return nil, 0, fmt.Errorf("API request retry budget exhausted")
}
