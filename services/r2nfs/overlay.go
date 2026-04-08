package main

import (
	"context"
	"fmt"
	"io/fs"
	"os"
	"path"
	"strings"
	"time"

	"github.com/go-git/go-billy/v5"
)

// OverlayFS implements billy.Filesystem with two R2 layers:
//   - Template layer (read-only): templates/{template_id}/
//   - Sandbox layer (read-write): sandboxes/{vm_id}/
//
// Reads check the sandbox layer first, then fall back to template.
// Writes always go to the sandbox layer.
// Deletes place whiteout markers to hide template files.
type OverlayFS struct {
	r2             *R2Client
	cache          *DiskCache
	sandboxPrefix  string
	templatePrefix string
}

func newOverlayFS(r2 *R2Client, cache *DiskCache, vmID, templateID string) *OverlayFS {
	return &OverlayFS{
		r2:             r2,
		cache:          cache,
		sandboxPrefix:  SandboxPrefix(vmID),
		templatePrefix: TemplatePrefix(templateID),
	}
}

// resolveKey finds the R2 key for a path, checking sandbox layer first.
// Returns the full key and which layer it came from.
func (o *OverlayFS) resolveKey(name string) (string, bool) {
	name = cleanPath(name)
	ctx := context.Background()

	// Check sandbox layer first.
	sandboxKey := o.sandboxPrefix + name
	if _, err := o.r2.HeadObject(ctx, sandboxKey); err == nil {
		return sandboxKey, true
	}

	// Check if whited out (deleted from overlay).
	whiteout := WhiteoutKey(o.sandboxPrefix, name)
	if _, err := o.r2.HeadObject(ctx, whiteout); err == nil {
		return "", false // Explicitly deleted
	}

	// Fall back to template layer.
	templateKey := o.templatePrefix + name
	if _, err := o.r2.HeadObject(ctx, templateKey); err == nil {
		return templateKey, true
	}

	return "", false
}

// --- billy.Filesystem implementation ---

func (o *OverlayFS) Create(filename string) (billy.File, error) {
	return o.OpenFile(filename, os.O_RDWR|os.O_CREATE|os.O_TRUNC, 0644)
}

func (o *OverlayFS) Open(filename string) (billy.File, error) {
	return o.OpenFile(filename, os.O_RDONLY, 0)
}

func (o *OverlayFS) OpenFile(filename string, flag int, perm os.FileMode) (billy.File, error) {
	filename = cleanPath(filename)
	ctx := context.Background()

	if flag&(os.O_WRONLY|os.O_RDWR|os.O_CREATE|os.O_TRUNC) != 0 {
		// Write mode: always target sandbox layer.
		return &r2File{
			name:    filename,
			key:     o.sandboxPrefix + filename,
			r2:      o.r2,
			cache:   o.cache,
			writable: true,
		}, nil
	}

	// Read mode: resolve from overlay.
	key, found := o.resolveKey(filename)
	if !found {
		return nil, &os.PathError{Op: "open", Path: filename, Err: os.ErrNotExist}
	}

	// Try cache first.
	cacheKey := key
	if data, ok := o.cache.Get(cacheKey); ok {
		return &r2File{
			name:    filename,
			key:     key,
			r2:      o.r2,
			cache:   o.cache,
			content: data,
		}, nil
	}

	data, err := o.r2.GetObject(ctx, key)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", filename, err)
	}
	o.cache.Put(cacheKey, data)

	return &r2File{
		name:    filename,
		key:     key,
		r2:      o.r2,
		cache:   o.cache,
		content: data,
	}, nil
}

func (o *OverlayFS) Stat(filename string) (os.FileInfo, error) {
	filename = cleanPath(filename)
	ctx := context.Background()

	if filename == "" || filename == "." {
		return &r2FileInfo{name: ".", size: 0, isDir: true, modTime: time.Now()}, nil
	}

	// Check as file first (sandbox then template).
	key, found := o.resolveKey(filename)
	if found {
		obj, err := o.r2.HeadObject(ctx, key)
		if err != nil {
			return nil, err
		}
		return &r2FileInfo{
			name:    path.Base(filename),
			size:    obj.Size,
			modTime: obj.LastModified,
		}, nil
	}

	// Check as directory (look for objects with this prefix).
	for _, prefix := range []string{o.sandboxPrefix, o.templatePrefix} {
		dirPrefix := prefix + filename + "/"
		objects, err := o.r2.ListObjects(ctx, dirPrefix)
		if err == nil && len(objects) > 0 {
			return &r2FileInfo{name: path.Base(filename), isDir: true, modTime: time.Now()}, nil
		}
	}

	return nil, &os.PathError{Op: "stat", Path: filename, Err: os.ErrNotExist}
}

func (o *OverlayFS) Rename(oldpath, newpath string) error {
	oldpath = cleanPath(oldpath)
	newpath = cleanPath(newpath)
	ctx := context.Background()

	// Read from overlay, write to sandbox layer.
	key, found := o.resolveKey(oldpath)
	if !found {
		return &os.PathError{Op: "rename", Path: oldpath, Err: os.ErrNotExist}
	}

	dstKey := o.sandboxPrefix + newpath
	if err := o.r2.CopyObject(ctx, key, dstKey); err != nil {
		return err
	}
	o.cache.Invalidate(key)
	o.cache.Invalidate(dstKey)

	return o.Remove(oldpath)
}

func (o *OverlayFS) Remove(filename string) error {
	filename = cleanPath(filename)
	ctx := context.Background()

	// Delete from sandbox layer if it exists there.
	sandboxKey := o.sandboxPrefix + filename
	o.r2.DeleteObject(ctx, sandboxKey) // ignore error
	o.cache.Invalidate(sandboxKey)

	// Place whiteout to hide the template-layer version.
	templateKey := o.templatePrefix + filename
	if _, err := o.r2.HeadObject(ctx, templateKey); err == nil {
		whiteout := WhiteoutKey(o.sandboxPrefix, filename)
		o.r2.PutObject(ctx, whiteout, []byte{})
	}

	return nil
}

func (o *OverlayFS) ReadDir(dirname string) ([]os.FileInfo, error) {
	dirname = cleanPath(dirname)
	ctx := context.Background()

	prefix := dirname
	if prefix != "" {
		prefix += "/"
	}

	seen := make(map[string]bool)
	whiteouts := make(map[string]bool)
	var result []os.FileInfo

	// Scan sandbox layer first.
	sandboxEntries, _ := o.r2.ListObjects(ctx, o.sandboxPrefix+prefix)
	for _, entry := range sandboxEntries {
		name := entry.Key
		if strings.HasPrefix(name, ".wh.") {
			// Track whiteouts — these hide template-layer files.
			whiteouts[strings.TrimPrefix(name, ".wh.")] = true
			continue
		}
		seen[name] = true
		result = append(result, &r2FileInfo{
			name:    name,
			size:    entry.Size,
			isDir:   entry.IsPrefix,
			modTime: entry.LastModified,
		})
	}

	// Scan template layer, skipping overridden and whited-out entries.
	templateEntries, _ := o.r2.ListObjects(ctx, o.templatePrefix+prefix)
	for _, entry := range templateEntries {
		name := entry.Key
		if seen[name] || whiteouts[name] {
			continue
		}
		result = append(result, &r2FileInfo{
			name:    name,
			size:    entry.Size,
			isDir:   entry.IsPrefix,
			modTime: entry.LastModified,
		})
	}

	return result, nil
}

func (o *OverlayFS) MkdirAll(dirname string, perm os.FileMode) error {
	dirname = cleanPath(dirname)
	ctx := context.Background()
	// Create a directory marker in the sandbox layer.
	key := o.sandboxPrefix + dirname + "/.keep"
	return o.r2.PutObject(ctx, key, []byte{})
}

// Lstat is the same as Stat (no symlink support).
func (o *OverlayFS) Lstat(filename string) (os.FileInfo, error) {
	return o.Stat(filename)
}

func (o *OverlayFS) Join(elem ...string) string {
	return path.Join(elem...)
}

func (o *OverlayFS) TempFile(dir, prefix string) (billy.File, error) {
	name := fmt.Sprintf("%s/%s%d", dir, prefix, time.Now().UnixNano())
	return o.Create(name)
}

func (o *OverlayFS) Chroot(p string) (billy.Filesystem, error) {
	return nil, fmt.Errorf("chroot not supported")
}

func (o *OverlayFS) Root() string {
	return "/"
}

func (o *OverlayFS) Readlink(link string) (string, error) {
	return "", fmt.Errorf("readlink not supported")
}

func (o *OverlayFS) Symlink(target, link string) error {
	return fmt.Errorf("symlink not supported")
}

func cleanPath(name string) string {
	name = strings.TrimPrefix(name, "/")
	name = strings.TrimSuffix(name, "/")
	if name == "." {
		return ""
	}
	return name
}

// --- r2File implements billy.File ---

type r2File struct {
	name     string
	key      string
	r2       *R2Client
	cache    *DiskCache
	content  []byte
	offset   int64
	writable bool
	buf      []byte
}

func (f *r2File) Name() string { return f.name }

func (f *r2File) Read(p []byte) (int, error) {
	if f.content == nil {
		return 0, fmt.Errorf("file not opened for reading")
	}
	if f.offset >= int64(len(f.content)) {
		return 0, fmt.Errorf("EOF")
	}
	n := copy(p, f.content[f.offset:])
	f.offset += int64(n)
	return n, nil
}

func (f *r2File) ReadAt(p []byte, off int64) (int, error) {
	if f.content == nil {
		return 0, fmt.Errorf("file not opened for reading")
	}
	if off >= int64(len(f.content)) {
		return 0, fmt.Errorf("EOF")
	}
	n := copy(p, f.content[off:])
	return n, nil
}

func (f *r2File) Write(p []byte) (int, error) {
	if !f.writable {
		return 0, fmt.Errorf("file not opened for writing")
	}
	f.buf = append(f.buf, p...)
	return len(p), nil
}

func (f *r2File) Seek(offset int64, whence int) (int64, error) {
	var newOffset int64
	switch whence {
	case 0:
		newOffset = offset
	case 1:
		newOffset = f.offset + offset
	case 2:
		newOffset = int64(len(f.content)) + offset
	}
	f.offset = newOffset
	return newOffset, nil
}

func (f *r2File) Close() error {
	if f.writable && f.buf != nil {
		ctx := context.Background()
		if err := f.r2.PutObject(ctx, f.key, f.buf); err != nil {
			return err
		}
		f.cache.Invalidate(f.key)
		f.cache.Put(f.key, f.buf)
	}
	return nil
}

func (f *r2File) Lock() error   { return nil }
func (f *r2File) Unlock() error { return nil }

func (f *r2File) Truncate(size int64) error {
	if size == 0 {
		f.buf = nil
	}
	return nil
}

// --- r2FileInfo implements os.FileInfo ---

type r2FileInfo struct {
	name    string
	size    int64
	isDir   bool
	modTime time.Time
}

func (fi *r2FileInfo) Name() string      { return fi.name }
func (fi *r2FileInfo) Size() int64       { return fi.size }
func (fi *r2FileInfo) Mode() fs.FileMode {
	if fi.isDir {
		return fs.ModeDir | 0755
	}
	return 0644
}
func (fi *r2FileInfo) ModTime() time.Time { return fi.modTime }
func (fi *r2FileInfo) IsDir() bool        { return fi.isDir }
func (fi *r2FileInfo) Sys() any           { return nil }
