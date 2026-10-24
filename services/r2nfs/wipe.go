package main

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/jverre/flint/services/r2nfs/blockstore"
	"golang.org/x/sys/unix"
)

// Deleting an image volume wipes what it held: its overlay (the writes since
// its last snapshot) and every chunk and layer in the local store that no
// other image references any more. Wiping punches a hole over the whole
// file, so the filesystem frees (and, mounted with discard, trims) its
// blocks, with zeros written instead where that isn't supported.
// Objects already pushed to a remote store are not removed from it.

// wipeFile discards the data of the regular file at path, in place.
func wipeFile(path string) (int64, error) {
	f, err := os.OpenFile(path, os.O_RDWR|unix.O_NOFOLLOW, 0)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	var st unix.Stat_t
	if err := unix.Fstat(int(f.Fd()), &st); err != nil {
		return 0, err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG || st.Nlink != 1 || st.Size == 0 {
		return 0, nil
	}
	held := st.Blocks * 512
	if unix.Fallocate(int(f.Fd()), unix.FALLOC_FL_PUNCH_HOLE|unix.FALLOC_FL_KEEP_SIZE, 0, st.Size) == nil {
		if _, err := unix.Seek(int(f.Fd()), 0, unix.SEEK_DATA); errors.Is(err, unix.ENXIO) {
			return held, nil // nothing left but holes
		}
	}
	zeros := make([]byte, 1<<20)
	for off := int64(0); off < st.Size; off += int64(len(zeros)) {
		n := min(int64(len(zeros)), st.Size-off)
		if _, err := f.WriteAt(zeros[:n], off); err != nil {
			return held, err
		}
	}
	return held, f.Sync()
}

// wipeAll wipes every file under dir, then removes it.
func wipeAll(dir string) (int64, error) {
	if _, err := os.Lstat(dir); errors.Is(err, fs.ErrNotExist) {
		return 0, nil // retry after the overlay was removed but collection failed
	} else if err != nil {
		return 0, err
	}
	var total int64
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.Type().IsRegular() {
			return nil
		}
		n, werr := wipeFile(path)
		total += n
		return werr
	})
	if err == nil {
		err = os.RemoveAll(dir)
	}
	return total, err
}

// collectGarbage wipes and removes the objects in the local store at
// storeDir that no image under imagesDir references, nor any of extra (the
// manifests of images open in memory). It keeps everything if it can't
// read every image's references. It must not run while an image is being
// created or snapshotted.
func collectGarbage(imagesDir string, store blockstore.Store, storeDir string, extra []blockstore.Manifest) (removed int, freed int64, err error) {
	keep := map[string]bool{}
	add := func(m blockstore.Manifest) error {
		refs, err := blockstore.References(store, m)
		for k := range refs {
			keep[k] = true
		}
		return err
	}
	entries, err := os.ReadDir(imagesDir)
	if err != nil {
		return 0, 0, err
	}
	for _, e := range entries {
		if !e.IsDir() || filepath.Join(imagesDir, e.Name()) == filepath.Clean(storeDir) {
			continue
		}
		m, err := blockstore.ReadManifest(filepath.Join(imagesDir, e.Name()))
		if errors.Is(err, fs.ErrNotExist) {
			continue // not an image
		}
		if err != nil {
			return 0, 0, err
		}
		if err := add(m); err != nil {
			return 0, 0, err
		}
	}
	for _, m := range extra {
		if err := add(m); err != nil {
			return 0, 0, err
		}
	}
	for _, kind := range []string{"chunks", "layers"} {
		dir := filepath.Join(storeDir, kind)
		objs, err := os.ReadDir(dir)
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return removed, freed, err
		}
		for _, o := range objs {
			key := kind + "/" + o.Name()
			if keep[key] || strings.HasPrefix(o.Name(), ".put-") || !o.Type().IsRegular() {
				continue
			}
			path := filepath.Join(dir, o.Name())
			n, werr := wipeFile(path)
			if werr != nil {
				return removed, freed, werr
			}
			if err := os.Remove(path); err != nil {
				return removed, freed, err
			}
			removed++
			freed += n
		}
	}
	return removed, freed, nil
}
