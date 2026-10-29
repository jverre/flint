// Package hostagent is the control plane's side of the contract with host
// agents (src/flint/daemon/server.py and src/flint/cluster/*.py): the calls
// it makes to them, and the reports they send it. Field names here are wire
// names shared with the Python side; change them on both sides together.
package hostagent

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/jverre/flint/cluster/internal/model"
)

// Client calls host agents over mTLS.
type Client struct {
	HTTP *http.Client
}

// Error is a non-2xx answer from a host agent.
type Error struct {
	Status int
	Detail string
}

func (e *Error) Error() string { return fmt.Sprintf("host agent: %d: %s", e.Status, e.Detail) }

// HTTPError: the agent's refusals (4xx) are the caller's to see; its
// failures are a bad gateway.
func (e *Error) HTTPError() (int, string) {
	if e.Status >= 400 && e.Status < 500 {
		return e.Status, e.Detail
	}
	return http.StatusBadGateway, e.Error()
}

// CapacityStatus is what an agent answers when it has no room for another VM.
const CapacityStatus = http.StatusInsufficientStorage

func statusIs(err error, status int) bool {
	var ae *Error
	return errors.As(err, &ae) && ae.Status == status
}

// IsCapacity reports whether the agent refused a VM for lack of room.
func IsCapacity(err error) bool { return statusIs(err, CapacityStatus) }

// IsNotFound reports whether the agent doesn't have what was asked for.
func IsNotFound(err error) bool { return statusIs(err, http.StatusNotFound) }

// IsConflict reports whether the agent refused because of the VM's state.
func IsConflict(err error) bool { return statusIs(err, http.StatusConflict) }

// Do sends a JSON request to a host agent and decodes the JSON answer.
func (c *Client) Do(ctx context.Context, h *model.Host, method, path string, in, out any, timeout time.Duration) error {
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
	resp, err := c.HTTP.Do(req)
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
		return &Error{Status: resp.StatusCode, Detail: detail}
	}
	if out != nil && len(data) > 0 {
		return json.Unmarshal(data, out)
	}
	return nil
}

// VMState is a VM's state as an agent reports it (the daemon's names).
type VMState string

const (
	VMStarted VMState = "Started"
	VMRunning VMState = "Running"
	VMPaused  VMState = "Paused"
	VMError   VMState = "Error"
	VMDead    VMState = "Dead"
	VMAbsent  VMState = "Absent" // live migration: no VMM for it on the host
)

// Up reports whether the VM is running.
func (s VMState) Up() bool { return s == VMStarted || s == VMRunning }

// Failed reports whether the VM's process died.
func (s VMState) Failed() bool { return s == VMError || s == VMDead }

// SnapshotRef tells an agent to restore from a snapshot, and which peer
// hosts may already have its chunks.
type SnapshotRef struct {
	ID    string   `json:"id"`
	Peers []string `json:"peers,omitempty"`
}

// CreateRequest is the body of POST /vms on an agent. The VM's spec rides
// along whole, so a spec field reaches hosts without touching this file.
type CreateRequest struct {
	model.VMSpec
	ReplaceSharedDisk bool               `json:"replace_shared_disk,omitempty"`
	VMID              string             `json:"vm_id"`
	Tenant            string             `json:"tenant"`
	TemplateID        string             `json:"template_id"`
	Availability      model.Availability `json:"availability"`
	Snapshot          *SnapshotRef       `json:"snapshot,omitempty"`
	TemplateSnapshot  *SnapshotRef       `json:"template_snapshot,omitempty"`
}

// NewCreateRequest is the request to create v as the catalog has it.
func NewCreateRequest(v *model.VM) CreateRequest {
	return CreateRequest{VMSpec: v.Spec, VMID: v.ID, Tenant: v.Tenant, TemplateID: v.TemplateID, Availability: v.Availability}
}

// Create creates (or restores) a VM and returns the agent's view of it.
func (c *Client) Create(ctx context.Context, h *model.Host, req CreateRequest) (map[string]any, error) {
	// The agent reads these from the query string (the daemon's public API).
	q := url.Values{}
	q.Set("template_id", req.TemplateID)
	q.Set("allow_internet_access", fmt.Sprint(req.AllowInternet))
	q.Set("tenant", req.Tenant)
	var out struct {
		VM map[string]any `json:"vm"`
	}
	if err := c.Do(ctx, h, http.MethodPost, "/vms?"+q.Encode(), req, &out, 5*time.Minute); err != nil {
		return nil, err
	}
	return out.VM, nil
}

// Delete removes a VM; one the agent doesn't have is already gone.
func (c *Client) Delete(ctx context.Context, h *model.Host, vmID string) error {
	err := c.Do(ctx, h, http.MethodDelete, "/vms/"+vmID, nil, nil, 60*time.Second)
	if IsNotFound(err) {
		return nil
	}
	return err
}

// Upload is when a snapshot reaches the object store.
type Upload string

const (
	UploadSync  Upload = "sync"  // before the agent answers
	UploadAsync Upload = "async" // in the background; peers fetch from the host meanwhile
)

type SnapshotRequest struct {
	SnapshotID string             `json:"snapshot_id"`
	Kind       model.SnapshotKind `json:"kind,omitempty"`
	Parent     string             `json:"parent,omitempty"`
	Upload     Upload             `json:"upload"`
}

type SnapshotResult struct {
	Snapshot struct {
		ID       string `json:"id"`
		Kind     string `json:"kind"`
		Bytes    int64  `json:"bytes"`
		Chunks   int    `json:"chunks"`
		Uploaded bool   `json:"uploaded"`
	} `json:"snapshot"`
	Timings map[string]float64 `json:"timings"`
}

// Pause pauses a VM and exports its pause snapshot to the object store.
func (c *Client) Pause(ctx context.Context, h *model.Host, vmID string, req SnapshotRequest) (*SnapshotResult, error) {
	var out SnapshotResult
	if err := c.Do(ctx, h, http.MethodPost, "/vms/"+vmID+"/pause", req, &out, 10*time.Minute); err != nil {
		return nil, err
	}
	return &out, nil
}

func (c *Client) Resume(ctx context.Context, h *model.Host, vmID string) (map[string]any, error) {
	var out struct {
		VM map[string]any `json:"vm"`
	}
	if err := c.Do(ctx, h, http.MethodPost, "/vms/"+vmID+"/resume", nil, &out, 5*time.Minute); err != nil {
		return nil, err
	}
	return out.VM, nil
}

// Checkpoint snapshots a running VM without stopping it (for fork and durable VMs).
func (c *Client) Checkpoint(ctx context.Context, h *model.Host, vmID string, req SnapshotRequest) (*SnapshotResult, error) {
	var out SnapshotResult
	if err := c.Do(ctx, h, http.MethodPost, "/vms/"+vmID+"/checkpoint", req, &out, 10*time.Minute); err != nil {
		return nil, err
	}
	return &out, nil
}

// Get fetches a VM's live view. It's an optional extra on top of the
// catalog, so a slow or hung host gets 2 seconds, not more.
func (c *Client) Get(ctx context.Context, h *model.Host, vmID string) (map[string]any, error) {
	var out struct {
		VM map[string]any `json:"vm"`
	}
	if err := c.Do(ctx, h, http.MethodGet, "/vms/"+vmID, nil, &out, 2*time.Second); err != nil {
		return nil, err
	}
	return out.VM, nil
}

// Probe asks a host for a VM's state: "" if the host doesn't have it.
func (c *Client) Probe(ctx context.Context, h *model.Host, vmID string) (VMState, error) {
	info, err := c.Get(ctx, h, vmID)
	if IsNotFound(err) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	st, _ := info["state"].(string)
	return VMState(st), nil
}

// SetTimeout applies a VM's timeout on its host.
func (c *Client) SetTimeout(ctx context.Context, h *model.Host, vmID string, secs float64, policy model.TimeoutPolicy) error {
	body := map[string]any{"timeout_seconds": secs, "timeout_policy": policy}
	return c.Do(ctx, h, http.MethodPatch, "/vms/"+vmID, body, nil, 30*time.Second)
}

// PutNetworkPolicy applies a VM's network policy on its host.
func (c *Client) PutNetworkPolicy(ctx context.Context, h *model.Host, vmID string, policy json.RawMessage) error {
	return c.Do(ctx, h, http.MethodPut, "/vms/"+vmID+"/network-policy", policy, nil, 30*time.Second)
}

// BuildTemplate starts a template build on a host.
func (c *Client) BuildTemplate(ctx context.Context, h *model.Host, name, dockerfile string, rootfsMB int) error {
	body := map[string]any{"name": name, "dockerfile": dockerfile, "rootfs_size_mb": rootfsMB}
	return c.Do(ctx, h, http.MethodPost, "/templates/build", body, nil, 60*time.Second)
}

func (c *Client) DeleteTemplate(ctx context.Context, h *model.Host, id string) error {
	return c.Do(ctx, h, http.MethodDelete, "/templates/"+id, nil, nil, 60*time.Second)
}
