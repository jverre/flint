package snapstore

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/errgroup"
	"golang.org/x/sys/unix"
)

// ChunkSize is the unit of dedup, upload and fetch.
const ChunkSize = 4 << 20

// pageSize is the unit of hole punching when a chunk is written out.
const pageSize = 4096

// FileEntry describes one file of a snapshot. Chunks[i] covers bytes
// [i*ChunkSize, (i+1)*ChunkSize); "" means the range is all zeros.
type FileEntry struct {
	Size   int64    `json:"size"`
	Mode   uint32   `json:"mode"`
	Chunks []string `json:"chunks"`
}

// Manifest is the immutable description of a snapshot.
type Manifest struct {
	ID        string               `json:"id"`
	Kind      string               `json:"kind"` // template | pause | checkpoint | fork
	Parent    string               `json:"parent,omitempty"`
	CreatedAt int64                `json:"created_at"` // unix ms
	ChunkSize int64                `json:"chunk_size"`
	Files     map[string]FileEntry `json:"files"`
	Meta      map[string]string    `json:"meta,omitempty"`
}

// Hashes returns the distinct non-zero chunks of the snapshot.
func (m *Manifest) Hashes() []string {
	seen := map[string]bool{}
	var out []string
	for _, f := range m.Files {
		for _, h := range f.Chunks {
			if h != "" && !seen[h] {
				seen[h] = true
				out = append(out, h)
			}
		}
	}
	return out
}

// Bytes is the total non-zero data in the snapshot.
func (m *Manifest) Bytes() int64 { return int64(len(m.Hashes())) * m.ChunkSize }

func manifestKey(id string) string { return "manifests/" + id + ".json" }
func chunkKey(h string) string     { return "chunks/" + h[:2] + "/" + h }

// ValidID reports whether id is safe to use in keys and paths.
func ValidID(id string) bool {
	if id == "" || len(id) > 128 {
		return false
	}
	for _, c := range id {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '-' || c == '_' || c == '.') {
			return false
		}
	}
	return !strings.Contains(id, "..")
}

func validHash(h string) bool {
	if len(h) != 64 {
		return false
	}
	_, err := hex.DecodeString(h)
	return err == nil
}

// validFileName accepts relative, clean paths like "mem" or "snapshot/state.json".
func validFileName(name string) bool {
	if name == "" || strings.HasPrefix(name, "/") || strings.Contains(name, "\\") {
		return false
	}
	clean := path.Clean(name)
	return clean == name && !strings.HasPrefix(clean, "..") && !strings.Contains(clean, "/../")
}

// Stats reports what an operation moved.
type Stats struct {
	Chunks       int   `json:"chunks"`
	NewChunks    int   `json:"new_chunks"`     // chunks not already in the cache (create) or fetched (restore)
	Uploaded     int   `json:"uploaded"`       // chunks written to the remote
	FromPeers    int   `json:"from_peers"`     // restore: chunks fetched from peer hosts
	FromRemote   int   `json:"from_remote"`    // restore: chunks fetched from the object store
	Bytes        int64 `json:"bytes"`          // non-zero bytes in the snapshot
	DurationMs   int64 `json:"duration_ms"`
}

// UploadState tracks a background upload.
type UploadState struct {
	mu    sync.Mutex
	done  bool
	err   error
	wait  chan struct{}
	stats Stats
}

func (u *UploadState) finish(err error) {
	u.mu.Lock()
	u.done, u.err = true, err
	u.mu.Unlock()
	close(u.wait)
}

// Status returns (done, err).
func (u *UploadState) Status() (bool, error) {
	u.mu.Lock()
	defer u.mu.Unlock()
	return u.done, u.err
}

// Store combines the shared remote, the local cache and peer hosts.
type Store struct {
	Remote Remote
	Cache  *Cache
	// Peers fetches chunks and manifests from other hosts (mTLS). Optional.
	Peers *http.Client

	mu      sync.Mutex
	uploads map[string]*UploadState
	// remoteKnown remembers chunks known to be in the remote, so repeated
	// snapshots of the same VM don't HEAD every chunk again.
	remoteKnown map[string]bool
}

func NewStore(remote Remote, cache *Cache, peers *http.Client) *Store {
	return &Store{Remote: remote, Cache: cache, Peers: peers, uploads: map[string]*UploadState{}, remoteKnown: map[string]bool{}}
}

// CreateRequest describes a snapshot to take from files on this host.
type CreateRequest struct {
	ID     string            `json:"id"`
	Kind   string            `json:"kind"`
	Parent string            `json:"parent,omitempty"`
	Files  map[string]string `json:"files"` // name in snapshot -> host path (relative to Root if set)
	// Root, if set, is a directory the files are opened beneath with no
	// symlinks followed anywhere in their path. Use it for files a VMM could
	// write (its jail), so a compromised guest can't swap in a link to a
	// host file and have it exported into a snapshot.
	Root string `json:"root,omitempty"`
	Meta   map[string]string `json:"meta,omitempty"`
	// Upload: "sync" (return once in the remote), "async" (return once cached
	// locally; peers can fetch immediately) or "none" (cache only).
	Upload string `json:"upload"`
}

// Create chunks the files into the cache, writes the manifest and uploads.
func (s *Store) Create(ctx context.Context, req CreateRequest) (*Manifest, Stats, error) {
	t0 := time.Now()
	var st Stats
	if !ValidID(req.ID) {
		return nil, st, fmt.Errorf("invalid snapshot id %q", req.ID)
	}
	if len(req.Files) == 0 {
		return nil, st, errors.New("snapshot has no files")
	}
	m := &Manifest{ID: req.ID, Kind: req.Kind, Parent: req.Parent, CreatedAt: time.Now().UnixMilli(),
		ChunkSize: ChunkSize, Files: map[string]FileEntry{}, Meta: req.Meta}
	var root *os.File
	if req.Root != "" {
		r, err := os.Open(req.Root)
		if err != nil {
			return nil, st, fmt.Errorf("root: %w", err)
		}
		defer r.Close()
		root = r
	}
	for name, p := range req.Files {
		if !validFileName(name) {
			return nil, st, fmt.Errorf("invalid file name %q", name)
		}
		f, err := openSnapshotFile(root, p)
		if err != nil {
			return nil, st, fmt.Errorf("%s: %w", name, err)
		}
		fe, n, err := s.chunkFile(ctx, f)
		f.Close()
		if err != nil {
			return nil, st, fmt.Errorf("%s: %w", name, err)
		}
		st.NewChunks += n
		m.Files[name] = fe
	}
	st.Chunks = len(m.Hashes())
	st.Bytes = m.Bytes()
	if err := s.Cache.PutManifest(m); err != nil {
		return nil, st, err
	}
	switch req.Upload {
	case "none":
	case "async":
		u := s.startUpload(m)
		_ = u
	default:
		u := s.startUpload(m)
		select {
		case <-u.wait:
		case <-ctx.Done():
			return nil, st, ctx.Err()
		}
		if _, err := u.Status(); err != nil {
			return nil, st, fmt.Errorf("upload: %w", err)
		}
		st.Uploaded = u.stats.Uploaded
	}
	st.DurationMs = time.Since(t0).Milliseconds()
	return m, st, nil
}

// openSnapshotFile opens p, beneath root without following symlinks if root is set.
func openSnapshotFile(root *os.File, p string) (*os.File, error) {
	if root == nil {
		return os.Open(p)
	}
	if filepath.IsAbs(p) || !validFileName(p) {
		return nil, fmt.Errorf("path %q must be relative to root", p)
	}
	fd, err := unix.Openat2(int(root.Fd()), p, &unix.OpenHow{
		Flags:   unix.O_RDONLY | unix.O_CLOEXEC | unix.O_NOFOLLOW,
		Resolve: unix.RESOLVE_BENEATH | unix.RESOLVE_NO_SYMLINKS | unix.RESOLVE_NO_MAGICLINKS,
	})
	if err != nil {
		return nil, err
	}
	return os.NewFile(uintptr(fd), filepath.Join(root.Name(), p)), nil
}

// chunkFile hashes f into the cache. Returns its entry and how many chunks were new.
func (s *Store) chunkFile(ctx context.Context, f *os.File) (FileEntry, int, error) {
	info, err := f.Stat()
	if err != nil {
		return FileEntry{}, 0, err
	}
	if !info.Mode().IsRegular() {
		return FileEntry{}, 0, fmt.Errorf("not a regular file")
	}
	size := info.Size()
	n := int((size + ChunkSize - 1) / ChunkSize)
	fe := FileEntry{Size: size, Mode: uint32(info.Mode().Perm()), Chunks: make([]string, n)}
	var newMu sync.Mutex
	newCount := 0
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(4)
	for i := 0; i < n; i++ {
		i := i
		off := int64(i) * ChunkSize
		if !hasData(f, off, ChunkSize) {
			continue
		}
		g.Go(func() error {
			if gctx.Err() != nil {
				return gctx.Err()
			}
			length := int64(ChunkSize)
			if off+length > size {
				length = size - off
			}
			buf := make([]byte, length)
			if _, err := f.ReadAt(buf, off); err != nil && err != io.EOF {
				return err
			}
			if isZero(buf) {
				return nil
			}
			sum := sha256.Sum256(buf)
			h := hex.EncodeToString(sum[:])
			fe.Chunks[i] = h
			added, err := s.Cache.PutChunk(h, buf)
			if err != nil {
				return err
			}
			if added {
				newMu.Lock()
				newCount++
				newMu.Unlock()
			}
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return FileEntry{}, 0, err
	}
	return fe, newCount, nil
}

// hasData reports whether [off, off+length) contains any allocated data.
func hasData(f *os.File, off, length int64) bool {
	next, err := unix.Seek(int(f.Fd()), off, unix.SEEK_DATA)
	if err != nil {
		// ENXIO: no data after off. Anything else (filesystem without
		// SEEK_DATA): assume data and let the zero check decide.
		return !errors.Is(err, unix.ENXIO)
	}
	return next < off+length
}

func isZero(b []byte) bool {
	for len(b) >= 8 {
		if b[0]|b[1]|b[2]|b[3]|b[4]|b[5]|b[6]|b[7] != 0 {
			return false
		}
		b = b[8:]
	}
	for _, c := range b {
		if c != 0 {
			return false
		}
	}
	return true
}

func (s *Store) startUpload(m *Manifest) *UploadState {
	s.mu.Lock()
	if u, ok := s.uploads[m.ID]; ok {
		s.mu.Unlock()
		return u
	}
	u := &UploadState{wait: make(chan struct{})}
	s.uploads[m.ID] = u
	s.mu.Unlock()
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Minute)
		defer cancel()
		n, err := s.upload(ctx, m)
		u.stats.Uploaded = n
		if err != nil {
			log.Printf("snapstore: upload %s failed: %v", m.ID, err)
		}
		u.finish(err)
	}()
	return u
}

// Upload pushes a cached snapshot to the remote. The manifest goes last, so a
// manifest in the remote means the whole snapshot is there.
func (s *Store) upload(ctx context.Context, m *Manifest) (int, error) {
	var uploaded int
	var mu sync.Mutex
	g, gctx := errgroup.WithContext(ctx)
	g.SetLimit(8)
	for _, h := range m.Hashes() {
		h := h
		s.mu.Lock()
		known := s.remoteKnown[h]
		s.mu.Unlock()
		if known {
			continue
		}
		g.Go(func() error {
			ok, err := s.Remote.Exists(gctx, chunkKey(h))
			if err != nil {
				return err
			}
			if !ok {
				data, err := s.Cache.GetChunk(h)
				if err != nil {
					return fmt.Errorf("chunk %s missing from cache: %w", h[:12], err)
				}
				if err := s.Remote.Put(gctx, chunkKey(h), data); err != nil {
					return err
				}
				mu.Lock()
				uploaded++
				mu.Unlock()
			}
			s.mu.Lock()
			s.remoteKnown[h] = true
			s.mu.Unlock()
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return uploaded, err
	}
	b, err := json.Marshal(m)
	if err != nil {
		return uploaded, err
	}
	return uploaded, s.Remote.Put(ctx, manifestKey(m.ID), b)
}

// UploadStatus returns whether a snapshot is fully in the remote.
func (s *Store) UploadStatus(ctx context.Context, id string) (done bool, err error) {
	s.mu.Lock()
	u := s.uploads[id]
	s.mu.Unlock()
	if u != nil {
		return u.Status()
	}
	return s.Remote.Exists(ctx, manifestKey(id))
}

// WaitUploaded blocks until a snapshot started on this host is in the remote.
func (s *Store) WaitUploaded(ctx context.Context, id string) error {
	s.mu.Lock()
	u := s.uploads[id]
	s.mu.Unlock()
	if u == nil {
		ok, err := s.Remote.Exists(ctx, manifestKey(id))
		if err == nil && !ok {
			return fmt.Errorf("snapshot %s is not uploaded", id)
		}
		return err
	}
	select {
	case <-u.wait:
		_, err := u.Status()
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Manifest finds a manifest: cache, then peers, then the remote.
func (s *Store) Manifest(ctx context.Context, id string, peers []string) (*Manifest, error) {
	if !ValidID(id) {
		return nil, fmt.Errorf("invalid snapshot id %q", id)
	}
	if m, err := s.Cache.GetManifest(id); err == nil {
		return m, nil
	}
	var data []byte
	for _, p := range peers {
		b, err := s.peerGet(ctx, p, "/v1/peer/manifests/"+id)
		if err == nil {
			data = b
			break
		}
	}
	if data == nil {
		b, err := s.Remote.Get(ctx, manifestKey(id))
		if err != nil {
			return nil, fmt.Errorf("manifest %s: %w", id, err)
		}
		data = b
	}
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("manifest %s: %w", id, err)
	}
	if m.ID != id || m.ChunkSize != ChunkSize {
		return nil, fmt.Errorf("manifest %s: inconsistent (id=%q chunk_size=%d)", id, m.ID, m.ChunkSize)
	}
	for name, f := range m.Files {
		if !validFileName(name) {
			return nil, fmt.Errorf("manifest %s: invalid file name %q", id, name)
		}
		for _, h := range f.Chunks {
			if h != "" && !validHash(h) {
				return nil, fmt.Errorf("manifest %s: invalid chunk hash", id)
			}
		}
	}
	if err := s.Cache.PutManifest(&m); err != nil {
		return nil, err
	}
	return &m, nil
}

func (s *Store) peerGet(ctx context.Context, peer, p string) ([]byte, error) {
	if s.Peers == nil {
		return nil, errors.New("no peer client")
	}
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(peer, "/")+p, nil)
	if err != nil {
		return nil, err
	}
	resp, err := s.Peers.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("peer %s: %s", peer, resp.Status)
	}
	return io.ReadAll(io.LimitReader(resp.Body, ChunkSize+1<<20))
}

// chunk returns a chunk's data, fetching and caching it if needed. Every
// fetched chunk is verified against its hash, so peers need not be trusted
// for integrity.
func (s *Store) chunk(ctx context.Context, h string, peers []string, st *Stats, mu *sync.Mutex) ([]byte, error) {
	if b, err := s.Cache.GetChunk(h); err == nil {
		return b, nil
	}
	verify := func(b []byte) bool {
		sum := sha256.Sum256(b)
		return hex.EncodeToString(sum[:]) == h
	}
	for _, p := range peers {
		b, err := s.peerGet(ctx, p, "/v1/peer/chunks/"+h)
		if err == nil && verify(b) {
			if _, err := s.Cache.PutChunk(h, b); err != nil {
				return nil, err
			}
			mu.Lock()
			st.FromPeers++
			mu.Unlock()
			return b, nil
		}
	}
	b, err := s.Remote.Get(ctx, chunkKey(h))
	if err != nil {
		return nil, fmt.Errorf("chunk %s: %w", h[:12], err)
	}
	if !verify(b) {
		return nil, fmt.Errorf("chunk %s: hash mismatch from object store", h[:12])
	}
	if _, err := s.Cache.PutChunk(h, b); err != nil {
		return nil, err
	}
	mu.Lock()
	st.FromRemote++
	mu.Unlock()
	return b, nil
}

// Restore writes a snapshot's files into destDir (sparse), creating it.
// names restricts which files to write (all if empty).
func (s *Store) Restore(ctx context.Context, id, destDir string, peers []string, names []string) (*Manifest, Stats, error) {
	t0 := time.Now()
	var st Stats
	m, err := s.Manifest(ctx, id, peers)
	if err != nil {
		return nil, st, err
	}
	want := map[string]bool{}
	for _, n := range names {
		want[n] = true
	}
	if err := os.MkdirAll(destDir, 0o700); err != nil {
		return nil, st, err
	}
	var mu sync.Mutex
	for name, fe := range m.Files {
		if len(want) > 0 && !want[name] {
			continue
		}
		dst := filepath.Join(destDir, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
			return nil, st, err
		}
		f, err := os.OpenFile(dst, os.O_CREATE|os.O_TRUNC|os.O_WRONLY|unix.O_NOFOLLOW, os.FileMode(fe.Mode&0o777))
		if err != nil {
			return nil, st, err
		}
		if err := f.Truncate(fe.Size); err != nil {
			f.Close()
			return nil, st, err
		}
		g, gctx := errgroup.WithContext(ctx)
		g.SetLimit(8)
		for i, h := range fe.Chunks {
			if h == "" {
				continue
			}
			i, h := i, h
			g.Go(func() error {
				data, err := s.chunk(gctx, h, peers, &st, &mu)
				if err != nil {
					return err
				}
				mu.Lock()
				st.Chunks++
				mu.Unlock()
				return writeSparse(f, int64(i)*ChunkSize, data)
			})
		}
		werr := g.Wait()
		cerr := f.Close()
		if werr != nil {
			return nil, st, fmt.Errorf("%s: %w", name, werr)
		}
		if cerr != nil {
			return nil, st, cerr
		}
	}
	st.NewChunks = st.FromPeers + st.FromRemote
	st.Bytes = m.Bytes()
	st.DurationMs = time.Since(t0).Milliseconds()
	return m, st, nil
}

// writeSparse writes data at off, skipping all-zero pages so they stay holes.
func writeSparse(f *os.File, off int64, data []byte) error {
	for start := 0; start < len(data); {
		// Find the next run of non-zero pages.
		for start < len(data) && isZero(data[start:minInt(start+pageSize, len(data))]) {
			start += pageSize
		}
		if start >= len(data) {
			break
		}
		end := start
		for end < len(data) && !isZero(data[end:minInt(end+pageSize, len(data))]) {
			end += pageSize
		}
		end = minInt(end, len(data))
		if _, err := f.WriteAt(data[start:end], off+int64(start)); err != nil {
			return err
		}
		start = end
	}
	return nil
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// Delete removes a snapshot's manifest from the remote and the cache. Its
// chunks are reclaimed by GC once nothing references them.
func (s *Store) Delete(ctx context.Context, id string) error {
	if !ValidID(id) {
		return fmt.Errorf("invalid snapshot id %q", id)
	}
	s.Cache.DeleteManifest(id)
	s.mu.Lock()
	delete(s.uploads, id)
	s.mu.Unlock()
	return s.Remote.Delete(ctx, manifestKey(id))
}

// GCResult reports a garbage collection pass.
type GCResult struct {
	ManifestsDeleted int `json:"manifests_deleted"`
	ChunksDeleted    int `json:"chunks_deleted"`
	ChunksKept       int `json:"chunks_kept"`
}

// GC deletes remote manifests not in live (older than grace), then chunks no
// remaining manifest references (older than grace, so chunks of an upload
// still in progress survive).
func GC(ctx context.Context, r Remote, live map[string]bool, grace time.Duration) (GCResult, error) {
	var res GCResult
	cutoff := time.Now().Add(-grace)
	objs, err := r.List(ctx, "manifests/")
	if err != nil {
		return res, err
	}
	referenced := map[string]bool{}
	for _, o := range objs {
		id := strings.TrimSuffix(path.Base(o.Key), ".json")
		if !live[id] && o.ModTime.Before(cutoff) {
			if err := r.Delete(ctx, o.Key); err != nil {
				return res, err
			}
			res.ManifestsDeleted++
			continue
		}
		b, err := r.Get(ctx, o.Key)
		if err != nil {
			if errors.Is(err, ErrNotFound) {
				continue
			}
			return res, err
		}
		var m Manifest
		if err := json.Unmarshal(b, &m); err != nil {
			continue
		}
		for _, h := range m.Hashes() {
			referenced[h] = true
		}
	}
	chunks, err := r.List(ctx, "chunks/")
	if err != nil {
		return res, err
	}
	for _, o := range chunks {
		h := path.Base(o.Key)
		if referenced[h] || o.ModTime.After(cutoff) {
			res.ChunksKept++
			continue
		}
		if err := r.Delete(ctx, o.Key); err != nil {
			return res, err
		}
		res.ChunksDeleted++
	}
	return res, nil
}
