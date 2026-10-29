package model

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
)

// VMSpec is what the caller asked for: kept with the VM to recreate it
// elsewhere, and sent to hosts as part of a create request.
type VMSpec struct {
	AllowInternet bool            `json:"allow_internet_access"`
	NetworkPolicy json.RawMessage `json:"network_policy,omitempty"`
	TimeoutSecs   float64         `json:"timeout_seconds,omitempty"`
	TimeoutPolicy TimeoutPolicy   `json:"timeout_policy,omitempty"`
}

// VMParams is how an API request describes a VM: POST /vms and VM sets read
// it, PATCH /vms/{id} changes its timeout. Fields the request leaves out are
// zero, so each caller applies its own defaults (WithDefaults) before
// checking it (Validate) and turning it into a VMSpec.
type VMParams struct {
	TemplateID    string          `json:"template_id,omitempty"`
	Availability  Availability    `json:"availability,omitempty"`
	AllowInternet *bool           `json:"allow_internet_access,omitempty"`
	NetworkPolicy json.RawMessage `json:"network_policy,omitempty"`
	TimeoutSecs   *float64        `json:"timeout_seconds,omitempty"`
	TimeoutPolicy TimeoutPolicy   `json:"timeout_policy,omitempty"`
}

// WithDefaults fills in what the request left out; avail is the caller's
// default availability.
func (p *VMParams) WithDefaults(avail Availability) {
	if p.TemplateID == "" {
		p.TemplateID = DefaultTemplate
	}
	if p.Availability == "" {
		p.Availability = avail
	}
	if isNull(p.NetworkPolicy) {
		p.NetworkPolicy = nil
	}
	if p.TimeoutSecs != nil && p.TimeoutPolicy == "" {
		p.TimeoutPolicy = TimeoutKill
	}
}

// Validate checks the fields that are set.
func (p *VMParams) Validate() error {
	switch {
	case p.Availability != "" && !p.Availability.Valid():
		return errors.New("availability must be ephemeral, restartable or durable")
	case !isNull(p.NetworkPolicy) && !isObject(p.NetworkPolicy):
		return errors.New("network_policy must be a JSON object")
	case p.TimeoutSecs != nil && (*p.TimeoutSecs < 0 || math.IsNaN(*p.TimeoutSecs) || math.IsInf(*p.TimeoutSecs, 0)):
		return errors.New("timeout_seconds must be a number >= 0")
	case p.TimeoutPolicy != "" && !p.TimeoutPolicy.Valid():
		return errors.New("timeout_policy must be kill or pause")
	}
	return nil
}

// OnlyTimeout reports whether the request sets nothing but the timeout (all
// a running VM can change).
func (p *VMParams) OnlyTimeout() bool {
	return p.TemplateID == "" && p.Availability == "" && p.AllowInternet == nil && p.NetworkPolicy == nil
}

// Spec is the VMSpec the parameters ask for. Internet access defaults to on.
func (p *VMParams) Spec() VMSpec {
	s := VMSpec{AllowInternet: p.AllowInternet == nil || *p.AllowInternet, NetworkPolicy: p.NetworkPolicy, TimeoutPolicy: p.TimeoutPolicy}
	if p.TimeoutSecs != nil {
		s.TimeoutSecs = *p.TimeoutSecs
	}
	return s
}

func isNull(raw json.RawMessage) bool {
	t := bytes.TrimSpace(raw)
	return len(t) == 0 || string(t) == "null"
}

func isObject(raw json.RawMessage) bool {
	var m map[string]json.RawMessage
	return json.Unmarshal(raw, &m) == nil
}
