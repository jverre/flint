package webui

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHandlerServesShellForClientRoutes(t *testing.T) {
	for _, p := range []string{"/ui/", "/ui/vms/abc", "/ui/hosts?x=1"} {
		rec := httptest.NewRecorder()
		Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, p, nil))
		if rec.Code != http.StatusOK && rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("%s: status %d", p, rec.Code)
		}
		if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "text/html") {
			t.Fatalf("%s: content-type %q", p, ct)
		}
	}
}

func TestHandlerRejectsTraversal(t *testing.T) {
	rec := httptest.NewRecorder()
	Handler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/ui/../../go.mod", nil))
	if strings.Contains(rec.Body.String(), "module github.com") {
		t.Fatal("served a file outside the UI build")
	}
}
