package main

import (
	"context"
	"encoding/base64"
	"io"
	"os"
	"os/exec"
	"strconv"
	"sync"
	"syscall"

	"github.com/creack/pty"
)

// outputEvent is a single chunk of output from a process.
type outputEvent struct {
	Type string `json:"type"` // "stdout", "stderr", "exit"
	Data string `json:"data,omitempty"`
	Code *int   `json:"exit_code,omitempty"`
}

// multiplexedChannel fans out events from a single source to N consumers.
type multiplexedChannel struct {
	mu        sync.RWMutex
	consumers []chan outputEvent
	closed    bool
}

func newMultiplexedChannel() *multiplexedChannel {
	return &multiplexedChannel{}
}

func (mc *multiplexedChannel) fork() <-chan outputEvent {
	ch := make(chan outputEvent, 256)
	mc.mu.Lock()
	defer mc.mu.Unlock()
	if mc.closed {
		close(ch)
		return ch
	}
	mc.consumers = append(mc.consumers, ch)
	return ch
}

func (mc *multiplexedChannel) send(ev outputEvent) {
	mc.mu.RLock()
	defer mc.mu.RUnlock()
	for _, ch := range mc.consumers {
		select {
		case ch <- ev:
		default:
			// Drop if consumer is slow
		}
	}
}

func (mc *multiplexedChannel) close() {
	mc.mu.Lock()
	defer mc.mu.Unlock()
	mc.closed = true
	for _, ch := range mc.consumers {
		close(ch)
	}
	mc.consumers = nil
}

// processHandler manages a single process.
type processHandler struct {
	pid       int
	tag       string
	isPty     bool
	cmd       *exec.Cmd
	ptyFile   *os.File              // master side (PTY mode)
	stdinPipe io.WriteCloser        // pipe mode
	stdinMu   sync.Mutex            // guards stdinPipe writes
	data      *multiplexedChannel
	earlyCh   <-chan outputEvent     // pre-forked consumer (used by /exec)
	exitCode  *int
	cancel    context.CancelFunc
	done      chan struct{}          // closed when process exits
}

// startProcessRequest contains the parameters for starting a new process.
type startProcessRequest struct {
	Cmd  []string          `json:"cmd"`
	Env  map[string]string `json:"env,omitempty"`
	Cwd  string            `json:"cwd,omitempty"`
	Pty  bool              `json:"pty,omitempty"`
	Cols uint16            `json:"cols,omitempty"`
	Rows uint16            `json:"rows,omitempty"`
	Tag  string            `json:"tag,omitempty"`
}

// startProcess creates and starts a new process. If earlySubscribe is true,
// a consumer channel is forked before readers start (prevents race for fast commands).
// The channel is returned as the second value (nil if earlySubscribe is false).
func startProcess(req startProcessRequest) (*processHandler, error) {
	return startProcessWithOptions(req, false)
}

func startProcessSubscribed(req startProcessRequest) (*processHandler, <-chan outputEvent, error) {
	h, err := startProcessWithOptions(req, true)
	if err != nil {
		return nil, nil, err
	}
	return h, h.earlyCh, nil
}

func startProcessWithOptions(req startProcessRequest, earlySubscribe bool) (*processHandler, error) {
	ctx, cancel := context.WithCancel(context.Background())

	cmd := exec.CommandContext(ctx, req.Cmd[0], req.Cmd[1:]...)

	// Set environment
	cmd.Env = os.Environ()
	for k, v := range req.Env {
		cmd.Env = append(cmd.Env, k+"="+v)
	}
	if req.Cwd != "" {
		cmd.Dir = req.Cwd
	}

	h := &processHandler{
		tag:    req.Tag,
		isPty:  req.Pty,
		cmd:    cmd,
		data:   newMultiplexedChannel(),
		cancel: cancel,
		done:   make(chan struct{}),
	}

	// Pre-fork a consumer before readers start to avoid missing output from fast commands
	if earlySubscribe {
		h.earlyCh = h.data.fork()
	}

	var readersWg sync.WaitGroup

	if req.Pty {
		// PTY mode: creack/pty needs Setsid for controlling terminal
		cols := req.Cols
		if cols == 0 {
			cols = 120
		}
		rows := req.Rows
		if rows == 0 {
			rows = 40
		}
		ptmx, err := pty.StartWithSize(cmd, &pty.Winsize{
			Cols: cols,
			Rows: rows,
		})
		if err != nil {
			cancel()
			return nil, err
		}
		h.ptyFile = ptmx
		h.pid = cmd.Process.Pid

		// Read from PTY master
		readersWg.Add(1)
		go func() {
			defer readersWg.Done()
			h.readLoop("stdout", ptmx, 16384)
		}()
	} else {
		// Pipe mode: separate stdout/stderr — use Setpgid for process group
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
		stdout, err := cmd.StdoutPipe()
		if err != nil {
			cancel()
			return nil, err
		}
		stderr, err := cmd.StderrPipe()
		if err != nil {
			cancel()
			return nil, err
		}
		stdin, err := cmd.StdinPipe()
		if err != nil {
			cancel()
			return nil, err
		}
		h.stdinPipe = stdin

		if err := cmd.Start(); err != nil {
			cancel()
			return nil, err
		}
		h.pid = cmd.Process.Pid

		readersWg.Add(2)
		go func() {
			defer readersWg.Done()
			h.readLoop("stdout", stdout, 32768)
		}()
		go func() {
			defer readersWg.Done()
			h.readLoop("stderr", stderr, 32768)
		}()
	}

	// Wait goroutine: wait for readers to drain BEFORE sending exit event
	go func() {
		// First wait for all reader goroutines to finish (they read until EOF)
		readersWg.Wait()
		// Now wait for the process to fully exit
		err := cmd.Wait()
		code := 0
		if err != nil {
			if exitErr, ok := err.(*exec.ExitError); ok {
				code = exitErr.ExitCode()
			} else {
				code = -1
			}
		}
		h.exitCode = &code
		h.data.send(outputEvent{Type: "exit", Code: &code})
		h.data.close()
		if h.ptyFile != nil {
			h.ptyFile.Close()
		}
		close(h.done)
		// Auto-remove from manager after exit
		pm.remove(h.pid)
	}()

	// Set OOM score for the child
	go setOOMScore(h.pid)

	pm.add(h)
	return h, nil
}

func (h *processHandler) readLoop(stream string, r io.Reader, chunkSize int) {
	buf := make([]byte, chunkSize)
	for {
		n, err := r.Read(buf)
		if n > 0 {
			data := base64.StdEncoding.EncodeToString(buf[:n])
			h.data.send(outputEvent{Type: stream, Data: data})
		}
		if err != nil {
			return
		}
	}
}

func (h *processHandler) writeInput(data []byte) error {
	if h.isPty && h.ptyFile != nil {
		_, err := h.ptyFile.Write(data)
		return err
	}
	if h.stdinPipe != nil {
		h.stdinMu.Lock()
		defer h.stdinMu.Unlock()
		_, err := h.stdinPipe.Write(data)
		return err
	}
	return nil
}

func (h *processHandler) sendSignal(sig syscall.Signal) error {
	if h.cmd.Process == nil {
		return nil
	}
	return h.cmd.Process.Signal(sig)
}

func (h *processHandler) resize(cols, rows uint16) error {
	if !h.isPty || h.ptyFile == nil {
		return nil
	}
	return pty.Setsize(h.ptyFile, &pty.Winsize{Cols: cols, Rows: rows})
}

func (h *processHandler) toJSON() map[string]any {
	result := map[string]any{
		"pid":   h.pid,
		"tag":   h.tag,
		"pty":   h.isPty,
		"cmd":   h.cmd.Args,
		"exited": h.exitCode != nil,
	}
	if h.exitCode != nil {
		result["exit_code"] = *h.exitCode
	}
	return result
}

func setOOMScore(pid int) {
	path := "/proc/" + strconv.Itoa(pid) + "/oom_score_adj"
	f, err := os.OpenFile(path, os.O_WRONLY, 0)
	if err != nil {
		return
	}
	defer f.Close()
	f.WriteString("500")
}

// waitDone blocks until the process exits.
func (h *processHandler) waitDone() {
	<-h.done
}

