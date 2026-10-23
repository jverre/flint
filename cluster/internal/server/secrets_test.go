package server

import (
	"encoding/json"
	"testing"
)

func TestKeepUnchangedRestoresRedactedValues(t *testing.T) {
	current := json.RawMessage(`{"allow":{"api.example.com":[{"transform":[{"headers":{"Authorization":"Bearer sk-live-123456","X-Short":"abc"}}]}]}}`)
	// What a client reads back, then edits: one value kept, one new, one domain added.
	next := json.RawMessage(`{"allow":{
		"api.example.com":[{"transform":[{"headers":{"Authorization":"Bear***3456","X-Short":"***","X-New":"fresh"}}]}],
		"other.example.com":[{"transform":[{"headers":{"Authorization":"Bear***3456"}}]}]}}`)
	got := headerValues(keepUnchanged(next, current))
	want := map[string]map[string]string{
		"api.example.com":   {"Authorization": "Bearer sk-live-123456", "X-Short": "abc", "X-New": "fresh"},
		"other.example.com": {"Authorization": "Bear***3456"}, // another domain never borrows a credential
	}
	for d, hs := range want {
		for k, v := range hs {
			if got[d][k] != v {
				t.Errorf("%s %s = %q, want %q", d, k, got[d][k], v)
			}
		}
	}
}

func TestKeepUnchangedTakesNewValues(t *testing.T) {
	current := json.RawMessage(`{"allow":{"api.example.com":[{"transform":[{"headers":{"Authorization":"Bearer old-secret-value"}}]}]}}`)
	next := json.RawMessage(`{"allow":{"api.example.com":[{"transform":[{"headers":{"Authorization":"Bearer rotated-secret"}}]}]}}`)
	if v := headerValues(keepUnchanged(next, current))["api.example.com"]["Authorization"]; v != "Bearer rotated-secret" {
		t.Fatalf("got %q", v)
	}
}
