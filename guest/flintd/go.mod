module github.com/anthropics/flint/guest/flintd

go 1.24

require (
	github.com/creack/pty v1.1.21
	github.com/gorilla/websocket v1.5.3
	github.com/jverre/flint/services/r2nfs v0.0.0
)

replace github.com/jverre/flint/services/r2nfs => ../../services/r2nfs
