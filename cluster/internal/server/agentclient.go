package server

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// AgentError is a non-2xx answer from a host agent.
type AgentError struct {
	Status int
	Detail string
}

func (e *AgentError) Error() string { return fmt.Sprintf("host agent: %d: %s", e.Status, e.Detail) }

// capacityStatus is what an agent answers when it has no room for another VM.
const capacityStatus = http.StatusInsufficientStorage

func isCapacityErr(err error) bool {
	ae, ok := err.(*AgentError)
	return ok && ae.Status == capacityStatus
}

func isAgentNotFound(err error) bool {
	ae, ok := err.(*AgentError)
	return ok && ae.Status == http.StatusNotFound
}

// agentDo sends a JSON request to a host agent and decodes the JSON answer.
func (s *Server) agentDo(ctx context.Context, h *Host, method, path string, in, out any, timeout time.Duration) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(h.AgentURL, "/")+path, body)
	if err != nil {
		return err
	}
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := s.agents.Do(req)
	if err != nil {
		return fmt.Errorf("host %s unreachable: %w", h.Name, err)
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if resp.StatusCode >= 300 {
		var e struct {
			Detail any `json:"detail"`
		}
		detail := strings.TrimSpace(string(data))
		if json.Unmarshal(data, &e) == nil && e.Detail != nil {
			detail = fmt.Sprint(e.Detail)
		}
		return &AgentError{Status: resp.StatusCode, Detail: detail}
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}

// snapshotRef tells an agent to restore from a snapshot, and which peer hosts
// may already have its chunks.
type snapshotRef struct {
	ID    string   `json:"id"`
	Peers []string `json:"peers,omitempty"`
}

type createOnHostReq struct {
	VMID             string          `json:"vm_id"`
	Tenant           string          `json:"tenant"`
	TemplateID       string          `json:"template_id"`
	AllowInternet    bool            `json:"allow_internet_access"`
	NetworkPolicy    json.RawMessage `json:"network_policy,omitempty"`
	Availability     string          `json:"availability"`
	Snapshot         *snapshotRef    `json:"snapshot,omitempty"`
	TemplateSnapshot *snapshotRef    `json:"template_snapshot,omitempty"`
	TimeoutSecs      float64         `json:"timeout_seconds,omitempty"`
	TimeoutPolicy    string          `json:"timeout_policy,omitempty"`
}

func (s *Server) createOnHost(ctx context.Context, h *Host, req createOnHostReq) (map[string]any, error) {
	q := url.Values{}
	q.Set("template_id", req.TemplateID)
	q.Set("allow_internet_access", fmt.Sprint(req.AllowInternet))
	q.Set("tenant", req.Tenant)
	var out struct {
		VM map[string]any `json:"vm"`
	}
	if err := s.agentDo(ctx, h, http.MethodPost, "/vms?"+q.Encode(), req, &out, 5*time.Minute); err != nil {
		return nil, err
	}
	return out.VM, nil
}

func (s *Server) deleteOnHost(ctx context.Context, h *Host, vmID string) error {
	err := s.agentDo(ctx, h, http.MethodDelete, "/vms/"+vmID, nil, nil, 60*time.Second)
	if isAgentNotFound(err) {
		return nil
	}
	return err
}

type snapResult struct {
	Snapshot struct {
		ID       string `json:"id"`
		Kind     string `json:"kind"`
		Bytes    int64  `json:"bytes"`
		Chunks   int    `json:"chunks"`
		Uploaded bool   `json:"uploaded"`
	} `json:"snapshot"`
	Timings map[string]float64 `json:"timings"`
}

type snapshotReq struct {
	SnapshotID string `json:"snapshot_id"`
	Kind       string `json:"kind,omitempty"`
	Parent     string `json:"parent,omitempty"`
	Upload     string `json:"upload"` // sync | async
}

// pauseOnHost pauses a VM and exports its pause snapshot to the object store.
func (s *Server) pauseOnHost(ctx context.Context, h *Host, vmID string, req snapshotReq) (*snapResult, error) {
	var out snapResult
	if err := s.agentDo(ctx, h, http.MethodPost, "/vms/"+vmID+"/pause", req, &out, 10*time.Minute); err != nil {
		return nil, err
	}
	return &out, nil
}

func (s *Server) resumeOnHost(ctx context.Context, h *Host, vmID string) (map[string]any, error) {
	var out struct {
		VM map[string]any `json:"vm"`
	}
	if err := s.agentDo(ctx, h, http.MethodPost, "/vms/"+vmID+"/resume", nil, &out, 5*time.Minute); err != nil {
		return nil, err
	}
	return out.VM, nil
}

// checkpointOnHost snapshots a running VM without stopping it (for fork and durable VMs).
func (s *Server) checkpointOnHost(ctx context.Context, h *Host, vmID string, req snapshotReq) (*snapResult, error) {
	var out snapResult
	if err := s.agentDo(ctx, h, http.MethodPost, "/vms/"+vmID+"/checkpoint", req, &out, 10*time.Minute); err != nil {
		return nil, err
	}
	return &out, nil
}

func (s *Server) getOnHost(ctx context.Context, h *Host, vmID string) (map[string]any, error) {
	var out struct {
		VM map[string]any `json:"vm"`
	}
	if err := s.agentDo(ctx, h, http.MethodGet, "/vms/"+vmID, nil, &out, 10*time.Second); err != nil {
		return nil, err
	}
	return out.VM, nil
}
