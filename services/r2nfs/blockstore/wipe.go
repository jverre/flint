package blockstore

import (
	"errors"
	"fmt"
	"os"

	"golang.org/x/sys/unix"
)

// WipeFile discards a private regular file before its final unlink. Open
// descriptors continue to see zeros after the name is removed.
func WipeFile(path string) (int64, error) {
	return wipeFile(path, false)
}

// WipePrivateFile also wipes linked metadata in a private image directory.
// Retirement links belong to that same image; they are not shared chunks.
func WipePrivateFile(path string) (int64, error) {
	return wipeFile(path, true)
}

func wipeFile(path string, private bool) (int64, error) {
	f, err := os.OpenFile(path, os.O_RDWR|unix.O_NOFOLLOW|unix.O_NONBLOCK, 0)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	var st unix.Stat_t
	if err := unix.Fstat(int(f.Fd()), &st); err != nil {
		return 0, err
	}
	if st.Mode&unix.S_IFMT != unix.S_IFREG || (!private && st.Nlink != 1) {
		return 0, fmt.Errorf("refusing to wipe non-private regular file %s", path)
	}
	if st.Size == 0 {
		return 0, f.Sync()
	}
	held := st.Blocks * 512
	if unix.Fallocate(int(f.Fd()), unix.FALLOC_FL_PUNCH_HOLE|unix.FALLOC_FL_KEEP_SIZE, 0, st.Size) == nil {
		if _, err := unix.Seek(int(f.Fd()), 0, unix.SEEK_DATA); errors.Is(err, unix.ENXIO) {
			return held, f.Sync()
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
