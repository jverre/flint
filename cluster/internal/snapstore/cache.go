package snapstore

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"sort"
	"sync"
	"time"
)

// Cache is a host's local, size-capped copy of chunks and manifests.
// Least recently used chunks are evicted when it grows past MaxBytes.
type Cache struct {
	Dir      string
	MaxBytes int64

	mu      sync.Mutex
	size    int64
	entries map[string]*cacheEntry // chunk hash -> entry
}

type cacheEntry struct {
	size    int64
	lastUse time.Time
}

// OpenCache indexes an existing cache directory (or creates it).
func OpenCache(dir string, maxBytes int64) (*Cache, error) {
	c := &Cache{Dir: dir, MaxBytes: maxBytes, entries: map[string]*cacheEntry{}}
	for _, sub := range []string{"chunks", "manifests"} {
		if err := os.MkdirAll(filepath.Join(dir, sub), 0o700); err != nil {
			return nil, err
		}
	}
	err := filepath.WalkDir(filepath.Join(dir, "chunks"), func(p string, d os.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		if !validHash(d.Name()) {
			os.Remove(p) // leftover temp file
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return nil
		}
		c.entries[d.Name()] = &cacheEntry{size: info.Size(), lastUse: info.ModTime()}
		c.size += info.Size()
		return nil
	})
	return c, err
}

func (c *Cache) chunkPath(h string) string {
	return filepath.Join(c.Dir, "chunks", h[:2], h)
}

func (c *Cache) manifestPath(id string) string {
	return filepath.Join(c.Dir, "manifests", id+".json")
}

// HasChunk reports whether a chunk is cached.
func (c *Cache) HasChunk(h string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, ok := c.entries[h]
	return ok
}

// GetChunk reads a cached chunk.
func (c *Cache) GetChunk(h string) ([]byte, error) {
	if !validHash(h) {
		return nil, ErrNotFound
	}
	c.mu.Lock()
	e, ok := c.entries[h]
	if ok {
		e.lastUse = time.Now()
	}
	c.mu.Unlock()
	if !ok {
		return nil, ErrNotFound
	}
	b, err := os.ReadFile(c.chunkPath(h))
	if errors.Is(err, os.ErrNotExist) {
		c.forget(h)
		return nil, ErrNotFound
	}
	return b, err
}

// PutChunk stores a chunk. Returns false if it was already cached.
func (c *Cache) PutChunk(h string, data []byte) (bool, error) {
	c.mu.Lock()
	if e, ok := c.entries[h]; ok {
		e.lastUse = time.Now()
		c.mu.Unlock()
		return false, nil
	}
	c.mu.Unlock()
	p := c.chunkPath(h)
	if err := os.MkdirAll(filepath.Dir(p), 0o700); err != nil {
		return false, err
	}
	if err := writeFileAtomic(p, data, 0o600); err != nil {
		return false, err
	}
	c.mu.Lock()
	if _, ok := c.entries[h]; !ok {
		c.entries[h] = &cacheEntry{size: int64(len(data)), lastUse: time.Now()}
		c.size += int64(len(data))
	}
	c.mu.Unlock()
	c.evict()
	return true, nil
}

func (c *Cache) forget(h string) {
	c.mu.Lock()
	if e, ok := c.entries[h]; ok {
		c.size -= e.size
		delete(c.entries, h)
	}
	c.mu.Unlock()
}

// evict drops least recently used chunks until the cache fits. Chunks used
// in the last minute are kept even over the cap, so a snapshot being taken
// or restored is never evicted under itself.
func (c *Cache) evict() {
	if c.MaxBytes <= 0 {
		return
	}
	c.mu.Lock()
	if c.size <= c.MaxBytes {
		c.mu.Unlock()
		return
	}
	type kv struct {
		h string
		e *cacheEntry
	}
	all := make([]kv, 0, len(c.entries))
	for h, e := range c.entries {
		all = append(all, kv{h, e})
	}
	sort.Slice(all, func(i, j int) bool { return all[i].e.lastUse.Before(all[j].e.lastUse) })
	recent := time.Now().Add(-time.Minute)
	var victims []string
	size := c.size
	for _, x := range all {
		if size <= c.MaxBytes || x.e.lastUse.After(recent) {
			break
		}
		victims = append(victims, x.h)
		size -= x.e.size
		c.size -= x.e.size
		delete(c.entries, x.h)
	}
	c.mu.Unlock()
	for _, h := range victims {
		os.Remove(c.chunkPath(h))
	}
}

// PutManifest caches a manifest.
func (c *Cache) PutManifest(m *Manifest) error {
	b, err := json.Marshal(m)
	if err != nil {
		return err
	}
	return writeFileAtomic(c.manifestPath(m.ID), b, 0o600)
}

// GetManifest reads a cached manifest.
func (c *Cache) GetManifest(id string) (*Manifest, error) {
	if !ValidID(id) {
		return nil, ErrNotFound
	}
	b, err := os.ReadFile(c.manifestPath(id))
	if errors.Is(err, os.ErrNotExist) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	var m Manifest
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	return &m, nil
}

// DeleteManifest drops a manifest from the cache (its chunks age out).
func (c *Cache) DeleteManifest(id string) {
	if ValidID(id) {
		os.Remove(c.manifestPath(id))
	}
}

// Complete returns the IDs of cached snapshots whose every chunk is cached:
// restoring them on this host needs no network.
func (c *Cache) Complete() []string {
	ents, err := os.ReadDir(filepath.Join(c.Dir, "manifests"))
	if err != nil {
		return nil
	}
	var out []string
	for _, e := range ents {
		id := e.Name()
		if filepath.Ext(id) != ".json" {
			continue
		}
		id = id[:len(id)-len(".json")]
		m, err := c.GetManifest(id)
		if err != nil {
			continue
		}
		ok := true
		for _, h := range m.Hashes() {
			if !c.HasChunk(h) {
				ok = false
				break
			}
		}
		if ok {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out
}

// Size returns the bytes of chunks currently cached.
func (c *Cache) Size() int64 {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.size
}
