package main

import (
	"context"
	"io"
	"os"
	"sort"
	"testing"
)

// helper: create an OverlayFS backed by a mock R2 and temp-dir cache.
func testOverlay(t *testing.T, vmID, templateID string) (*OverlayFS, *mockR2Client) {
	t.Helper()
	mock := newMockR2()
	cache, err := newDiskCache(t.TempDir(), 64*1024*1024)
	if err != nil {
		t.Fatal(err)
	}
	ofs := newOverlayFS(mock, cache, vmID, templateID)
	return ofs, mock
}

// ---------------------------------------------------------------------------
// cleanPath security
// ---------------------------------------------------------------------------

func TestCleanPathRejectsTraversal(t *testing.T) {
	cases := []struct {
		input string
		want  string
	}{
		{"normal/path", "normal/path"},
		{"file.txt", "file.txt"},
		{".", ""},
		{"", ""},
		{"/absolute", "absolute"},
		{"..", ""},
		{"../../etc/passwd", ""},
		{"foo/../../../bar", ""},
		{"a/b/../c", "a/c"},      // normalised but stays within root
		{"a/./b", "a/b"},         // dot collapsed
		{"/a/b/c/", "a/b/c"},    // leading + trailing slashes
		{"../", ""},
		{"foo/../../..", ""},
	}
	for _, tc := range cases {
		got := cleanPath(tc.input)
		if got != tc.want {
			t.Errorf("cleanPath(%q) = %q, want %q", tc.input, got, tc.want)
		}
	}
}

// ---------------------------------------------------------------------------
// Overlay read/write round-trip
// ---------------------------------------------------------------------------

func TestOverlayWriteReadRoundtrip(t *testing.T) {
	ofs, _ := testOverlay(t, "vm-1", "tmpl-1")

	// Write a file.
	f, err := ofs.Create("hello.txt")
	if err != nil {
		t.Fatal(err)
	}
	f.Write([]byte("world"))
	f.Close()

	// Read it back.
	f2, err := ofs.Open("hello.txt")
	if err != nil {
		t.Fatal(err)
	}
	data, _ := io.ReadAll(f2)
	f2.Close()

	if string(data) != "world" {
		t.Errorf("got %q, want %q", data, "world")
	}
}

// ---------------------------------------------------------------------------
// Template fallback
// ---------------------------------------------------------------------------

func TestOverlayTemplateFallback(t *testing.T) {
	ofs, mock := testOverlay(t, "vm-1", "tmpl-1")

	// Pre-populate the template layer directly in mock R2.
	mock.PutObject(context.Background(), "templates/tmpl-1/readme.md", []byte("from template"))

	f, err := ofs.Open("readme.md")
	if err != nil {
		t.Fatalf("Open template file: %v", err)
	}
	data, _ := io.ReadAll(f)
	f.Close()

	if string(data) != "from template" {
		t.Errorf("got %q, want %q", data, "from template")
	}
}

// ---------------------------------------------------------------------------
// Sandbox overrides template
// ---------------------------------------------------------------------------

func TestOverlaySandboxOverridesTemplate(t *testing.T) {
	ofs, mock := testOverlay(t, "vm-1", "tmpl-1")
	ctx := context.Background()

	// Template layer has the file.
	mock.PutObject(ctx, "templates/tmpl-1/config.json", []byte(`{"old":true}`))

	// Sandbox writes a new version.
	f, _ := ofs.Create("config.json")
	f.Write([]byte(`{"new":true}`))
	f.Close()

	// Read should return sandbox version.
	f2, err := ofs.Open("config.json")
	if err != nil {
		t.Fatal(err)
	}
	data, _ := io.ReadAll(f2)
	f2.Close()

	if string(data) != `{"new":true}` {
		t.Errorf("got %q, want sandbox version", data)
	}
}

// ---------------------------------------------------------------------------
// Sandbox isolation: two overlays can't see each other's writes
// ---------------------------------------------------------------------------

func TestOverlaySandboxIsolation(t *testing.T) {
	mock := newMockR2()
	cache, _ := newDiskCache(t.TempDir(), 64*1024*1024)
	ofs1 := newOverlayFS(mock, cache, "vm-A", "tmpl-1")
	ofs2 := newOverlayFS(mock, cache, "vm-B", "tmpl-1")

	// vm-A writes a secret file.
	f, _ := ofs1.Create("secret.txt")
	f.Write([]byte("vm-A only"))
	f.Close()

	// vm-B should not be able to read it.
	_, err := ofs2.Open("secret.txt")
	if err == nil {
		t.Error("vm-B should NOT be able to read vm-A's file")
	}
	if !os.IsNotExist(err) {
		// Check the wrapped error
		pe, ok := err.(*os.PathError)
		if !ok || !os.IsNotExist(pe.Err) {
			// Accept any "not found" style error
			t.Logf("got error type %T: %v (acceptable if not-found)", err, err)
		}
	}
}

// ---------------------------------------------------------------------------
// Delete creates whiteout and hides template file
// ---------------------------------------------------------------------------

func TestOverlayDeleteCreatesWhiteout(t *testing.T) {
	ofs, mock := testOverlay(t, "vm-1", "tmpl-1")
	ctx := context.Background()

	// Template has a file.
	mock.PutObject(ctx, "templates/tmpl-1/old.txt", []byte("ancient"))

	// Verify readable before delete.
	_, err := ofs.Open("old.txt")
	if err != nil {
		t.Fatalf("should be readable before delete: %v", err)
	}

	// Delete it.
	if err := ofs.Remove("old.txt"); err != nil {
		t.Fatal(err)
	}

	// Should no longer be readable.
	_, err = ofs.Open("old.txt")
	if err == nil {
		t.Error("file should be hidden after delete")
	}

	// Whiteout marker should exist in R2.
	whiteoutKey := "sandboxes/vm-1/.wh.old.txt"
	if !mock.has(whiteoutKey) {
		t.Error("whiteout marker should exist in sandbox layer")
	}
}

// ---------------------------------------------------------------------------
// ReadDir merges both layers
// ---------------------------------------------------------------------------

func TestOverlayReadDirMergesBothLayers(t *testing.T) {
	ofs, mock := testOverlay(t, "vm-1", "tmpl-1")
	ctx := context.Background()

	// Template has files A and B.
	mock.PutObject(ctx, "templates/tmpl-1/a.txt", []byte("A"))
	mock.PutObject(ctx, "templates/tmpl-1/b.txt", []byte("B-template"))

	// Sandbox overrides B and adds C.
	mock.PutObject(ctx, "sandboxes/vm-1/b.txt", []byte("B-sandbox"))
	mock.PutObject(ctx, "sandboxes/vm-1/c.txt", []byte("C"))

	entries, err := ofs.ReadDir("")
	if err != nil {
		t.Fatal(err)
	}

	var names []string
	for _, e := range entries {
		names = append(names, e.Name())
	}
	sort.Strings(names)

	want := []string{"a.txt", "b.txt", "c.txt"}
	if len(names) != len(want) {
		t.Fatalf("got %v, want %v", names, want)
	}
	for i := range want {
		if names[i] != want[i] {
			t.Errorf("entry[%d] = %q, want %q", i, names[i], want[i])
		}
	}
}

// ---------------------------------------------------------------------------
// ReadDir respects whiteouts
// ---------------------------------------------------------------------------

func TestOverlayReadDirRespectsWhiteouts(t *testing.T) {
	ofs, mock := testOverlay(t, "vm-1", "tmpl-1")
	ctx := context.Background()

	// Template has files A and B.
	mock.PutObject(ctx, "templates/tmpl-1/a.txt", []byte("A"))
	mock.PutObject(ctx, "templates/tmpl-1/b.txt", []byte("B"))

	// Delete B via overlay (creates whiteout).
	ofs.Remove("b.txt")

	entries, err := ofs.ReadDir("")
	if err != nil {
		t.Fatal(err)
	}

	for _, e := range entries {
		if e.Name() == "b.txt" {
			t.Error("b.txt should be hidden by whiteout")
		}
		if e.Name() == ".wh.b.txt" {
			t.Error("whiteout marker should not appear in directory listing")
		}
	}
}

// ---------------------------------------------------------------------------
// Stat resolves through overlay
// ---------------------------------------------------------------------------

func TestOverlayStatTemplate(t *testing.T) {
	ofs, mock := testOverlay(t, "vm-1", "tmpl-1")
	mock.PutObject(context.Background(), "templates/tmpl-1/info.txt", []byte("12345"))

	fi, err := ofs.Stat("info.txt")
	if err != nil {
		t.Fatal(err)
	}
	if fi.Name() != "info.txt" {
		t.Errorf("Name() = %q", fi.Name())
	}
	if fi.Size() != 5 {
		t.Errorf("Size() = %d, want 5", fi.Size())
	}
}

// ---------------------------------------------------------------------------
// Rename within sandbox
// ---------------------------------------------------------------------------

func TestOverlayRename(t *testing.T) {
	ofs, _ := testOverlay(t, "vm-1", "tmpl-1")

	f, _ := ofs.Create("old-name.txt")
	f.Write([]byte("content"))
	f.Close()

	if err := ofs.Rename("old-name.txt", "new-name.txt"); err != nil {
		t.Fatal(err)
	}

	// Old name gone.
	if _, err := ofs.Open("old-name.txt"); err == nil {
		t.Error("old name should not exist after rename")
	}

	// New name readable.
	f2, err := ofs.Open("new-name.txt")
	if err != nil {
		t.Fatal(err)
	}
	data, _ := io.ReadAll(f2)
	f2.Close()
	if string(data) != "content" {
		t.Errorf("got %q after rename", data)
	}
}
