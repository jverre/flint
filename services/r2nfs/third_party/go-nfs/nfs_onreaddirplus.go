package nfs

import (
	"bytes"
	"context"
	"path"

	"github.com/willscott/go-nfs-client/nfs/xdr"
)

type readDirPlusArgs struct {
	Handle      []byte
	Cookie      uint64
	CookieVerif uint64
	DirCount    uint32
	MaxCount    uint32
}

type readDirPlusEntity struct {
	FileID     uint64
	Name       []byte
	Cookie     uint64
	Attributes *FileAttribute `xdr:"optional"`
	Handle     *[]byte        `xdr:"optional"`
	Next       bool
}

func joinPath(parent []string, elements ...string) []string {
	joinedPath := make([]string, 0, len(parent)+len(elements))
	joinedPath = append(joinedPath, parent...)
	joinedPath = append(joinedPath, elements...)
	return joinedPath
}

func onReadDirPlus(ctx context.Context, w *response, userHandle Handler) error {
	w.errorFmt = opAttrErrorFormatter
	obj := readDirPlusArgs{}
	if err := xdr.Read(w.req.Body, &obj); err != nil {
		return &NFSStatusError{NFSStatusInval, err}
	}

	// in case of test, nfs-client send:
	// DirCount = 512
	// MaxCount = 4096
	if obj.DirCount < 512 || obj.MaxCount < 4096 {
		return &NFSStatusError{NFSStatusTooSmall, nil}
	}

	fs, p, err := userHandle.FromHandle(obj.Handle)
	if err != nil {
		return &NFSStatusError{NFSStatusStale, err}
	}

	contents, dotdotCookie, verifier, err := getDirListing(fs, p, obj.Cookie)
	if err != nil {
		return err
	}

	entities := make([]readDirPlusEntity, 0)
	// Directory information (what a READDIR reply would carry), limited by
	// DirCount, and the whole reply (READDIRPLUS3resok) as encoded, limited
	// by MaxCount: status, directory attributes, verifier, the first
	// value-follows flag and eof, then per entry its fileid, name, cookie,
	// optional attributes and handle, and next value-follows flag.
	dirBytes := uint32(0)
	maxBytes := uint32(4 + 88 + 8 + 4 + 4)
	dirSize := func(name string) uint32 { return 8 + xdrOpaqueSize(len(name)) + 8 }
	entrySize := func(e readDirPlusEntity) uint32 {
		size := dirSize(string(e.Name)) + 4 + 4 + 4
		if e.Attributes != nil {
			size += 84
		}
		if e.Handle != nil {
			size += xdrOpaqueSize(len(*e.Handle))
		}
		return size
	}

	if obj.Cookie <= cookieDot {
		// add '.' and '..' to entities
		if obj.Cookie == 0 {
			dotFileID := uint64(0)
			da := tryStat(fs, p)
			if da != nil {
				dotFileID = da.Fileid
			}
			entities = append(entities, readDirPlusEntity{Name: []byte("."), Cookie: cookieDot, Next: true, FileID: dotFileID, Attributes: da})
		}
		dotdotFileID := uint64(0)
		if len(p) > 0 {
			if dda := tryStat(fs, p[0:len(p)-1]); dda != nil {
				dotdotFileID = dda.Fileid
			}
		}
		entities = append(entities, readDirPlusEntity{Name: []byte(".."), Cookie: dotdotCookie, Next: true, FileID: dotdotFileID})
		for _, e := range entities {
			dirBytes += dirSize(string(e.Name))
			maxBytes += entrySize(e)
		}
	}

	eof := true
	maxEntities := userHandle.HandleLimit() / 2
	for _, c := range contents {
		if dirBytes+dirSize(c.name) > obj.DirCount || len(entities) > maxEntities {
			eof = false
			break
		}
		info := c.stat(fs, p)
		if info == nil {
			continue // removed since the listing was read
		}
		filePath := joinPath(p, c.name)
		handle := userHandle.ToHandle(fs, filePath)
		attrs := ToFileAttribute(info, path.Join(filePath...))
		e := readDirPlusEntity{
			FileID:     attrs.Fileid,
			Name:       []byte(c.name),
			Cookie:     c.cookie,
			Attributes: attrs,
			Handle:     &handle,
			Next:       true,
		}
		if maxBytes+entrySize(e) > obj.MaxCount {
			eof = false
			break
		}
		dirBytes += dirSize(c.name)
		maxBytes += entrySize(e)
		entities = append(entities, e)
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
