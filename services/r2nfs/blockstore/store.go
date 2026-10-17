// Package blockstore implements Flint's lazy copy-on-write block images.
//
// An image is a stack of immutable layers plus one writable overlay:
//
//   - A layer maps chunk indexes to content-addressed chunks. Chunks and
//     layers live in a Store: a local directory, optionally backed by a remote
//     object store, so an image can be opened on a host that has none of its
//     data and fetch chunks on first read.
//   - The overlay is a sparse file the size of the image plus a bitmap of the
//     blocks written since the last snapshot. Writing part of an unwritten
//     block copies just that block up from the layers, so the worst-case
//     overhead of a write is one block.
//
// Snapshotting turns the overlay into a new layer. Forking an image is a new
// manifest pointing at the same layers with an empty overlay, so it is cheap
// and shares every chunk.
package blockstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
)

// Store holds immutable, content-addressed objects (chunks and layers).
type Store interface {
	// Get returns the object stored under key, or an error wrapping
	// fs.ErrNotExist.
	Get(key string) ([]byte, error)
	// Put stores data under key. Keys are content hashes, so storing a key
	// twice must store the same bytes.
	Put(key string, data []byte) error
	// ReadAt reads part of an object. Reading past its end reads fewer bytes
	// (objects drop trailing zeros), without an error.
	ReadAt(key string, p []byte, off int64) (int, error)
}

// Remote is the subset of an object store a TieredStore needs.
type Remote interface {
	GetObject(ctx context.Context, key string) ([]byte, error)
	PutObject(ctx context.Context, key string, data []byte) error
}

func hashKey(prefix string, data []byte) string {
	sum := sha256.Sum256(data)
	return prefix + hex.EncodeToString(sum[:])
}

func validKey(key string) error {
	clean := filepath.Clean(key)
	if key == "" || clean != key || filepath.IsAbs(key) || clean == ".." || len(clean) > 2 && clean[:3] == "../" {
		return fmt.Errorf("invalid store key %q", key)
	}
	return nil
}

// LocalStore keeps objects as files under a directory.
type LocalStore struct {
	Dir string
}

func (s *LocalStore) path(key string) string {
	return filepath.Join(s.Dir, key)
}

func (s *LocalStore) Get(key string) ([]byte, error) {
	if err := validKey(key); err != nil {
		return nil, err
	}
	return os.ReadFile(s.path(key))
}

func (s *LocalStore) Put(key string, data []byte) error {
	if err := validKey(key); err != nil {
		return err
	}
	p := s.path(key)
	if _, err := os.Stat(p); err == nil {
		return nil // content-addressed: already there
	}
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(p), ".put-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), p)
}

func (s *LocalStore) ReadAt(key string, p []byte, off int64) (int, error) {
	if err := validKey(key); err != nil {
		return 0, err
	}
	f, err := os.Open(s.path(key))
	if err != nil {
		return 0, err
	}
	defer f.Close()
	n, err := f.ReadAt(p, off)
	if errors.Is(err, io.EOF) {
		err = nil
	}
	return n, err
}

// TieredStore reads through a local cache to a remote object store and
// writes to both, so images are durable remotely and fast locally.
type TieredStore struct {
	Local  *LocalStore
	Remote Remote
}

func (s *TieredStore) Get(key string) ([]byte, error) {
	data, err := s.Local.Get(key)
	if err == nil || !errors.Is(err, fs.ErrNotExist) {
		return data, err
	}
	data, err = s.Remote.GetObject(context.Background(), key)
	if err != nil {
		return nil, fmt.Errorf("%s: %w (%v)", key, fs.ErrNotExist, err)
	}
	if err := s.Local.Put(key, data); err != nil {
		return nil, err
	}
	return data, nil
}

func (s *TieredStore) ReadAt(key string, p []byte, off int64) (int, error) {
	n, err := s.Local.ReadAt(key, p, off)
	if err == nil || !errors.Is(err, fs.ErrNotExist) {
		return n, err
	}
	if _, err := s.Get(key); err != nil { // fetches the whole chunk into the cache
		return 0, err
	}
	return s.Local.ReadAt(key, p, off)
}

func (s *TieredStore) Put(key string, data []byte) error {
	if err := s.Remote.PutObject(context.Background(), key, data); err != nil {
		return err
	}
	return s.Local.Put(key, data)
}
