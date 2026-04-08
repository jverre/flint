package main

import (
	"net"

	"github.com/go-git/go-billy/v5"
	nfs "github.com/willscott/go-nfs"
	nfshelper "github.com/willscott/go-nfs/helpers"
)

// nfsHandler wraps ExportManager to provide per-client overlay filesystems.
type nfsHandler struct {
	exports *ExportManager
}

func newNFSHandler(exports *ExportManager) nfs.Handler {
	return &nfsHandler{exports: exports}
}

// Mount returns the billy.Filesystem for the requesting client.
// Each client gets its own overlay (sandbox layer + template layer).
func (h *nfsHandler) Mount(conn net.Conn, req nfs.MountRequest) (nfs.MountStatus, billy.Filesystem, []nfs.AuthFlavor) {
	clientIP := extractIP(conn.RemoteAddr())

	export := h.exports.Lookup(clientIP)
	if export == nil {
		return nfs.MountStatusNoEnt, nil, nil
	}

	fs := newOverlayFS(h.exports.r2, h.exports.cache, export.VMID, export.TemplateID)
	return nfs.MountStatusOk, fs, []nfs.AuthFlavor{nfs.AuthFlavorNull}
}

// Change returns a handler that tracks filesystem changes for NFS.
func (h *nfsHandler) Change(fs billy.Filesystem) billy.Change {
	return nfshelper.NewCachingHandler(fs, 1024)
}

// FSStat returns filesystem statistics.
func (h *nfsHandler) FSStat(conn net.Conn, fs billy.Filesystem) nfs.FSStat {
	return nfs.FSStat{
		TotalSize: 1 << 40, // 1 TB virtual
		FreeSize:  1 << 40,
		AvailSize: 1 << 40,
	}
}

// ToHandle converts a path to an NFS file handle.
func (h *nfsHandler) ToHandle(fs billy.Filesystem, path []string) []byte {
	return nfshelper.ToFileHandle(fs, path)
}

// FromHandle converts an NFS file handle back to a path.
func (h *nfsHandler) FromHandle(fh []byte) (billy.Filesystem, []string, error) {
	return nfshelper.HandleFromFileHandle(fh)
}

// HandleLimit returns the max number of file handles to track.
func (h *nfsHandler) HandleLimit() int {
	return 1024
}

func extractIP(addr net.Addr) string {
	switch a := addr.(type) {
	case *net.TCPAddr:
		return a.IP.String()
	case *net.UDPAddr:
		return a.IP.String()
	}
	host, _, _ := net.SplitHostPort(addr.String())
	return host
}
