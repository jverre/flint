package main

import (
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"sync"
)

// DiskCache provides a local disk cache for R2 objects.
// Reads are served from cache when available; writes are staged
// locally and flushed to R2 on close.
type DiskCache struct {
	dir     string
	maxSize int64
	mu      sync.Mutex
	size    int64
}

func newDiskCache(dir string, maxSize int64) (*DiskCache, error) {
	if err := os.MkdirAll(dir, 0755); err != nil {
		return nil, fmt.Errorf("create cache dir: %w", err)
	}
	return &DiskCache{dir: dir, maxSize: maxSize}, nil
}

func (c *DiskCache) keyPath(key string) string {
	h := sha256.Sum256([]byte(key))
	hex := fmt.Sprintf("%x", h)
	// Two-level directory structure to avoid too many files in one dir.
	return filepath.Join(c.dir, hex[:2], hex[2:4], hex[4:])
}

// Get returns cached data for a key, or nil if not cached.
func (c *DiskCache) Get(key string) ([]byte, bool) {
	data, err := os.ReadFile(c.keyPath(key))
	if err != nil {
		return nil, false
	}
	return data, true
}

// Put stores data in the cache.
func (c *DiskCache) Put(key string, data []byte) {
	c.mu.Lock()
	defer c.mu.Unlock()

	// Simple size check — skip caching if over limit.
	if c.size+int64(len(data)) > c.maxSize {
		return
	}

	path := c.keyPath(key)
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return
	}
	if err := os.WriteFile(path, data, 0644); err != nil {
		return
	}
	c.size += int64(len(data))
}

// Invalidate removes a key from the cache.
func (c *DiskCache) Invalidate(key string) {
	path := c.keyPath(key)
	info, err := os.Stat(path)
	if err != nil {
		return
	}
	c.mu.Lock()
	c.size -= info.Size()
	c.mu.Unlock()
	os.Remove(path)
}

// Close cleans up the cache directory.
func (c *DiskCache) Close() {
	// Leave cache intact for faster restart.
}
