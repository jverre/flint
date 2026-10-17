// Package pki issues the cluster's certificates.
//
// Two CAs keep roles apart:
//
//   - the hosts CA signs host certificates. The control plane and other hosts'
//     snapshot servers accept clients signed by it.
//   - the control-plane CA signs flint-server's certificates. Host agents only
//     accept clients signed by it, so a host certificate can never drive
//     another host's VMs.
package pki

import (
	"crypto"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"math/big"
	"net"
	"strings"
	"time"
)

// CA is a certificate authority with its private key.
type CA struct {
	Cert    *x509.Certificate
	Key     crypto.Signer
	CertPEM []byte
	KeyPEM  []byte
}

// NewCA creates a self-signed CA valid for ten years.
func NewCA(commonName string) (*CA, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	tmpl := &x509.Certificate{
		SerialNumber:          serial(),
		Subject:               pkix.Name{CommonName: commonName, Organization: []string{"flint"}},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(10 * 365 * 24 * time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		MaxPathLenZero:        true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		return nil, err
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, err
	}
	return LoadCA(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}),
	)
}

// LoadCA parses a CA certificate and key in PEM.
func LoadCA(certPEM, keyPEM []byte) (*CA, error) {
	pair, err := tls.X509KeyPair(certPEM, keyPEM)
	if err != nil {
		return nil, fmt.Errorf("ca keypair: %w", err)
	}
	cert, err := x509.ParseCertificate(pair.Certificate[0])
	if err != nil {
		return nil, err
	}
	signer, ok := pair.PrivateKey.(crypto.Signer)
	if !ok || !cert.IsCA {
		return nil, errors.New("not a CA keypair")
	}
	return &CA{Cert: cert, Key: signer, CertPEM: certPEM, KeyPEM: keyPEM}, nil
}

// Pool returns a cert pool trusting only this CA.
func (ca *CA) Pool() *x509.CertPool {
	p := x509.NewCertPool()
	p.AddCert(ca.Cert)
	return p
}

// Fingerprint is the SHA-256 of the CA certificate, as pinned by join tokens.
func (ca *CA) Fingerprint() string { return Fingerprint(ca.Cert.Raw) }

// Fingerprint returns the hex SHA-256 of a DER certificate.
func Fingerprint(der []byte) string {
	sum := sha256.Sum256(der)
	return hex.EncodeToString(sum[:])
}

// Issue signs a certificate for pub. names become DNS or IP SANs.
func (ca *CA) Issue(pub crypto.PublicKey, commonName string, names []string, ttl time.Duration) ([]byte, error) {
	tmpl := &x509.Certificate{
		SerialNumber: serial(),
		Subject:      pkix.Name{CommonName: commonName, Organization: []string{"flint"}},
		NotBefore:    time.Now().Add(-5 * time.Minute),
		NotAfter:     time.Now().Add(ttl),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth, x509.ExtKeyUsageClientAuth},
	}
	for _, n := range names {
		if n == "" {
			continue
		}
		if ip := net.ParseIP(n); ip != nil {
			tmpl.IPAddresses = append(tmpl.IPAddresses, ip)
		} else {
			tmpl.DNSNames = append(tmpl.DNSNames, n)
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, ca.Cert, pub, ca.Key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), nil
}

// IssueFromCSR verifies a PEM CSR and signs it. The CSR's own subject and
// SANs are ignored: the caller decides the identity.
func (ca *CA) IssueFromCSR(csrPEM []byte, commonName string, names []string, ttl time.Duration) ([]byte, error) {
	block, _ := pem.Decode(csrPEM)
	if block == nil || block.Type != "CERTIFICATE REQUEST" {
		return nil, errors.New("invalid CSR PEM")
	}
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil {
		return nil, err
	}
	if err := csr.CheckSignature(); err != nil {
		return nil, fmt.Errorf("csr signature: %w", err)
	}
	return ca.Issue(csr.PublicKey, commonName, names, ttl)
}

// Keypair issues a fresh key and certificate (for the control plane's own identity).
func (ca *CA) Keypair(commonName string, names []string, ttl time.Duration) (tls.Certificate, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, err
	}
	certPEM, err := ca.Issue(&key.PublicKey, commonName, names, ttl)
	if err != nil {
		return tls.Certificate{}, err
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return tls.Certificate{}, err
	}
	return tls.X509KeyPair(certPEM, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}))
}

// NewKeyAndCSR makes a host key and a CSR for it. Returns PEMs.
func NewKeyAndCSR(commonName string) (keyPEM, csrPEM []byte, err error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, err
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: commonName},
	}, key)
	if err != nil {
		return nil, nil, err
	}
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}),
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), nil
}

// CSRFromKey makes a CSR for an existing PEM key (certificate renewal).
func CSRFromKey(keyPEM []byte, commonName string) ([]byte, error) {
	block, _ := pem.Decode(keyPEM)
	if block == nil {
		return nil, errors.New("invalid key PEM")
	}
	key, err := x509.ParseECPrivateKey(block.Bytes)
	if err != nil {
		return nil, err
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{Subject: pkix.Name{CommonName: commonName}}, key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), nil
}

// ParseCertPEM returns the first certificate in a PEM blob.
func ParseCertPEM(b []byte) (*x509.Certificate, error) {
	block, _ := pem.Decode(b)
	if block == nil || block.Type != "CERTIFICATE" {
		return nil, errors.New("invalid certificate PEM")
	}
	return x509.ParseCertificate(block.Bytes)
}

// PoolFromPEM builds a cert pool from PEM certificates.
func PoolFromPEM(b []byte) (*x509.CertPool, error) {
	p := x509.NewCertPool()
	if !p.AppendCertsFromPEM(b) {
		return nil, errors.New("no certificates in PEM")
	}
	return p, nil
}

func serial() *big.Int {
	n, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 126))
	return n
}

// RandomSecret returns a URL-safe random string with n bytes of entropy.
func RandomSecret(n int) string {
	b := make([]byte, n)
	rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

// HashSecret is how tokens are stored: never the secret itself.
func HashSecret(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

const joinPrefix = "flintjoin1."

// JoinToken is what a new host needs to join: where the control plane's
// host endpoint is, a single-use secret, and the control-plane CA fingerprint
// to pin, so joining over an untrusted network can't be intercepted.
type JoinToken struct {
	Server      string `json:"s"`
	ID          string `json:"i"`
	Secret      string `json:"k"`
	Fingerprint string `json:"f"`
}

// Encode renders the token as a single string.
func (t JoinToken) Encode() string {
	b, _ := json.Marshal(t)
	return joinPrefix + base64.RawURLEncoding.EncodeToString(b)
}

// DecodeJoinToken parses a token string.
func DecodeJoinToken(s string) (JoinToken, error) {
	var t JoinToken
	s = strings.TrimSpace(s)
	if !strings.HasPrefix(s, joinPrefix) {
		return t, errors.New("not a flint join token")
	}
	b, err := base64.RawURLEncoding.DecodeString(strings.TrimPrefix(s, joinPrefix))
	if err != nil {
		return t, fmt.Errorf("join token: %w", err)
	}
	if err := json.Unmarshal(b, &t); err != nil {
		return t, fmt.Errorf("join token: %w", err)
	}
	if t.Server == "" || t.ID == "" || t.Secret == "" || len(t.Fingerprint) != 64 {
		return t, errors.New("join token: incomplete")
	}
	return t, nil
}

// PinnedTLS returns a client TLS config that trusts exactly the server
// certificates chaining to a CA with this fingerprint (used once, at join).
func PinnedTLS(fingerprint string) *tls.Config {
	return &tls.Config{
		MinVersion:         tls.VersionTLS12,
		InsecureSkipVerify: true, // replaced by the pin check below
		VerifyPeerCertificate: func(raw [][]byte, _ [][]*x509.Certificate) error {
			if len(raw) == 0 {
				return errors.New("no server certificate")
			}
			leaf, err := x509.ParseCertificate(raw[0])
			if err != nil {
				return err
			}
			for _, der := range raw[1:] {
				if Fingerprint(der) != fingerprint {
					continue
				}
				ca, err := x509.ParseCertificate(der)
				if err != nil {
					return err
				}
				pool := x509.NewCertPool()
				pool.AddCert(ca)
				_, err = leaf.Verify(x509.VerifyOptions{Roots: pool, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}})
				return err
			}
			return errors.New("server certificate does not chain to the pinned control-plane CA")
		},
	}
}
