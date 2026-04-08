package main

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"
)

// mockR2Client is an in-memory implementation of the R2Client methods
// used by OverlayFS. It stores objects in a simple map so tests can
// exercise the full overlay pipeline without network access.
type mockR2Client struct {
	mu      sync.RWMutex
	objects map[string][]byte // key → content
}

func newMockR2() *mockR2Client {
	return &mockR2Client{objects: make(map[string][]byte)}
}

func (m *mockR2Client) GetObject(_ context.Context, key string) ([]byte, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	data, ok := m.objects[key]
	if !ok {
		return nil, fmt.Errorf("NoSuchKey: %s", key)
	}
	return append([]byte(nil), data...), nil // return copy
}

func (m *mockR2Client) PutObject(_ context.Context, key string, data []byte) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.objects[key] = append([]byte(nil), data...)
	return nil
}

func (m *mockR2Client) HeadObject(_ context.Context, key string) (*R2Object, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()
	data, ok := m.objects[key]
	if !ok {
		return nil, fmt.Errorf("NoSuchKey: %s", key)
	}
	return &R2Object{Key: key, Size: int64(len(data)), LastModified: time.Now()}, nil
}

func (m *mockR2Client) DeleteObject(_ context.Context, key string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.objects, key)
	return nil
}

func (m *mockR2Client) CopyObject(_ context.Context, srcKey, dstKey string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	data, ok := m.objects[srcKey]
	if !ok {
		return fmt.Errorf("NoSuchKey: %s", srcKey)
	}
	m.objects[dstKey] = append([]byte(nil), data...)
	return nil
}

func (m *mockR2Client) ListObjects(_ context.Context, prefix string) ([]R2Object, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	// Mimic the real ListObjects: return entries directly under prefix,
	// collapsing deeper paths into common-prefix (directory) entries.
	seen := make(map[string]bool)
	var result []R2Object

	for key, data := range m.objects {
		if !strings.HasPrefix(key, prefix) {
			continue
		}
		rest := strings.TrimPrefix(key, prefix)
		if rest == "" {
			continue
		}

		if idx := strings.Index(rest, "/"); idx >= 0 {
			// Directory entry.
			dirName := rest[:idx]
			if !seen[dirName] {
				seen[dirName] = true
				result = append(result, R2Object{Key: dirName, IsPrefix: true})
			}
		} else {
			// File entry.
			result = append(result, R2Object{
				Key:          rest,
				Size:         int64(len(data)),
				LastModified: time.Now(),
			})
		}
	}

	sort.Slice(result, func(i, j int) bool { return result[i].Key < result[j].Key })
	return result, nil
}

// has checks if a key exists (test helper).
func (m *mockR2Client) has(key string) bool {
	m.mu.RLock()
	defer m.mu.RUnlock()
	_, ok := m.objects[key]
	return ok
}
