package main

import (
	"bufio"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
	"unsafe"

	"github.com/jverre/flint/services/r2nfs/blockstore"
	"github.com/jverre/flint/services/r2nfs/nbd"
	"golang.org/x/sys/unix"
)

// ImageManager serves block images (blockstore) two ways:
//
//   - Mounted on the host (shared volumes): attached to a host NBD device and
//     mounted where volumes are served from, so an image-backed volume is
//     shared over NFS exactly like a directory volume.
//   - Exported to a sandbox (exclusive volumes, and read-only snapshots of
//     them): served over NBD on the bridge to the one client IP allowed to
//     attach it, whose own kernel mounts the filesystem.
//
// An image a sandbox has written is marked guest-written and the host never
// mounts it again: its filesystem is untrusted input for the host kernel.
type ImageManager struct {
	dir        string // image directories: dir/<id>
	store      blockstore.Store
	local      *blockstore.LocalStore // store's local part, at dir/store
	volumesDir string                 // image <id> is mounted at volumesDir/<id>

	servingNBD atomic.Bool // sandboxes can attach exports

	mu       sync.Mutex
	attached map[string]*attachment  // host-mounted images, by image id
	exported map[string]*sharedImage // images open for exports, by image id
	exports  map[string]*nbdExport   // by export name
}

// sharedImage is an image opened once for all its exports.
type sharedImage struct {
	img  *blockstore.Image
	refs int
}

// nbdExport lets one client IP attach one image. Revoking it makes the
// image unreachable through connections already open.
type nbdExport struct {
	name, imageID, clientIP string
	readOnly                bool
	revoked                 atomic.Bool
}

type attachment struct {
	img    *blockstore.Image
	dev    string
	device *nbd.Device
	conn   net.Conn // our end of the socket the kernel serves the device over
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
	m := &ImageManager{
		dir: dir, store: store, local: local, volumesDir: volumesDir,
		attached: map[string]*attachment{}, exported: map[string]*sharedImage{}, exports: map[string]*nbdExport{},
	}
	go m.flushLoop()
	return m, nil
}

// flushInterval bounds how much a crash of this process can lose of what
// clients haven't synced yet: an NFS COMMIT (a guest's fsync) flushes its
// file through to the image right away, everything else within a second.
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

func (m *ImageManager) imageDir(id string) string  { return filepath.Join(m.dir, id) }
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
		manifest.Size, manifest.Layers, manifest.GuestWritten = src.Size, src.Layers, src.GuestWritten
	}
	img, err := blockstore.Create(m.imageDir(id), m.store, manifest)
	if err != nil {
		return err
	}
	return img.Close()
}

// snapshotLocked snapshots image id, freezing its filesystem while it does
// if the image is mounted on the host, and returns the resulting manifest.
// An image exported to a sandbox is snapshotted through its open instance;
// the caller freezes the sandbox's filesystem first.
func (m *ImageManager) snapshotLocked(id string) (blockstore.Manifest, error) {
	if sh := m.exported[id]; sh != nil {
		return sh.img.Snapshot()
	}
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
	if m.exported[id] != nil {
		return fmt.Errorf("image %s is attached to a sandbox", id)
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
	if img.Manifest().GuestWritten {
		img.Close()
		return fmt.Errorf("image %s has been written by a sandbox; the host won't mount it", id)
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

// Unmount unmounts and detaches image id.
func (m *ImageManager) Unmount(id string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	a := m.attached[id]
	if a == nil {
		return
	}
	path := m.mountPath(id)
	syncFS(path)
	unmountAndWait(path, a.dev)
	delete(m.attached, id)
	m.detach(a)
	log.Printf("Unmounted image %s", id)
}

// unmountAndWait unmounts the filesystem on dev at path, and makes sure it's
// gone before the device is disconnected: a filesystem still alive then is
// shut down mid-flight and pins the device in the kernel, whose next user
// gets I/O errors. If something still holds it, the unmount is lazy and
// waits a while for the filesystem to be released.
func unmountAndWait(path, dev string) {
	if err := syscall.Unmount(path, 0); err == nil || errors.Is(err, syscall.EINVAL) {
		return // unmounted, or wasn't mounted
	}
	syscall.Unmount(path, syscall.MNT_DETACH)
	alive := filepath.Join("/sys/fs/ext4", filepath.Base(dev))
	deadline := time.Now().Add(10 * time.Second)
	for {
		if _, err := os.Stat(alive); err != nil {
			return
		}
		if time.Now().After(deadline) {
			log.Printf("filesystem on %s still in use 10s after unmounting %s; detaching anyway", dev, path)
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// Delete removes image id (unmounting it first), wiping its overlay and the
// chunks and layers no other image shares (see blockstore/wipe.go).
func (m *ImageManager) Delete(id string) error {
	m.mu.Lock()
	exported := m.exported[id] != nil
	m.mu.Unlock()
	if exported {
		return fmt.Errorf("image %s is attached to a sandbox", id)
	}
	m.Unmount(id)
	m.mu.Lock()
	defer m.mu.Unlock()
	wiped, err := blockstore.Delete(m.imageDir(id))
	if err != nil {
		return err
	}
	var open []blockstore.Manifest
	for _, a := range m.attached {
		open = append(open, a.img.Manifest())
	}
	for _, sh := range m.exported {
		open = append(open, sh.img.Manifest())
	}
	n, freed, err := collectGarbage(m.dir, m.store, m.local, open)
	if err != nil {
		return fmt.Errorf("image %s: collecting unshared chunks failed; retry deletion: %w", id, err)
	}
	log.Printf("Deleted image %s: wiped %d bytes of overlay, %d unshared objects (%d bytes)", id, wiped, n, freed)
	return nil
}

// collectGarbage wipes and removes the objects in local that no image under
// imagesDir references, nor any of open (the manifests of images open in
// memory). It keeps everything if it can't read every image's references.
// It must not run while an image is being created or snapshotted.
func collectGarbage(imagesDir string, store blockstore.Store, local *blockstore.LocalStore, open []blockstore.Manifest) (removed int, freed int64, err error) {
	manifests := append([]blockstore.Manifest(nil), open...)
	entries, err := os.ReadDir(imagesDir)
	if err != nil {
		return 0, 0, err
	}
	for _, e := range entries {
		dir := filepath.Join(imagesDir, e.Name())
		if !e.IsDir() || dir == filepath.Clean(local.Dir) {
			continue
		}
		m, err := blockstore.ReadManifest(dir)
		if errors.Is(err, fs.ErrNotExist) {
			continue // not an image
		}
		if err != nil {
			return 0, 0, err
		}
		manifests = append(manifests, m)
	}
	keep := map[string]bool{}
	for _, m := range manifests {
		refs, err := blockstore.References(store, m)
		if err != nil {
			return 0, 0, err
		}
		for k := range refs {
			keep[k] = true
		}
	}
	return local.Collect(keep)
}

// Export lets clientIP attach image imageID over NBD as export name. A
// read-write export is exclusive, and its first connection marks the image
// guest-written;
// read-only exports are only of read-only images (snapshots), and any
// number may coexist. Exporting the same name again with the same
// parameters is a no-op, so the daemon can replay after a restart.
func (m *ImageManager) Export(name, imageID, clientIP string, readOnly bool) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if e := m.exports[name]; e != nil {
		if e.imageID == imageID && e.clientIP == clientIP && e.readOnly == readOnly {
			return nil
		}
		return fmt.Errorf("export %s exists for another image or client", name)
	}
	if m.attached[imageID] != nil {
		return fmt.Errorf("image %s is mounted on the host; unmount it first", imageID)
	}
	for _, e := range m.exports {
		if e.imageID == imageID && (!readOnly || !e.readOnly) {
			return fmt.Errorf("image %s is attached read-write to another sandbox", imageID)
		}
	}
	sh := m.exported[imageID]
	if sh == nil {
		img, err := blockstore.Open(m.imageDir(imageID), m.store)
		if err != nil {
			return err
		}
		sh = &sharedImage{img: img}
	}
	manifest := sh.img.Manifest()
	var err error
	switch {
	case readOnly && !manifest.ReadOnly:
		err = fmt.Errorf("only read-only images (snapshots) can be attached read-only")
	case !readOnly && manifest.ReadOnly:
		err = fmt.Errorf("image %s is read-only", imageID)
	}
	if err != nil {
		if sh.refs == 0 {
			sh.img.Close()
		}
		return err
	}
	sh.refs++
	m.exported[imageID] = sh
	m.exports[name] = &nbdExport{name: name, imageID: imageID, clientIP: clientIP, readOnly: readOnly}
	log.Printf("Exported image %s as %s to %s (readonly=%v)", imageID, name, clientIP, readOnly)
	return nil
}

// Unexport revokes an export and closes its image when no export uses it.
func (m *ImageManager) Unexport(name string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	e := m.exports[name]
	if e == nil {
		return
	}
	e.revoked.Store(true)
	delete(m.exports, name)
	if sh := m.exported[e.imageID]; sh != nil {
		if sh.refs--; sh.refs == 0 {
			delete(m.exported, e.imageID)
			if err := sh.img.Close(); err != nil {
				log.Printf("closing image %s: %v", e.imageID, err)
			}
		}
	}
	log.Printf("Unexported %s", name)
}

// ServeNBD accepts sandboxes' NBD connections on l. Each client can attach
// only the exports registered for its IP with this listener's mode.
func (m *ImageManager) ServeNBD(l net.Listener, readOnly bool) error {
	for {
		conn, err := l.Accept()
		if err != nil {
			return err
		}
		if tcp, ok := conn.(*net.TCPConn); ok {
			// Notices sandboxes that died without hanging up.
			tcp.SetKeepAlive(true)
			tcp.SetKeepAlivePeriod(30 * time.Second)
		}
		clientIP := extractIP(conn.RemoteAddr())
		go func() {
			if err := nbd.Serve(conn, m.lookup(clientIP, readOnly)); err != nil && !errors.Is(err, net.ErrClosed) {
				log.Printf("NBD client %s: %v", clientIP, err)
			}
		}()
	}
}

func (m *ImageManager) lookup(clientIP string, readOnly bool) nbd.Lookup {
	return func(name string) (*nbd.Export, error) {
		m.mu.Lock()
		defer m.mu.Unlock()
		e := m.exports[name]
		if e == nil || e.clientIP != clientIP || e.readOnly != readOnly {
			return nil, fmt.Errorf("no export %q for %s", name, clientIP)
		}
		img := m.exported[e.imageID].img
		// From the first connection on, the sandbox may write anything.
		if !readOnly {
			if err := img.MarkGuestWritten(); err != nil {
				return nil, err
			}
		}
		return &nbd.Export{Backend: &exportBackend{e: e, img: img}, ReadOnly: readOnly}, nil
	}
}

// exportBackend serves an export until it is revoked.
type exportBackend struct {
	e   *nbdExport
	img *blockstore.Image
}

var errExportRevoked = errors.New("export revoked")

func (b *exportBackend) ReadAt(p []byte, off int64) (int, error) {
	if b.e.revoked.Load() {
		return 0, errExportRevoked
	}
	return b.img.ReadAt(p, off)
}

func (b *exportBackend) WriteAt(p []byte, off int64) (int, error) {
	if b.e.revoked.Load() {
		return 0, errExportRevoked
	}
	return b.img.WriteAt(p, off)
}

func (b *exportBackend) Size() (int64, error) { return b.img.Size() }

func (b *exportBackend) Sync() error {
	if b.e.revoked.Load() {
		return errExportRevoked
	}
	return b.img.Sync()
}

// Count returns the number of mounted images.
func (m *ImageManager) Count() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.attached)
}

func (m *ImageManager) attachLocked(img *blockstore.Image, readOnly bool) (*attachment, error) {
	inUse := map[string]bool{}
	for _, a := range m.attached {
		inUse[a.dev] = true
	}
	export := &nbd.Export{Backend: img, ReadOnly: readOnly}
	// Each attempt gets a socket pair: our end serves the image, the other
	// goes to the kernel.
	dial := func() (nbd.FileConn, error) {
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
			if err := nbd.Serve(serverConn, func(string) (*nbd.Export, error) { return export, nil }); err != nil && !errors.Is(err, net.ErrClosed) {
				log.Printf("NBD server: %v", err)
			}
		}()
		return clientConn.(*net.UnixConn), nil
	}
	device, conn, err := nbd.AttachFree(dial, "default", 0, inUse)
	if err != nil {
		return nil, err
	}
	return &attachment{img: img, dev: device.Path, device: device, conn: conn.(net.Conn)}, nil
}

func (m *ImageManager) detach(a *attachment) {
	if err := a.device.Disconnect(); err != nil {
		log.Printf("NBD %s: %v", a.dev, err)
	}
	a.conn.Close()
	if err := a.img.Close(); err != nil {
		log.Printf("closing image on %s: %v", a.dev, err)
	}
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
