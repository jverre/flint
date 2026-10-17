// Package nbd serves block devices over the NBD protocol (fixed newstyle
// handshake) and attaches the Linux kernel's NBD client to such a server.
//
// The server handles requests concurrently and supports flush and FUA, so a
// filesystem on the device gets real durability barriers: a guest's fsync
// reaches Backend.Sync.
package nbd

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
)

// Backend is what an export serves.
type Backend interface {
	io.ReaderAt
	io.WriterAt
	Size() (int64, error)
	Sync() error
}

// Export is a named device a client may attach.
type Export struct {
	Backend  Backend
	ReadOnly bool
}

// Lookup resolves the export a client asks for, or returns an error to
// refuse it.
type Lookup func(name string) (*Export, error)

const (
	nbdMagic       = 0x4e42444d41474943 // "NBDMAGIC"
	optsMagic      = 0x49484156454f5054 // "IHAVEOPT"
	repMagic       = 0x0003e889045565a9
	requestMagic   = 0x25609513
	replyMagic     = 0x67446698
	maxOptionData  = 4096
	maxRequestSize = 32 << 20
	maxInFlight    = 64

	flagFixedNewstyle = 1 << 0
	flagNoZeroes      = 1 << 1

	optExportName = 1
	optAbort      = 2
	optInfo       = 6
	optGo         = 7

	repAck        = 1
	repInfo       = 3
	repErrUnsup   = 1<<31 + 1
	repErrUnknown = 1<<31 + 6

	infoExport = 0

	// Transmission flags.
	tfHasFlags  = 1 << 0
	tfReadOnly  = 1 << 1
	tfSendFlush = 1 << 2
	tfSendFUA   = 1 << 3

	cmdRead  = 0
	cmdWrite = 1
	cmdDisc  = 2
	cmdFlush = 3

	cmdFlagFUA = 1 << 0

	errPerm  = 1
	errIO    = 5
	errInval = 22
)

func transmissionFlags(e *Export) uint16 {
	f := uint16(tfHasFlags | tfSendFlush | tfSendFUA)
	if e.ReadOnly {
		f |= tfReadOnly
	}
	return f
}

// Serve runs one client connection: the handshake, then requests until the
// client disconnects or the connection fails.
func Serve(conn net.Conn, lookup Lookup) error {
	defer conn.Close()
	exp, err := handshake(conn, lookup)
	if err != nil || exp == nil {
		return err
	}
	return transmit(conn, exp)
}

func handshake(conn net.Conn, lookup Lookup) (*Export, error) {
	var hello [18]byte
	binary.BigEndian.PutUint64(hello[0:], nbdMagic)
	binary.BigEndian.PutUint64(hello[8:], optsMagic)
	binary.BigEndian.PutUint16(hello[16:], flagFixedNewstyle|flagNoZeroes)
	if _, err := conn.Write(hello[:]); err != nil {
		return nil, err
	}
	var clientFlags uint32
	if err := binary.Read(conn, binary.BigEndian, &clientFlags); err != nil {
		return nil, err
	}
	noZeroes := clientFlags&flagNoZeroes != 0

	for {
		var hdr struct {
			Magic  uint64
			Option uint32
			Length uint32
		}
		if err := binary.Read(conn, binary.BigEndian, &hdr); err != nil {
			return nil, err
		}
		if hdr.Magic != optsMagic {
			return nil, errors.New("bad option magic")
		}
		if hdr.Length > maxOptionData {
			return nil, fmt.Errorf("option data too long (%d)", hdr.Length)
		}
		data := make([]byte, hdr.Length)
		if _, err := io.ReadFull(conn, data); err != nil {
			return nil, err
		}

		switch hdr.Option {
		case optExportName:
			exp, err := lookup(string(data))
			if err != nil {
				return nil, err // this option can't carry an error: hang up
			}
			size, err := exp.Backend.Size()
			if err != nil {
				return nil, err
			}
			reply := make([]byte, 10, 134)
			binary.BigEndian.PutUint64(reply[0:], uint64(size))
			binary.BigEndian.PutUint16(reply[8:], transmissionFlags(exp))
			if !noZeroes {
				reply = append(reply, make([]byte, 124)...)
			}
			_, err = conn.Write(reply)
			return exp, err

		case optInfo, optGo:
			if len(data) < 4 {
				return nil, errors.New("short INFO/GO option")
			}
			nameLen := binary.BigEndian.Uint32(data)
			if uint64(len(data)) < 4+uint64(nameLen)+2 {
				return nil, errors.New("short INFO/GO option")
			}
			name := string(data[4 : 4+nameLen])
			exp, err := lookup(name)
			if err != nil {
				if err := writeOptReply(conn, hdr.Option, repErrUnknown, []byte(err.Error())); err != nil {
					return nil, err
				}
				continue
			}
			size, err := exp.Backend.Size()
			if err != nil {
				return nil, err
			}
			info := make([]byte, 12)
			binary.BigEndian.PutUint16(info[0:], infoExport)
			binary.BigEndian.PutUint64(info[2:], uint64(size))
			binary.BigEndian.PutUint16(info[10:], transmissionFlags(exp))
			if err := writeOptReply(conn, hdr.Option, repInfo, info); err != nil {
				return nil, err
			}
			if err := writeOptReply(conn, hdr.Option, repAck, nil); err != nil {
				return nil, err
			}
			if hdr.Option == optGo {
				return exp, nil
			}

		case optAbort:
			writeOptReply(conn, hdr.Option, repAck, nil)
			return nil, nil

		default:
			if err := writeOptReply(conn, hdr.Option, repErrUnsup, nil); err != nil {
				return nil, err
			}
		}
	}
}

func writeOptReply(w io.Writer, option, typ uint32, data []byte) error {
	buf := make([]byte, 20+len(data))
	binary.BigEndian.PutUint64(buf[0:], repMagic)
	binary.BigEndian.PutUint32(buf[8:], option)
	binary.BigEndian.PutUint32(buf[12:], typ)
	binary.BigEndian.PutUint32(buf[16:], uint32(len(data)))
	copy(buf[20:], data)
	_, err := w.Write(buf)
	return err
}

type request struct {
	Magic  uint32
	Flags  uint16
	Type   uint16
	Handle uint64
	Offset uint64
	Length uint32
}

func transmit(conn net.Conn, exp *Export) error {
	size, err := exp.Backend.Size()
	if err != nil {
		return err
	}
	var (
		writeMu  sync.Mutex
		inFlight sync.WaitGroup
		slots    = make(chan struct{}, maxInFlight)
		replyErr error
	)
	reply := func(handle uint64, code uint32, data []byte) {
		var hdr [16]byte
		binary.BigEndian.PutUint32(hdr[0:], replyMagic)
		binary.BigEndian.PutUint32(hdr[4:], code)
		binary.BigEndian.PutUint64(hdr[8:], handle)
		writeMu.Lock()
		defer writeMu.Unlock()
		if replyErr != nil {
			return
		}
		if _, err := conn.Write(hdr[:]); err != nil {
			replyErr = err
			return
		}
		if code == 0 && len(data) > 0 {
			if _, err := conn.Write(data); err != nil {
				replyErr = err
			}
		}
	}
	// Each request runs in its own goroutine; replies may complete out of
	// order, which NBD allows (they carry the request's handle).
	run := func(f func()) {
		slots <- struct{}{}
		inFlight.Add(1)
		go func() {
			defer func() { <-slots; inFlight.Done() }()
			f()
		}()
	}
	defer inFlight.Wait()

	for {
		var req request
		if err := binary.Read(conn, binary.BigEndian, &req); err != nil {
			if errors.Is(err, io.EOF) {
				return nil
			}
			return err
		}
		if req.Magic != requestMagic {
			return errors.New("bad request magic")
		}
		if req.Length > maxRequestSize {
			return fmt.Errorf("request too large (%d)", req.Length)
		}
		inRange := req.Offset <= uint64(size) && uint64(req.Length) <= uint64(size)-req.Offset

		switch req.Type {
		case cmdRead:
			if !inRange {
				reply(req.Handle, errInval, nil)
				continue
			}
			run(func() {
				buf := make([]byte, req.Length)
				if _, err := exp.Backend.ReadAt(buf, int64(req.Offset)); err != nil {
					reply(req.Handle, errIO, nil)
					return
				}
				reply(req.Handle, 0, buf)
			})

		case cmdWrite:
			buf := make([]byte, req.Length) // the payload follows the header on the wire
			if _, err := io.ReadFull(conn, buf); err != nil {
				return err
			}
			if exp.ReadOnly {
				reply(req.Handle, errPerm, nil)
				continue
			}
			if !inRange {
				reply(req.Handle, errInval, nil)
				continue
			}
			run(func() {
				if _, err := exp.Backend.WriteAt(buf, int64(req.Offset)); err != nil {
					reply(req.Handle, errIO, nil)
					return
				}
				if req.Flags&cmdFlagFUA != 0 {
					if err := exp.Backend.Sync(); err != nil {
						reply(req.Handle, errIO, nil)
						return
					}
				}
				reply(req.Handle, 0, nil)
			})

		case cmdFlush:
			// Covers every write already replied to, which is all a flush must.
			run(func() {
				if err := exp.Backend.Sync(); err != nil {
					reply(req.Handle, errIO, nil)
					return
				}
				reply(req.Handle, 0, nil)
			})

		case cmdDisc:
			inFlight.Wait()
			if !exp.ReadOnly {
				return exp.Backend.Sync()
			}
			return nil

		default: // trim, write-zeroes, ...: not advertised, and carry no payload
			reply(req.Handle, errInval, nil)
		}
	}
}
