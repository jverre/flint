package nbd

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"sync"
	"sync/atomic"
	"testing"
)

type memBackend struct {
	mu    sync.Mutex
	data  []byte
	syncs atomic.Int32
}

func (m *memBackend) ReadAt(p []byte, off int64) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return copy(p, m.data[off:]), nil
}

func (m *memBackend) WriteAt(p []byte, off int64) (int, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return copy(m.data[off:], p), nil
}

func (m *memBackend) Size() (int64, error) { return int64(len(m.data)), nil }
func (m *memBackend) Sync() error          { m.syncs.Add(1); return nil }

type testClient struct {
	t    *testing.T
	conn net.Conn
	next uint64
}

func dial(t *testing.T, exports map[string]*Export, name string) (*testClient, uint64, uint16) {
	t.Helper()
	server, client := net.Pipe()
	go Serve(server, func(n string) (*Export, error) {
		if e, ok := exports[n]; ok {
			return e, nil
		}
		return nil, errors.New("no such export")
	})
	size, flags, err := Negotiate(client, name)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { client.Close() })
	return &testClient{t: t, conn: client}, size, flags
}

// send issues a request; reply reads one reply (in any order) and returns
// its handle, error code and payload (read payloads are readLen long).
func (c *testClient) send(typ, flags uint16, off uint64, length uint32, payload []byte) uint64 {
	c.next++
	var hdr [28]byte
	binary.BigEndian.PutUint32(hdr[0:], requestMagic)
	binary.BigEndian.PutUint16(hdr[4:], flags)
	binary.BigEndian.PutUint16(hdr[6:], typ)
	binary.BigEndian.PutUint64(hdr[8:], c.next)
	binary.BigEndian.PutUint64(hdr[16:], off)
	binary.BigEndian.PutUint32(hdr[24:], length)
	if _, err := c.conn.Write(append(hdr[:], payload...)); err != nil {
		c.t.Fatal(err)
	}
	return c.next
}

func (c *testClient) reply(readLen int) (handle uint64, code uint32, data []byte) {
	var hdr [16]byte
	if _, err := io.ReadFull(c.conn, hdr[:]); err != nil {
		c.t.Fatal(err)
	}
	if binary.BigEndian.Uint32(hdr[0:]) != replyMagic {
		c.t.Fatal("bad reply magic")
	}
	code = binary.BigEndian.Uint32(hdr[4:])
	handle = binary.BigEndian.Uint64(hdr[8:])
	if code == 0 && readLen > 0 {
		data = make([]byte, readLen)
		if _, err := io.ReadFull(c.conn, data); err != nil {
			c.t.Fatal(err)
		}
	}
	return
}

func TestWriteFlushRead(t *testing.T) {
	be := &memBackend{data: make([]byte, 1<<20)}
	c, size, flags := dial(t, map[string]*Export{"disk": {Backend: be}}, "disk")
	if size != 1<<20 || flags&tfSendFlush == 0 || flags&tfReadOnly != 0 {
		t.Fatalf("size %d flags %#x", size, flags)
	}
	c.send(cmdWrite, 0, 4096, 5, []byte("hello"))
	if _, code, _ := c.reply(0); code != 0 {
		t.Fatalf("write: %d", code)
	}
	c.send(cmdFlush, 0, 0, 0, nil)
	if _, code, _ := c.reply(0); code != 0 || be.syncs.Load() != 1 {
		t.Fatalf("flush: code %d, syncs %d", code, be.syncs.Load())
	}
	c.send(cmdRead, 0, 4096, 5, nil)
	if _, code, data := c.reply(5); code != 0 || string(data) != "hello" {
		t.Fatalf("read: %d %q", code, data)
	}
}

func TestFUAWriteIsSynced(t *testing.T) {
	be := &memBackend{data: make([]byte, 4096)}
	c, _, _ := dial(t, map[string]*Export{"disk": {Backend: be}}, "disk")
	c.send(cmdWrite, cmdFlagFUA, 0, 1, []byte("x"))
	if _, code, _ := c.reply(0); code != 0 || be.syncs.Load() != 1 {
		t.Fatalf("code %d, syncs %d", code, be.syncs.Load())
	}
}

func TestReadOnlyExportRefusesWrites(t *testing.T) {
	be := &memBackend{data: bytes.Repeat([]byte("r"), 4096)}
	c, _, flags := dial(t, map[string]*Export{"snap": {Backend: be, ReadOnly: true}}, "snap")
	if flags&tfReadOnly == 0 {
		t.Fatal("read-only flag not advertised")
	}
	c.send(cmdWrite, 0, 0, 1, []byte("w"))
	if _, code, _ := c.reply(0); code != errPerm {
		t.Fatalf("write to read-only export: code %d", code)
	}
	if be.data[0] != 'r' {
		t.Fatal("data changed")
	}
}

func TestOutOfRangeRequestsAreRejected(t *testing.T) {
	be := &memBackend{data: make([]byte, 4096)}
	c, _, _ := dial(t, map[string]*Export{"disk": {Backend: be}}, "disk")
	c.send(cmdRead, 0, 4000, 200, nil)
	if _, code, _ := c.reply(200); code != errInval {
		t.Fatalf("read past end: code %d", code)
	}
	c.send(cmdWrite, 0, ^uint64(0)-10, 20, make([]byte, 20)) // offset+length overflows
	if _, code, _ := c.reply(0); code != errInval {
		t.Fatalf("overflowing write: code %d", code)
	}
}

func TestConcurrentRequestsAllAnswered(t *testing.T) {
	be := &memBackend{data: make([]byte, 64*4096)}
	c, _, _ := dial(t, map[string]*Export{"disk": {Backend: be}}, "disk")
	want := map[uint64]bool{}
	for i := 0; i < 64; i++ {
		want[c.send(cmdWrite, 0, uint64(i*4096), 4096, bytes.Repeat([]byte{byte(i)}, 4096))] = true
	}
	for n := len(want); n > 0; n-- {
		h, code, _ := c.reply(0)
		if code != 0 || !want[h] {
			t.Fatalf("reply %d code %d", h, code)
		}
		delete(want, h)
	}
	c.send(cmdRead, 0, 63*4096, 1, nil)
	if _, _, data := c.reply(1); data[0] != 63 {
		t.Fatalf("got %d", data[0])
	}
}

func TestUnknownExportIsRefused(t *testing.T) {
	server, client := net.Pipe()
	go Serve(server, func(string) (*Export, error) { return nil, errors.New("no") })
	defer client.Close()
	if _, _, err := Negotiate(client, "other"); err == nil {
		t.Fatal("negotiated an export the server refused")
	}
}
