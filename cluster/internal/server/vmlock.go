package server

import (
	"context"
	"errors"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"
)

// Every operation that changes a VM (create, pause, resume, fork, checkpoint,
// migrate, recover, delete) holds that VM's lock for its whole duration. The
// lock is a lease row in the database, so it serialises operations across
// every replica, not just within one; a replica that dies mid-operation stops
// renewing it and the leader takes over its unfinished operation (see
// operations.go) once it expires.
const (
	vmLockTTL   = 10 * time.Second
	vmLockRenew = 3 * time.Second
	// vmLockWait is how long an API request waits for another operation on
	// the same VM before answering 409.
	vmLockWait = 15 * time.Second
)

var errVMBusy = &apiError{http.StatusConflict, "another operation on this VM is in progress; retry shortly"}

func vmLockName(id string) string { return "vm:" + id }

// vmLock is a held VM lock. Its context is cancelled if the lease is lost,
// so the operation stops instead of acting on a VM someone else now owns.
type vmLock struct {
	s      *Server
	name   string
	holder string
	ctx    context.Context
	cancel context.CancelFunc
	done   chan struct{}
}

// lockVM takes a VM's lock, waiting up to wait for it (0: try once). It
// returns errVMBusy if the VM stays locked.
func (s *Server) lockVM(ctx context.Context, id string, wait time.Duration) (*vmLock, error) {
	name := vmLockName(id)
	holder := s.cfg.ReplicaID + "/" + uuid.NewString()
	deadline := time.Now().Add(wait)
	delay := 50 * time.Millisecond
	for {
		now := nowMs()
		ok, err := s.db.TryLease(ctx, name, holder, now, now+vmLockTTL.Milliseconds())
		if err != nil {
			return nil, err
		}
		if ok {
			break
		}
		if !time.Now().Before(deadline) {
			return nil, errVMBusy
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(delay):
		}
		if delay < time.Second {
			delay *= 2
		}
	}
	// The operation outlives the request that started it: a client that
	// disconnects must not leave a VM half-moved. Only losing the lock stops it.
	lctx, cancel := context.WithCancel(context.WithoutCancel(ctx))
	l := &vmLock{s: s, name: name, holder: holder, ctx: lctx, cancel: cancel, done: make(chan struct{})}
	go l.renew()
	return l, nil
}

func (l *vmLock) renew() {
	t := time.NewTicker(vmLockRenew)
	defer t.Stop()
	renewed := time.Now()
	for {
		select {
		case <-l.done:
			return
		case <-t.C:
		}
		ok, err := l.s.db.RenewLease(l.ctx, l.name, l.holder, nowMs()+vmLockTTL.Milliseconds())
		switch {
		case err == nil && ok:
			renewed = time.Now()
		case err == nil:
			log.Printf("lost %s; stopping its operation", l.name)
			l.cancel()
			return
		default:
			log.Printf("renewing %s: %v", l.name, err)
			// Stop before it can expire: past that, another replica may take over.
			if time.Since(renewed) > vmLockTTL-vmLockRenew {
				log.Printf("can't renew %s in time; stopping its operation", l.name)
				l.cancel()
				return
			}
		}
	}
}

// Context is cancelled when the lock is lost.
func (l *vmLock) Context() context.Context { return l.ctx }

func (l *vmLock) Release() {
	close(l.done)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := l.s.db.ReleaseLease(ctx, l.name, l.holder); err != nil {
		log.Printf("releasing %s: %v", l.name, err)
	}
	l.cancel()
}

// busyVMs returns the VMs an operation holds. Heartbeats leave those alone:
// the operation owns their state until it finishes.
func (s *Server) busyVMs(ctx context.Context) (map[string]bool, error) {
	rows, err := s.db.Query(ctx, `SELECT name FROM leases WHERE name LIKE 'vm:%' AND expires_at >= ?`, nowMs())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	busy := map[string]bool{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		busy[strings.TrimPrefix(name, "vm:")] = true
	}
	return busy, rows.Err()
}

func isBusy(err error) bool { return errors.Is(err, errVMBusy) }
