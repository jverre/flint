package blockstore

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"

	"golang.org/x/sys/unix"
)

// PrepareDelete removes aliases left by a crash before metadata publication.
// Replaced generations remain named so the caller can wipe them with the image.
func PrepareDelete(dir string) error {
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

// WipeFile discards the data of the regular file at path, in place.
func WipeFile(path string) (int64, error) {
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
