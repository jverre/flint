package main

import (
	"bytes"
	"os"
	"path/filepath"
	"testing"

	"github.com/jverre/flint/services/r2nfs/blockstore"
)

// A deleted image's own data leaves the store; what another image still
// shares stays readable.
func TestCollectGarbageKeepsOnlySharedChunks(t *testing.T) {
	dir := t.TempDir()
	storeDir := filepath.Join(dir, "store")
	store := &blockstore.LocalStore{Dir: storeDir}
	size := int64(4 * blockstore.ChunkSize)

	base, err := blockstore.Create(filepath.Join(dir, "base"), store, blockstore.Manifest{Size: size})
	if err != nil {
		t.Fatal(err)
	}
	shared := bytes.Repeat([]byte("shared by both "), 1000)
	if _, err := base.WriteAt(shared, 0); err != nil {
		t.Fatal(err)
	}
	snap, err := base.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	// A fork shares base's layer; then base writes data only it has.
	fork, err := blockstore.Create(filepath.Join(dir, "fork"), store, blockstore.Manifest{Size: size, Layers: snap.Layers})
	if err != nil {
		t.Fatal(err)
	}
	fork.Close()
	private := bytes.Repeat([]byte("base's secret "), 1000)
	if _, err := base.WriteAt(private, blockstore.ChunkSize); err != nil {
		t.Fatal(err)
	}
	if _, err := base.Snapshot(); err != nil {
		t.Fatal(err)
	}
	base.Close()

	if _, err := wipeAll(filepath.Join(dir, "base")); err != nil {
		t.Fatal(err)
	}
	removed, _, err := collectGarbage(dir, store, storeDir, nil)
	if err != nil {
		t.Fatal(err)
	}
	if removed != 2 { // base's private chunk and its second layer
		t.Errorf("removed %d objects, want 2", removed)
	}
	filepath.Walk(storeDir, func(path string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			data, _ := os.ReadFile(path)
			if bytes.Contains(data, []byte("base's secret")) {
				t.Errorf("%s still holds the deleted image's data", path)
			}
		}
		return nil
	})
	fork, err = blockstore.Open(filepath.Join(dir, "fork"), store)
	if err != nil {
		t.Fatal(err)
	}
	defer fork.Close()
	got := make([]byte, len(shared))
	if _, err := fork.ReadAt(got, 0); err != nil || !bytes.Equal(got, shared) {
		t.Fatalf("the fork lost shared data: %v", err)
	}
}
