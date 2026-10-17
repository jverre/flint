package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"github.com/jverre/flint/services/r2nfs/blockstore"
	"github.com/pojntfx/go-nbd/pkg/client"
	"github.com/pojntfx/go-nbd/pkg/server"
	"golang.org/x/sys/unix"
)

// ImageManager serves block images (blockstore) to the host as NBD devices
// and mounts their filesystems where volumes are served from, so an
// image-backed volume is shared exactly like a directory volume.
type ImageManager struct {
	dir        string // image directories: dir/<id>
	store      blockstore.Store
	volumesDir string // image <id> is mounted at volumesDir/<id>

	mu       sync.Mutex
	attached map[string]*attachment
}

type attachment struct {
	img     *blockstore.Image
	dev     string
	devFile *os.File
	done    chan error // client.Connect's result: the device was disconnected
}

func newImageManager(dir, volumesDir string, remote blockstore.Remote) (*ImageManager, error) {
	if err := os.MkdirAll(filepath.Join(dir, "store"), 0o700); err != nil {
		return nil, err
	}
	if _, err := os.Stat("/dev/nbd0"); err != nil {
		if out, err := exec.Command("modprobe", "nbd", "nbds_max=256").CombinedOutput(); err != nil {
			return nil, fmt.Errorf("NBD unavailable (modprobe nbd: %v: %s)", err, strings.TrimSpace(string(out)))
		}
	}
	local := &blockstore.LocalStore{Dir: filepath.Join(dir, "store")}
	var store blockstore.Store = local
	if remote != nil {
		store = &blockstore.TieredStore{Local: local, Remote: prefixedRemote{"blocks/", remote}}
	}
	m := &ImageManager{dir: dir, store: store, volumesDir: volumesDir, attached: map[string]*attachment{}}
	go m.flushLoop()
	return m, nil
}

// flushInterval bounds how much a crash of this process can lose: NFS COMMIT
// doesn't reach the host filesystem (go-nfs answers it itself), so mounted
// images are flushed on a timer instead.
const flushInterval = time.Second

func (m *ImageManager) flushLoop() {
	for range time.Tick(flushInterval) {
		m.Flush()
	}
}

// Flush writes everything written to mounted images through to their storage.
func (m *ImageManager) Flush() {
	m.mu.Lock()
	defer m.mu.Unlock()
	for id, a := range m.attached {
		if a.img.Manifest().ReadOnly {
			continue
		}
		syncFS(m.mountPath(id)) // flushes the filesystem, then the device (Image.Sync)
		if err := a.img.Sync(); err != nil {
			log.Printf("flushing image %s: %v", id, err)
		}
	}
}

// prefixedRemote keeps image objects under one prefix of the bucket.
type prefixedRemote struct {
	prefix string
	r      blockstore.Remote
}

func (p prefixedRemote) GetObject(ctx context.Context, key string) ([]byte, error) {
	return p.r.GetObject(ctx, p.prefix+key)
}

func (p prefixedRemote) PutObject(ctx context.Context, key string, data []byte) error {
	return p.r.PutObject(ctx, p.prefix+key, data)
}

func (m *ImageManager) imageDir(id string) string { return filepath.Join(m.dir, id) }
func (m *ImageManager) mountPath(id string) string { return filepath.Join(m.volumesDir, id) }

// Create makes image id: empty and size bytes, or a copy of image from
// (which is snapshotted, frozen if mounted, and shares all its chunks).
func (m *ImageManager) Create(id string, size int64, from string, readOnly bool) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	manifest := blockstore.Manifest{Size: size, ReadOnly: readOnly}
	if from != "" {
		src, err := m.snapshotLocked(from)
		if err != nil {
			return err
		}
		manifest.Size, manifest.Layers = src.Size, src.Layers
	}
	img, err := blockstore.Create(m.imageDir(id), m.store, manifest)
	if err != nil {
		return err
	}
	return img.Close()
}

// snapshotLocked snapshots image id, freezing its filesystem while it does
// if the image is mounted, and returns the resulting manifest.
func (m *ImageManager) snapshotLocked(id string) (blockstore.Manifest, error) {
	if a := m.attached[id]; a != nil && !a.img.Manifest().ReadOnly {
		thaw, err := freeze(m.mountPath(id))
		if err != nil {
			return blockstore.Manifest{}, err
		}
		defer thaw()
		return a.img.Snapshot()
	}
	img, err := blockstore.Open(m.imageDir(id), m.store)
	if err != nil {
		return blockstore.Manifest{}, err
	}
	defer img.Close()
	return img.Snapshot()
}

// Mount attaches image id to an NBD device and mounts it at volumesDir/<id>,
// formatting it on first use. Mounting a mounted image is a no-op, so the
// daemon can replay its volumes after a restart.
func (m *ImageManager) Mount(id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.attached[id] != nil {
		return nil
	}
	path := m.mountPath(id)
	// A mount left by a previous run of this server is dead: its device went
	// with the process.
	if isMountpoint(path) {
		syscall.Unmount(path, syscall.MNT_DETACH)
	}
	img, err := blockstore.Open(m.imageDir(id), m.store)
	if err != nil {
		return err
	}
	readOnly := img.Manifest().ReadOnly
	a, err := m.attachLocked(img, readOnly)
	if err != nil {
		img.Close()
		return err
	}
	fail := func(err error) error {
		m.detach(a)
		return err
	}
	if !readOnly && !hasFilesystem(a.dev) {
		if out, err := exec.Command("mkfs.ext4", "-q", "-E", "lazy_itable_init=1,lazy_journal_init=1", a.dev).CombinedOutput(); err != nil {
			return fail(fmt.Errorf("mkfs.ext4: %v: %s", err, strings.TrimSpace(string(out))))
		}
	}
	if err := os.MkdirAll(path, 0o755); err != nil {
		return fail(err)
	}
	opts := "noatime"
	if readOnly {
		opts = "ro,noload" // snapshots are taken frozen: no journal to replay
	}
	if out, err := exec.Command("mount", "-t", "ext4", "-o", opts, a.dev, path).CombinedOutput(); err != nil {
		return fail(fmt.Errorf("mount: %v: %s", err, strings.TrimSpace(string(out))))
	}
	m.attached[id] = a
	log.Printf("Mounted image %s (%s) at %s", id, a.dev, path)
	return nil
}

// Unmount unmounts and detaches image id. Lazily: the NFS server may still
// hold handles of mounts it has just revoked.
func (m *ImageManager) Unmount(id string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	a := m.attached[id]
	if a == nil {
		return
	}
	path := m.mountPath(id)
	syncFS(path)
	syscall.Unmount(path, syscall.MNT_DETACH)
	delete(m.attached, id)
	m.detach(a)
	log.Printf("Unmounted image %s", id)
}

// Delete removes image id (unmounting it first). Its chunks stay in the
// store; other images may share them.
func (m *ImageManager) Delete(id string) error {
	m.Unmount(id)
	return os.RemoveAll(m.imageDir(id))
}

// Count returns the number of mounted images.
func (m *ImageManager) Count() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.attached)
}

func (m *ImageManager) attachLocked(img *blockstore.Image, readOnly bool) (*attachment, error) {
	dev, err := m.freeDeviceLocked()
	if err != nil {
		return nil, err
	}
	fds, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM|syscall.SOCK_CLOEXEC, 0)
	if err != nil {
		return nil, err
	}
	serverConn, err := fileConn(fds[0])
	if err != nil {
		syscall.Close(fds[1])
		return nil, err
	}
	clientConn, err := fileConn(fds[1])
	if err != nil {
		serverConn.Close()
		return nil, err
	}
	go func() {
		exports := []*server.Export{{Name: "default", Backend: img}}
		if err := server.Handle(serverConn, exports, &server.Options{ReadOnly: readOnly}); err != nil && !errors.Is(err, net.ErrClosed) {
			log.Printf("NBD server for %s: %v", dev, err)
		}
		serverConn.Close()
	}()
	devFile, err := os.OpenFile(dev, os.O_RDWR, 0)
	if err != nil {
		clientConn.Close()
		serverConn.Close()
		return nil, err
	}
	a := &attachment{img: img, dev: dev, devFile: devFile, done: make(chan error, 1)}
	ready := make(chan struct{})
	go func() {
		a.done <- client.Connect(clientConn.(*net.UnixConn), devFile, &client.Options{
			BlockSize:   4096,
			OnConnected: func() { close(ready) },
		})
		clientConn.Close()
	}()
	select {
	case <-ready:
		return a, nil
	case err := <-a.done:
		devFile.Close()
		return nil, fmt.Errorf("attaching %s: %v", dev, err)
	case <-time.After(10 * time.Second):
		client.Disconnect(devFile)
		devFile.Close()
		return nil, fmt.Errorf("attaching %s: timed out", dev)
	}
}

func (m *ImageManager) detach(a *attachment) {
	client.Disconnect(a.devFile)
	select {
	case <-a.done:
	case <-time.After(5 * time.Second):
		log.Printf("NBD %s: disconnect timed out", a.dev)
	}
	a.devFile.Close()
	if err := a.img.Close(); err != nil {
		log.Printf("closing image on %s: %v", a.dev, err)
	}
}

// freeDeviceLocked finds an NBD device nobody is connected to.
func (m *ImageManager) freeDeviceLocked() (string, error) {
	inUse := map[string]bool{}
	for _, a := range m.attached {
		inUse[a.dev] = true
	}
	devs, _ := filepath.Glob("/sys/block/nbd*")
	for _, sys := range devs {
		dev := "/dev/" + filepath.Base(sys)
		if inUse[dev] {
			continue
		}
		if _, err := os.Stat(filepath.Join(sys, "pid")); err == nil {
			continue // connected (another process, or a dying attachment)
		}
		if size, err := os.ReadFile(filepath.Join(sys, "size")); err == nil && strings.TrimSpace(string(size)) != "0" {
			continue
		}
		return dev, nil
	}
	return "", errors.New("no free NBD device")
}

func fileConn(fd int) (net.Conn, error) {
	f := os.NewFile(uintptr(fd), "nbd-socket")
	defer f.Close()
	return net.FileConn(f)
}

func hasFilesystem(dev string) bool {
	return exec.Command("blkid", "-p", dev).Run() == nil
}

func isMountpoint(path string) bool {
	f, err := os.Open("/proc/self/mountinfo")
	if err != nil {
		return false
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		if fields := strings.Fields(sc.Text()); len(fields) > 4 && fields[4] == path {
			return true
		}
	}
	return false
}

func syncFS(path string) {
	if f, err := os.Open(path); err == nil {
		unix.Syncfs(int(f.Fd()))
		f.Close()
	}
}

const (
	ioctlFIFREEZE = 0xC0045877
	ioctlFITHAW   = 0xC0045878
)

// freeze flushes and freezes the filesystem mounted at path; call the
// returned function to thaw it.
func freeze(path string) (func(), error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	var arg int
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), ioctlFIFREEZE, uintptr(unsafe.Pointer(&arg))); errno != 0 {
		f.Close()
		return nil, fmt.Errorf("freezing %s: %w", path, errno)
	}
	return func() {
		syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), ioctlFITHAW, uintptr(unsafe.Pointer(&arg)))
		f.Close()
	}, nil
}
