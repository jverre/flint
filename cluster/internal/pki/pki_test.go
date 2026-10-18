package pki

import (
	"encoding/base64"
	"encoding/json"
	"strings"
	"testing"
)

func TestJoinTokensRoundTripAndAcceptTerraformsForm(t *testing.T) {
	ca, err := NewCA("cp")
	if err != nil {
		t.Fatal(err)
	}
	tok := JoinToken{Server: "https://cp:9301", ID: "abc", Secret: "s3cret", Fingerprint: ca.Fingerprint()}
	got, err := DecodeJoinToken(tok.Encode())
	if err != nil || got != tok {
		t.Fatalf("round trip: %+v, %v", got, err)
	}

	// What terraform/modules/flint-pki builds: base64encode (standard, padded)
	// of a JSON object carrying the CA certificate instead of its fingerprint.
	body, _ := json.Marshal(map[string]string{"s": "https://cp:9301", "i": "static", "k": "x", "ca": string(ca.CertPEM)})
	tf, err := DecodeJoinToken("flintjoin1." + base64.StdEncoding.EncodeToString(body))
	if err != nil {
		t.Fatal(err)
	}
	if tf.Fingerprint != ca.Fingerprint() {
		t.Fatalf("fingerprint from the CA = %s, want %s", tf.Fingerprint, ca.Fingerprint())
	}

	for _, bad := range []string{
		"",
		"flintjoin1.%%%",
		"flintjoin2." + base64.RawURLEncoding.EncodeToString(body),
		"flintjoin1." + base64.RawURLEncoding.EncodeToString([]byte(`{"s":"https://cp","i":"x","k":"y"}`)), // nothing to pin
	} {
		if _, err := DecodeJoinToken(bad); err == nil {
			t.Errorf("accepted %q", bad)
		}
	}
}

func TestPinnedTLSRejectsServersOfAnotherCA(t *testing.T) {
	good, _ := NewCA("cp")
	other, _ := NewCA("impostor")
	cert, err := other.Keypair("flint-server", []string{"cp"}, 3600e9)
	if err != nil {
		t.Fatal(err)
	}
	chain := append(cert.Certificate, other.Cert.Raw)
	err = PinnedTLS(good.Fingerprint()).VerifyPeerCertificate(chain, nil)
	if err == nil || !strings.Contains(err.Error(), "pinned") {
		t.Fatalf("an impostor's certificate was accepted (err=%v)", err)
	}
	cert, _ = good.Keypair("flint-server", []string{"cp"}, 3600e9)
	if err := PinnedTLS(good.Fingerprint()).VerifyPeerCertificate(append(cert.Certificate, good.Cert.Raw), nil); err != nil {
		t.Fatalf("the real control plane was refused: %v", err)
	}
}
