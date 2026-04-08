package main

import (
	"log"
	"sync"
)

// SandboxExport tracks the R2 prefix mapping for a single sandbox.
type SandboxExport struct {
	ClientIP   string
	VMID       string
	TemplateID string
}

// ExportManager tracks per-sandbox NFS exports and maps client IPs
// to their R2 overlay filesystem (template base + sandbox layer).
type ExportManager struct {
	r2    *R2Client
	cache *DiskCache

	mu      sync.RWMutex
	exports map[string]*SandboxExport // client IP → export
}

func newExportManager(r2 *R2Client, cache *DiskCache) *ExportManager {
	return &ExportManager{
		r2:      r2,
		cache:   cache,
		exports: make(map[string]*SandboxExport),
	}
}

// Register creates an NFS export for a sandbox.
func (m *ExportManager) Register(clientIP, vmID, templateID string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.exports[clientIP] = &SandboxExport{
		ClientIP:   clientIP,
		VMID:       vmID,
		TemplateID: templateID,
	}
	log.Printf("Registered export: %s → vm=%s template=%s", clientIP, vmID, templateID)
}

// Deregister removes the NFS export for a client IP.
func (m *ExportManager) Deregister(clientIP string) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.exports, clientIP)
	log.Printf("Deregistered export: %s", clientIP)
}

// Lookup returns the export for a client IP.
func (m *ExportManager) Lookup(clientIP string) *SandboxExport {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.exports[clientIP]
}

// Count returns the number of active exports.
func (m *ExportManager) Count() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return len(m.exports)
}

// SandboxPrefix returns the R2 key prefix for a sandbox's writable layer.
func SandboxPrefix(vmID string) string {
	return "sandboxes/" + vmID + "/"
}

// TemplatePrefix returns the R2 key prefix for a template's read-only layer.
func TemplatePrefix(templateID string) string {
	return "templates/" + templateID + "/"
}

// WhiteoutKey returns the whiteout marker key for a deleted file.
// Whiteouts hide template-layer files from the overlay view.
func WhiteoutKey(sandboxPrefix, name string) string {
	return sandboxPrefix + ".wh." + name
}
