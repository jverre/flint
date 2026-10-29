package blockstore

// Deleting an image wipes what it held: its overlay (the writes since its
// last snapshot) and, through garbage collection, every chunk and layer in
// the local store that no other image references any more. Wiping punches a
// hole over the whole file, so the filesystem frees (and, mounted with
// discard, trims) its blocks, with zeros written instead where that isn't
// supported. Objects already pushed to a remote store are not removed from
// it.

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/unix"
)

// Delete wipes every file of the image in dir, then removes dir. It returns
// the bytes wiped. Deleting an image that is already gone is a no-op, so a
// deletion can be retried after garbage collection failed. The image must
// not be open.
func Delete(dir string) (int64, error) {
	if _, err := os.Lstat(dir); errors.Is(err, fs.ErrNotExist) {
		return 0, nil
	} else if err != nil {
		return 0, err
	}
	if err := dropCrashAliases(dir); err != nil {
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

// dropCrashAliases removes the second names a crash during metadata
// publication can leave for a live file (see writeFileAtomic): wipeFile
// refuses files with more than one link. Replaced generations keep their
// own name, so Delete wipes them with the image.
func dropCrashAliases(dir string) error {
	for _, name := range []string{manifestFile, bitmapFile} {
		current, err := os.Stat(filepath.Join(dir, name))
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return err
		}
		retired := filepath.Join(dir, name+".retired")
		old, oldErr := os.Stat(retired)
		if oldErr != nil && !errors.Is(oldErr, os.ErrNotExist) {
			return oldErr
		}
		if err == nil && oldErr == nil && os.SameFile(current, old) {
			if err := os.Remove(retired); err != nil {
				return err
			}
		}
	}
	return nil
}

// Collect wipes and removes every object in the store that isn't in keep
// (the References of every image still in use). It must not run while an
// image is being created or snapshotted, as their new objects aren't
// referenced yet.
func (s *LocalStore) Collect(keep map[string]bool) (removed int, freed int64, err error) {
	for _, kind := range objectKinds {
		dir := filepath.Join(s.Dir, kind)
		objs, err := os.ReadDir(dir)
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			return removed, freed, err
		}
		for _, o := range objs {
			if keep[kind+"/"+o.Name()] || strings.HasPrefix(o.Name(), tempPrefix) || !o.Type().IsRegular() {
				continue
			}
			path := filepath.Join(dir, o.Name())
			n, err := wipeFile(path)
			if err != nil {
				return removed, freed, err
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

// wipeFile discards the data of the regular file at path, in place, and
// returns the bytes it held.
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
	if st.Mode&unix.S_IFMT != unix.S_IFREG || st.Nlink != 1 {
		return 0, fmt.Errorf("refusing to wipe non-private regular file %s", path)
	}
	if st.Size == 0 {
		return 0, f.Sync()
	}
	held := st.Blocks * 512
	if unix.Fallocate(int(f.Fd()), unix.FALLOC_FL_PUNCH_HOLE|unix.FALLOC_FL_KEEP_SIZE, 0, st.Size) == nil {
		if _, err := unix.Seek(int(f.Fd()), 0, unix.SEEK_DATA); errors.Is(err, unix.ENXIO) {
			return held, f.Sync() // durability is required on the discard fast path too
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
