package main

import (
	"fmt"
	"sync"
)

// processManager tracks all running processes.
type processManager struct {
	mu        sync.RWMutex
	processes map[int]*processHandler
}

var pm = &processManager{
	processes: make(map[int]*processHandler),
}

func (m *processManager) add(h *processHandler) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.processes[h.pid] = h
}

func (m *processManager) get(pid int) *processHandler {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return m.processes[pid]
}

func (m *processManager) remove(pid int) {
	m.mu.Lock()
	defer m.mu.Unlock()
	delete(m.processes, pid)
}

func (m *processManager) list() []*processHandler {
	m.mu.RLock()
	defer m.mu.RUnlock()
	result := make([]*processHandler, 0, len(m.processes))
	for _, h := range m.processes {
		result = append(result, h)
	}
	return result
}

func (m *processManager) count() int {
	m.mu.RLock()
	defer m.mu.RUnlock()
	return len(m.processes)
}

func requireProcess(pid int) (*processHandler, error) {
	h := pm.get(pid)
	if h == nil {
		return nil, fmt.Errorf("process %d not found", pid)
	}
	return h, nil
}
