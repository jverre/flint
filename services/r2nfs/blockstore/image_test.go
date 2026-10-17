package blockstore

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io/fs"
	"path/filepath"
	"sync"
	"testing"
)

const testSize = 3*ChunkSize + 5*BlockSize // spans a partial last chunk

func newImage(t *testing.T, store Store, m Manifest) *Image {
	t.Helper()
	if m.Size == 0 {
		m.Size = testSize
	}
	img, err := Create(filepath.Join(t.TempDir(), "img"), store, m)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { img.Close() })
	return img
}

func write(t *testing.T, img *Image, off int64, data string) {
	t.Helper()
	if _, err := img.WriteAt([]byte(data), off); err != nil {
		t.Fatalf("write at %d: %v", off, err)
	}
}

func read(t *testing.T, img *Image, off int64, n int) string {
	t.Helper()
	buf := make([]byte, n)
	if _, err := img.ReadAt(buf, off); err != nil {
		t.Fatalf("read at %d: %v", off, err)
	}
	return string(buf)
}

func zeros(n int) string { return string(make([]byte, n)) }

func TestNewImageReadsZeros(t *testing.T) {
	img := newImage(t, &LocalStore{Dir: t.TempDir()}, Manifest{})
	if got := read(t, img, testSize-100, 100); got != zeros(100) {
		t.Fatalf("got %q", got)
	}
}

func TestPartialWriteKeepsRestOfBlockFromLayers(t *testing.T) {
	img := newImage(t, &LocalStore{Dir: t.TempDir()}, Manifest{})
	write(t, img, ChunkSize+10, "base-data-spanning")
	if _, err := img.Snapshot(); err != nil {
		t.Fatal(err)
	}
	write(t, img, ChunkSize+15, "XX") // inside the block, over the layer's bytes
	if got := read(t, img, ChunkSize+10, 18); got != "base-XXta-spanning" {
		t.Fatalf("got %q", got)
	}
}

func TestWritesAcrossBlocksAndChunks(t *testing.T) {
	img := newImage(t, &LocalStore{Dir: t.TempDir()}, Manifest{})
	data := bytes.Repeat([]byte("0123456789"), BlockSize/2) // 5 blocks
	off := int64(ChunkSize - BlockSize - 3)
	if _, err := img.WriteAt(data, off); err != nil {
		t.Fatal(err)
	}
	if _, err := img.Snapshot(); err != nil {
		t.Fatal(err)
	}
	if got := read(t, img, off, len(data)); got != string(data) {
		t.Fatal("data changed after snapshot")
	}
	if got := read(t, img, off-5, 5); got != zeros(5) {
		t.Fatalf("bytes before the write: %q", got)
	}
}

func TestForkIsIndependent(t *testing.T) {
	store := &LocalStore{Dir: t.TempDir()}
	base := newImage(t, store, Manifest{})
	write(t, base, 0, "shared")
	m, err := base.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	fork := newImage(t, store, m)
	write(t, fork, 0, "forked")
	write(t, base, 6, "-base")
	if got := read(t, fork, 0, 11); got != "forked"+zeros(5) {
		t.Fatalf("fork: %q", got)
	}
	if got := read(t, base, 0, 11); got != "shared-base" {
		t.Fatalf("base: %q", got)
	}
}

func TestZeroedChunkHidesLowerLayers(t *testing.T) {
	store := &LocalStore{Dir: t.TempDir()}
	img := newImage(t, store, Manifest{})
	write(t, img, 2*ChunkSize, "secret")
	before, err := img.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	write(t, img, 2*ChunkSize, zeros(6))
	if _, err := img.Snapshot(); err != nil {
		t.Fatal(err)
	}
	if got := read(t, img, 2*ChunkSize, 6); got != zeros(6) {
		t.Fatalf("got %q", got)
	}
	old := newImage(t, store, before)
	if got := read(t, old, 2*ChunkSize, 6); got != "secret" {
		t.Fatalf("older snapshot: %q", got)
	}
}

func TestSyncedWritesSurviveReopen(t *testing.T) {
	store := &LocalStore{Dir: t.TempDir()}
	dir := filepath.Join(t.TempDir(), "img")
	img, err := Create(dir, store, Manifest{Size: testSize})
	if err != nil {
		t.Fatal(err)
	}
	write(t, img, 100, "layered")
	if _, err := img.Snapshot(); err != nil {
		t.Fatal(err)
	}
	write(t, img, 200, "overlay")
	if err := img.Sync(); err != nil {
		t.Fatal(err)
	}
	img.Close()

	img, err = Open(dir, store)
	if err != nil {
		t.Fatal(err)
	}
	defer img.Close()
	if got := read(t, img, 100, 7) + read(t, img, 200, 7); got != "layeredoverlay" {
		t.Fatalf("got %q", got)
	}
}

func TestReadOnlyImageRefusesWrites(t *testing.T) {
	img := newImage(t, &LocalStore{Dir: t.TempDir()}, Manifest{ReadOnly: true})
	if _, err := img.WriteAt([]byte("x"), 0); !errors.Is(err, ErrReadOnly) {
		t.Fatalf("got %v", err)
	}
}

type fakeRemote struct {
	mu      sync.Mutex
	objects map[string][]byte
	gets    []string
}

func (r *fakeRemote) GetObject(_ context.Context, key string) ([]byte, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.gets = append(r.gets, key)
	data, ok := r.objects[key]
	if !ok {
		return nil, fmt.Errorf("%s: %w", key, fs.ErrNotExist)
	}
	return data, nil
}

func (r *fakeRemote) PutObject(_ context.Context, key string, data []byte) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.objects[key] = append([]byte(nil), data...)
	return nil
}

func TestImageOpensLazilyOnAnotherHost(t *testing.T) {
	remote := &fakeRemote{objects: map[string][]byte{}}
	src := newImage(t, &TieredStore{Local: &LocalStore{Dir: t.TempDir()}, Remote: remote}, Manifest{})
	write(t, src, 0, "chunk-zero")
	write(t, src, 2*ChunkSize, "chunk-two")
	m, err := src.Snapshot()
	if err != nil {
		t.Fatal(err)
	}

	// A host with an empty cache: only the layer index is fetched up front.
	remote.gets = nil
	dst := newImage(t, &TieredStore{Local: &LocalStore{Dir: t.TempDir()}, Remote: remote}, m)
	if len(remote.gets) != 1 {
		t.Fatalf("opening fetched %v, want just the layer", remote.gets)
	}
	if got := read(t, dst, 2*ChunkSize, 9); got != "chunk-two" {
		t.Fatalf("got %q", got)
	}
	read(t, dst, 2*ChunkSize+100, 10) // cached now
	if len(remote.gets) != 2 {
		t.Fatalf("fetches %v, want the layer and one chunk", remote.gets)
	}
}
