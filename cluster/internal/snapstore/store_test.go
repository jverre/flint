package snapstore

import (
	"bytes"
	"context"
	"crypto/rand"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

// writeSparseFile makes a 64 MiB file with data at a few offsets and holes elsewhere.
func writeSparseFile(t *testing.T, path string) []byte {
	t.Helper()
	const size = 64 << 20
	want := make([]byte, size)
	for _, off := range []int{0, 5<<20 + 123, 40 << 20} {
		rand.Read(want[off : off+70000])
	}
	f, err := os.Create(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	if err := f.Truncate(size); err != nil {
		t.Fatal(err)
	}
	for _, off := range []int{0, 5<<20 + 123, 40 << 20} {
		if _, err := f.WriteAt(want[off:off+70000], int64(off)); err != nil {
			t.Fatal(err)
		}
	}
	return want
}

func newHost(t *testing.T, remote Remote, peers *http.Client) *Store {
	t.Helper()
	c, err := OpenCache(t.TempDir(), 1<<30)
	if err != nil {
		t.Fatal(err)
	}
	return NewStore(remote, c, peers)
}

func allocated(t *testing.T, path string) int64 {
	var st syscall.Stat_t
	if err := syscall.Stat(path, &st); err != nil {
		t.Fatal(err)
	}
	return st.Blocks * 512
}

func TestSnapshotRestoresOnAnotherHostSparseAndDeduped(t *testing.T) {
	ctx := context.Background()
	remote := &FileRemote{Root: t.TempDir()}
	a := newHost(t, remote, nil)
	src := filepath.Join(t.TempDir(), "mem")
	want := writeSparseFile(t, src)

	m, st, err := a.Create(ctx, CreateRequest{ID: "snap-1", Kind: "pause", Files: map[string]string{"pause-mem": src}, Upload: "sync"})
	if err != nil {
		t.Fatal(err)
	}
	if st.Chunks != 3 || st.Uploaded != 3 {
		t.Fatalf("want 3 non-zero chunks uploaded, got %+v", st)
	}

	// Same content again (e.g. a second checkpoint with nothing changed): nothing new goes up.
	_, st2, err := a.Create(ctx, CreateRequest{ID: "snap-2", Kind: "checkpoint", Parent: "snap-1", Files: map[string]string{"pause-mem": src}, Upload: "sync"})
	if err != nil {
		t.Fatal(err)
	}
	if st2.Uploaded != 0 || st2.NewChunks != 0 {
		t.Fatalf("unchanged snapshot re-uploaded data: %+v", st2)
	}

	b := newHost(t, remote, nil)
	dest := t.TempDir()
	if _, rst, err := b.Restore(ctx, m.ID, dest, nil, nil); err != nil {
		t.Fatal(err)
	} else if rst.FromRemote != 3 {
		t.Fatalf("want 3 chunks from the object store, got %+v", rst)
	}
	got, err := os.ReadFile(filepath.Join(dest, "pause-mem"))
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, want) {
		t.Fatal("restored file differs from the original")
	}
	if a := allocated(t, filepath.Join(dest, "pause-mem")); a > 1<<20 {
		t.Fatalf("restored file is not sparse: %d bytes allocated", a)
	}
}

func TestRestoreFetchesFromPeerBeforeUpload(t *testing.T) {
	ctx := context.Background()
	remote := &FileRemote{Root: t.TempDir()}
	a := newHost(t, remote, nil)
	src := filepath.Join(t.TempDir(), "rootfs.ext4")
	want := writeSparseFile(t, src)
	// Cache only: the object store never sees it, so B must get it from A.
	if _, _, err := a.Create(ctx, CreateRequest{ID: "fork-1", Kind: "fork", Files: map[string]string{"rootfs.ext4": src}, Upload: "none"}); err != nil {
		t.Fatal(err)
	}
	peer := httptest.NewServer(PeerHandler(a.Cache))
	defer peer.Close()

	b := newHost(t, remote, peer.Client())
	dest := t.TempDir()
	_, st, err := b.Restore(ctx, "fork-1", dest, []string{peer.URL}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if st.FromPeers != 3 || st.FromRemote != 0 {
		t.Fatalf("want all chunks from the peer, got %+v", st)
	}
	got, _ := os.ReadFile(filepath.Join(dest, "rootfs.ext4"))
	if !bytes.Equal(got, want) {
		t.Fatal("restored file differs from the original")
	}
}

func TestRestoreRejectsTamperedChunks(t *testing.T) {
	ctx := context.Background()
	remote := &FileRemote{Root: t.TempDir()}
	a := newHost(t, remote, nil)
	src := filepath.Join(t.TempDir(), "mem")
	writeSparseFile(t, src)
	m, _, err := a.Create(ctx, CreateRequest{ID: "s", Kind: "pause", Files: map[string]string{"mem": src}, Upload: "sync"})
	if err != nil {
		t.Fatal(err)
	}
	h := m.Hashes()[0]
	if err := remote.Put(ctx, chunkKey(h), []byte("evil")); err != nil {
		t.Fatal(err)
	}
	b := newHost(t, remote, nil)
	if _, _, err := b.Restore(ctx, "s", t.TempDir(), nil, nil); err == nil {
		t.Fatal("restore accepted a chunk whose content doesn't match its hash")
	}
}

func TestGCKeepsLiveAndRecentData(t *testing.T) {
	ctx := context.Background()
	remote := &FileRemote{Root: t.TempDir()}
	a := newHost(t, remote, nil)
	dir := t.TempDir()
	srcLive, srcDead := filepath.Join(dir, "live"), filepath.Join(dir, "dead")
	writeSparseFile(t, srcLive)
	writeSparseFile(t, srcDead)
	for id, src := range map[string]string{"live": srcLive, "dead": srcDead} {
		if _, _, err := a.Create(ctx, CreateRequest{ID: id, Kind: "pause", Files: map[string]string{"f": src}, Upload: "sync"}); err != nil {
			t.Fatal(err)
		}
	}
	// A grace period covering everything just written deletes nothing.
	res, err := GC(ctx, remote, map[string]bool{"live": true}, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if res.ManifestsDeleted != 0 || res.ChunksDeleted != 0 {
		t.Fatalf("GC deleted data inside the grace period: %+v", res)
	}
	res, err = GC(ctx, remote, map[string]bool{"live": true}, -time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if res.ManifestsDeleted != 1 || res.ChunksDeleted != 3 || res.ChunksKept != 3 {
		t.Fatalf("unexpected GC result: %+v", res)
	}
	b := newHost(t, remote, nil)
	if _, _, err := b.Restore(ctx, "live", t.TempDir(), nil, nil); err != nil {
		t.Fatalf("live snapshot broken after GC: %v", err)
	}
}

func TestCreateUnderRootRefusesSymlinks(t *testing.T) {
	ctx := context.Background()
	a := newHost(t, &FileRemote{Root: t.TempDir()}, nil)
	jail := t.TempDir()
	secret := filepath.Join(t.TempDir(), "host-secret")
	os.WriteFile(secret, []byte("host credentials"), 0o600)
	os.WriteFile(filepath.Join(jail, "rootfs.ext4"), []byte("disk"), 0o600)
	// What a compromised VMM could do inside its jail.
	os.Symlink(secret, filepath.Join(jail, "pause-mem"))
	os.Mkdir(filepath.Join(jail, "real"), 0o700)
	os.Symlink(filepath.Join(jail, "real"), filepath.Join(jail, "pause"))
	os.WriteFile(filepath.Join(jail, "real", "state.json"), []byte("{}"), 0o600)

	for _, name := range []string{"pause-mem", "pause/state.json", "../" + filepath.Base(secret)} {
		_, _, err := a.Create(ctx, CreateRequest{ID: "x", Kind: "pause", Root: jail,
			Files: map[string]string{"f": name}, Upload: "none"})
		if err == nil {
			t.Fatalf("exported %q through a symlink or outside the jail", name)
		}
	}
	if _, _, err := a.Create(ctx, CreateRequest{ID: "ok", Kind: "pause", Root: jail,
		Files: map[string]string{"rootfs.ext4": "rootfs.ext4"}, Upload: "none"}); err != nil {
		t.Fatalf("a regular file under root was refused: %v", err)
	}
}
