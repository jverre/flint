package engine

import (
	"context"
	"errors"
	"log"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jverre/flint/cluster/internal/httpx"
	"github.com/jverre/flint/cluster/internal/model"
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
	// LockWait is how long an API request waits for another operation on
	// the same VM before answering 409.
	LockWait = 15 * time.Second
)

// placementLock serialises placement decisions (and anything that changes
// what placement sees: drains, handovers) across replicas.
const placementLock = "_placement"

// ErrVMBusy: another operation holds the VM.
var ErrVMBusy = httpx.Conflict("another operation on this VM is in progress; retry shortly")

func IsBusy(err error) bool { return errors.Is(err, ErrVMBusy) }

const vmLockPrefix = "vm:"

func vmLockName(id string) string { return vmLockPrefix + id }

// Lock is a held VM lock. Its context is cancelled if the lease is lost, so
// the operation stops instead of acting on a VM someone else now owns.
type Lock struct {
	e      *Engine
	name   string
	holder string
	ctx    context.Context
	cancel context.CancelFunc
	done   chan struct{}
}

// lockVM takes a VM's lock, waiting up to wait for it (0: try once). It
// returns ErrVMBusy if the VM stays locked.
func (e *Engine) lockVM(ctx context.Context, id string, wait time.Duration) (*Lock, error) {
	name := vmLockName(id)
	holder := e.cfg.ReplicaID + "/" + uuid.NewString()
	deadline := time.Now().Add(wait)
	delay := 50 * time.Millisecond
	for {
		now := model.NowMs()
		ok, err := e.store.TryLease(ctx, name, holder, now, now+vmLockTTL.Milliseconds())
		if err != nil {
			return nil, err
		}
		if ok {
			break
		}
		if !time.Now().Before(deadline) {
			return nil, ErrVMBusy
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
	l := &Lock{e: e, name: name, holder: holder, ctx: lctx, cancel: cancel, done: make(chan struct{})}
	go l.renew()
	return l, nil
}

// LockPlacement takes the cluster-wide placement lock (waiting up to LockWait).
func (e *Engine) LockPlacement(ctx context.Context) (*Lock, error) {
	return e.lockVM(ctx, placementLock, LockWait)
}

func (l *Lock) renew() {
	t := time.NewTicker(vmLockRenew)
	defer t.Stop()
	renewed := time.Now()
	for {
		select {
		case <-l.done:
			return
		case <-t.C:
		}
		ok, err := l.e.store.RenewLease(l.ctx, l.name, l.holder, model.NowMs()+vmLockTTL.Milliseconds())
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
func (l *Lock) Context() context.Context { return l.ctx }

func (l *Lock) Release() {
	close(l.done)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := l.e.store.ReleaseLease(ctx, l.name, l.holder); err != nil {
		log.Printf("releasing %s: %v", l.name, err)
	}
	l.cancel()
}

// BusyVMs returns the VMs an operation holds. Heartbeats leave those alone:
// the operation owns their state until it finishes.
func (e *Engine) BusyVMs(ctx context.Context) (map[string]bool, error) {
	names, err := e.store.HeldLeases(ctx, vmLockPrefix, model.NowMs())
	if err != nil {
		return nil, err
	}
	busy := map[string]bool{}
	for _, name := range names {
		busy[strings.TrimPrefix(name, vmLockPrefix)] = true
	}
	return busy, nil
}
