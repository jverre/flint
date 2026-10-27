// flint-import pulls a container image and writes its flattened filesystem to
// stdout as one tar stream.
//
// It runs inside Flint's disposable template builder VM. Nothing it reads from
// the registry is executed: layers are only merged (honouring whiteouts) and
// passed on to tar. Registry credentials are read from a file that is removed
// as soon as it has been read, and never printed.
package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strings"
	"time"

	"github.com/google/go-containerregistry/pkg/authn"
	"github.com/google/go-containerregistry/pkg/name"
	v1 "github.com/google/go-containerregistry/pkg/v1"
	"github.com/google/go-containerregistry/pkg/v1/mutate"
	"github.com/google/go-containerregistry/pkg/v1/remote"
	"github.com/google/go-containerregistry/pkg/v1/remote/transport"
)

// source is what the import pins: the image as named and the exact manifest.
type source struct {
	Image  string `json:"image"`
	Digest string `json:"digest"`
}

func main() {
	image := flag.String("image", "", "image reference, e.g. python:3.12-slim")
	authFile := flag.String("auth", "", "JSON file with username and password; removed once read")
	maxBytes := flag.Int64("max-bytes", 0, "fail early if the compressed layers exceed this size")
	sourceOut := flag.String("source-out", "", "where to write the resolved image and digest")
	envOut := flag.String("env-out", "", "where to write the image's environment as shell exports")
	flag.Parse()
	if err := run(*image, *authFile, *maxBytes, *sourceOut, *envOut); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(image, authFile string, maxBytes int64, sourceOut, envOut string) error {
	if image == "" || sourceOut == "" || envOut == "" {
		return errors.New("-image, -source-out and -env-out are required")
	}
	auth, err := readAuth(authFile)
	if err != nil {
		return err
	}
	ref, err := name.ParseReference(image)
	if err != nil {
		return fmt.Errorf("invalid image reference %q: %v", image, err)
	}
	platform := v1.Platform{OS: "linux", Architecture: "amd64"}
	img, err := remote.Image(ref, remote.WithAuth(auth), remote.WithPlatform(platform), remote.WithUserAgent("flint-import"),
		// Registries rate-limit anonymous pulls (429); back off rather than fail the import.
		remote.WithRetryStatusCodes(http.StatusTooManyRequests, http.StatusRequestTimeout, http.StatusInternalServerError,
			http.StatusBadGateway, http.StatusServiceUnavailable, http.StatusGatewayTimeout),
		remote.WithRetryBackoff(remote.Backoff{Duration: 2 * time.Second, Factor: 2, Jitter: 0.2, Steps: 6}))
	if err != nil {
		var te *transport.Error
		if errors.As(err, &te) && (te.StatusCode == 401 || te.StatusCode == 403 || te.StatusCode == 404) {
			return fmt.Errorf("pulling %s: the image doesn't exist or needs registry credentials (%d)", image, te.StatusCode)
		}
		return fmt.Errorf("pulling %s: %v", image, err)
	}
	cfg, err := img.ConfigFile()
	if err != nil {
		return fmt.Errorf("reading image config: %v", err)
	}
	if cfg.OS != platform.OS || cfg.Architecture != platform.Architecture {
		return fmt.Errorf("%s is %s/%s; Flint runs linux/amd64 images", image, cfg.OS, cfg.Architecture)
	}
	if maxBytes > 0 {
		if err := checkSize(img, maxBytes); err != nil {
			return err
		}
	}
	digest, err := img.Digest()
	if err != nil {
		return fmt.Errorf("reading image digest: %v", err)
	}
	if err := writeJSON(sourceOut, source{Image: ref.Name(), Digest: digest.String()}); err != nil {
		return err
	}
	if err := writeEnv(envOut, cfg.Config.Env); err != nil {
		return err
	}
	// Extract merges the layers in order and drops whited-out paths.
	fs := mutate.Extract(img)
	defer fs.Close()
	out := bufio.NewWriterSize(os.Stdout, 1<<20)
	if _, err := io.Copy(out, fs); err != nil {
		return fmt.Errorf("reading layers: %v", err)
	}
	return out.Flush()
}

func readAuth(path string) (authn.Authenticator, error) {
	if path == "" {
		return authn.Anonymous, nil
	}
	data, err := os.ReadFile(path)
	os.Remove(path)
	if err != nil {
		return nil, fmt.Errorf("reading registry credentials: %v", err)
	}
	var c struct {
		Username string `json:"username"`
		Password string `json:"password"`
	}
	if err := json.Unmarshal(data, &c); err != nil || c.Username == "" || c.Password == "" {
		return nil, errors.New("registry credentials need a username and password")
	}
	return authn.FromConfig(authn.AuthConfig{Username: c.Username, Password: c.Password}), nil
}

func checkSize(img v1.Image, maxBytes int64) error {
	layers, err := img.Layers()
	if err != nil {
		return fmt.Errorf("reading layers: %v", err)
	}
	var total int64
	for _, l := range layers {
		size, err := l.Size()
		if err != nil {
			return fmt.Errorf("reading layer size: %v", err)
		}
		total += size
	}
	if total > maxBytes {
		return fmt.Errorf("the image's layers are %d MiB compressed, more than the template's %d MiB disk; raise rootfs_size_mb",
			total>>20, maxBytes>>20)
	}
	return nil
}

// writeEnv writes the image's ENV as shell exports, so commands in the VM see
// the same environment as `docker run` would give them (PATH included).
func writeEnv(path string, env []string) error {
	sort.Strings(env)
	var b strings.Builder
	for _, kv := range env {
		k, v, ok := strings.Cut(kv, "=")
		if !ok || !validName(k) {
			continue
		}
		b.WriteString("export " + k + "='" + strings.ReplaceAll(v, "'", `'\''`) + "'\n")
	}
	return os.WriteFile(path, []byte(b.String()), 0o644)
}

func validName(k string) bool {
	if k == "" || k[0] >= '0' && k[0] <= '9' {
		return false
	}
	for _, c := range k {
		if !(c == '_' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9') {
			return false
		}
	}
	return true
}

func writeJSON(path string, v any) error {
	data, err := json.Marshal(v)
	if err != nil {
		return err
	}
	return os.WriteFile(path, data, 0o644)
}
