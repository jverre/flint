package nfs

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"hash/fnv"
	"io"
	"io/fs"
	"os"
	"path"
	"sort"

	"github.com/go-git/go-billy/v5"
	"github.com/willscott/go-nfs-client/nfs/xdr"
)

type readDirArgs struct {
	Handle      []byte
	Cookie      uint64
	CookieVerif uint64
	Count       uint32
}

type readDirEntity struct {
	FileID uint64
	Name   []byte
	Cookie uint64
	Next   bool
}

func onReadDir(ctx context.Context, w *response, userHandle Handler) error {
	w.errorFmt = opAttrErrorFormatter
	obj := readDirArgs{}
	err := xdr.Read(w.req.Body, &obj)
	if err != nil {
		return &NFSStatusError{NFSStatusInval, err}
	}

	if obj.Count < 1024 {
		return &NFSStatusError{NFSStatusTooSmall, io.ErrShortBuffer}
	}

	fs, p, err := userHandle.FromHandle(obj.Handle)
	if err != nil {
		return &NFSStatusError{NFSStatusStale, err}
	}

	contents, dotdotCookie, verifier, err := getDirListing(fs, p, obj.Cookie)
	if err != nil {
		return err
	}

	entities := make([]readDirEntity, 0)
	// Reply size (READDIR3resok) as encoded: status, directory attributes,
	// verifier, the first value-follows flag and eof, then per entry its
	// fileid, name, cookie and next value-follows flag.
	maxBytes := uint32(4 + 88 + 8 + 4 + 4)
	entrySize := func(name string) uint32 { return 8 + xdrOpaqueSize(len(name)) + 8 + 4 }

	if obj.Cookie <= cookieDot {
		// add '.' and '..' to entities
		if obj.Cookie == 0 {
			dotFileID := uint64(0)
			if da := tryStat(fs, p); da != nil {
				dotFileID = da.Fileid
			}
			entities = append(entities, readDirEntity{Name: []byte("."), Cookie: cookieDot, Next: true, FileID: dotFileID})
			maxBytes += entrySize(".")
		}
		dotdotFileID := uint64(0)
		if len(p) > 0 {
			if dda := tryStat(fs, p[0:len(p)-1]); dda != nil {
				dotdotFileID = dda.Fileid
			}
		}
		entities = append(entities, readDirEntity{Name: []byte(".."), Cookie: dotdotCookie, Next: true, FileID: dotdotFileID})
		maxBytes += entrySize("..")
	}

	eof := true
	maxEntities := userHandle.HandleLimit() / 2
	for _, c := range contents {
		size := entrySize(c.name)
		if maxBytes+size > obj.Count || len(entities) > maxEntities {
			eof = false
			break
		}
		info := c.stat(fs, p)
		if info == nil {
			continue // removed since the listing was read
		}
		maxBytes += size
		attrs := ToFileAttribute(info, path.Join(append(p, c.name)...))
		entities = append(entities, readDirEntity{
			FileID: attrs.Fileid,
			Name:   []byte(c.name),
			Cookie: c.cookie,
			Next:   true,
		})
	}

	writer := bytes.NewBuffer([]byte{})
	if err := xdr.Write(writer, uint32(NFSStatusOk)); err != nil {
		return &NFSStatusError{NFSStatusServerFault, err}
	}
	if err := WritePostOpAttrs(writer, tryStat(fs, p)); err != nil {
		return &NFSStatusError{NFSStatusServerFault, err}
	}

	if err := xdr.Write(writer, verifier); err != nil {
		return &NFSStatusError{NFSStatusServerFault, err}
	}

	if err := xdr.Write(writer, len(entities) > 0); err != nil { // next
		return &NFSStatusError{NFSStatusServerFault, err}
	}
	if len(entities) > 0 {
		entities[len(entities)-1].Next = false
		// no next for last entity

		for _, e := range entities {
			if err := xdr.Write(writer, e); err != nil {
				return &NFSStatusError{NFSStatusServerFault, err}
			}
		}
	}
	if err := xdr.Write(writer, eof); err != nil {
		return &NFSStatusError{NFSStatusServerFault, err}
	}
	// TODO: track writer size at this point to validate maxcount estimation and stop early if needed.

	if err := w.Write(writer.Bytes()); err != nil {
		return &NFSStatusError{NFSStatusServerFault, err}
	}
	return nil
}

// DirNamesReader is implemented by filesystems that can list a directory's
// names without stat-ing every entry. Readdir then stats only the entries
// that go into a reply.
type DirNamesReader interface {
	ReadDirNames(path string) ([]string, error)
}

// dirEntry is an entry of a directory listing; info is nil until stat-ed.
type dirEntry struct {
	pos    uint64 // readDirPos(name)
	cookie uint64 // where to resume after it: the next entry's pos
	name   string
	info   fs.FileInfo
}

// stat returns the entry's attributes, or nil if it no longer exists.
func (e *dirEntry) stat(fs billy.Filesystem, dir []string) fs.FileInfo {
	if e.info == nil {
		if info, err := fs.Lstat(fs.Join(append(dir, e.name)...)); err == nil {
			e.info = info
		}
	}
	return e.info
}

// Cookies: 0 starts a listing, cookieDot resumes after '.', cookieEnd is
// after the last entry, and any other cookie resumes at the first entry
// whose position (a hash of its name, readDirPos) is at least the cookie.
//
// Positions don't depend on the rest of the directory, so a client resumes
// at the right place even if entries were added or removed meanwhile. And
// like Linux's own server, an entry's cookie is the position of the entry
// after it: when the entry a client just read is removed (`rm -r` removes
// as it lists), the entry before it, or '..', now carries that cookie, so
// the client finds where it was in a fresh listing instead of searching
// the whole directory for a cookie that no longer exists.
const (
	cookieDot = 1
	cookieEnd = ^uint64(0)
)

// getDirListing reads directory p fresh and returns its entries from cookie
// on, in position order, the cookie of '..' (where its first entry is), and
// the directory's cookie verifier, which never changes because cookies
// never go stale.
func getDirListing(fs billy.Filesystem, p []string, cookie uint64) ([]dirEntry, uint64, uint64, error) {
	path := fs.Join(p...)
	var entries []dirEntry
	var err error
	if nr, ok := fs.(DirNamesReader); ok {
		var names []string
		if names, err = nr.ReadDirNames(path); err == nil {
			entries = make([]dirEntry, len(names))
			for i, name := range names {
				entries[i] = dirEntry{pos: readDirPos(name), name: name}
			}
		}
	} else {
		var infos []os.FileInfo
		if infos, err = fs.ReadDir(path); err == nil {
			entries = make([]dirEntry, len(infos))
			for i, info := range infos {
				entries[i] = dirEntry{pos: readDirPos(info.Name()), name: info.Name(), info: info}
			}
		}
	}
	if err != nil {
		if os.IsPermission(err) {
			return nil, 0, 0, &NFSStatusError{NFSStatusAccess, err}
		}
		return nil, 0, 0, &NFSStatusError{NFSStatusNotDir, err}
	}

	sort.Slice(entries, func(i, j int) bool {
		if entries[i].pos != entries[j].pos {
			return entries[i].pos < entries[j].pos
		}
		return entries[i].name < entries[j].name
	})
	dotdotCookie := uint64(cookieEnd)
	if len(entries) > 0 {
		dotdotCookie = entries[0].pos
	}
	for i := range entries {
		entries[i].cookie = cookieEnd
		if i+1 < len(entries) {
			entries[i].cookie = entries[i+1].pos
		}
	}
	if cookie > cookieDot {
		entries = entries[sort.Search(len(entries), func(i int) bool { return entries[i].pos >= cookie }):]
	}
	return entries, dotdotCookie, hashPathAndContents(path, nil), nil
}

// readDirPos is the position of the directory entry called name: a 64-bit
// hash of its name, clear of the special cookies.
func readDirPos(name string) uint64 {
	h := fnv.New64a()
	h.Write([]byte(name))
	pos := h.Sum64()
	if pos <= cookieDot {
		return cookieDot + 1
	}
	if pos == cookieEnd {
		return cookieEnd - 1
	}
	return pos
}

// xdrOpaqueSize is the encoded size of variable-length opaque data or a
// string of n bytes: its length, then the bytes padded to 4.
func xdrOpaqueSize(n int) uint32 {
	return 4 + uint32(n+3)&^3
}

func hashPathAndContents(path string, contents []fs.FileInfo) uint64 {
	//calculate a cookie-verifier.
	vHash := sha256.New()

	// Add the path to avoid collisions of directories with the same content
	vHash.Write([]byte(path))

	for _, c := range contents {
		vHash.Write([]byte(c.Name())) // Never fails according to the docs
	}

	verify := vHash.Sum(nil)[0:8]
	return binary.BigEndian.Uint64(verify)
}
