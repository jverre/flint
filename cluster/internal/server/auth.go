package server

import (
	"errors"
	"log"
	"net/http"

	"github.com/jverre/flint/cluster/internal/auth"
	"github.com/jverre/flint/cluster/internal/httpx"
)

// requireAuth wraps API handlers; admin=true restricts to admin tokens.
func (s *Server) requireAuth(admin bool, h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p, err := s.auth.Authenticate(r)
		switch {
		case errors.Is(err, auth.ErrBadToken):
			httpx.WriteDetail(w, http.StatusUnauthorized, err.Error())
			return
		case errors.Is(err, auth.ErrCrossOrigin):
			httpx.WriteDetail(w, http.StatusForbidden, "Cross-origin request refused")
			return
		case err != nil:
			log.Printf("auth: checking token: %v", err)
			httpx.WriteDetail(w, http.StatusServiceUnavailable, "cannot verify API token right now")
			return
		case admin && !p.IsAdmin():
			httpx.WriteDetail(w, http.StatusForbidden, "admin token required")
			return
		}
		s.withAudit(w, r, p, h)
	}
}
