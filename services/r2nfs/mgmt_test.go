package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// helper: stand up the management API handler backed by a mock.
func testMgmtHandler(t *testing.T) (http.Handler, *ExportManager) {
	t.Helper()
	mock := newMockR2()
	cache, err := newDiskCache(t.TempDir(), 64*1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	exports := newExportManager(mock, cache)
	return newMgmtHandler(exports), exports
}

// ---------------------------------------------------------------------------
// Full lifecycle: register → health → deregister → health
// ---------------------------------------------------------------------------

func TestMgmtExportLifecycle(t *testing.T) {
	handler, _ := testMgmtHandler(t)

	// Register an export.
	body := `{"client_ip":"10.0.0.2","vm_id":"abc-123","template_id":"default"}`
	req := httptest.NewRequest("POST", "/exports", strings.NewReader(body))
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("POST /exports: status %d, body %s", w.Code, w.Body.String())
	}

	// Health should show 1 export.
	req = httptest.NewRequest("GET", "/health", nil)
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("GET /health: status %d", w.Code)
	}
	var health struct {
		Status  string `json:"status"`
		Exports int    `json:"exports"`
	}
	json.Unmarshal(w.Body.Bytes(), &health)
	if health.Exports != 1 {
		t.Errorf("expected 1 export, got %d", health.Exports)
	}

	// Deregister.
	req = httptest.NewRequest("DELETE", "/exports/10.0.0.2", nil)
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Fatalf("DELETE /exports: status %d", w.Code)
	}

	// Health should show 0 exports.
	req = httptest.NewRequest("GET", "/health", nil)
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	json.Unmarshal(w.Body.Bytes(), &health)
	if health.Exports != 0 {
		t.Errorf("expected 0 exports after deregister, got %d", health.Exports)
	}
}

// ---------------------------------------------------------------------------
// Reject invalid IP
// ---------------------------------------------------------------------------

func TestMgmtRejectsInvalidIP(t *testing.T) {
	handler, _ := testMgmtHandler(t)

	cases := []struct {
		name string
		body string
	}{
		{"not-an-ip", `{"client_ip":"not-an-ip","vm_id":"abc"}`},
		{"partial", `{"client_ip":"10.0.0","vm_id":"abc"}`},
		{"empty", `{"client_ip":"","vm_id":"abc"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest("POST", "/exports", strings.NewReader(tc.body))
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, req)
			if w.Code != 400 {
				t.Errorf("expected 400, got %d for body %s", w.Code, tc.body)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Reject path-traversal in vm_id / template_id
// ---------------------------------------------------------------------------

func TestMgmtRejectsPathTraversalInIDs(t *testing.T) {
	handler, _ := testMgmtHandler(t)

	cases := []struct {
		name string
		body string
	}{
		{"vm_id traversal", `{"client_ip":"10.0.0.2","vm_id":"../../evil"}`},
		{"vm_id slash", `{"client_ip":"10.0.0.2","vm_id":"foo/bar"}`},
		{"vm_id dot", `{"client_ip":"10.0.0.2","vm_id":"."}`},
		{"template_id traversal", `{"client_ip":"10.0.0.2","vm_id":"abc","template_id":"../../etc"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest("POST", "/exports", strings.NewReader(tc.body))
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, req)
			if w.Code != 400 {
				t.Errorf("expected 400, got %d for %s", w.Code, tc.name)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Reject empty required fields
// ---------------------------------------------------------------------------

func TestMgmtRejectsEmptyFields(t *testing.T) {
	handler, _ := testMgmtHandler(t)

	cases := []struct {
		name string
		body string
	}{
		{"empty vm_id", `{"client_ip":"10.0.0.2","vm_id":""}`},
		{"missing vm_id", `{"client_ip":"10.0.0.2"}`},
		{"invalid json", `not json`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest("POST", "/exports", strings.NewReader(tc.body))
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, req)
			if w.Code != 400 {
				t.Errorf("expected 400, got %d for %s", w.Code, tc.name)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Valid export with IPv6
// ---------------------------------------------------------------------------

func TestMgmtAcceptsIPv6(t *testing.T) {
	handler, _ := testMgmtHandler(t)

	body := `{"client_ip":"::1","vm_id":"vm-1","template_id":"tmpl-1"}`
	req := httptest.NewRequest("POST", "/exports", strings.NewReader(body))
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)
	if w.Code != 200 {
		t.Errorf("expected 200 for IPv6, got %d: %s", w.Code, w.Body.String())
	}
}

// ---------------------------------------------------------------------------
// Export lookup after registration
// ---------------------------------------------------------------------------

func TestMgmtExportLookup(t *testing.T) {
	_, exports := testMgmtHandler(t)

	exports.Register("10.0.0.5", "vm-xyz", "tmpl-prod")

	exp := exports.Lookup("10.0.0.5")
	if exp == nil {
		t.Fatal("expected export for 10.0.0.5")
	}
	if exp.VMID != "vm-xyz" || exp.TemplateID != "tmpl-prod" {
		t.Errorf("got vmid=%s template=%s", exp.VMID, exp.TemplateID)
	}

	// Unknown IP returns nil.
	if exports.Lookup("10.0.0.99") != nil {
		t.Error("unknown IP should return nil")
	}
}

// ---------------------------------------------------------------------------
// Export isolation: two IPs are independent
// ---------------------------------------------------------------------------

func TestMgmtExportIsolation(t *testing.T) {
	_, exports := testMgmtHandler(t)

	exports.Register("10.0.0.2", "vm-A", "tmpl-1")
	exports.Register("10.0.0.3", "vm-B", "tmpl-1")

	a := exports.Lookup("10.0.0.2")
	b := exports.Lookup("10.0.0.3")
	if a.VMID == b.VMID {
		t.Error("exports should map to different VMs")
	}

	// Deregister A, B unaffected.
	exports.Deregister("10.0.0.2")
	if exports.Lookup("10.0.0.2") != nil {
		t.Error("A should be gone")
	}
	if exports.Lookup("10.0.0.3") == nil {
		t.Error("B should still exist")
	}
}
