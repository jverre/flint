# go-nfs, patched for Flint

[willscott/go-nfs](https://github.com/willscott/go-nfs) v0.0.4 (commit
`209a01f`, Apache-2.0), without its tests, examples and `helpers/memfs`.
`services/r2nfs/go.mod` points at this copy with a `replace`.

Flint's changes:

- **Readdir cookies survive changes to the directory** (`nfs_onreaddir.go`,
  `nfs_onreaddirplus.go`). An entry's position is a hash of its name, not
  its index in a sorted listing, and, as in Linux's own NFS server, its
  cookie is the position of the entry after it. A client resumes in the
  right place even when entries were removed meanwhile, and when the entry
  it just read was removed (`rm -r` removes as it lists) the entry before
  it, or `..`, carries the cookie it's looking for. Upstream's index cookies
  made `rm -r` of a directory larger than one reply skip entries and fail.
  Listings are read fresh for every reply (names only when the filesystem
  implements `DirNamesReader`, stat-ing just the entries replied with),
  replies are filled to their exact encoded size, and the cookie verifier
  never changes, so `NFS3ERR_BAD_COOKIE` is never returned.
- **WRITE tells the truth about stability, and COMMIT syncs**
  (`nfs_onwrite.go`, `nfs_oncommit.go`). Writes to files that can sync are
  answered `UNSTABLE` unless the client asked for a stable write (synced
  before answering). Upstream answered every write `FILE_SYNC` and made
  COMMIT a no-op, so a guest's `fsync` returned before the data left the
  host's page cache.
- **Handle lookups are O(path depth)** (`helpers/cachinghandler.go`).
  `FromHandle` refreshed a handle's ancestors by scanning the whole handle
  cache on every request, so bulk operations got slower with every file.
- `FSINFO` advertises 1 MiB directory reads instead of 8 KiB.

To take a newer upstream, copy it over this directory and reapply the above.
