// Package httpx is the API's HTTP plumbing: JSON answers, errors that carry
// their status, and handlers that return an error instead of writing one.
package httpx

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

// Error is an error with the HTTP answer it should get.
type Error struct {
	Status int
	Detail string
}

func (e *Error) Error() string { return e.Detail }

func (e *Error) HTTPError() (int, string) { return e.Status, e.Detail }

func NewError(status int, detail string) *Error { return &Error{status, detail} }

func BadRequest(detail string) *Error  { return &Error{http.StatusBadRequest, detail} }
func Forbidden(detail string) *Error   { return &Error{http.StatusForbidden, detail} }
func NotFound(detail string) *Error    { return &Error{http.StatusNotFound, detail} }
func Conflict(detail string) *Error    { return &Error{http.StatusConflict, detail} }
func Unavailable(detail string) *Error { return &Error{http.StatusServiceUnavailable, detail} }

// statusError is implemented by errors that map to a particular answer
// (Error, and the engine's and host agent client's own error types).
type statusError interface {
	error
	HTTPError() (status int, detail string)
}

// StatusOf maps an error to an HTTP status and the detail to show.
func StatusOf(err error) (int, string) {
	var se statusError
	if errors.As(err, &se) {
		return se.HTTPError()
	}
	return http.StatusInternalServerError, err.Error()
}

// Prefix puts context in front of an error's detail, keeping its status.
func Prefix(prefix string, err error) error {
	if err == nil {
		return nil
	}
	status, detail := StatusOf(err)
	return &Error{status, prefix + detail}
}

// HandlerFunc is a handler that returns its failure instead of writing it.
type HandlerFunc func(w http.ResponseWriter, r *http.Request) error

// Handle adapts h: an error it returns becomes a {"detail": ...} answer.
func Handle(h HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if err := h(w, r); err != nil {
			WriteErr(w, err)
		}
	}
}

func WriteJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

// WriteErr answers with err's status, in FastAPI's {"detail": ...} shape so
// SDK error handling is shared with the single daemon.
func WriteErr(w http.ResponseWriter, err error) {
	status, detail := StatusOf(err)
	WriteDetail(w, status, detail)
}

func WriteDetail(w http.ResponseWriter, status int, detail string) {
	WriteJSON(w, status, map[string]any{"detail": detail})
}

// OK is the answer of an action with nothing else to say.
func OK(w http.ResponseWriter) error {
	WriteJSON(w, http.StatusOK, map[string]any{"ok": true})
	return nil
}

// JSON writes a 200 answer.
func JSON(w http.ResponseWriter, v any) error {
	WriteJSON(w, http.StatusOK, v)
	return nil
}

// Decode reads a JSON body of at most limit bytes into v. An empty body
// leaves v alone; anything else that isn't JSON is a 400 with msg.
func Decode(w http.ResponseWriter, r *http.Request, limit int64, v any, msg string) error {
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, limit))
	if err != nil {
		return BadRequest("request body too large or unreadable")
	}
	if len(raw) == 0 {
		return nil
	}
	if err := json.Unmarshal(raw, v); err != nil {
		return BadRequest(msg)
	}
	return nil
}
