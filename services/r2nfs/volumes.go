package main

import (
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	securejoin "github.com/cyphar/filepath-securejoin"
	"github.com/go-git/go-billy/v5"
	"github.com/go-git/go-billy/v5/osfs"
)

// VolumeMount authorizes one client IP to mount one directory of a volume,
// as NFS export "/<id>". Deregistering revokes it: the filesystem handed out
// at mount time checks the flag on every operation, so a guest that keeps
// old file handles after unmount gets nothing.
//
// The directory is pinned by an open handle and served through
// /proc/self/fd/N, so a sandbox with write access to a parent directory
// can't swap it for a symlink and redirect another mount onto host files.
type VolumeMount struct {
	ID       string
	ClientIP string
	Volume   string
	Subpath  string
	Dir      string // /proc/self/fd/N of handle
	ReadOnly bool
	handle   *os.File
	revoked  atomic.Bool
	ops      sync.RWMutex // held shared by every operation on the mount
}

// revoke stops serving the mount. It waits for operations in flight, then
// closes the directory handle right away: its fd number can't be reused
// under an operation, and nothing keeps the volume's filesystem busy, so an
// unmount right after this really detaches it.
func (m *VolumeMount) revoke() {
	m.ops.Lock()
	defer m.ops.Unlock()
	if !m.revoked.Swap(true) {
		m.handle.Close()
	}
}

// VolumeManager tracks volume mounts by ID.
type VolumeManager struct {
	root string // host directory holding all volumes; "" disables volumes

	mu     sync.RWMutex
	mounts map[string]*VolumeMount
}

func newVolumeManager(root string) *VolumeManager {
	return &VolumeManager{root: root, mounts: make(map[string]*VolumeMount)}
}

// Register authorizes clientIP to mount subpath of volume as "/<id>",
// creating subpath if needed. Registering an ID again with the same
// parameters is a no-op, so the daemon can replay its mounts after a restart
// without breaking guests' file handles; different parameters replace it.
func (m *VolumeManager) Register(id, clientIP, volume, subpath string, readOnly bool) error {
	if m.root == "" {
		return fmt.Errorf("volumes are not enabled (start with --volumes-dir)")
	}
	if !isValidID(volume) {
		return fmt.Errorf("invalid volume %q", volume)
	}
	m.mu.RLock()
	old := m.mounts[id]
	m.mu.RUnlock()
	if old != nil && old.ClientIP == clientIP && old.Volume == volume && old.Subpath == subpath && old.ReadOnly == readOnly {
		return nil
	}
	root, err := os.OpenFile(filepath.Join(m.root, volume), os.O_RDONLY|syscall.O_DIRECTORY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return fmt.Errorf("volume %q: %w", volume, err)
	}
	defer root.Close()
	// Resolves subpath inside the volume even if sandboxes planted symlinks in it.
	handle, err := securejoin.MkdirAllHandle(root, subpath, 0o755)
	if err != nil {
		return fmt.Errorf("volume %q subpath %q: %w", volume, subpath, err)
	}
	mount := &VolumeMount{
		ID:       id,
		ClientIP: clientIP,
		Volume:   volume,
		Subpath:  subpath,
		Dir:      fmt.Sprintf("/proc/self/fd/%d", handle.Fd()),
		ReadOnly: readOnly,
		handle:   handle,
	}
	m.mu.Lock()
	old = m.mounts[id]
	m.mounts[id] = mount
	m.mu.Unlock()
	if old != nil {
		old.revoke()
	}
	log.Printf("Registered volume mount %s: %s → %s/%s (readonly=%v)", id, clientIP, volume, subpath, readOnly)
	return nil
}

// Deregister removes and revokes a mount.
func (m *VolumeManager) Deregister(id string) {
	m.mu.Lock()
	mount := m.mounts[id]
	delete(m.mounts, id)
	m.mu.Unlock()
	if mount != nil {
		mount.revoke()
		log.Printf("Deregistered volume mount %s", id)
	}
}

// Lookup returns the mount with this ID if clientIP may use it.
func (m *VolumeManager) Lookup(id, clientIP string) *VolumeMount {
	m.mu.RLock()
	defer m.mu.RUnlock()
	mount := m.mounts[id]
	if mount == nil || mount.ClientIP != clientIP {
		return nil
	}
	return mount
}

// Count returns the number of registered mounts.
func (m *VolumeManager) Count() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return len(m.mounts)
}

// volumeFS serves one VolumeMount. Paths are resolved inside the mount
// directory (BoundOS), so symlinks a guest creates can't reach host files.
type volumeFS struct {
	billy.Filesystem
	mount *VolumeMount
}

func newVolumeFS(mount *VolumeMount) *volumeFS {
	return &volumeFS{Filesystem: osfs.New(mount.Dir, osfs.WithBoundOS()), mount: mount}
}

var errRevoked = fmt.Errorf("volume unmounted: %w", os.ErrPermission)

// enter starts an operation on the mount; call done when it's finished.
func (v *volumeFS) enter(write bool) (done func(), err error) {
	v.mount.ops.RLock()
	if v.mount.revoked.Load() {
		v.mount.ops.RUnlock()
		return nil, errRevoked
	}
	if write && v.mount.ReadOnly {
		v.mount.ops.RUnlock()
		return nil, os.ErrPermission
	}
	return v.mount.ops.RUnlock, nil
}

func (v *volumeFS) Capabilities() billy.Capability {
	if v.mount.ReadOnly {
		return billy.ReadCapability | billy.SeekCapability
	}
	return billy.DefaultCapabilities
}

func (v *volumeFS) Create(name string) (billy.File, error) {
	done, err := v.enter(true)
	if err != nil {
		return nil, err
	}
	defer done()
	return v.Filesystem.Create(name)
}

func (v *volumeFS) Open(name string) (billy.File, error) {
	done, err := v.enter(false)
	if err != nil {
		return nil, err
	}
	defer done()
	return v.Filesystem.Open(name)
}

func (v *volumeFS) OpenFile(name string, flag int, perm os.FileMode) (billy.File, error) {
	done, err := v.enter(flag&(os.O_WRONLY|os.O_RDWR|os.O_CREATE|os.O_TRUNC|os.O_APPEND) != 0)
	if err != nil {
		return nil, err
	}
	defer done()
	return v.Filesystem.OpenFile(name, flag, perm)
}

func (v *volumeFS) Stat(name string) (os.FileInfo, error) {
	done, err := v.enter(false)
	if err != nil {
		return nil, err
	}
	defer done()
	return v.Filesystem.Stat(name)
}

func (v *volumeFS) Lstat(name string) (os.FileInfo, error) {
	done, err := v.enter(false)
	if err != nil {
		return nil, err
	}
	defer done()
	// BoundOS can't place its own root when that's a /proc/self/fd link,
	// and the root is never a symlink to the guest anyway.
	if filepath.Clean("/"+name) == "/" {
		return os.Stat(v.mount.Dir)
	}
	return v.Filesystem.Lstat(name)
}

func (v *volumeFS) ReadDir(name string) ([]os.FileInfo, error) {
	done, err := v.enter(false)
	if err != nil {
		return nil, err
	}
	defer done()
	return v.Filesystem.ReadDir(name)
}

// ReadDirNames lists a directory without stat-ing its entries (go-nfs's
// DirNamesReader): readdir then stats only the entries it replies with.
func (v *volumeFS) ReadDirNames(name string) ([]string, error) {
	done, err := v.enter(false)
	if err != nil {
		return nil, err
	}
	defer done()
	p, err := v.hostPath(name)
	if err != nil {
		return nil, err
	}
	dir, err := os.OpenFile(p, os.O_RDONLY|syscall.O_DIRECTORY, 0)
	if err != nil {
		return nil, err
	}
	defer dir.Close()
	return dir.Readdirnames(-1)
}

func (v *volumeFS) Readlink(name string) (string, error) {
	done, err := v.enter(false)
	if err != nil {
		return "", err
	}
	defer done()
	return v.Filesystem.Readlink(name)
}

func (v *volumeFS) Rename(from, to string) error {
	done, err := v.enter(true)
	if err != nil {
		return err
	}
	defer done()
	return v.Filesystem.Rename(from, to)
}

func (v *volumeFS) Remove(name string) error {
	done, err := v.enter(true)
	if err != nil {
		return err
	}
	defer done()
	return v.Filesystem.Remove(name)
}

func (v *volumeFS) MkdirAll(name string, perm os.FileMode) error {
	done, err := v.enter(true)
	if err != nil {
		return err
	}
	defer done()
	return v.Filesystem.MkdirAll(name, perm)
}

func (v *volumeFS) Symlink(target, link string) error {
	done, err := v.enter(true)
	if err != nil {
		return err
	}
	defer done()
	return v.Filesystem.Symlink(target, link)
}

func (v *volumeFS) TempFile(dir, prefix string) (billy.File, error) {
	done, err := v.enter(true)
	if err != nil {
		return nil, err
	}
	defer done()
	if dir == "" {
		dir = "/" // BoundOS would use the host's temp dir.
	}
	return v.Filesystem.TempFile(dir, prefix)
}

func (v *volumeFS) Chroot(string) (billy.Filesystem, error) {
	return nil, billy.ErrNotSupported
}

// billy.Change, which BoundOS lacks; go-nfs needs it for chmod/chown/utimes.

// hostPath resolves name inside the mount, following symlinks only within it.
func (v *volumeFS) hostPath(name string) (string, error) {
	return securejoin.SecureJoin(v.mount.Dir, name)
}

func (v *volumeFS) Chmod(name string, mode os.FileMode) error {
	done, err := v.enter(true)
	if err != nil {
		return err
	}
	defer done()
	p, err := v.hostPath(name)
	if err != nil {
		return err
	}
	return os.Chmod(p, mode)
}

func (v *volumeFS) Lchown(name string, uid, gid int) error {
	done, err := v.enter(true)
	if err != nil {
		return err
	}
	defer done()
	dir, err := v.hostPath(filepath.Dir(filepath.Clean("/" + name)))
	if err != nil {
		return err
	}
	return os.Lchown(filepath.Join(dir, filepath.Base(name)), uid, gid)
}

func (v *volumeFS) Chown(name string, uid, gid int) error {
	done, err := v.enter(true)
	if err != nil {
		return err
	}
	defer done()
	p, err := v.hostPath(name)
	if err != nil {
		return err
	}
	return os.Chown(p, uid, gid)
}

func (v *volumeFS) Chtimes(name string, atime, mtime time.Time) error {
	done, err := v.enter(true)
	if err != nil {
		return err
	}
	defer done()
	p, err := v.hostPath(name)
	if err != nil {
		return err
	}
	return os.Chtimes(p, atime, mtime)
}
