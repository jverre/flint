package blockstore

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"sync"
)

const (
	// ChunkSize is the unit layers are stored and fetched in.
	ChunkSize = 4 << 20
	// BlockSize is the copy-on-write unit of the overlay.
	BlockSize = 64 << 10

	blocksPerChunk = ChunkSize / BlockSize

	// zeroChunk marks a chunk a layer knows to be all zeros, hiding lower layers.
	zeroChunk = "0"
)

var ErrReadOnly = errors.New("image is read-only")

// Manifest describes an image: its size and its layers, bottom first.
type Manifest struct {
	Size     int64    `json:"size"`
	Layers   []string `json:"layers"`
	ReadOnly bool     `json:"readonly,omitempty"`
	// GuestWritten records that a sandbox has had the raw image, so its
	// filesystem is untrusted: only guests may mount it, never the host
	// kernel. Copies inherit it.
	GuestWritten bool `json:"guest_written,omitempty"`
}

type layer struct {
	ChunkSize int64             `json:"chunk_size"`
	Chunks    map[string]string `json:"chunks"` // chunk index -> chunk key, or zeroChunk
}

// Image is an open block image. It is safe for concurrent use.
type Image struct {
	dir   string
	store Store

	mu       sync.RWMutex
	manifest Manifest
	layers   []layer // bottom first, parallel to manifest.Layers
	overlay  *os.File
	dirty    []byte // bitmap of overlay blocks
	unsynced bool   // bitmap changed since it was last persisted
}

const (
	manifestFile = "manifest.json"
	overlayFile  = "overlay.data"
	bitmapFile   = "overlay.bitmap"
)

// Create makes a new image in dir (which must not exist) on top of layers.
func Create(dir string, store Store, m Manifest) (*Image, error) {
	if m.Size <= 0 || m.Size%BlockSize != 0 {
		return nil, fmt.Errorf("image size must be a positive multiple of %d", BlockSize)
	}
	if err := os.Mkdir(dir, 0o700); err != nil {
		return nil, err
	}
	if err := writeJSON(filepath.Join(dir, manifestFile), m); err != nil {
		os.RemoveAll(dir)
		return nil, err
	}
	img, err := Open(dir, store)
	if err != nil {
		os.RemoveAll(dir)
	}
	return img, err
}

// Open opens an existing image, fetching its layer indexes from the store.
func Open(dir string, store Store) (*Image, error) {
	img := &Image{dir: dir, store: store}
	data, err := os.ReadFile(filepath.Join(dir, manifestFile))
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(data, &img.manifest); err != nil {
		return nil, fmt.Errorf("manifest: %w", err)
	}
	for _, key := range img.manifest.Layers {
		l, err := loadLayer(store, key)
		if err != nil {
			return nil, err
		}
		img.layers = append(img.layers, l)
	}
	img.overlay, err = os.OpenFile(filepath.Join(dir, overlayFile), os.O_RDWR|os.O_CREATE, 0o600)
	if err != nil {
		return nil, err
	}
	if err := img.overlay.Truncate(img.manifest.Size); err != nil {
		img.overlay.Close()
		return nil, err
	}
	nblocks := img.manifest.Size / BlockSize
	img.dirty = make([]byte, (nblocks+7)/8)
	if bm, err := os.ReadFile(filepath.Join(dir, bitmapFile)); err == nil && len(bm) == len(img.dirty) {
		copy(img.dirty, bm)
	} else if err != nil && !errors.Is(err, fs.ErrNotExist) {
		img.overlay.Close()
		return nil, err
	}
	return img, nil
}

func loadLayer(store Store, key string) (layer, error) {
	data, err := store.Get(key)
	if err != nil {
		return layer{}, fmt.Errorf("layer %s: %w", key, err)
	}
	var l layer
	if err := json.Unmarshal(data, &l); err != nil {
		return layer{}, fmt.Errorf("layer %s: %w", key, err)
	}
	if l.ChunkSize != ChunkSize {
		return layer{}, fmt.Errorf("layer %s: chunk size %d, want %d", key, l.ChunkSize, ChunkSize)
	}
	return l, nil
}

// Manifest returns a copy of the image's manifest.
func (img *Image) Manifest() Manifest {
	img.mu.RLock()
	defer img.mu.RUnlock()
	m := img.manifest
	m.Layers = append([]string(nil), m.Layers...)
	return m
}

func (img *Image) Size() (int64, error) { return img.manifest.Size, nil }

func (img *Image) isDirty(block int64) bool {
	return img.dirty[block/8]&(1<<(block%8)) != 0
}

func (img *Image) setDirty(block int64) {
	img.dirty[block/8] |= 1 << (block % 8)
	img.unsynced = true
}

// ReadAt implements io.ReaderAt.
func (img *Image) ReadAt(p []byte, off int64) (int, error) {
	img.mu.RLock()
	defer img.mu.RUnlock()
	return img.readLocked(p, off)
}

func (img *Image) readLocked(p []byte, off int64) (int, error) {
	if off < 0 || off+int64(len(p)) > img.manifest.Size {
		return 0, fmt.Errorf("read [%d, %d) outside image of %d bytes", off, off+int64(len(p)), img.manifest.Size)
	}
	done := 0
	for done < len(p) {
		pos := off + int64(done)
		block := pos / BlockSize
		n := min(int(BlockSize-pos%BlockSize), len(p)-done)
		if img.isDirty(block) {
			if _, err := img.overlay.ReadAt(p[done:done+n], pos); err != nil {
				return done, err
			}
		} else if err := img.readLower(p[done:done+n], pos); err != nil {
			return done, err
		}
		done += n
	}
	return done, nil
}

// readLower fills p (within one chunk) from the topmost layer that has it.
func (img *Image) readLower(p []byte, off int64) error {
	idx := strconv.FormatInt(off/ChunkSize, 10)
	for i := len(img.layers) - 1; i >= 0; i-- {
		key, ok := img.layers[i].Chunks[idx]
		if !ok {
			continue
		}
		if key == zeroChunk {
			break
		}
		n, err := img.store.ReadAt(key, p, off%ChunkSize)
		if err != nil {
			return fmt.Errorf("chunk %s: %w", key, err)
		}
		clear(p[n:]) // chunks drop trailing zeros
		return nil
	}
	clear(p)
	return nil
}

// WriteAt implements io.WriterAt.
func (img *Image) WriteAt(p []byte, off int64) (int, error) {
	img.mu.Lock()
	defer img.mu.Unlock()
	if img.manifest.ReadOnly {
		return 0, ErrReadOnly
	}
	if off < 0 || off+int64(len(p)) > img.manifest.Size {
		return 0, fmt.Errorf("write [%d, %d) outside image of %d bytes", off, off+int64(len(p)), img.manifest.Size)
	}
	done := 0
	for done < len(p) {
		pos := off + int64(done)
		block := pos / BlockSize
		n := min(int(BlockSize-pos%BlockSize), len(p)-done)
		if !img.isDirty(block) && n < BlockSize {
			// Copy the rest of the block up before overwriting part of it.
			buf := make([]byte, BlockSize)
			if err := img.readLower(buf, block*BlockSize); err != nil {
				return done, err
			}
			copy(buf[pos%BlockSize:], p[done:done+n])
			if _, err := img.overlay.WriteAt(buf, block*BlockSize); err != nil {
				return done, err
			}
		} else if _, err := img.overlay.WriteAt(p[done:done+n], pos); err != nil {
			return done, err
		}
		img.setDirty(block)
		done += n
	}
	return done, nil
}

// Sync makes every completed write durable (the block device's flush).
func (img *Image) Sync() error {
	img.mu.Lock()
	defer img.mu.Unlock()
	return img.syncLocked()
}

func (img *Image) syncLocked() error {
	if err := img.overlay.Sync(); err != nil {
		return err
	}
	if !img.unsynced {
		return nil
	}
	if err := writeFileAtomic(filepath.Join(img.dir, bitmapFile), img.dirty); err != nil {
		return err
	}
	img.unsynced = false
	return nil
}

// Snapshot turns everything written since the last snapshot into a new
// layer and returns the image's manifest after it. The caller must quiesce
// any filesystem on the image first (flush and freeze), or the snapshot holds
// whatever the device had at that moment.
func (img *Image) Snapshot() (Manifest, error) {
	img.mu.Lock()
	defer img.mu.Unlock()

	l := layer{ChunkSize: ChunkSize, Chunks: map[string]string{}}
	nchunks := (img.manifest.Size + ChunkSize - 1) / ChunkSize
	buf := make([]byte, ChunkSize)
	for c := int64(0); c < nchunks; c++ {
		if !img.chunkDirty(c) {
			continue
		}
		size := min(ChunkSize, img.manifest.Size-c*ChunkSize)
		data := buf[:size]
		if _, err := img.readLocked(data, c*ChunkSize); err != nil {
			return Manifest{}, err
		}
		data = bytes.TrimRight(data, "\x00")
		idx := strconv.FormatInt(c, 10)
		if len(data) == 0 {
			l.Chunks[idx] = zeroChunk
			continue
		}
		key := hashKey(chunkKind, data)
		if err := img.store.Put(key, data); err != nil {
			return Manifest{}, err
		}
		l.Chunks[idx] = key
	}
	if len(l.Chunks) == 0 {
		return img.copyManifest(), nil
	}

	data, err := json.Marshal(l)
	if err != nil {
		return Manifest{}, err
	}
	key := hashKey(layerKind, data)
	if err := img.store.Put(key, data); err != nil {
		return Manifest{}, err
	}
	next := img.manifest
	next.Layers = append(append([]string(nil), img.manifest.Layers...), key)
	// The manifest goes first: if we crash before the overlay is cleared, the
	// overlay still holds the same bytes as the new layer.
	if err := writeJSON(filepath.Join(img.dir, manifestFile), next); err != nil {
		return Manifest{}, err
	}
	img.manifest = next
	img.layers = append(img.layers, l)

	if err := img.overlay.Truncate(0); err != nil {
		return Manifest{}, err
	}
	if err := img.overlay.Truncate(img.manifest.Size); err != nil {
		return Manifest{}, err
	}
	clear(img.dirty)
	img.unsynced = true
	if err := img.syncLocked(); err != nil {
		return Manifest{}, err
	}
	return img.copyManifest(), nil
}

func (img *Image) chunkDirty(c int64) bool {
	first := c * blocksPerChunk
	last := min(first+blocksPerChunk, img.manifest.Size/BlockSize)
	for b := first; b < last; b++ {
		if img.isDirty(b) {
			return true
		}
	}
	return false
}

func (img *Image) copyManifest() Manifest {
	m := img.manifest
	m.Layers = append([]string(nil), m.Layers...)
	return m
}

// MarkGuestWritten persistently records that a sandbox writes the raw image.
func (img *Image) MarkGuestWritten() error {
	img.mu.Lock()
	defer img.mu.Unlock()
	if img.manifest.GuestWritten {
		return nil
	}
	next := img.manifest
	next.GuestWritten = true
	if err := writeJSON(filepath.Join(img.dir, manifestFile), next); err != nil {
		return err
	}
	img.manifest = next
	return nil
}

// Close syncs and closes the image.
func (img *Image) Close() error {
	img.mu.Lock()
	defer img.mu.Unlock()
	err := img.syncLocked()
	if cerr := img.overlay.Close(); err == nil {
		err = cerr
	}
	return err
}

// ChunkKeys lists every chunk the image's layers reference, for garbage
// collection and for pushing an image to a remote store.
func (img *Image) ChunkKeys() []string {
	img.mu.RLock()
	defer img.mu.RUnlock()
	seen := map[string]bool{}
	for _, l := range img.layers {
		for _, k := range l.Chunks {
			if k != zeroChunk {
				seen[k] = true
			}
		}
	}
	keys := make([]string, 0, len(seen))
	for k := range seen {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func writeJSON(path string, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return writeFileAtomic(path, data)
}

func writeFileAtomic(path string, data []byte) error {
	// Keep replaced metadata reachable until it is wiped. Otherwise a final
	// flush can unlink an old bitmap which deletion can no longer discard.
	retired := path + ".retired"
	if old, err := os.Stat(retired); err == nil {
		current, currentErr := os.Stat(path)
		if currentErr != nil && !errors.Is(currentErr, fs.ErrNotExist) {
			return currentErr
		}
		if currentErr != nil || !os.SameFile(old, current) {
			if _, err := wipeFile(retired); err != nil {
				return err
			}
		}
		// A crash before rename may leave an alias of the still-live file.
		if err := os.Remove(retired); err != nil {
			return err
		}
	} else if !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	tmp := path + ".tmp"
	f, err := os.OpenFile(tmp, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err := f.Close(); err != nil {
		return err
	}
	if err := os.Link(path, retired); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	if err := dir.Sync(); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		return err
	}
	if err := dir.Sync(); err != nil {
		return err
	}
	if _, err := wipeFile(retired); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	if err := os.Remove(retired); err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	return dir.Sync()
}

// References returns the store keys an image with manifest m needs: its
// layers and every chunk they point to. Garbage collection keeps these.
func References(store Store, m Manifest) (map[string]bool, error) {
	refs := map[string]bool{}
	for _, key := range m.Layers {
		l, err := loadLayer(store, key)
		if err != nil {
			return nil, err
		}
		refs[key] = true
		for _, c := range l.Chunks {
			if c != zeroChunk {
				refs[c] = true
			}
		}
	}
	return refs, nil
}

// ReadManifest reads the manifest of the image in dir without opening it.
func ReadManifest(dir string) (Manifest, error) {
	var m Manifest
	data, err := os.ReadFile(filepath.Join(dir, manifestFile))
	if err != nil {
		return m, err
	}
	if err := json.Unmarshal(data, &m); err != nil {
		return m, fmt.Errorf("manifest: %w", err)
	}
	return m, nil
}
