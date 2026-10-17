package main

import (
	"context"
	"net"
	"strings"

	"github.com/go-git/go-billy/v5"
	"github.com/go-git/go-billy/v5/memfs"
	nfs "github.com/willscott/go-nfs"
)

// nfsHandler serves two kinds of export: the client's R2 workspace overlay,
// and volume mounts ("/<mount id>").
type nfsHandler struct {
	exports *ExportManager
	volumes *VolumeManager
}

func newNFSHandler(exports *ExportManager, volumes *VolumeManager) nfs.Handler {
	return &nfsHandler{exports: exports, volumes: volumes}
}

// Mount returns the billy.Filesystem for the requesting client.
// A path naming a volume mount registered for this client gets that volume;
// anything else gets the client's workspace overlay (sandbox + template layer).
func (h *nfsHandler) Mount(ctx context.Context, conn net.Conn, req nfs.MountRequest) (nfs.MountStatus, billy.Filesystem, []nfs.AuthFlavor) {
	clientIP := extractIP(conn.RemoteAddr())

	if mount := h.volumes.Lookup(strings.Trim(string(req.Dirpath), "/"), clientIP); mount != nil {
		return nfs.MountStatusOk, newVolumeFS(mount), []nfs.AuthFlavor{nfs.AuthFlavorNull}
	}

	if h.exports.r2 == nil {
		return nfs.MountStatusErrNoEnt, refusedFS, nil
	}
	export := h.exports.Lookup(clientIP)
	if export == nil {
		return nfs.MountStatusErrNoEnt, refusedFS, nil
	}

	fs := newOverlayFS(h.exports.r2, h.exports.cache, export.VMID, export.TemplateID)
	return nfs.MountStatusOk, fs, []nfs.AuthFlavor{nfs.AuthFlavorNull}
}

// refusedFS is returned with refused mounts: go-nfs makes a handle for the
// filesystem even then (and crashes on nil), but never sends it.
var refusedFS = memfs.New()

// Change returns a billy.Change for tracking filesystem mutations.
func (h *nfsHandler) Change(fs billy.Filesystem) billy.Change {
	if c, ok := fs.(billy.Change); ok {
		return c
	}
	return nil
}

// FSStat fills in filesystem statistics.
func (h *nfsHandler) FSStat(ctx context.Context, fs billy.Filesystem, stat *nfs.FSStat) error {
	stat.TotalSize = 1 << 40 // 1 TB virtual
	stat.FreeSize = 1 << 40
	stat.AvailableSize = 1 << 40
	return nil
}

// ToHandle is a no-op — handled by the CachingHandler wrapper.
func (h *nfsHandler) ToHandle(fs billy.Filesystem, path []string) []byte {
	return []byte{}
}

// FromHandle is a no-op — handled by the CachingHandler wrapper.
func (h *nfsHandler) FromHandle(fh []byte) (billy.Filesystem, []string, error) {
	return nil, nil, nil
}

// InvalidateHandle is called on rename/delete.
func (h *nfsHandler) InvalidateHandle(fs billy.Filesystem, fh []byte) error {
	return nil
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
