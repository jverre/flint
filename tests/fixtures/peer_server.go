// A static test service: no dependence on the guest distribution's utilities.
package main

import (
	"fmt"
	"net"
	"net/http"
	"os"
)

func main() {
	address := os.Getenv("FLINT_TEST_LISTEN")
	if address == "" {
		address = ":18080"
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		panic(err)
	}
	if err := os.WriteFile("/tmp/peer-ready", []byte("ready"), 0600); err != nil {
		panic(err)
	}
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { fmt.Fprintln(w, "tenant-a") })
	if err := http.Serve(listener, nil); err != nil {
		panic(err)
	}
}
