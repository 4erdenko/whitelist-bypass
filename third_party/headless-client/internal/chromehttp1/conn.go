package chromehttp1

import (
	"bufio"
	"context"
	"io"
	"net/http"
)

const drainLimit = 1 << 16

type Conn struct {
	connection io.ReadWriteCloser
	reader     *bufio.Reader
	reusable   bool
	onIdle     func(*Conn)
}

func NewConn(connection io.ReadWriteCloser) *Conn {
	return &Conn{connection: connection, reader: bufio.NewReader(connection), reusable: true}
}

func (c *Conn) SetIdleHandler(onIdle func(*Conn)) {
	c.onIdle = onIdle
}

func (c *Conn) Reusable() bool {
	return c.reusable
}

func (c *Conn) Close() error {
	c.reusable = false

	return c.connection.Close()
}

func (c *Conn) RoundTrip(request *http.Request) (*http.Response, error) {
	cancelled := make(chan struct{})
	stop := context.AfterFunc(request.Context(), func() {
		c.connection.Close()
		close(cancelled)
	})
	stopCancellation := func() {
		if !stop() {
			<-cancelled
			c.reusable = false
		}
	}
	if err := WriteRequest(c.connection, request); err != nil {
		stopCancellation()
		c.reusable = false
		return nil, err
	}
	response, err := http.ReadResponse(c.reader, request)
	if err != nil {
		stopCancellation()
		c.reusable = false
		return nil, err
	}
	if response.Close || request.Close {
		c.reusable = false
	}
	response.Body = &pooledBody{ReadCloser: response.Body, connection: c, stopCancellation: stopCancellation}

	return response, nil
}

type pooledBody struct {
	io.ReadCloser
	connection       *Conn
	stopCancellation func()
	done             bool
}

func (b *pooledBody) Read(p []byte) (int, error) {
	read, err := b.ReadCloser.Read(p)
	if err != nil && err != io.EOF {
		b.connection.reusable = false
	}

	return read, err
}

func (b *pooledBody) Close() error {
	if b.done {
		return nil
	}
	b.done = true

	if b.connection.reusable {
		drained, err := io.CopyN(io.Discard, b.ReadCloser, drainLimit)
		if err != io.EOF || drained == drainLimit {
			b.connection.reusable = false
		}
	}
	err := b.ReadCloser.Close()
	if b.stopCancellation != nil {
		b.stopCancellation()
	}

	if b.connection.onIdle != nil {
		b.connection.onIdle(b.connection)
	} else if !b.connection.reusable {
		b.connection.Close()
	}

	return err
}
