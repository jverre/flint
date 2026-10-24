package server

import (
	"encoding/json"
	"regexp"
)

// Network policy header values are credentials. Hosts return them redacted
// (first and last four characters), so a client that reads a policy, edits it
// and writes it back sends the values it didn't change in redacted form.
// keepUnchanged swaps those back for the stored values, the way the host
// does, so the spec a recovered VM is recreated from keeps the credentials.

var redactRE = regexp.MustCompile(`(?s)^(.{4}).+(.{4})$`)

func redact(v string) string {
	if m := redactRE.FindStringSubmatch(v); m != nil {
		return m[1] + "***" + m[2]
	}
	return "***"
}

type policyDoc struct {
	Allow map[string][]map[string]json.RawMessage `json:"allow"`
}

type transform struct {
	Headers map[string]string `json:"headers"`
}

// headerValues is domain -> header -> value, later transforms winning.
func headerValues(raw json.RawMessage) map[string]map[string]string {
	out := map[string]map[string]string{}
	var p policyDoc
	if json.Unmarshal(raw, &p) != nil {
		return out
	}
	for domain, rules := range p.Allow {
		for _, rule := range rules {
			var ts []transform
			if json.Unmarshal(rule["transform"], &ts) != nil {
				continue
			}
			for _, t := range ts {
				for k, v := range t.Headers {
					if out[domain] == nil {
						out[domain] = map[string]string{}
					}
					out[domain][k] = v
				}
			}
		}
	}
	return out
}

// keepUnchanged returns next with every header value that is the redacted
// form of the current value of the same header on the same domain replaced
// by that value. Anything it can't parse is returned as it came.
func keepUnchanged(next, current json.RawMessage) json.RawMessage {
	old := headerValues(current)
	if len(old) == 0 {
		return next
	}
	var doc map[string]any
	if json.Unmarshal(next, &doc) != nil {
		return next
	}
	allow, _ := doc["allow"].(map[string]any)
	for domain, rules := range allow {
		rs, _ := rules.([]any)
		for _, rule := range rs {
			r, _ := rule.(map[string]any)
			ts, _ := r["transform"].([]any)
			for _, t := range ts {
				tm, _ := t.(map[string]any)
				hs, _ := tm["headers"].(map[string]any)
				for k, v := range hs {
					s, _ := v.(string)
					if prev, ok := old[domain][k]; ok && s == redact(prev) {
						hs[k] = prev
					}
				}
			}
		}
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return next
	}
	return out
}
