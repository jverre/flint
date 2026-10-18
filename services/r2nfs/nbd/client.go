package nbd

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"syscall"
	"time"
	"unsafe"
)

// Negotiate runs the client side of the handshake for export and returns its
// size and transmission flags. The connection is then ready for requests.
func Negotiate(conn io.ReadWriter, export string) (size uint64, flags uint16, err error) {
	var hello struct {
		Magic     uint64
		OptsMagic uint64
		Flags     uint16
	}
	if err := binary.Read(conn, binary.BigEndian, &hello); err != nil {
		return 0, 0, err
	}
	if hello.Magic != nbdMagic || hello.OptsMagic != optsMagic || hello.Flags&flagFixedNewstyle == 0 {
		return 0, 0, errors.New("not a fixed-newstyle NBD server")
	}
	noZeroes := hello.Flags&flagNoZeroes != 0
	clientFlags := uint32(flagFixedNewstyle)
	if noZeroes {
		clientFlags |= flagNoZeroes
	}
	opt := make([]byte, 20+len(export))
	binary.BigEndian.PutUint32(opt[0:], clientFlags)
	binary.BigEndian.PutUint64(opt[4:], optsMagic)
	binary.BigEndian.PutUint32(opt[12:], optExportName)
	binary.BigEndian.PutUint32(opt[16:], uint32(len(export)))
	copy(opt[20:], export)
	if _, err := conn.Write(opt); err != nil {
		return 0, 0, err
	}
	reply := make([]byte, 10)
	if _, err := io.ReadFull(conn, reply); err != nil {
		return 0, 0, fmt.Errorf("export %q refused: %w", export, err)
	}
	if !noZeroes {
		if _, err := io.CopyN(io.Discard, conn, 124); err != nil {
			return 0, 0, err
		}
	}
	return binary.BigEndian.Uint64(reply[0:]), binary.BigEndian.Uint16(reply[8:]), nil
}

// The kernel NBD client's ioctls (linux/nbd.h).
const (
	ioctlSetSock       = 0xab00
	ioctlSetBlksize    = 0xab01
	ioctlDoIt          = 0xab03
	ioctlClearSock     = 0xab04
	ioctlClearQue      = 0xab05
	ioctlSetSizeBlocks = 0xab07
	ioctlDisconnect    = 0xab08
	ioctlSetTimeout    = 0xab09
	ioctlSetFlags      = 0xab0a

	blockSize = 4096
)

// Device is a kernel NBD device attached to a server.
type Device struct {
	Path string
	file *os.File
	sock *os.File
	done chan error
}

// FileConn is a connection that can hand its socket to the kernel
// (*net.TCPConn, *net.UnixConn).
type FileConn interface {
	io.ReadWriter
	File() (*os.File, error)
}

// Attach negotiates export over conn, then gives the connection to the
// kernel to serve devPath (/dev/nbdN). It returns once the device is live.
// The caller must not use conn afterwards, except to close it after
// Disconnect.
func Attach(conn FileConn, devPath, export string, timeout time.Duration) (*Device, error) {
	size, flags, err := Negotiate(conn, export)
	if err != nil {
		return nil, err
	}
	sock, err := conn.File() // a blocking duplicate of the socket, for the kernel
	if err != nil {
		return nil, err
	}
	file, err := os.OpenFile(devPath, os.O_RDWR, 0)
	if err != nil {
		sock.Close()
		return nil, err
	}
	// Claim the device for as long as it's attached (the lock goes with the
	// file), so another process choosing a free device at the same moment
	// can't configure it under us: it sees EBUSY and moves on.
	if err := syscall.Flock(int(file.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		file.Close()
		sock.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			err = syscall.EBUSY
		}
		return nil, fmt.Errorf("%s: %w", devPath, err)
	}
	d := &Device{Path: devPath, file: file, sock: sock, done: make(chan error, 1)}
	for _, op := range []struct {
		req, arg uintptr
	}{
		{ioctlSetBlksize, blockSize},
		{ioctlSetSizeBlocks, uintptr(size / blockSize)},
		{ioctlSetFlags, uintptr(flags)},
		{ioctlSetTimeout, uintptr(timeout / time.Second)},
		{ioctlSetSock, sock.Fd()},
	} {
		if err := d.ioctl(op.req, op.arg); err != nil {
			d.ioctl(ioctlClearSock, 0)
			d.close()
			return nil, fmt.Errorf("%s: ioctl %#x: %w", devPath, op.req, err)
		}
	}
	go func() {
		// A signal interrupting NBD_DO_IT makes the kernel shut the device
		// down, and Go delivers process signals (SIGCHLD from every command
		// we run, like mkfs) to any thread. So this goroutine keeps its
		// thread, with every signal blocked; the thread exits with it.
		runtime.LockOSThread()
		blockSignals()
		err := d.ioctl(ioctlDoIt, 0) // serves requests until disconnected
		d.ioctl(ioctlClearQue, 0)
		d.ioctl(ioctlClearSock, 0)
		d.done <- err
	}()

	// Ready once the kernel serves the device (its pid attribute appears)
	// and has published its size, which some kernels do a moment later.
	sys := filepath.Join("/sys/block", filepath.Base(devPath))
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := os.Stat(filepath.Join(sys, "pid")); err == nil {
			if sz, err := os.ReadFile(filepath.Join(sys, "size")); err == nil && len(sz) > 0 && sz[0] != '0' {
				return d, nil
			}
		}
		select {
		case err := <-d.done:
			d.close()
			return nil, fmt.Errorf("%s: device stopped while starting: %w", devPath, err)
		default:
		}
		if time.Now().After(deadline) {
			d.Disconnect()
			return nil, fmt.Errorf("%s: not ready after 5s", devPath)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

// Disconnect detaches the device from the server.
func (d *Device) Disconnect() error {
	err := d.ioctl(ioctlDisconnect, 0)
	select {
	case <-d.done:
	case <-time.After(10 * time.Second):
		err = fmt.Errorf("%s: disconnect timed out", d.Path)
	}
	d.close()
	return err
}

func (d *Device) close() {
	d.file.Close()
	d.sock.Close()
}

// blockSignals blocks every signal on the calling thread.
func blockSignals() {
	all := ^uint64(0)
	const sigBlock = 0
	syscall.RawSyscall6(syscall.SYS_RT_SIGPROCMASK, sigBlock, uintptr(unsafe.Pointer(&all)), 0, unsafe.Sizeof(all), 0, 0)
}

func (d *Device) ioctl(req, arg uintptr) error {
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, d.file.Fd(), req, arg); errno != 0 {
		return errno
	}
	return nil
}

// AttachFree attaches export to the first free NBD device that takes it,
// skipping inUse. A device that was just detached can still be busy in the
// kernel for a moment, so on EBUSY it moves on to the next one, with a new
// connection from dial (each attempt consumes one).
func AttachFree(dial func() (FileConn, error), export string, timeout time.Duration, inUse map[string]bool) (*Device, FileConn, error) {
	skip := map[string]bool{}
	for dev := range inUse {
		skip[dev] = true
	}
	for attempt := 0; ; attempt++ {
		dev, err := FreeDevice(skip)
		if err != nil {
			return nil, nil, err
		}
		conn, err := dial()
		if err != nil {
			return nil, nil, err
		}
		d, err := Attach(conn, dev, export, timeout)
		if err == nil {
			return d, conn, nil
		}
		if c, ok := conn.(io.Closer); ok {
			c.Close()
		}
		if !errors.Is(err, syscall.EBUSY) || attempt == 15 {
			return nil, nil, err
		}
		skip[dev] = true
	}
}

// FreeDevice returns an NBD device nobody is attached to, skipping inUse.
func FreeDevice(inUse map[string]bool) (string, error) {
	devs, _ := filepath.Glob("/sys/block/nbd*")
	for _, sys := range devs {
		dev := "/dev/" + filepath.Base(sys)
		if inUse[dev] {
			continue
		}
		if _, err := os.Stat(filepath.Join(sys, "pid")); err == nil {
			continue
		}
		if size, err := os.ReadFile(filepath.Join(sys, "size")); err == nil && len(size) > 0 && string(size[:1]) != "0" {
			continue
		}
		return dev, nil
	}
	return "", errors.New("no free NBD device")
}
