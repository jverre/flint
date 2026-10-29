package model

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestVMParamsValidation(t *testing.T) {
	for body, want := range map[string]string{
		`{}`: "",
		`{"availability":"durable","network_policy":{"allow":{}},"timeout_seconds":30,"timeout_policy":"pause"}`: "",
		`{"network_policy":null}`:                          "",
		`{"availability":"forever"}`:                       "availability",
		`{"network_policy":["x"]}`:                         "network_policy",
		`{"timeout_seconds":-1}`:                           "timeout_seconds",
		`{"timeout_seconds":5,"timeout_policy":"explode"}`: "timeout_policy",
	} {
		var p VMParams
		if err := json.Unmarshal([]byte(body), &p); err != nil {
			t.Fatal(err)
		}
		p.WithDefaults(AvailEphemeral)
		err := p.Validate()
		if want == "" && err != nil || want != "" && (err == nil || !strings.Contains(err.Error(), want)) {
			t.Errorf("%s: got %v, want %q", body, err, want)
		}
	}
}

func TestVMParamsDefaults(t *testing.T) {
	var p VMParams
	json.Unmarshal([]byte(`{"timeout_seconds":10,"network_policy":null}`), &p)
	p.WithDefaults(AvailRestartable)
	s := p.Spec()
	if p.TemplateID != DefaultTemplate || p.Availability != AvailRestartable || !s.AllowInternet || s.NetworkPolicy != nil ||
		s.TimeoutSecs != 10 || s.TimeoutPolicy != TimeoutKill {
		t.Fatalf("defaults: %+v -> %+v", p, s)
	}
	off := false
	p = VMParams{AllowInternet: &off}
	if p.Spec().AllowInternet {
		t.Fatal("allow_internet_access: false ignored")
	}
}

// VM sets embed VMParams; their stored JSON keeps its field names.
func TestVMSetSpecJSONIsFlat(t *testing.T) {
	var s VMSetSpec
	if err := json.Unmarshal([]byte(`{"replicas":2,"template_id":"py","allow_internet_access":false,"spread":"host"}`), &s); err != nil {
		t.Fatal(err)
	}
	s.WithDefaults()
	if s.TemplateID != "py" || s.Spec().AllowInternet || s.Availability != AvailRestartable || s.Validate() != nil {
		t.Fatalf("decoded: %+v", s)
	}
	b, _ := json.Marshal(s)
	for _, k := range []string{`"replicas":2`, `"template_id":"py"`, `"availability":"restartable"`, `"allow_internet_access":false`, `"spread":"host"`} {
		if !strings.Contains(string(b), k) {
			t.Errorf("%s missing from %s", k, b)
		}
	}
}
